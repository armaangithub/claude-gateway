"""
Smoke test: prove the Claude Agent SDK works end-to-end in THIS environment
before building the gateway on top of it.

Verifies the four things the user asked to confirm first:
  1. Claude responds          (one-shot query, subscription auth)
  2. Session persists         (set id -> resume -> remembers fact)
  3. Streaming works          (partial StreamEvents arrive live)
  4. Tool events are visible  (ToolUseBlock / ToolResultBlock + usage/cost)

Run:  .venv/bin/python smoke_test.py
"""

import asyncio
import os
import sys
import tempfile
import uuid
from pathlib import Path

# We are (likely) running INSIDE a Claude Code session. The SDK spawns `claude`
# as a subprocess; inheriting these vars can confuse the nested process. Scrub
# them in the parent before the SDK ever forks.
for _v in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT"):
    os.environ.pop(_v, None)

from claude_agent_sdk import (  # noqa: E402
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    __version__,
    query,
)

WORKSPACE = Path(tempfile.mkdtemp(prefix="gw-smoke-"))
PASS, FAIL = "\033[92mPASS\033[0m", "\033[91mFAIL\033[0m"
results: dict[str, bool] = {}


def base_opts(**kw) -> ClaudeAgentOptions:
    o = ClaudeAgentOptions(cwd=str(WORKSPACE), **kw)
    return o


async def collect(prompt, options):
    """Run a query, return (text, result_message, all_messages)."""
    text_parts, result, msgs = [], None, []
    async for msg in query(prompt=prompt, options=options):
        msgs.append(msg)
        if isinstance(msg, AssistantMessage):
            for b in msg.content:
                if isinstance(b, TextBlock):
                    text_parts.append(b.text)
        elif isinstance(msg, ResultMessage):
            result = msg
    return "".join(text_parts), result, msgs


async def test_1_responds():
    print("\n--- Test 1: Claude responds (one-shot) ---")
    text, result, _ = await collect(
        "Reply with exactly the word: PONG. Nothing else.",
        base_opts(max_turns=1),
    )
    print(f"  response: {text!r}")
    if result:
        print(f"  session_id: {result.session_id}")
        print(f"  cost_usd:   {result.total_cost_usd}")
        print(f"  usage:      {result.usage}")
    ok = "PONG" in text.upper() and result is not None
    results["1_responds"] = ok
    print(f"  => {PASS if ok else FAIL}")


async def test_2_session_persist():
    print("\n--- Test 2: Session persistence (set id -> resume) ---")
    sid = str(uuid.uuid4())
    # Turn 1: establish a fact under a controlled session id.
    _, r1, _ = await collect(
        "Remember this codeword: BANANA-42. Just acknowledge with 'ok'.",
        base_opts(max_turns=1, session_id=sid),
    )
    real_sid = r1.session_id if r1 else sid
    print(f"  requested sid: {sid}")
    print(f"  actual sid:    {real_sid}")

    # Turn 2: resume and check recall.
    text2, r2, _ = await collect(
        "What was the codeword I gave you? Reply with just the codeword.",
        base_opts(max_turns=1, resume=real_sid),
    )
    print(f"  recall response: {text2!r}")
    print(f"  resumed sid:     {r2.session_id if r2 else None}")
    ok = "BANANA-42" in text2.upper()
    results["2_session_persist"] = ok
    print(f"  => {PASS if ok else FAIL}")
    return real_sid


async def test_3_streaming():
    print("\n--- Test 3: Streaming (partial events) ---")
    stream_events = 0
    assistant_chunks = 0
    async for msg in query(
        prompt="Count slowly from 1 to 5, one number per line.",
        options=base_opts(max_turns=1, include_partial_messages=True),
    ):
        if isinstance(msg, StreamEvent):
            stream_events += 1
            etype = msg.event.get("type")
            if stream_events <= 3:
                print(f"  StreamEvent[{stream_events}] type={etype}")
        elif isinstance(msg, AssistantMessage):
            assistant_chunks += 1
    print(f"  total StreamEvents: {stream_events}")
    ok = stream_events > 0
    results["3_streaming"] = ok
    print(f"  => {PASS if ok else FAIL}")


async def test_4_tool_events():
    print("\n--- Test 4: Tool events visible ---")
    secret_file = WORKSPACE / "auth.py"
    secret_file.write_text("API_PASSWORD = 'hunter2_smoke'\n")
    tool_calls, tool_results = [], []
    result = None
    async for msg in query(
        prompt=f"Read the file auth.py in the current directory and tell me the password value.",
        options=base_opts(
            max_turns=4,
            allowed_tools=["Read", "Bash", "Grep"],
            permission_mode="bypassPermissions",
        ),
    ):
        if isinstance(msg, AssistantMessage):
            for b in msg.content:
                if isinstance(b, ToolUseBlock):
                    tool_calls.append((b.name, b.input))
                    print(f"  Tool call: {b.name}  input={b.input}")
        elif isinstance(msg, (AssistantMessage,)):
            pass
        if isinstance(msg, ResultMessage):
            result = msg
        # tool results come back as UserMessage content blocks
        from claude_agent_sdk import UserMessage
        if isinstance(msg, UserMessage) and isinstance(msg.content, list):
            for b in msg.content:
                if isinstance(b, ToolResultBlock):
                    summary = str(b.content)[:60]
                    tool_results.append(summary)
                    print(f"  Tool result: {summary!r}")
    if result:
        print(f"  final cost_usd: {result.total_cost_usd}  num_turns: {result.num_turns}")
    ok = len(tool_calls) > 0 and len(tool_results) > 0
    results["4_tool_events"] = ok
    print(f"  => {PASS if ok else FAIL}")


async def main():
    print(f"claude-agent-sdk version: {__version__}")
    print(f"workspace: {WORKSPACE}")
    try:
        await asyncio.wait_for(test_1_responds(), timeout=90)
    except Exception as e:
        print(f"  test 1 error: {e!r}"); results["1_responds"] = False
    try:
        await asyncio.wait_for(test_2_session_persist(), timeout=120)
    except Exception as e:
        print(f"  test 2 error: {e!r}"); results["2_session_persist"] = False
    try:
        await asyncio.wait_for(test_3_streaming(), timeout=90)
    except Exception as e:
        print(f"  test 3 error: {e!r}"); results["3_streaming"] = False
    try:
        await asyncio.wait_for(test_4_tool_events(), timeout=120)
    except Exception as e:
        print(f"  test 4 error: {e!r}"); results["4_tool_events"] = False

    print("\n========== SUMMARY ==========")
    for k, v in results.items():
        print(f"  {k:22s} {PASS if v else FAIL}")
    all_ok = all(results.values())
    print(f"\n  OVERALL: {PASS if all_ok else FAIL}")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
