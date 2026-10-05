"""Catalogue, sessions de calcul, flux, artefacts et garde-fous de suppression."""

import asyncio
import io
import json
import logging

import httpx
import pytest
from mcp import Client

from cursor_cloud_mcp import redaction
from cursor_cloud_mcp.client import CursorCloudClient
from cursor_cloud_mcp.config import Settings, load_settings
from cursor_cloud_mcp.errors import CursorFailure, ErrorCode, failure
from cursor_cloud_mcp.server import _configure_logging, build_server
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


def _capture_root() -> tuple[io.StringIO, logging.Handler, list[tuple[logging.Handler, logging.Formatter | None]]]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    root = logging.getLogger()
    root.addHandler(handler)
    previous = [(item, item.formatter) for item in root.handlers]
    return stream, handler, previous


def _release_root(handler: logging.Handler, previous: list[tuple[logging.Handler, logging.Formatter | None]]) -> None:
    for item, formatter in previous:
        item.setFormatter(formatter)  # type: ignore[arg-type]
    logging.getLogger().removeHandler(handler)


def test_formatter_redacts_child_sdk_loggers_and_tracebacks() -> None:
    secret = "redact-sentinel-xyz"
    stream, handler, previous = _capture_root()
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
        logging.getLogger("cursor_cloud_mcp.client").warning("child %s", secret)
        logging.getLogger("mcp").warning("sdk %s", secret)
        try:
            raise RuntimeError(f"échec avec {secret}")
        except RuntimeError:
            logging.getLogger("mcp").exception("crash")
        text = stream.getvalue()
        assert secret not in text
        assert text.count("[redacted]") >= 3
        assert "RuntimeError" in text
    finally:
        _release_root(handler, previous)


def test_short_and_per_call_secrets_are_redacted(isolated_secrets: None) -> None:
    redaction.register("k9z")
    redaction.register("per-call-value-1234")
    assert redaction.redact("a k9z b per-call-value-1234") == "a [redacted] b [redacted]"


def test_short_secret_is_masked_as_a_whole_word_only(isolated_secrets: None) -> None:
    redaction.register("en", "1")
    assert redaction.redact("agent_id=bc-1 token en") == "agent_id=bc-[redacted] token [redacted]"
    assert redaction.redact("status=401 tenant") == "status=401 tenant"


def test_error_payload_stays_valid_json_with_short_secrets(isolated_secrets: None) -> None:
    redaction.register("1", "en")
    body = failure(ErrorCode.AUTHENTICATION, "refusé : 1", http_status=401, agent_id="bc-x").as_dict()
    redacted = redaction.redact_value(body)
    assert json.loads(json.dumps(redacted)) == redacted
    assert set(redacted) == set(body)  # type: ignore[arg-type]
    assert redacted["http_status"] == 401  # type: ignore[index]
    assert redacted["message"] == "refusé : [redacted]"  # type: ignore[index]


def test_permanent_secrets_survive_eviction_of_per_call_values(
    isolated_secrets: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(redaction, "_MAX_RECENT", 2)
    redaction.register("permanent-api-key", permanent=True)
    redaction.register("value-number-one", "value-number-two", "value-number-three")
    text = redaction.redact("permanent-api-key value-number-one value-number-three")
    assert text == "[redacted] value-number-one [redacted]"


async def test_reasoning_level_is_resolved_before_post() -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json=_MODELS)
        body = json.loads(request.content.decode())
        assert body["model"] == {
            "id": "grok-4.6",
            "params": [{"id": "thinking", "value": "true"}, {"id": "effort", "value": "high"}],
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
                    "model_params": [{"id": "thinking", "value": "true"}],
                    "agent_id": _AGENT,
                },
            )
        )
    finally:
        await client.__aexit__(None, None, None)
    assert created["agent_id"] == _AGENT
    assert router.calls[0][1] == "/v1/models"
    assert router.calls[1][0] == "POST"


_CATALOG = {
    "items": [
        {
            "id": "gpt-5.5",
            "displayName": "GPT-5.5",
            "aliases": ["gpt-5-5", "gpt"],
            "parameters": [
                {"id": "reasoning", "values": [{"value": "low"}, {"value": "high"}]},
                {"id": "fast", "values": [{"value": "false"}, {"value": "true"}]},
            ],
            # Trois variantes sur quatre combinaisons : low + fast n'existe pas.
            "variants": [
                {
                    "params": [{"id": "reasoning", "value": "low"}, {"id": "fast", "value": "false"}],
                    "displayName": "GPT-5.5",
                    "isDefault": True,
                },
                {"params": [{"id": "reasoning", "value": "high"}, {"id": "fast", "value": "false"}], "displayName": "x"},
                {"params": [{"id": "reasoning", "value": "high"}, {"id": "fast", "value": "true"}], "displayName": "x"},
            ],
        },
        {"id": "gpt-5.4", "displayName": "GPT-5.4", "aliases": ["gpt"]},
    ]
}


