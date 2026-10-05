"""Mutations interrompues, budgets, garde-fous fermés, flux SSE et isolation du mode simulé."""

import asyncio
import json
from collections.abc import AsyncIterator

import httpx
import pytest
from mcp import Client
from mcp.server.mcpserver.utilities.func_metadata import ArgModelBase

from cursor_cloud_mcp import budget, redaction
from cursor_cloud_mcp.client import CursorCloudClient
from cursor_cloud_mcp.errors import CursorFailure, ErrorCode
from cursor_cloud_mcp.fixture import SEEDED_AGENT_ID
from cursor_cloud_mcp.server import build_server
from cursor_cloud_mcp.stream import read_run_events
from tests.test_tools import Router, _agent, _data, _error_payload, _run, _session, _settings

pytestmark = pytest.mark.anyio

_AGENT = "bc-33333333-3333-3333-3333-333333333333"
_RUN = "run-00000000-0000-0000-0000-000000000001"


class _BrokenStream(httpx.AsyncByteStream):
    """Quelques octets, puis une coupure réseau après les en-têtes."""

    def __init__(self, *chunks: bytes, error: Exception | None = None, close_error: bool = False) -> None:
        self._chunks = chunks
        self._error = error
        self._close_error = close_error

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk
        if self._error is not None:
            raise self._error

    async def aclose(self) -> None:
        if self._close_error:
            raise httpx.ReadError("fermeture ratée")


async def _client(handler: object, **kwargs: object) -> CursorCloudClient:
    client = CursorCloudClient(
        api_key="test-secret-key",
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
        **kwargs,  # type: ignore[arg-type]
    )
    await client.open()
    return client


async def test_post_cut_after_headers_is_an_uncertain_mutation_with_context() -> None:
    posts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        posts["n"] += 1
        return httpx.Response(
            201,
            headers={"x-request-id": "req-cut"},
            stream=_BrokenStream(b'{"agent": {"id"', error=httpx.ReadError("coupé")),
        )

    client = await _client(handler)
    try:
        with pytest.raises(CursorFailure) as caught:
            await client.create_agent({"prompt": {"text": "x"}, "agentId": _AGENT}, agent_id=_AGENT)
    finally:
        await client.aclose()
    body = caught.value.body
    assert posts["n"] == 1
    assert body.code is ErrorCode.MUTATION_OUTCOME_UNKNOWN
    assert body.agent_id == _AGENT
    assert body.http_status == 201
    assert body.request_id == "req-cut"
    assert body.safe_to_retry_automatically is False


async def test_followup_cut_after_headers_keeps_previous_run() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, stream=_BrokenStream(b"{", error=httpx.RemoteProtocolError("fin")))

    client = await _client(handler)
    try:
        with pytest.raises(CursorFailure) as caught:
            await client.create_run(_AGENT, {"prompt": {"text": "x"}}, previous_latest_run_id="run-old")
    finally:
        await client.aclose()
    assert caught.value.body.code is ErrorCode.MUTATION_OUTCOME_UNKNOWN
    assert caught.value.body.previous_latest_run_id == "run-old"


async def test_get_cut_after_headers_is_a_read_failure() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_BrokenStream(b"{", error=httpx.ReadError("coupé")))

    client = await _client(handler)
    try:
        with pytest.raises(CursorFailure) as caught:
            await client.get_account()
    finally:
        await client.aclose()
    assert caught.value.body.code is ErrorCode.TIMEOUT
    assert caught.value.body.http_status == 200


async def test_close_error_does_not_override_an_established_result() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        payload = json.dumps({"apiKeyName": "demo", "createdAt": "2026-10-01T00:00:00Z"}).encode()
        return httpx.Response(
            200,
            headers={"content-length": str(len(payload))},
            stream=_BrokenStream(payload, close_error=True),
        )

    client = await _client(handler)
    try:
        account = await client.get_account()
    finally:
        await client.aclose()
    assert account.apiKeyName == "demo"


async def test_exhausted_budget_sends_no_mutation() -> None:
    posts = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        posts["n"] += 1
        return httpx.Response(201, json={})

    client = await _client(handler)
    try:
        with budget.tool_budget(1.0), pytest.raises(CursorFailure) as caught:
            await client.create_agent({"prompt": {"text": "x"}}, agent_id=_AGENT)
    finally:
        await client.aclose()
    assert posts["n"] == 0
    assert caught.value.body.code is ErrorCode.TIMEOUT


