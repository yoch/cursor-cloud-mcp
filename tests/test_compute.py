"""Catalogue, sessions de calcul, flux, artefacts et garde-fous de suppression."""

import asyncio
import json
import logging

import httpx
import pytest
from mcp import Client

from cursor_cloud_mcp.client import CursorCloudClient
from cursor_cloud_mcp.config import Settings, load_settings
from cursor_cloud_mcp.errors import CursorFailure, ErrorCode
from cursor_cloud_mcp.server import _configure_logging, _RedactFilter, build_server
from cursor_cloud_mcp.stream import SseParser
from tests.test_tools import (
    Router,
    _agent,
    _data,
    _error_payload,
    _run,
    _session,
    _settings,
)

pytestmark = pytest.mark.anyio

_BRANCH = "release/2026"
_AGENT = "bc-22222222-2222-2222-2222-222222222222"
_RUN = "run-00000000-0000-0000-0000-000000000001"
_MODELS = {
    "items": [
        {
            "id": "grok-4.6",
            "displayName": "Grok 4.6",
            "parameters": [
                {"id": "effort", "values": [{"value": "low"}, {"value": "high"}, {"value": "xhigh"}]},
                {"id": "thinking", "values": [{"value": "true"}, {"value": "false"}]},
            ],
        },
        {
            "id": "gpt-5.5",
            "displayName": "GPT-5.5",
            "parameters": [
                {"id": "reasoning", "values": [{"value": "medium"}, {"value": "extra-high"}]},
            ],
        },
    ]
}


def _created(agent_id: str = _AGENT) -> httpx.Response:
    agent = _agent(agent_id=agent_id, repos=[])
    run = _run(status="CREATING", result=None)
    run["agentId"] = agent_id
    return httpx.Response(201, json={"agent": agent, "run": run})


def test_sse_parser_keeps_a_split_event() -> None:
    parser = SseParser()
    assert parser.feed('id: 1\nevent: assistant\ndata: {"te') == []
    found = parser.feed('xt":"bonjour"}\n\n')
    assert len(found) == 1
    assert found[0].event_id == "1"
    assert found[0].event == "assistant"
    assert json.loads(found[0].data)["text"] == "bonjour"


def test_fixture_and_real_key_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CURSOR_MCP_FIXTURE", "1")
    monkeypatch.setenv("CURSOR_API_KEY", "real-key-value")
    settings = load_settings()
    assert settings.fixture is True
    assert settings.config_error is not None
    assert "CURSOR_API_KEY" in settings.config_error
    assert "real-key-value" not in settings.config_error
    monkeypatch.setenv("CURSOR_MCP_ALLOW_DELETE", "1")
    monkeypatch.setenv("CURSOR_MCP_FORWARD_ENV", " WORK_TOKEN , OTHER ")
    monkeypatch.delenv("CURSOR_API_KEY")
    allowed = load_settings()
    assert allowed.config_error is None
    assert allowed.allow_delete is True
    assert allowed.forward_env == frozenset({"WORK_TOKEN", "OTHER"})


def test_handler_filter_redacts_child_and_sdk_loggers(caplog: pytest.LogCaptureFixture) -> None:
    secret = "redact-sentinel-xyz"
    root = logging.getLogger()
    previous = [(handler, list(handler.filters)) for handler in root.handlers]
    caplog.set_level(logging.INFO)
    _configure_logging(
        Settings(
            api_key=secret,
            allow_writes=False,
            log_level="INFO",
            fixture=False,
            config_error=None,
        )
    )
    try:
        logging.getLogger("cursor_cloud_mcp.client").info("child %s", secret)
        logging.getLogger("mcp").warning("sdk %s", secret)
        assert secret not in caplog.text
        assert caplog.text.count("[redacted]") >= 2
    finally:
        for handler, filters in previous:
            handler.filters[:] = filters
        for handler in root.handlers:
            handler.filters = [item for item in handler.filters if not isinstance(item, _RedactFilter)]


