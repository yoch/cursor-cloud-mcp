"""The public cursor-agents skill: its script on the fictional transport, its tool names against the server."""

import json
import os
import re
import selectors
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters

from cursor_cloud_mcp.fixture import BUSY_AGENT_ID

SKILL = Path(__file__).resolve().parents[1] / "skills" / "cursor-agents"
CCM = SKILL / "scripts" / "ccm.py"
AGENT_ID = "bc-11111111-2222-3333-4444-555555555555"
CREATE = {"prompt": "probe", "name": "skill-w01", "agent_id": AGENT_ID, "on_conflict": "reuse"}


def _env(**extra: str) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "CURSOR_MCP_FIXTURE": "1",
        "CURSOR_MCP_LOG_LEVEL": "ERROR",
        "CCM_SERVER": f"{shlex.quote(sys.executable)} -m cursor_cloud_mcp",
    }
    env.update(extra)
    return env


def _ccm(tmp_path: Path, *args: str, stdin: str | None = None, **extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CCM), *args],
        input=stdin,
        check=False,
        capture_output=True,
        text=True,
        env=_env(**extra),
        cwd=tmp_path,
        timeout=120,
    )


def test_ccm_reads_with_writes_off(tmp_path: Path) -> None:
    done = _ccm(tmp_path, "cursor_get_account")
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["api_key_name"] == "fixture"


def test_ccm_refuses_a_write_without_the_flag(tmp_path: Path) -> None:
    # An inherited CURSOR_MCP_ALLOW_WRITES=1 must not enable writes on its own.
    done = _ccm(tmp_path, "cursor_create_agent", json.dumps(CREATE), CURSOR_MCP_ALLOW_WRITES="1")
    assert done.returncode == 1
    assert json.loads(done.stdout)["code"] == "READ_ONLY"


def test_ccm_batch_writes_with_the_flag_over_one_session(tmp_path: Path) -> None:
    calls = [
        {"tool": "cursor_create_agent", "args": CREATE},
        {"tool": "cursor_create_agent", "args": CREATE},
        {"tool": "cursor_supervise", "args": {"name": "skill-w", "status": "all"}},
    ]
    done = _ccm(tmp_path, "--allow-writes", "--batch", stdin="\n".join(json.dumps(call) for call in calls))
    assert done.returncode == 0, done.stderr
    created, reused, fleet = (json.loads(line) for line in done.stdout.splitlines())
    assert created["result"]["agent_id"] == AGENT_ID
    assert "run_id" in created["result"]
    # Same session: the second creation finds the first agent instead of paying for another.
    assert reused["result"]["reused"] is True
    assert "run_id" not in reused["result"]
    assert [row["agent_id"] for row in fleet["result"]["items"]] == [AGENT_ID]


def test_ccm_batch_writes_each_result_before_the_next_call_ends(tmp_path: Path) -> None:
    # A supervisor that kills a slow batch must still get the ids of the paid calls that went through.
    calls = [
        {"tool": "cursor_create_agent", "args": CREATE},
        {"tool": "cursor_get_run", "args": {"agent_id": BUSY_AGENT_ID, "run_id": "run-busy", "wait_seconds": 60}},
    ]
    process = subprocess.Popen(
        [sys.executable, str(CCM), "--allow-writes", "--batch"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env=_env(),
        cwd=tmp_path,
    )
    try:
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write("\n".join(json.dumps(call) for call in calls))
        process.stdin.close()
        # The second call waits about 60 s on a RUNNING run: the first line must come well before.
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=30), "no result line while the second call was still running"
        created = json.loads(process.stdout.readline())
    finally:
        process.kill()
        process.wait(timeout=10)
    assert created["ok"] is True
    assert created["result"]["agent_id"] == AGENT_ID


@pytest.mark.anyio
async def test_skill_cites_only_tools_the_server_lists(tmp_path: Path) -> None:
    text = "\n".join(path.read_text(encoding="utf-8") for path in SKILL.rglob("*.md"))
    cited = set(re.findall(r"\bcursor_[a-z_]+\b", text))
    params = StdioServerParameters(command=sys.executable, args=["-m", "cursor_cloud_mcp"], env=_env(), cwd=tmp_path)
    async with Client(params) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
    assert cited
    assert cited <= listed, sorted(cited - listed)
