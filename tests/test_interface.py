"""Ergonomie de l'interface : sorties compactes, recherche, coût, erreurs de run, événements."""

import json

import httpx
import pytest

from tests.test_tools import (
    Router,
    _agent,
    _data,
    _error_payload,
    _run,
    _session,
)

pytestmark = pytest.mark.anyio

_AGENT = "bc-11111111-1111-1111-1111-111111111111"
_RUN = "run-00000000-0000-0000-0000-000000000001"


async def test_text_is_compact_json_without_nulls() -> None:
    client, _router = await _session(Router(lambda _request: httpx.Response(200, json=_run(status="RUNNING", result=None))))
    try:
        result = await client.call_tool("cursor_get_run", {"agent_id": _AGENT, "run_id": _RUN})
    finally:
        await client.__aexit__(None, None, None)
    text = result.content[0].text
    assert "\n" not in text and ": " not in text
    assert "null" not in text
    payload = json.loads(text)
    assert payload == result.structured_content
    assert payload["status"] == "RUNNING"
    assert "result" not in payload and "timed_out" not in payload


async def test_name_search_scans_pages_and_reports_what_it_read() -> None:
    pages = {
        None: ([_agent(f"bc-00000000-0000-0000-0000-00000000000{i}", name=f"autre {i}") for i in range(3)], "p2"),
        "p2": ([_agent(_AGENT, name="Smoke Repo")], "p3"),
        "p3": ([_agent("bc-99999999-9999-9999-9999-999999999999", name="smoke-env")], None),
    }

    def responder(request: httpx.Request) -> httpx.Response:
        assert request.url.params["limit"] == "100"
        items, following = pages[request.url.params.get("cursor")]
        payload: dict[str, object] = {"items": items}
        if following is not None:
            payload["nextCursor"] = following
        return httpx.Response(200, json=payload)

    client, router = await _session(Router(responder))
    try:
        found = _data(await client.call_tool("cursor_list_agents", {"name": "SMOKE"}))
        first = _data(await client.call_tool("cursor_list_agents", {"name": "smoke", "limit": 1}))
    finally:
        await client.__aexit__(None, None, None)
    assert [item["name"] for item in found["items"]] == ["Smoke Repo", "smoke-env"]
    assert found["scanned"] == 5
    assert found["has_more"] is False and "next_cursor" not in found
    # Assez de résultats : arrêt à la page qui les contient, reprise possible ensuite.
    assert [item["name"] for item in first["items"]] == ["Smoke Repo"]
    assert first["next_cursor"] == "p3" and first["has_more"] is True
    assert len(router.calls) == 5


async def test_pr_url_filter_is_sent_to_the_api() -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        assert request.url.params["prUrl"] == "https://github.com/acme/demo/pull/7"
        return httpx.Response(200, json={"items": [_agent(_AGENT)]})

    client, _router = await _session(Router(responder))
    try:
        page = _data(
            await client.call_tool("cursor_list_agents", {"pr_url": "https://github.com/acme/demo/pull/7"})
        )
    finally:
        await client.__aexit__(None, None, None)
    assert [item["agent_id"] for item in page["items"]] == [_AGENT]


async def test_repository_query_filters_locally() -> None:
    repos = {"items": [{"url": "https://github.com/acme/demo"}, {"url": "https://github.com/acme/Other"}]}
    client, router = await _session(Router(lambda _request: httpx.Response(200, json=repos)))
    try:
        found = _data(await client.call_tool("cursor_list_repositories", {"query": "other"}))
        again = _data(await client.call_tool("cursor_list_repositories", {}))
    finally:
        await client.__aexit__(None, None, None)
    assert found["items"] == ["https://github.com/acme/Other"]
    assert found["total_count"] == 2
    assert len(again["items"]) == 2 and again["cache_hit"] is True
    assert len(router.calls) == 1


async def test_usage_reports_the_cost_returned_by_the_api() -> None:
    tokens = {"inputTokens": 4, "outputTokens": 1, "cacheWriteTokens": 0, "cacheReadTokens": 2, "totalTokens": 7}
    with_cost = {
        "totalUsage": tokens,
        "cost": {"rawCostCents": 27.52600001, "chargedCents": 27.526},
        "runs": [{"id": _RUN, "usage": tokens, "cost": {"rawCostCents": 27.526, "chargedCents": 27.526}}],
    }
    without_cost = {"totalUsage": tokens, "runs": [{"id": _RUN, "usage": tokens}]}
    answers = iter([with_cost, without_cost])
    client, _router = await _session(Router(lambda _request: httpx.Response(200, json=next(answers))))
    try:
        priced = _data(await client.call_tool("cursor_get_usage", {"agent_id": _AGENT}))
        bare = _data(await client.call_tool("cursor_get_usage", {"agent_id": _AGENT}))
    finally:
        await client.__aexit__(None, None, None)
    assert priced["total_cost"] == {"raw_cents": 27.526, "charged_cents": 27.526}
    assert priced["runs"][0]["cost"]["charged_cents"] == 27.526
    assert "total_cost" not in bare and "cost" not in bare["runs"][0]