async def test_model_catalog_is_compact_and_detailed_on_request() -> None:
    client, _router = await _session(Router(lambda _request: httpx.Response(200, json=_CATALOG)))
    try:
        compact = _data(await client.call_tool("cursor_list_models", {}))
        detail = _data(await client.call_tool("cursor_list_models", {"model_id": "gpt-5-5"}))
        ambiguous = await client.call_tool("cursor_list_models", {"model_id": "gpt"})
    finally:
        await client.__aexit__(None, None, None)
    first = compact["items"][0]
    assert first == {
        "id": "gpt-5.5",
        "display_name": "GPT-5.5",
        "aliases": ["gpt-5-5", "gpt"],
        "params": {"reasoning": ["low", "high"], "fast": ["false", "true"]},
        "defaults": {"reasoning": "low", "fast": "false"},
        "reasoning_param": "reasoning",
        "restricted_combinations": True,
    }
    assert compact["items"][1] == {"id": "gpt-5.4", "display_name": "GPT-5.4", "aliases": ["gpt"]}
    assert [item["id"] for item in detail["items"]] == ["gpt-5.5"]
    assert {"reasoning": "low", "fast": "true"} not in detail["items"][0]["variants"]
    assert len(detail["items"][0]["variants"]) == 3
    assert "gpt-5.5" in str(_error_payload(ambiguous)["message"])


async def test_alias_and_invalid_combination_are_resolved_before_post() -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json=_CATALOG)
        body = json.loads(request.content.decode())
        assert body["model"] == {"id": "gpt-5.5", "params": [{"id": "reasoning", "value": "high"}]}
        return _created(body["agentId"])

    client, router = await _session(Router(responder))
    try:
        created = await client.call_tool(
            "cursor_create_agent", {"prompt": "x", "model_id": "gpt-5-5", "reasoning_level": "high"}
        )
        refused = await client.call_tool(
            "cursor_create_agent",
            {
                "prompt": "x",
                "model_id": "gpt-5.5",
                "reasoning_level": "low",
                "model_params": [{"id": "fast", "value": "true"}],
            },
        )
        ambiguous = await client.call_tool("cursor_create_agent", {"prompt": "x", "model_id": "gpt"})
    finally:
        await client.__aexit__(None, None, None)
    assert created.is_error is False
    assert _error_payload(refused)["code"] == "VALIDATION"
    assert "Combinaison" in str(_error_payload(refused)["message"])
    assert _error_payload(ambiguous)["code"] == "VALIDATION"
    assert [call[0] for call in router.calls].count("POST") == 1


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
                "starting_ref": _BRANCH,
                "env_type": "cloud",
                "env_name": "Release",
            },
        )
        multi = await client.call_tool(
            "cursor_create_agent",
            {
                "prompt": "x",
                "repositories": [
                    {"url": "https://github.com/acme/demo", "starting_ref": _BRANCH},
                    {"url": "https://github.com/acme/other", "starting_ref": _BRANCH},
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
                        {"url": "https://github.com/acme/demo", "starting_ref": _BRANCH},
                        {"url": "https://github.com/acme/other", "starting_ref": _BRANCH},
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
                "cursor_get_run",
                {"agent_id": _AGENT, "run_id": _RUN, "wait_seconds": 5},
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
            "cursor_read_artifact",
            {"agent_id": _AGENT, "path": "../secrets", "url_only": True},
        )
        located = _data(
            await client.call_tool(
                "cursor_read_artifact",
                {"agent_id": _AGENT, "path": "artifacts/result.txt", "url_only": True},
            )
        )
    assert text["text"] == "résultat"
    assert "url" not in text
    assert _error_payload(refused)["code"] == "VALIDATION"
    assert located == {
        "path": "artifacts/result.txt",
        "expires_at": "2026-10-01T00:15:00Z",
        "url": "https://bucket.s3.us-east-1.amazonaws.com/result.txt",
    }


async def test_binary_or_large_artifact_returns_its_url() -> None:
    def api(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "url": "https://bucket.s3.us-east-1.amazonaws.com/" + request.url.params["path"],
                "expiresAt": "2026-10-01T00:15:00Z",
            },
        )

    def download(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".png"):
            return httpx.Response(200, content=b"\x89PNG\xff\xfe")
        return httpx.Response(200, content=b"x" * 5_000_001)

    server = build_server(
        _settings(),
        transport=httpx.MockTransport(api),
        download_transport=httpx.MockTransport(download),
    )
    async with Client(server) as client:
        binary = _data(
            await client.call_tool("cursor_read_artifact", {"agent_id": _AGENT, "path": "artifacts/plot.png"})
        )
        large = _data(
            await client.call_tool("cursor_read_artifact", {"agent_id": _AGENT, "path": "artifacts/big.txt"})
        )
    assert binary["text_unavailable"] == "not_utf8"
    assert binary["url"].endswith("artifacts/plot.png")
    assert "text" not in binary
    assert large["text_unavailable"] == "too_large"
    assert "text" not in large


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
