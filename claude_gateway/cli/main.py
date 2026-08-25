"""``gateway`` CLI — start / stop / status / sessions / jobs.

Zero non-stdlib deps beyond httpx (already required). The server is launched as
a detached background process by default; ``--foreground`` runs it inline.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time

import httpx

from ..config import get_settings


def _base_url(args) -> str:
    s = get_settings()
    host = args.host or s.host
    port = args.port or s.port
    host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    return f"http://{host}:{port}"


def _headers(args) -> dict[str, str]:
    key = args.api_key or os.environ.get("API_KEY") or get_settings().api_key
    return {"Authorization": f"Bearer {key}"} if key else {}


def _read_pid(s) -> int | None:
    pf = s.pidfile()
    if not pf.exists():
        return None
    try:
        return int(pf.read_text().strip())
    except (ValueError, OSError):
        return None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# ---- commands -------------------------------------------------------------
def cmd_start(args) -> int:
    s = get_settings()
    s.ensure_dirs()
    pid = _read_pid(s)
    if pid and _alive(pid):
        print(f"gateway already running (pid {pid}) at {_base_url(args)}")
        return 0

    env = dict(os.environ)
    if args.host:
        env["HOST"] = args.host
    if args.port:
        env["PORT"] = str(args.port)

    if args.foreground:
        from ..main import main as run_main

        run_main()
        return 0

    log_path = s.home / "gateway.out.log"
    logf = open(log_path, "ab")
    proc = subprocess.Popen(
        [sys.executable, "-m", "claude_gateway.main"],
        stdout=logf,
        stderr=logf,
        env=env,
        start_new_session=True,
    )
    s.pidfile().write_text(str(proc.pid))
    print(f"starting gateway (pid {proc.pid})… logs: {log_path}")

    url = _base_url(args)
    for _ in range(40):
        time.sleep(0.25)
        if proc.poll() is not None:
            print("gateway exited during startup; check the log:")
            print(log_path.read_text()[-1500:])
            return 1
        try:
            r = httpx.get(f"{url}/v1/health", timeout=1.0)
            h = r.json()
            if r.status_code == 200 and "backend" in h:
                print(f"✓ gateway up at {url}  (backend={h['backend']}, "
                      f"sdk={h.get('sdk_version')})")
                print(f"  dashboard: {url}/dashboard")
                print(f"  docs:      {url}/docs")
                return 0
            # Something else is answering on this port (e.g. a proxy).
            print(f"warning: {url}/v1/health returned an unexpected response "
                  f"(status {r.status_code}); is another service on this port?")
        except Exception:
            continue
    print(f"gateway started (pid {proc.pid}) but health check timed out; see {log_path}")
    return 1


def cmd_stop(args) -> int:
    s = get_settings()
    pid = _read_pid(s)
    if not pid:
        print("no pidfile; gateway not running (or started in foreground)")
        return 1
    if not _alive(pid):
        print(f"stale pidfile (pid {pid} not running); cleaning up")
        s.pidfile().unlink(missing_ok=True)
        return 0
    os.kill(pid, signal.SIGTERM)
    for _ in range(20):
        if not _alive(pid):
            break
        time.sleep(0.2)
    s.pidfile().unlink(missing_ok=True)
    print(f"stopped gateway (pid {pid})")
    return 0


def cmd_status(args) -> int:
    s = get_settings()
    pid = _read_pid(s)
    running = bool(pid and _alive(pid))
    print(f"pidfile: {s.pidfile()}  pid={pid or '-'}  process={'alive' if running else 'down'}")
    try:
        r = httpx.get(f"{_base_url(args)}/v1/health", timeout=2.0)
        h = r.json()
        print(f"health: {h['status']}  backend={h['backend']} "
              f"sdk={h.get('sdk_version')} running_jobs={h['running_jobs']} "
              f"warm={h['warm_sessions']} uptime={h['uptime_s']}s")
        return 0
    except httpx.HTTPError as e:
        print(f"health: unreachable ({e})")
        return 1 if not running else 0


def cmd_sessions(args) -> int:
    try:
        r = httpx.get(f"{_base_url(args)}/v1/sessions?limit={args.limit}",
                      headers=_headers(args), timeout=10.0)
        r.raise_for_status()
    except httpx.HTTPError as e:
        print(f"error: {e}")
        return 1
    rows = r.json()
    if not rows:
        print("no sessions")
        return 0
    print(f"{'SESSION':36}  {'KIND':8} {'MSGS':>5} {'COST':>9}  WARM")
    for s in rows:
        print(f"{s['session_id']:36}  {s['kind']:8} {s['message_count']:>5} "
              f"${s['total_cost_usd']:>8.4f}  {'yes' if s['warm'] else 'no'}")
    return 0


def cmd_jobs(args) -> int:
    q = f"?limit={args.limit}" + (f"&status={args.status}" if args.status else "")
    try:
        r = httpx.get(f"{_base_url(args)}/v1/jobs{q}", headers=_headers(args), timeout=10.0)
        r.raise_for_status()
    except httpx.HTTPError as e:
        print(f"error: {e}")
        return 1
    rows = r.json()
    if not rows:
        print("no jobs")
        return 0
    print(f"{'JOB':36}  {'KIND':7} {'STATUS':10} {'TOKENS':>8} {'COST':>9}  PROMPT")
    for j in rows:
        prompt = (j.get("prompt") or "").replace("\n", " ")[:40]
        print(f"{j['job_id']:36}  {j['kind']:7} {j['status']:10} "
              f"{j['usage']['total_tokens']:>8} ${(j.get('cost_usd') or 0):>8.4f}  {prompt}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gateway", description="Local Claude Code Gateway")
    p.add_argument("--host", help="override host")
    p.add_argument("--port", type=int, help="override port")
    p.add_argument("--api-key", help="API key for authenticated commands")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("start", help="start the gateway server")
    sp.add_argument("--foreground", action="store_true", help="run in the foreground")
    sp.set_defaults(func=cmd_start)

    sub.add_parser("stop", help="stop the gateway server").set_defaults(func=cmd_stop)
    sub.add_parser("status", help="show server status").set_defaults(func=cmd_status)

    ss = sub.add_parser("sessions", help="list sessions")
    ss.add_argument("--limit", type=int, default=50)
    ss.set_defaults(func=cmd_sessions)

    sj = sub.add_parser("jobs", help="list jobs")
    sj.add_argument("--limit", type=int, default=50)
    sj.add_argument("--status", help="filter by status (RUNNING, COMPLETED, …)")
    sj.set_defaults(func=cmd_jobs)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