async def test_run_error_is_exposed() -> None:
    failed = _run(status="ERROR", result=None, error={"code": "vm_failed", "message": "La VM n'a pas démarré"})
    client, _router = await _session(Router(lambda _request: httpx.Response(200, json=failed)))
    try:
        run = _data(await client.call_tool("cursor_get_run", {"agent_id": _AGENT, "run_id": _RUN}))
    finally:
        await client.__aexit__(None, None, None)
    assert run["status"] == "ERROR" and run["terminal"] is True
    assert run["error"] == "vm_failed: La VM n'a pas démarré"


async def test_status_and_result_events_do_not_repeat_raw_json() -> None:
    sse = (
        'id: 1\nevent: status\ndata: {"runId":"r","status":"RUNNING"}\n\n'
        'id: 2\nevent: assistant\ndata: {"text":"Je"}\n\n'
        'id: 2b\nevent: assistant\ndata: {"text":" regarde."}\n\n'
        'id: 3\nevent: result\ndata: {"runId":"r","status":"FINISHED","result":"OK","git":{"branches":[]}}\n\n'
    )

    def responder(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=sse.encode(), headers={"content-type": "text/event-stream"})

    client, _router = await _session(Router(responder))
    try:
        view = _data(
            await client.call_tool("cursor_read_run_events", {"agent_id": _AGENT, "run_id": _RUN, "max_wait_seconds": 5})
        )
    finally:
        await client.__aexit__(None, None, None)
    status, assistant, result = view["events"]
    assert status == {"event_id": "1", "kind": "status", "status": "RUNNING"}
    # Fragments fusionnés ; l'identifiant est celui du dernier, point de reprise exact.
    assert assistant["text"] == "Je regarde." and assistant["event_id"] == "2b"
    assert result["text"] == "OK" and result["status"] == "FINISHED"
    assert view["finished"] is True


async def test_integration_error_keeps_help_url_and_provider() -> None:
    body = {
        "error": {
            "code": "integration_not_connected",
            "message": "GitHub n'est pas connecté.",
            "helpUrl": "https://cursor.com/dashboard/integrations",
            "provider": "github",
        }
    }
    client, _router = await _session(Router(lambda _request: httpx.Response(400, json=body)))
    try:
        refused = await client.call_tool("cursor_list_repositories", {})
    finally:
        await client.__aexit__(None, None, None)
    payload = _error_payload(refused)
    assert payload["remote_code"] == "integration_not_connected"
    assert payload["help_url"] == "https://cursor.com/dashboard/integrations"
    assert payload["provider"] == "github"


async def test_tool_call_keeps_only_its_last_state() -> None:
    running = '{"callId":"c1","name":"shell","status":"running","args":{"cmd":"ls"}}'
    done = '{"callId":"c1","name":"shell","status":"completed","args":{"cmd":"ls"},"result":"a b"}'
    other = '{"data":{"callId":"c2","name":"read","status":"running","args":{"path":"x"}}}'
    sse = (
        f"id: 1\nevent: tool_call\ndata: {running}\n\n"
        f"id: 2\nevent: tool_call\ndata: {done}\n\n"
        f"id: 3\nevent: tool_call\ndata: {other}\n\n"
    )

    def responder(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=sse.encode(), headers={"content-type": "text/event-stream"})

    client, _router = await _session(Router(responder))
    try:
        view = _data(
            await client.call_tool("cursor_read_run_events", {"agent_id": _AGENT, "run_id": _RUN, "max_wait_seconds": 2})
        )
    finally:
        await client.__aexit__(None, None, None)
    first, second = view["events"]
    assert first == {
        "event_id": "2",
        "kind": "tool_call",
        "call_id": "c1",
        "tool_name": "shell",
        "tool_status": "completed",
        "tool_args": '{"cmd": "ls"}',
        "tool_result": "a b",
    }
    assert second["call_id"] == "c2" and second["tool_name"] == "read"
    assert view["last_event_id"] == "3"