async def test_reasoning_level_is_resolved_before_post() -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json=_MODELS)
        body = json.loads(request.content.decode())
        assert body["model"] == {
            "id": "grok-4.6",
            "params": [{"id": "effort", "value": "high"}, {"id": "thinking", "value": "true"}],
        }
        assert "repos" not in body
        assert body["workOnCurrentBranch"] is False
        return _created(body["agentId"])

    client, router = await _session(Router(responder))
    try:
        created = _data(
            await client.call_tool(
                "cursor_create_agent",
                {
                    "prompt": "Calcule 2+2 et écris artifacts/result.txt",
                    "model_id": "grok-4.6",
                    "reasoning_level": "high",
                    "thinking": True,
                    "agent_id": _AGENT,
                },
            )
        )
    finally:
        await client.__aexit__(None, None, None)
    assert created["agent_id"] == _AGENT
    assert router.calls[0][1] == "/v1/models"
    assert router.calls[1][0] == "POST"


async def test_unknown_reasoning_value_does_not_post() -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json=_MODELS)
        raise AssertionError(request.url.path)

    client, router = await _session(Router(responder))
    try:
        refused = await client.call_tool(
            "cursor_create_agent",
            {"prompt": "x", "model_id": "gpt-5.5", "reasoning_level": "xhigh"},
        )
    finally:
        await client.__aexit__(None, None, None)
    payload = _error_payload(refused)
    assert payload["code"] == "VALIDATION"
    assert "extra-high" in str(payload["message"])
    assert all(call[0] == "GET" for call in router.calls)


async def test_named_cloud_with_repos_and_unpooled_multi_repo_are_local_errors() -> None:
    router = Router(lambda _request: (_ for _ in ()).throw(AssertionError("réseau")))
    client, _router = await _session(router)
    try:
        named = await client.call_tool(
            "cursor_create_agent",
            {
                "prompt": "x",
                "repository": "https://github.com/acme/demo",
                "starting_sha": _BRANCH,
                "env_type": "cloud",
                "env_name": "Release",
            },
        )
        multi = await client.call_tool(
            "cursor_create_agent",
            {
                "prompt": "x",
                "repositories": [
                    {"url": "https://github.com/acme/demo", "starting_sha": _BRANCH},
                    {"url": "https://github.com/acme/other", "starting_sha": _BRANCH},
                ],
            },
        )
    finally:
        await client.__aexit__(None, None, None)
    assert _error_payload(named)["code"] == "VALIDATION"
    assert _error_payload(multi)["code"] == "VALIDATION"
    assert router.calls == []


