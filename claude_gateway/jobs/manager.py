"""JobManager — every request becomes a Job with a lifecycle and an event stream.

States: QUEUED -> RUNNING -> COMPLETED | FAILED | CANCELLED.

A submitted job runs as a background asyncio task that drives the backend,
normalizes its events, persists them (events/messages/usage tables), fans them
out to the broker (for SSE/WS subscribers), and finally resolves a future so
synchronous REST callers can ``await`` the result.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from ..backend.base import CancelToken, ClaudeBackend, RunConfig
from ..config import Settings
from ..logging_config import get_logger
from ..models import EventType, JobState
from ..observability import Metrics
from ..sessions import SessionManager
from ..storage import Database
from ..streaming import EventBroker
from ..backend.events import GatewayEvent

log = get_logger("gateway.jobs")


@dataclass
class JobHandle:
    job_id: str
    session_id: str | None
    kind: str
    state: JobState = JobState.QUEUED
    cancel_token: CancelToken = field(default_factory=CancelToken)
    task: asyncio.Task | None = None
    future: asyncio.Future | None = None
    result: dict[str, Any] | None = None
    created_at: float = field(default_factory=time.time)


class JobManager:
    def __init__(
        self,
        db: Database,
        backend: ClaudeBackend,
        broker: EventBroker,
        sessions: SessionManager,
        settings: Settings,
        metrics: Metrics,
    ) -> None:
        self.db = db
        self.backend = backend
        self.broker = broker
        self.sessions = sessions
        self.settings = settings
        self.metrics = metrics
        self._jobs: dict[str, JobHandle] = {}

    # ---- introspection ----------------------------------------------------
    def get(self, job_id: str) -> JobHandle | None:
        return self._jobs.get(job_id)

    def running_count(self) -> int:
        return sum(1 for h in self._jobs.values() if h.state == JobState.RUNNING)

    async def list(self, **kw: Any) -> list[dict[str, Any]]:
        return await self.db.list_jobs(**kw)

    # ---- submit -----------------------------------------------------------
    async def submit(
        self,
        *,
        kind: str,
        prompt: str,
        config: RunConfig,
        session_id: str | None = None,
        resume: bool = False,
        stateless: bool = False,
        owner: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> JobHandle:
        job_id = str(uuid.uuid4())
        handle = JobHandle(job_id=job_id, session_id=session_id, kind=kind)
        handle.future = asyncio.get_event_loop().create_future()
        self._jobs[job_id] = handle

        await self.db.insert_job(
            {
                "job_id": job_id,
                "session_id": session_id,
                "kind": kind,
                "prompt": prompt,
                "status": JobState.QUEUED.value,
                "backend": self.backend.name,
                "owner": owner,
                "created_at": handle.created_at,
                "meta": meta or {},
            }
        )
        self.metrics.inc("requests_total", kind=kind)

        # Create the broker channel up front so a streaming subscriber that
        # connects before the task publishes its first event still finds it
        # (avoids a "channel not yet created" race on fast jobs).
        self.broker.get_or_create(job_id)

        handle.task = asyncio.create_task(
            self._execute(handle, prompt, config, resume, stateless)
        )
        self._update_gauges()
        return handle

    # ---- execution --------------------------------------------------------
    async def _execute(
        self,
        handle: JobHandle,
        prompt: str,
        config: RunConfig,
        resume: bool,
        stateless: bool,
    ) -> None:
        job_id = handle.job_id
        started = time.time()
        handle.state = JobState.RUNNING
        seq = 0

        async def persist_publish(ev: GatewayEvent) -> None:
            nonlocal seq
            seq += 1
            wire = ev.to_wire(seq)
            try:
                await self.db.insert_event(job_id, seq, ev.type.value, ev.data)
            except Exception as e:  # pragma: no cover
                log.warning("event persist failed: %s", e)
            self.broker.publish(job_id, wire)

        await self.db.update_job(job_id, status=JobState.RUNNING.value, started_at=started)
        await persist_publish(GatewayEvent.started(job_id, handle.session_id, handle.kind))

        final: dict[str, Any] | None = None
        error: dict[str, Any] | None = None
        cancelled = False

        async def consume() -> None:
            nonlocal final, error, cancelled
            async for ev in self.backend.run(
                prompt=prompt,
                config=config,
                session_id=handle.session_id,
                resume=resume,
                stateless=stateless,
                cancel_token=handle.cancel_token,
            ):
                await persist_publish(ev)
                if ev.type == EventType.JOB_COMPLETED:
                    final = ev.data
                elif ev.type == EventType.JOB_FAILED:
                    error = ev.data
                elif ev.type == EventType.JOB_CANCELLED:
                    cancelled = True

        timeout = self.settings.effective_job_timeout()
        try:
            if timeout is None:
                await consume()  # no timeout — bounded only by the Claude Code plan
            else:
                await asyncio.wait_for(consume(), timeout=timeout)
        except asyncio.TimeoutError:
            handle.cancel_token.cancel()
            if handle.session_id:
                await self.backend.interrupt(handle.session_id)
            error = {"error": f"job timed out after {self.settings.job_timeout_s}s"}
            await persist_publish(GatewayEvent.failed(error["error"]))
        except asyncio.CancelledError:
            cancelled = True
            handle.cancel_token.cancel()
        except Exception as e:  # pragma: no cover
            error = {"error": str(e), "detail": repr(e)}
            await persist_publish(GatewayEvent.failed(str(e), detail=repr(e)))

        await self._finalize(handle, started, final, error, cancelled, prompt)

    async def _finalize(
        self,
        handle: JobHandle,
        started: float,
        final: dict[str, Any] | None,
        error: dict[str, Any] | None,
        cancelled: bool,
        prompt: str,
    ) -> None:
        job_id = handle.job_id
        duration_ms = int((time.time() - started) * 1000)

        if cancelled:
            handle.state = JobState.CANCELLED
            await self.db.update_job(
                job_id, status=JobState.CANCELLED.value,
                finished_at=time.time(), duration_ms=duration_ms,
                error="cancelled",
            )
            self.metrics.inc("jobs_cancelled_total")
            result = {"status": JobState.CANCELLED.value, "job_id": job_id}
        elif error is not None or final is None:
            handle.state = JobState.FAILED
            err_msg = (error or {}).get("error", "unknown error")
            await self.db.update_job(
                job_id, status=JobState.FAILED.value,
                finished_at=time.time(), duration_ms=duration_ms,
                error=err_msg,
            )
            self.metrics.inc("errors_total", kind=handle.kind)
            result = {"status": JobState.FAILED.value, "job_id": job_id, "error": err_msg}
        else:
            handle.state = JobState.COMPLETED
            usage = final.get("usage", {}) or {}
            cost = final.get("cost_usd")
            claude_sid = final.get("session_id")
            await self.db.update_job(
                job_id,
                status=JobState.COMPLETED.value,
                response=final.get("response", ""),
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                cache_read_tokens=usage.get("cache_read_input_tokens", 0),
                cache_creation_tokens=usage.get("cache_creation_input_tokens", 0),
                total_tokens=usage.get("total_tokens", 0),
                cost_usd=cost,
                num_turns=final.get("num_turns"),
                duration_ms=final.get("duration_ms", duration_ms),
                finished_at=time.time(),
            )
            # usage + session bookkeeping + transcript messages
            await self.db.insert_usage(
                {
                    "job_id": job_id,
                    "session_id": handle.session_id,
                    "model": final.get("model"),
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                    "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
                    "cache_creation_tokens": usage.get("cache_creation_input_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                    "cost_usd": cost or 0.0,
                }
            )
            if handle.session_id:
                await self.db.insert_message(handle.session_id, job_id, "user", prompt)
                await self.db.insert_message(
                    handle.session_id, job_id, "assistant", final.get("response", "")
                )
                await self.sessions.record_turn(
                    handle.session_id, cost=cost or 0.0, claude_session_id=claude_sid
                )
            self.metrics.inc("tokens_total", usage.get("total_tokens", 0))
            if cost:
                self.metrics.inc("cost_usd_total", cost)
            self.metrics.observe("latency_ms", duration_ms, kind=handle.kind)
            self.metrics.inc("jobs_completed_total", kind=handle.kind)
            result = {"status": JobState.COMPLETED.value, "job_id": job_id, **final}

        handle.result = result
        if handle.future and not handle.future.done():
            handle.future.set_result(result)
        self.broker.close(job_id)
        self._update_gauges()

    # ---- wait / cancel ----------------------------------------------------
    async def wait(self, job_id: str, timeout: float | None = None) -> dict[str, Any]:
        handle = self._jobs.get(job_id)
        if handle is None:
            raise KeyError(job_id)
        if handle.result is not None:
            return handle.result
        assert handle.future is not None
        if timeout is None:
            return await handle.future
        return await asyncio.wait_for(asyncio.shield(handle.future), timeout=timeout)

    async def cancel(self, job_id: str) -> bool:
        handle = self._jobs.get(job_id)
        if handle is None or handle.state.terminal:
            return False
        handle.cancel_token.cancel()
        if handle.session_id:
            await self.backend.interrupt(handle.session_id)
        # Give the cooperative path a beat; if still running, cancel the task.
        await asyncio.sleep(0.1)
        if handle.task and not handle.task.done():
            handle.task.cancel()
        return True

    def _update_gauges(self) -> None:
        self.metrics.gauge("running_jobs", self.running_count())
        warm = getattr(self.backend, "warm_session_ids", lambda: [])()
        self.metrics.gauge("warm_sessions", len(warm))

    async def aclose(self) -> None:
        for handle in list(self._jobs.values()):
            if handle.task and not handle.task.done():
                handle.task.cancel()