async def test_slow_catalog_consumes_the_create_budget() -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        await asyncio.sleep(10)
        return httpx.Response(200, json={"items": []})

    client = await _client(handler)
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        with budget.tool_budget(0.5):
            with pytest.raises(CursorFailure) as caught:
                await client.cached_models()
            with pytest.raises(CursorFailure):
                await client.create_agent({"prompt": {"text": "x"}}, agent_id=_AGENT)
    finally:
        await client.aclose()
    assert caught.value.body.code is ErrorCode.TIMEOUT
    assert loop.time() - started < 3
    assert "POST" not in calls


async def test_continuation_is_refused_when_safety_fields_are_unknown() -> None:
    state = {"agent": _agent(_AGENT)}

    def responder(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=state["agent"])
        return httpx.Response(201, json={"run": _run(status="CREATING", result=None)})

    client, router = await _session(Router(responder))
    try:
        missing = dict(_agent(_AGENT))
        del missing["workOnCurrentBranch"]
        state["agent"] = missing
        absent = await client.call_tool("cursor_create_run", {"agent_id": _AGENT, "prompt": "suite"})
        state["agent"] = _agent(_AGENT, status="MIGRATING")
        unknown = await client.call_tool("cursor_create_run", {"agent_id": _AGENT, "prompt": "suite"})
    finally:
        await client.__aexit__(None, None, None)
    assert _error_payload(absent)["code"] == "CONTINUATION_REFUSED"
    assert "absent" in str(_error_payload(absent)["message"])
    assert _error_payload(unknown)["code"] == "CONTINUATION_REFUSED"
    assert [call for call in router.calls if call[0] == "POST"] == []


async def test_unarchive_with_unknown_status_is_not_confirmed() -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"id": _AGENT})
        return httpx.Response(200, json=_agent(_AGENT, status="RESTORING"))

    client, _router = await _session(Router(responder))
    try:
        view = _data(await client.call_tool("cursor_archive_agent", {"agent_id": _AGENT, "unarchive": True}))
    finally:
        await client.__aexit__(None, None, None)
    assert view["action"] == "unarchive"
    assert view["request_accepted"] is True
    assert view["outcome_confirmed"] is False


async def test_cancel_racing_with_finish_is_not_a_cancellation() -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"id": _RUN})
        return httpx.Response(200, json=_run(status="FINISHED"))

    client, _router = await _session(Router(responder))
    try:
        view = _data(await client.call_tool("cursor_cancel_run", {"agent_id": _AGENT, "run_id": _RUN}))
    finally:
        await client.__aexit__(None, None, None)
    assert view["outcome"] == "ended_without_cancel"
    assert view["observed_terminal"] is True
    assert view["outcome_confirmed"] is False


@pytest.mark.parametrize("value", ["per-call-secret-value-42", "4"])
async def test_secret_reflected_by_cursor_is_redacted_in_the_tool_error(isolated_secrets: None, value: str) -> None:
    def responder(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"code": "validation_error", "message": f"refusé: {value}"}})

    client, _router = await _session(Router(responder))
    try:
        result = await client.call_tool(
            "cursor_create_agent",
            {"prompt": "x", "name": "avec secret", "env_vars": {"WORK_TOKEN": value}},
        )
    finally:
        await client.__aexit__(None, None, None)
    # Un secret court (« 4 ») ne doit ni fuir ni casser le JSON, ni toucher http_status 400.
    payload = _error_payload(result)
    assert payload["code"] == "VALIDATION"
    assert payload["http_status"] == 400
    assert str(payload["message"]).endswith(f"refusé: {redaction.REDACTED}")


async def test_unknown_arguments_are_refused_without_patching_the_sdk() -> None:
    client, router = await _session(Router(lambda _request: httpx.Response(200, json=_agent(_AGENT))))
    try:
        listed = await client.list_tools()
        refused = await client.call_tool("cursor_get_agent", {"agent_id": _AGENT, "surprise": 1})
    finally:
        await client.__aexit__(None, None, None)
    assert refused.is_error is True
    assert router.calls == []
    schema = next(tool.input_schema for tool in listed.tools if tool.name == "cursor_get_agent")
    assert schema.get("additionalProperties") is False
    # La classe du SDK n'est pas modifiée : un autre serveur du processus garde son comportement.
    assert ArgModelBase.model_config.get("extra") != "forbid"


