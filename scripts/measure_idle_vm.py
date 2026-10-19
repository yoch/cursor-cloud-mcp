"""Real, PAID measurement: what is left of a Cursor VM after its run ends and the agent sits idle.

Opt-in: `SMOKE_PAID=1`, and `MEASURE_MODEL=id[:param=value,...]` (no default model: choose a cheap
one, with every cost option explicit). One agent without repository starts a `nohup` heartbeat
(a timestamp file rewritten every 30 s), records its machine id and boot time, and ends its
turn. Then, at each delay of `MEASURE_CHECKS` (minutes after the first run ended, default
`15,60,180`), the agent status is read and, unless archived, a short follow-up reports: is the
heartbeat process alive, how old is its last beat, same machine id and boot time, are the files
still there. A few short runs in all, a few cents. The agent is deleted at the end, except with
`SMOKE_KEEP=1`.

This is one observation of one VM on one day, not a guarantee: the result goes, dated, into
docs/verification.md. The key comes from the environment or from `.env`, read by this script only.
Run it detached (`nohup uv run python scripts/measure_idle_vm.py >> measure.log 2>&1 &`): it
sleeps between checks and prints one JSON line per observation.
"""

import asyncio
import datetime as dt
import json
import os
import re
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import TextIO

from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client
from smoke_live import NETWORK_ENV, call, load_key, wait_terminal

from cursor_cloud_mcp.config import parse_default_model

START_PROMPT = """Measurement probe. Do not read or print any credential. Run exactly, then end your turn at once:
mkdir -p ~/idle-probe && cd ~/idle-probe
nohup sh -c 'while true; do date -u +%s > ~/idle-probe/beat; sleep 30; done' >> ~/idle-probe/log 2>&1 &
echo $! > ~/idle-probe/pid
sleep 5
echo "PROBE pid=$(cat ~/idle-probe/pid) machine=$(cat /etc/machine-id 2>/dev/null || hostname) boot=$(uptime -s | tr ' ' T) beat=$(cat ~/idle-probe/beat)"
Final answer: only the PROBE line. Do not wait, do not monitor, start nothing else."""

CHECK_PROMPT = """Check only: start nothing, kill nothing, do not read or print any credential. Run exactly:
now=$(date -u +%s); beat=$(cat ~/idle-probe/beat 2>/dev/null || echo none)
alive=0; kill -0 "$(cat ~/idle-probe/pid 2>/dev/null)" 2>/dev/null && alive=1
age=none; [ "$beat" != none ] && age=$((now - beat))
files=0; [ -d ~/idle-probe ] && files=1
echo "CHECK alive=$alive beat_age=$age files=$files machine=$(cat /etc/machine-id 2>/dev/null || hostname) boot=$(uptime -s | tr ' ' T)"
Final answer: only the CHECK line, then end your turn."""

_FIELDS = re.compile(r"(\w+)=(\S+)")


def emit(**row: object) -> None:
    row["at"] = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    print(json.dumps(row), flush=True)


def marker(result: object, word: str) -> dict[str, str] | None:
    if not isinstance(result, str):
        return None
    line = next((line for line in result.splitlines() if line.strip().startswith(word)), None)
    return None if line is None else dict(_FIELDS.findall(line))


async def main() -> int:
    root = Path(__file__).resolve().parents[1]
    if os.environ.get("SMOKE_PAID") != "1":
        print("Paid measurement not run: set SMOKE_PAID=1.")
        return 2
    model, _error = parse_default_model(os.environ.get("MEASURE_MODEL"))
    if model is None:
        print("MEASURE_MODEL must be id[:param=value,...], with every cost option explicit.")
        return 2
    checks = [float(part) for part in os.environ.get("MEASURE_CHECKS", "15,60,180").split(",") if part.strip()]
    load_key(root)
    key = os.environ.get("CURSOR_API_KEY")
    if not key:
        print("CURSOR_API_KEY missing")
        return 2
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "cursor_cloud_mcp"],
        env={
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "LANG": "C.UTF-8",
            "CURSOR_API_KEY": key,
            "CURSOR_MCP_ALLOW_WRITES": "1",
            "CURSOR_MCP_ALLOW_DELETE": "1",
            "CURSOR_MCP_STATE_DIR": "off",
            **{name: os.environ[name] for name in NETWORK_ENV if name in os.environ},
        },
        cwd=root,
    )
    model_args: dict[str, object] = {"model_id": model[0]}
    if model[1]:
        model_args["model_params"] = [{"id": name, "value": value} for name, value in model[1]]
    agent_id = f"bc-{uuid.uuid4()}"
    errlog_path = Path(tempfile.mkdtemp()) / "stderr.txt"
    with errlog_path.open("w", encoding="utf-8") as errlog:
        # From the creation call on, the agent may exist (even after MUTATION_OUTCOME_UNKNOWN):
        # every exit goes through the deletion below.
        try:
            return await probe(params, errlog, agent_id, model_args, checks)
        finally:
            async with Client(stdio_client(params, errlog=errlog)) as client:
                ok, usage = await call(client, "cursor_get_usage", {"agent_id": agent_id})
                emit(step="usage", total_cost=usage.get("total_cost") if ok else usage.get("code"))
                if os.environ.get("SMOKE_KEEP") == "1":
                    emit(step="kept", agent_id=agent_id)
                else:
                    ok, deleted = await call(
                        client, "cursor_delete_agent", {"agent_id": agent_id, "confirm_agent_id": agent_id}
                    )
                    # NOT_FOUND: the creation never went through, nothing is left behind.
                    emit(step="delete", ok=ok, code=None if ok else deleted.get("code"))


async def probe(
    params: StdioServerParameters,
    errlog: TextIO,
    agent_id: str,
    model_args: dict[str, object],
    checks: list[float],
) -> int:
    async with Client(stdio_client(params, errlog=errlog)) as client:
        ok, created = await call(
            client,
            "cursor_create_agent",
            {"prompt": START_PROMPT, "name": f"idle-probe-{agent_id[3:11]}", "agent_id": agent_id, **model_args},
        )
        if not ok:
            emit(step="create", error=created.get("code"), message=created.get("message"))
            return 1
        emit(step="create", agent_id=agent_id, url=created.get("url"), model=created.get("model"))
        run = await wait_terminal(client, agent_id, str(created["run_id"]))
        emit(step="start", status=run.get("status"), probe=marker(run.get("result"), "PROBE"))
    ended = time.monotonic()
    for minutes in checks:
        await asyncio.sleep(max(0.0, ended + minutes * 60 - time.monotonic()))
        async with Client(stdio_client(params, errlog=errlog)) as client:
            ok, agent = await call(client, "cursor_get_agent", {"agent_id": agent_id})
            status = agent.get("status") if ok else agent.get("code")
            if status == "ARCHIVED" or not ok:
                emit(step="check", minutes=minutes, agent_status=status)
                break
            ok, sent = await call(client, "cursor_create_run", {"agent_id": agent_id, "prompt": CHECK_PROMPT})
            if not ok:
                emit(step="check", minutes=minutes, agent_status=status, error=sent.get("code"))
                break
            run = await wait_terminal(client, agent_id, str(sent["run_id"]))
            emit(
                step="check",
                minutes=minutes,
                agent_status=status,
                run_status=run.get("status"),
                check=marker(run.get("result"), "CHECK"),
            )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
