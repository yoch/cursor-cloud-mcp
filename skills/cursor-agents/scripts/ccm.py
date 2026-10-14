#!/usr/bin/env python3
"""Call cursor-cloud-mcp tools from scripts (supervisor loops, periodic checks) without an MCP client.

  ccm.py [--allow-writes] TOOL '{"arg": ...}'   one call; result as JSON on stdout; exit 1 on a tool error
  ccm.py [--allow-writes] --batch < calls.jsonl
        many calls over ONE server session; each input line is {"tool": "...", "args": {...}};
        each output line is {"tool": "...", "ok": true|false, "result": ...}, written as soon as its
        call ends, so a batch killed mid-way keeps the results (ids of paid calls) already obtained

Writes are off unless --allow-writes is given, and even then they are enabled only when the call
or the batch contains a state-changing tool. Pass --allow-writes only in a script the user approved
for paid calls: it is not a way around a read-only MCP server. Deletion is never enabled here.

--batch keeps one session: each server start costs seconds and drops the model catalog cache and
the cache of finished runs' activity.

The key comes from the environment (CURSOR_API_KEY), never from an argument. Needs the `mcp`
package, e.g. `uv run --with mcp python ccm.py ...`. Server command: $CCM_SERVER (shell-split),
else `cursor-cloud-mcp` on PATH, else `uvx cursor-cloud-mcp`.
"""

import asyncio
import datetime
import json
import os
import shlex
import shutil
import sys
from importlib.metadata import version

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

WRITE_TOOLS = {"cursor_create_agent", "cursor_create_run", "cursor_cancel_run", "cursor_archive_agent"}
# The server bounds every call at 95 s; leave room for the transport.
CALL_TIMEOUT_SECONDS = 110.0
# mcp 1.x takes a timedelta for read_timeout_seconds, 2.x a float.
CALL_TIMEOUT = (
    CALL_TIMEOUT_SECONDS
    if int(version("mcp").split(".")[0]) >= 2
    else datetime.timedelta(seconds=CALL_TIMEOUT_SECONDS)
)


def server_command() -> list[str]:
    if os.environ.get("CCM_SERVER"):
        return shlex.split(os.environ["CCM_SERVER"])
    if shutil.which("cursor-cloud-mcp"):
        return ["cursor-cloud-mcp"]
    return ["uvx", "cursor-cloud-mcp"]


def field(result, snake: str, camel: str):
    # mcp 2.x names the result fields in snake_case, 1.x in camelCase.
    return getattr(result, snake) if hasattr(result, snake) else getattr(result, camel)


def payload(result) -> object:
    structured = field(result, "structured_content", "structuredContent")
    if structured is not None:
        return structured
    texts = [getattr(c, "text", "") for c in result.content]
    text = "\n".join(t for t in texts if t)
    try:
        return json.loads(text)  # tool errors are pure JSON since 0.3
    except ValueError:
        return text


async def run(calls: list[tuple[str, dict]], allow_writes: bool = False, emit=None) -> list[dict]:
    env = dict(os.environ)
    writes = allow_writes and any(tool in WRITE_TOOLS for tool, _ in calls)
    env["CURSOR_MCP_ALLOW_WRITES"] = "1" if writes else "0"
    env["CURSOR_MCP_ALLOW_DELETE"] = "0"
    command = server_command()
    params = StdioServerParameters(command=command[0], args=command[1:], env=env)
    out = []
    async with stdio_client(params, errlog=sys.stderr) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        for tool, args in calls:
            try:
                result = await session.call_tool(tool, args, read_timeout_seconds=CALL_TIMEOUT)
                item = {"tool": tool, "ok": not field(result, "is_error", "isError"), "result": payload(result)}
            # Transport failure or timeout, whatever its type: the outcome of a write is then unknown.
            except Exception as exc:  # noqa: BLE001
                item = {"tool": tool, "ok": False, "result": {"code": "CLIENT_ERROR", "message": repr(exc)}}
            out.append(item)
            if emit is not None:
                emit(item)
    return out


def main() -> int:
    argv = sys.argv[1:]
    allow_writes = "--allow-writes" in argv
    argv = [a for a in argv if a != "--allow-writes"]
    if argv == ["--batch"]:
        calls = []
        for line in sys.stdin:
            if line.strip():
                item = json.loads(line)
                calls.append((item["tool"], item.get("args") or {}))
        results = asyncio.run(
            run(calls, allow_writes, emit=lambda item: print(json.dumps(item, ensure_ascii=False), flush=True))
        )
        return 0 if all(item["ok"] for item in results) else 1
    if len(argv) not in (1, 2) or argv[0].startswith("-"):
        print(__doc__, file=sys.stderr)
        return 2
    args = json.loads(argv[1]) if len(argv) == 2 else {}
    [item] = asyncio.run(run([(argv[0], args)], allow_writes))
    print(json.dumps(item["result"], indent=1, ensure_ascii=False))
    return 0 if item["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