async def test_starting_ref_is_the_only_branch_name() -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        return httpx.Response(
            201,
            json={
                "agent": _agent(body["agentId"], status="ACTIVE"),
                "run": _run(status="CREATING", result=None, agentId=body["agentId"]),
            },
        )

    client, router = await _session(Router(responder))
    try:
        single = await client.call_tool(
            "cursor_create_agent",
            {"prompt": "x", "repository": "https://github.com/acme/demo", "starting_ref": "main"},
        )
        listed = await client.call_tool(
            "cursor_create_agent",
            {"prompt": "x", "repositories": [{"url": "https://github.com/acme/demo", "starting_ref": "dev"}]},
        )
        legacy = await client.call_tool(
            "cursor_create_agent",
            {"prompt": "x", "repository": "https://github.com/acme/demo", "starting_sha": "main"},
        )
    finally:
        await client.__aexit__(None, None, None)
    assert single.is_error is False and listed.is_error is False
    refs = [call[2]["repos"][0]["startingRef"] for call in router.calls]  # type: ignore[index]
    assert refs == ["main", "dev"]
    # L'ancien nom n'est plus accepté : argument inconnu, rien n'est envoyé.
    assert legacy.is_error is True
    assert len(router.calls) == 2


def _sse(handler_chunks: list[bytes], *, error: Exception | None = None) -> httpx.MockTransport:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_BrokenStream(*handler_chunks, error=error),
        )

    return httpx.MockTransport(handler)


async def _events(transport: httpx.MockTransport, **kwargs: object) -> dict[str, object]:
    client = CursorCloudClient(api_key="k", transport=transport)
    await client.open()
    try:
        view = await read_run_events(
            client,
            agent_id=_AGENT,
            run_id=_RUN,
            after_event_id=kwargs.get("after_event_id"),  # type: ignore[arg-type]
            max_wait_seconds=2,
            max_events=50,
            include_thinking=False,
        )
    finally:
        await client.aclose()
    return view.model_dump()


async def test_sse_utf8_split_across_chunks_is_decoded() -> None:
    raw = 'id: 1\nevent: assistant\ndata: {"text": "café"}\n\nevent: done\ndata: {}\n\n'.encode()
    cut = raw.index("é".encode()) + 1
    view = await _events(_sse([raw[:cut], raw[cut:]]))
    assert view["events"][0]["text"] == "café"  # type: ignore[index]
    assert view["finished"] is True


async def test_sse_error_event_is_not_a_finished_run() -> None:
    raw = b'id: 7\nevent: error\ndata: {"message": "flux perdu"}\n\n'
    view = await _events(_sse([raw]))
    assert view["stream_error"] is True
    assert view["finished"] is False
    assert view["last_event_id"] == "7"


async def test_sse_disconnect_keeps_partial_events_and_cursor() -> None:
    raw = b'id: 3\nevent: assistant\ndata: {"text": "un"}\n\nid: 4\nevent: assi'
    view = await _events(_sse([raw], error=httpx.ReadError("coupé")))
    assert view["interrupted"] is True
    assert [event["text"] for event in view["events"]] == ["un"]  # type: ignore[index]
    assert view["last_event_id"] == "3"


async def test_sse_cursor_is_kept_without_new_events_and_clipping_is_flagged() -> None:
    quiet = await _events(_sse([b": ping\n\n"]), after_event_id="41")
    assert quiet["last_event_id"] == "41"
    long_text = "x" * 2000
    raw = f'id: 5\nevent: assistant\ndata: {{"text": "{long_text}"}}\n\nevent: done\ndata: {{}}\n\n'.encode()
    view = await _events(_sse([raw]))
    assert view["events"][0]["clipped"] is True  # type: ignore[index]


async def test_fixture_mode_never_opens_a_network_connection_for_artifacts() -> None:
    server = build_server(_settings(api_key=None, fixture=True))
    async with Client(server) as client:
        result = await client.call_tool(
            "cursor_read_artifact",
            {"agent_id": SEEDED_AGENT_ID, "path": "artifacts/result.txt"},
        )
    assert _data(result)["text"] == "fixture artefact\n"


async def test_abandoned_wait_does_not_cancel_the_run(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("INFO", logger="cursor_cloud_mcp.server")

    def responder(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_run(status="RUNNING", result=None))

    client, router = await _session(Router(responder))
    try:
        waiting = asyncio.create_task(
            client.call_tool("cursor_get_run", {"agent_id": _AGENT, "run_id": _RUN, "wait_seconds": 30})
        )
        await asyncio.sleep(0.3)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        await asyncio.sleep(0.2)
    finally:
        await client.__aexit__(None, None, None)
    assert [call for call in router.calls if call[0] == "POST"] == []
    assert "outcome=cancelled" in caplog.text
