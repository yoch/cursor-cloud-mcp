"""The real entry point, in a subprocess, on the fictional transport."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client

from cursor_cloud_mcp.fixture import ERROR_PROMPT, SEEDED_AGENT_ID, SEEDED_RUN_ID

pytestmark = pytest.mark.anyio

_BRANCH = "release/2026"


def _env(**extra: str) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }
    env.update(extra)
    return env


def _params(tmp_path: Path, **extra: str) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "cursor_cloud_mcp"],
        env=_env(**extra),
        cwd=tmp_path,
    )


def _payload(result: object) -> dict[str, object]:
    text = result.content[0].text
    assert text is not None
    if result.is_error:
        return json.loads(text[text.find("{") :])
    assert result.structured_content is not None
    return result.structured_content


async def test_stdio_auto_and_legacy_negotiate_and_call_tools(tmp_path: Path) -> None:
    assert not (tmp_path / ".git").exists()
    params = _params(tmp_path, CURSOR_MCP_FIXTURE="1", CURSOR_MCP_ALLOW_WRITES="1")
    async with Client(params) as client:
        assert client.protocol_version == "2026-07-28"
        listed = await client.list_tools()
        assert len(listed.tools) == 17
        assert all(tool.input_schema.get("type") == "object" for tool in listed.tools)
        account = await client.call_tool("cursor_get_account", {})
        assert account.is_error is False
        created = await client.call_tool(
            "cursor_create_agent",
            {
                "repository": "https://github.com/example/demo",
                "starting_ref": _BRANCH,
                "prompt": "Describe the fictional repository",
            },
        )
        created_data = _payload(created)
        assert created.is_error is False
        agent_id = str(created_data["agent_id"])
        run_id = str(created_data["run_id"])
        detail = await client.call_tool("cursor_get_run", {"agent_id": agent_id, "run_id": run_id})
        assert detail.is_error is False
        canonical = await client.call_tool(
            "cursor_create_agent",
            {
                "repository": "https://github.com/example/demo",
                "starting_ref": _BRANCH,
                "prompt": "Canonical name",
            },
        )
        assert canonical.is_error is False
        unknown = await client.call_tool("cursor_get_agent", {"agent_id": agent_id, "surprise": True})
        assert unknown.is_error is True
        artifact = await client.call_tool(
            "cursor_read_artifact",
            {"agent_id": SEEDED_AGENT_ID, "path": "artifacts/result.txt"},
        )
        assert _payload(artifact)["text"] == "fixture artifact\n"
        follow = await client.call_tool(
            "cursor_create_run",
            {"agent_id": agent_id, "prompt": "Add a sentence"},
        )
        assert follow.is_error is False
        assert _payload(follow)["agent_id"] == agent_id
        failed = await client.call_tool(
            "cursor_create_run",
            {"agent_id": SEEDED_AGENT_ID, "prompt": ERROR_PROMPT},
        )
        assert _payload(failed)["code"] == "VALIDATION"
        seeded = await client.call_tool(
            "cursor_get_run",
            {"agent_id": SEEDED_AGENT_ID, "run_id": SEEDED_RUN_ID, "result_limit": 20},
        )
        seeded_data = _payload(seeded)
        assert seeded_data["result_truncated"] is True
        assert seeded_data["result_total_chars"] > 20

    async with Client(params, mode="legacy") as legacy:
        assert legacy.protocol_version == "2025-11-25"
        listed = await legacy.list_tools()
        assert len(listed.tools) == 17
        account = await legacy.call_tool("cursor_get_account", {})
        assert account.is_error is False


async def test_stdio_read_only_and_clean_stderr(tmp_path: Path) -> None:
    secret = "stdio-sentinel-secret"
    mixed = _params(
        tmp_path,
        CURSOR_MCP_FIXTURE="1",
        CURSOR_MCP_ALLOW_WRITES="0",
        CURSOR_API_KEY=secret,
    )
    errlog_path = tmp_path / "stderr.txt"
    with errlog_path.open("w", encoding="utf-8") as errlog:
        async with Client(stdio_client(mixed, errlog=errlog)) as client:
            blocked = await client.call_tool("cursor_get_account", {})
    assert _payload(blocked)["code"] == "CONFIGURATION_MISSING"
    stderr = errlog_path.read_text(encoding="utf-8")
    assert secret not in stderr
    assert "Authorization" not in stderr

    readonly = _params(tmp_path, CURSOR_MCP_FIXTURE="1", CURSOR_MCP_ALLOW_WRITES="0")
    async with Client(readonly) as client:
        refused = await client.call_tool(
            "cursor_create_agent",
            {
                "repository": "https://github.com/example/demo",
                "starting_ref": _BRANCH,
                "prompt": "read-only",
            },
        )
    assert _payload(refused)["code"] == "READ_ONLY"


def test_stdout_has_no_banner_and_eof_stops_the_process(tmp_path: Path) -> None:
    proc = subprocess.Popen(
        [sys.executable, "-m", "cursor_cloud_mcp"],
        cwd=tmp_path,
        env=_env(CURSOR_MCP_FIXTURE="1", CURSOR_MCP_ALLOW_WRITES="0"),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdin is not None
    assert proc.stdout is not None
    message = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "probe", "version": "0"},
        },
    }
    proc.stdin.write((json.dumps(message) + "\n").encode())
    proc.stdin.flush()
    line = proc.stdout.readline()
    assert line.startswith(b"{")
    proc.stdin.close()
    try:
        code = proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
        raise
    assert code is not None
    assert proc.poll() is not None
    stderr = proc.stderr.read().decode() if proc.stderr is not None else ""
    assert "Traceback" not in stderr
    assert "SIMULATED MODE" in stderr


async def test_stdio_per_call_secret_never_reaches_stderr(tmp_path: Path) -> None:
    value = "k7"  # short: the old filter ignored secrets shorter than 8 characters
    params = _params(
        tmp_path,
        CURSOR_MCP_FIXTURE="1",
        CURSOR_MCP_ALLOW_WRITES="1",
        CURSOR_MCP_LOG_LEVEL="DEBUG",
    )
    errlog_path = tmp_path / "stderr.txt"
    with errlog_path.open("w", encoding="utf-8") as errlog:
        async with Client(stdio_client(params, errlog=errlog)) as client:
            created = await client.call_tool(
                "cursor_create_agent",
                {"prompt": "per-call secret", "name": "with-env", "env_vars": {"WORK_TOKEN": f"zz{value}zz"}},
            )
    assert created.is_error is False
    stderr = errlog_path.read_text(encoding="utf-8")
    assert f"zz{value}zz" not in stderr