async def test_forward_env_reads_allowlist_and_omits_agent_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CURSOR_MCP_FORWARD_ENV", "WORK_TOKEN")
    monkeypatch.setenv("WORK_TOKEN", "super-secret-token")

    def responder(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        assert body["envVars"] == {"WORK_TOKEN": "super-secret-token", "PUBLIC": "visible"}
        assert "agentId" not in body
        assert body["name"] == "calcul"
        assert body["env"] == {"type": "pool", "name": "gpu"}
        return _created("bc-33333333-3333-3333-3333-333333333333")

    client, _router = await _session(
        Router(responder),
        forward_env=frozenset({"WORK_TOKEN"}),
    )
    try:
        created = _data(
            await client.call_tool(
                "cursor_create_agent",
                {
                    "prompt": "x",
                    "name": "calcul",
                    "env_type": "pool",
                    "env_name": "gpu",
                    "env_vars": {"PUBLIC": "visible"},
                    "forward_env": ["WORK_TOKEN"],
                    "repositories": [
                        {"url": "https://github.com/acme/demo", "starting_sha": _BRANCH},
                        {"url": "https://github.com/acme/other", "starting_sha": _BRANCH},
                    ],
                },
            )
        )
        blocked = await client.call_tool(
            "cursor_create_agent",
            {"prompt": "x", "name": "calcul", "forward_env": ["OTHER_SECRET"]},
        )
    finally:
        await client.__aexit__(None, None, None)
    assert created["agent_id"].startswith("bc-")
    assert _error_payload(blocked)["code"] == "VALIDATION"
    assert "super-secret-token" not in json.dumps(_error_payload(blocked))


async def test_stream_resume_and_expired_code() -> None:
    sse = (
        'id: 1\nevent: status\ndata: {"status":"RUNNING"}\n\n'
        'id: 2\nevent: heartbeat\ndata: {}\n\n'
        'id: 3\nevent: tool_call\ndata: {"name":"shell","status":"completed","args":{"cmd":"x"},"result":{"ok":true}}\n\n'
        'id: 4\nevent: result\ndata: {"status":"FINISHED","text":"fait"}\n\n'
    )

    def responder(request: httpx.Request) -> httpx.Response:
        if request.headers.get("last-event-id") == "expired":
            return httpx.Response(410, json={"error": {"code": "stream_expired", "message": "parti"}})
        assert request.headers.get("last-event-id") == "1"
        return httpx.Response(
            200,
            content=sse.encode(),
            headers={"content-type": "text/event-stream", "x-cursor-stream-retention-seconds": "90"},
        )

    client, _router = await _session(Router(responder))
    try:
        view = _data(
            await client.call_tool(
                "cursor_read_run_events",
                {"agent_id": _AGENT, "run_id": _RUN, "after_event_id": "1", "max_wait_seconds": 5},
            )
        )
        expired = await client.call_tool(
            "cursor_read_run_events",
            {"agent_id": _AGENT, "run_id": _RUN, "after_event_id": "expired"},
        )
    finally:
        await client.__aexit__(None, None, None)
    kinds = [item["kind"] for item in view["events"]]
    assert "heartbeat" not in kinds
    assert "tool_call" in kinds
    assert view["finished"] is True
    assert view["run_status"] == "FINISHED"
    assert view["retention_seconds"] == 90
    assert view["last_event_id"] == "4"
    expired_payload = _error_payload(expired)
    assert expired_payload["code"] == "STREAM_EXPIRED"
    assert "cursor_get_run" in str(expired_payload["recovery"])


async def test_wait_run_returns_when_terminal() -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/runs/" + _RUN)
        return httpx.Response(200, json=_run(status="FINISHED", result="ok"))

    client, router = await _session(Router(responder))
    try:
        view = _data(
            await client.call_tool(
                "cursor_wait_run",
                {"agent_id": _AGENT, "run_id": _RUN, "max_wait_seconds": 5},
            )
        )
    finally:
        await client.__aexit__(None, None, None)
    assert view["timed_out"] is False
    assert view["terminal"] is True
    assert view["result"] == "ok"
    assert len(router.calls) == 1


async def test_artifact_download_has_no_cursor_authorization_and_rejects_other_hosts() -> None:
    def api(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/download"):
            assert request.url.params["path"] == "artifacts/result.txt"
            return httpx.Response(
                200,
                json={
                    "url": "https://bucket.s3.us-east-1.amazonaws.com/result.txt",
                    "expiresAt": "2026-10-01T00:15:00Z",
                },
            )
        raise AssertionError(request.url.path)

    def download(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in {key.lower() for key in request.headers}
        assert request.url.host == "bucket.s3.us-east-1.amazonaws.com"
        return httpx.Response(200, content="résultat".encode())

    server = build_server(
        _settings(),
        transport=httpx.MockTransport(api),
        download_transport=httpx.MockTransport(download),
    )
    async with Client(server) as client:
        text = _data(
            await client.call_tool(
                "cursor_read_artifact",
                {"agent_id": _AGENT, "path": "artifacts/result.txt"},
            )
        )
        refused = await client.call_tool(
            "cursor_get_artifact_url",
            {"agent_id": _AGENT, "path": "../secrets"},
        )
    assert text["text"] == "résultat"
    assert "url" not in text
    assert _error_payload(refused)["code"] == "VALIDATION"


async def test_presigned_host_outside_amazonaws_is_not_fetched() -> None:
    calls = {"download": 0}

    def api(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"url": "https://evil.example/collect", "expiresAt": "2026-10-01T00:15:00Z"},
        )

    def download(_request: httpx.Request) -> httpx.Response:
        calls["download"] += 1
        return httpx.Response(200, content=b"no")

    server = build_server(
        _settings(),
        transport=httpx.MockTransport(api),
        download_transport=httpx.MockTransport(download),
    )
    async with Client(server) as client:
        refused = await client.call_tool(
            "cursor_read_artifact",
            {"agent_id": _AGENT, "path": "artifacts/result.txt"},
        )
    assert _error_payload(refused)["code"] == "VALIDATION"
    assert calls["download"] == 0


async def test_delete_requires_both_guards_and_confirmation() -> None:
    router = Router(lambda _request: httpx.Response(200, json={"id": _AGENT}))
    client, _router = await _session(router, allow_delete=False)
    try:
        disabled = await client.call_tool(
            "cursor_delete_agent",
            {"agent_id": _AGENT, "confirm_agent_id": _AGENT},
        )
    finally:
        await client.__aexit__(None, None, None)
    assert _error_payload(disabled)["code"] == "DELETE_DISABLED"
    assert router.calls == []

    client, router = await _session(Router(lambda _request: httpx.Response(200, json={"id": _AGENT})), allow_delete=True)
    try:
        mismatch = await client.call_tool(
            "cursor_delete_agent",
            {"agent_id": _AGENT, "confirm_agent_id": "bc-00000000-0000-0000-0000-000000000000"},
        )
        deleted = _data(
            await client.call_tool(
                "cursor_delete_agent",
                {"agent_id": _AGENT, "confirm_agent_id": _AGENT},
            )
        )
    finally:
        await client.__aexit__(None, None, None)
    assert _error_payload(mismatch)["code"] == "VALIDATION"
    assert deleted["deleted"] is True
    assert router.calls[0][0] == "DELETE"


async def test_cancel_rejects_a_different_returned_id() -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"id": "run-other"})
        raise AssertionError("relecture")

    client, _router = await _session(Router(responder))
    try:
        refused = await client.call_tool("cursor_cancel_run", {"agent_id": _AGENT, "run_id": _RUN})
    finally:
        await client.__aexit__(None, None, None)
    assert _error_payload(refused)["code"] == "INCOMPATIBLE_RESPONSE"


