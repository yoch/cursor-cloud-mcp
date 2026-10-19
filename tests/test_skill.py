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
PUSH_WATCH = SKILL / "scripts" / "push_watch.py"
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


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        timeout=60,
    )


def test_push_watch_flags_a_branch_whose_sha_stopped_moving(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    _git(tmp_path, "init", "--bare", str(remote))
    _git(tmp_path, "init", str(work))
    _git(work, "commit", "--allow-empty", "-m", "one")
    _git(work, "push", str(remote), "HEAD:refs/heads/fleet/w01")
    state = tmp_path / "state.json"

    def watch() -> tuple[int, dict[str, object]]:
        done = subprocess.run(
            [sys.executable, str(PUSH_WATCH), "--remote", str(remote), "--state", str(state),
             "--stale-minutes", "30", "fleet/w*", "fleet/w02"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        return done.returncode, json.loads(done.stdout)

    code, first = watch()
    assert code == 0, first
    assert first["missing"] == ["fleet/w02"] and first["stale"] == []
    # Pretend both clocks started an hour ago: the unchanged SHA and the absent branch are stale.
    saved = json.loads(state.read_text())
    for row in saved.values():
        row["since"] = "2000-01-01T00:00:00+00:00"
    state.write_text(json.dumps(saved))
    code, second = watch()
    assert code == 2
    assert second["stale"] == ["fleet/w01", "fleet/w02"]
    # A new push restarts the clock of that branch only.
    _git(work, "commit", "--allow-empty", "-m", "two")
    _git(work, "push", str(remote), "HEAD:refs/heads/fleet/w01")
    code, third = watch()
    assert code == 2 and third["stale"] == ["fleet/w02"]


def test_push_watch_restarts_the_clock_of_a_damaged_state_entry(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    _git(tmp_path, "init", "--bare", str(remote))
    _git(tmp_path, "init", str(work))
    _git(work, "commit", "--allow-empty", "-m", "one")
    _git(work, "push", str(remote), "HEAD:refs/heads/fleet/w01")
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"fleet/w01": {"sha": "abc"}, "fleet/w09": 5, "fleet/w08": {"since": "nope"}}))
    done = subprocess.run(
        [sys.executable, str(PUSH_WATCH), "--remote", str(remote), "--state", str(state),
         "--stale-minutes", "30", "fleet/w*"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    # The damaged entry of a branch that no longer exists is still watched, with a fresh clock.
    assert sorted(json.loads(done.stdout)["branches"]) == ["fleet/w01", "fleet/w08"]