async def test_creation_has_a_longer_deadline_than_reads() -> None:
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.6)
        if request.method == "POST":
            return _created(_AGENT)
        return httpx.Response(200, json={"apiKeyName": "demo", "createdAt": "2026-09-30T00:00:00Z"})

    client = CursorCloudClient(
        api_key="test-secret-key",
        transport=httpx.MockTransport(slow),
        deadline_seconds=0.3,
        create_deadline_seconds=3,
    )
    await client.open()
    try:
        created = await client.create_agent({"prompt": {"text": "x"}}, agent_id=_AGENT)
        with pytest.raises(CursorFailure) as caught:
            await client.get_account()
    finally:
        await client.aclose()
    assert created.agent.id == _AGENT
    assert caught.value.body.code is ErrorCode.TIMEOUT


async def test_rate_limit_without_retry_after_waits_once() -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"error": {"code": "rate_limit_exceeded", "message": "slow"}})
        return httpx.Response(200, json={"apiKeyName": "demo", "createdAt": "2026-09-30T00:00:00Z"})

    client = CursorCloudClient(api_key="test-secret-key", transport=httpx.MockTransport(handler), deadline_seconds=3)
    await client.open()
    try:
        account = await client.get_account()
    finally:
        await client.aclose()
    assert calls["n"] == 2
    assert account.apiKeyName == "demo"
