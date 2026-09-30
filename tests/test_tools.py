"""Les onze outils, leurs corps REST et leurs erreurs, sur un transport simulé."""

import json
from collections.abc import Callable

import httpx
import pytest
from mcp import Client
from mcp.types import CallToolResult

from cursor_cloud_mcp.config import Settings
from cursor_cloud_mcp.server import build_server

pytestmark = pytest.mark.anyio

_SHA = "a" * 40
_SHA256 = "b" * 64
_AGENT = "bc-22222222-2222-2222-2222-222222222222"
_RUN = "run-00000000-0000-0000-0000-000000000001"


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "api_key": "test-secret-key",
        "allow_writes": True,
        "log_level": "WARNING",
        "fixture": False,
        "config_error": None,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def _agent(agent_id: str = _AGENT, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": agent_id,
        "name": "Démo",
        "status": "IDLE",
        "env": {"type": "cloud"},
        "url": f"https://cursor.com/agents/{agent_id}",
        "createdAt": "2026-09-30T00:00:00.000Z",
        "updatedAt": "2026-09-30T00:00:00.000Z",
        "latestRunId": _RUN,
        "repos": [{"url": "https://github.com/acme/demo", "startingRef": _SHA}],
        "workOnCurrentBranch": False,
        "autoCreatePR": False,
    }
    payload.update(overrides)
    return payload


def _run(status: str = "FINISHED", result: str | None = "fait", **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": _RUN,
        "agentId": _AGENT,
        "status": status,
        "createdAt": "2026-09-30T00:00:00.000Z",
        "updatedAt": "2026-09-30T00:01:00.000Z",
        "durationMs": 10,
    }
    if result is not None:
        payload["result"] = result
        payload["git"] = {
            "branches": [
                {
                    "repoUrl": "github.com/acme/demo",
                    "branch": "cursor/demo",
                    "prUrl": "https://github.com/acme/demo/pull/1",
                }
            ]
        }
    payload.update(overrides)
    return payload


def _error_payload(result: CallToolResult) -> dict[str, object]:
    assert result.is_error is True
    text = result.content[0].text
    assert text is not None
    return json.loads(text[text.find("{") :])


def _data(result: CallToolResult) -> dict[str, object]:
    assert result.is_error is False
    assert result.structured_content is not None
    text = result.content[0].text
    assert text is not None
    assert json.loads(text) == result.structured_content
    return result.structured_content


class Router:
    def __init__(self, responder: Callable[[httpx.Request], httpx.Response]) -> None:
        self.responder = responder
        self.calls: list[tuple[str, str, object]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body: object = None
        if request.content:
            body = json.loads(request.content.decode())
        self.calls.append((request.method, request.url.path, body))
        return self.responder(request)


async def _session(router: Router, **settings: object) -> tuple[Client, Router]:
    server = build_server(_settings(**settings), transport=httpx.MockTransport(router))
    client = Client(server)
    await client.__aenter__()
    return client, router


async def test_listing_tools_does_not_call_cursor() -> None:
    router = Router(lambda _request: httpx.Response(500, json={"error": {"code": "internal_error", "message": "x"}}))
    client, _router = await _session(router, api_key=None)
    try:
        listed = await client.list_tools()
    finally:
        await client.__aexit__(None, None, None)
    names = [tool.name for tool in listed.tools]
    assert names == [
        "cursor_get_account",
        "cursor_list_models",
        "cursor_list_repositories",
        "cursor_list_agents",
        "cursor_get_agent",
        "cursor_create_agent",
        "cursor_list_runs",
        "cursor_get_run",
        "cursor_create_run",
        "cursor_cancel_run",
        "cursor_get_usage",
    ]
    assert router.calls == []
    reads = [tool for tool in listed.tools if tool.name.startswith("cursor_get") or tool.name.startswith("cursor_list")]
    assert all(tool.annotations is not None and tool.annotations.read_only_hint is True for tool in reads)
    create = next(tool for tool in listed.tools if tool.name == "cursor_create_agent")
    cancel = next(tool for tool in listed.tools if tool.name == "cursor_cancel_run")
    assert create.annotations is not None and create.annotations.read_only_hint is False
    assert cancel.annotations is not None and cancel.annotations.destructive_hint is True


async def test_missing_and_invalid_key_fail_without_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    router = Router(lambda _request: (_ for _ in ()).throw(AssertionError("réseau")))
    client, _router = await _session(router, api_key=None)
    try:
        missing = await client.call_tool("cursor_get_account", {})
    finally:
        await client.__aexit__(None, None, None)
    assert _error_payload(missing)["code"] == "CONFIGURATION_MISSING"
    assert router.calls == []

    client, _router = await _session(
        router,
        api_key=None,
        config_error="CURSOR_API_KEY n'est pas interpolée. Le serveur ne charge pas de fichier .env.",
    )
    try:
        invalid = await client.call_tool("cursor_list_models", {})
    finally:
        await client.__aexit__(None, None, None)
    payload = _error_payload(invalid)
    assert payload["code"] == "CONFIGURATION_MISSING"
    assert "CURSOR_API_KEY" in str(payload["message"])
    assert "${" not in str(payload["message"])


async def test_read_tools_map_the_contract() -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/me":
            return httpx.Response(
                200,
                json={"apiKeyName": "prod", "createdAt": "2026-09-30T00:00:00Z", "userEmail": "a@b.c"},
                headers={"x-request-id": "req-me"},
            )
        if path == "/v1/models":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "composer-2",
                            "displayName": "Composer 2",
                            "aliases": ["composer"],
                            "parameters": [
                                {"id": "fast", "values": [{"value": "true", "displayName": "Fast"}]}
                            ],
                        }
                    ]
                },
            )
        if path == "/v1/repositories":
            return httpx.Response(200, json={"items": [{"url": "https://github.com/acme/demo"}]})
        if path == "/v1/agents" and request.method == "GET":
            assert request.url.params["limit"] == "1"
            assert request.url.params["cursor"] == "page-a"
            return httpx.Response(
                200,
                json={"items": [_agent()], "nextCursor": "page-b"},
            )
        if path == f"/v1/agents/{_AGENT}" and request.method == "GET":
            return httpx.Response(200, json=_agent(status="IDLE"))
        if path == f"/v1/agents/{_AGENT}/runs" and request.method == "GET":
            return httpx.Response(200, json={"items": [_run()]})
        if path == f"/v1/agents/{_AGENT}/usage":
            assert request.url.params["runId"] == _RUN
            usage = {
                "inputTokens": 4,
                "outputTokens": 1,
                "cacheWriteTokens": 0,
                "cacheReadTokens": 2,
                "totalTokens": 7,
            }
            return httpx.Response(200, json={"totalUsage": usage, "runs": [{"id": _RUN, "usage": usage}]})
        raise AssertionError(path)

    router = Router(responder)
    client, _router = await _session(router)
    try:
        account = _data(await client.call_tool("cursor_get_account", {}))
        models = _data(await client.call_tool("cursor_list_models", {}))
        repos = _data(await client.call_tool("cursor_list_repositories", {}))
        agents = _data(await client.call_tool("cursor_list_agents", {"limit": 1, "cursor": "page-a"}))
        agent = _data(await client.call_tool("cursor_get_agent", {"agent_id": _AGENT}))
        runs = _data(await client.call_tool("cursor_list_runs", {"agent_id": _AGENT}))
        usage = _data(await client.call_tool("cursor_get_usage", {"agent_id": _AGENT, "run_id": _RUN}))
    finally:
        await client.__aexit__(None, None, None)
    assert account["api_key_name"] == "prod"
    assert models["items"][0]["id"] == "composer-2"
    assert models["items"][0]["parameters"][0]["values"][0]["value"] == "true"
    assert repos["items"] == ["https://github.com/acme/demo"]
    assert repos["cache_hit"] is False
    assert agents["next_cursor"] == "page-b"
    assert agents["has_more"] is True
    assert agent["status"] == "IDLE"
    assert agent["status_known"] is True
    assert agent["work_on_current_branch"] is False
    assert runs["has_more"] is False
    assert "cost" not in usage
    assert usage["total_usage"]["total_tokens"] == 7


async def test_unknown_status_and_absent_result_are_not_success() -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/runs/" + _RUN):
            if request.url.path.count("/") == 5:
                return httpx.Response(200, json=_run(status="PAUSED", result=None))
        if request.url.path == f"/v1/agents/{_AGENT}":
            return httpx.Response(200, json=_agent(status="MYSTERY"))
        raise AssertionError(request.url.path)

    router = Router(responder)
    client, _router = await _session(router)
    try:
        agent = _data(await client.call_tool("cursor_get_agent", {"agent_id": _AGENT}))
        run = _data(await client.call_tool("cursor_get_run", {"agent_id": _AGENT, "run_id": _RUN}))
    finally:
        await client.__aexit__(None, None, None)
    assert agent["status"] == "MYSTERY"
    assert agent["status_known"] is False
    assert run["status"] == "PAUSED"
    assert run["status_known"] is False
    assert run["terminal"] is None
    assert run["result_present"] is False
    assert run["result"] is None
    assert "final_sha" not in json.dumps(run)


async def test_result_window_reassembles_without_gap() -> None:
    text = "".join(f"{index:04d}" for index in range(20))

    def responder(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_run(result=text))

    router = Router(responder)
    client, _router = await _session(router)
    pieces: list[str] = []
    offset = 0
    try:
        while True:
            view = _data(
                await client.call_tool(
                    "cursor_get_run",
                    {"agent_id": _AGENT, "run_id": _RUN, "result_offset": offset, "result_limit": 8},
                )
            )
            assert view["result_total_chars"] == len(text)
            chunk = view["result"]
            assert isinstance(chunk, str)
            pieces.append(chunk)
            if view["result_truncated"] is False:
                assert view["next_result_offset"] is None
                break
            assert view["next_result_offset"] == offset + len(chunk)
            offset = int(view["next_result_offset"])
    finally:
        await client.__aexit__(None, None, None)
    assert "".join(pieces) == text
    assert view["git"]["scope"] == "agent_current_state"
    assert view["git"]["branches"][0]["repo_url"] == "github.com/acme/demo"


async def test_create_agent_sends_exact_rest_fields() -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        assert body["workOnCurrentBranch"] is False
        assert body["autoCreatePR"] is True
        assert body["repos"] == [{"url": "https://github.com/acme/demo", "startingRef": _SHA256}]
        assert body["model"] == {"id": "composer-2", "params": [{"id": "fast", "value": "true"}]}
        assert body["mode"] == "plan"
        assert body["agentId"] == _AGENT
        assert "envVars" not in body
        assert None not in body.values()
        agent = _agent(agent_id=body["agentId"], autoCreatePR=True)
        run = _run(status="CREATING", result=None)
        run["agentId"] = body["agentId"]
        return httpx.Response(201, json={"agent": agent, "run": run})

    router = Router(responder)
    client, _router = await _session(router)
    try:
        created = _data(
            await client.call_tool(
                "cursor_create_agent",
                {
                    "repository": "https://github.com/acme/demo.git",
                    "starting_sha": _SHA256,
                    "prompt": "Ajouter une note",
                    "name": "Note",
                    "model_id": "composer-2",
                    "model_params": [{"id": "fast", "value": "true"}],
                    "mode": "plan",
                    "auto_create_pr": True,
                    "agent_id": _AGENT,
                },
            )
        )
    finally:
        await client.__aexit__(None, None, None)
    assert created["agent_id"] == _AGENT
    assert created["run_id"] == _RUN
    assert router.calls[0][0] == "POST"
    assert len(router.calls) == 1


async def test_validation_rejects_sha_url_and_extra_fields_before_http() -> None:
    router = Router(lambda _request: (_ for _ in ()).throw(AssertionError("réseau")))
    client, _router = await _session(router)
    try:
        short = await client.call_tool(
            "cursor_create_agent",
            {"repository": "https://github.com/acme/demo", "starting_sha": "abc", "prompt": "x"},
        )
        secret_url = await client.call_tool(
            "cursor_create_agent",
            {
                "repository": "https://user:token@github.com/acme/demo",
                "starting_sha": _SHA,
                "prompt": "x",
            },
        )
        blank = await client.call_tool(
            "cursor_create_agent",
            {"repository": "https://github.com/acme/demo", "starting_sha": _SHA, "prompt": "   "},
        )
        extra = await client.call_tool("cursor_get_account", {"unexpected": True})
    finally:
        await client.__aexit__(None, None, None)
    assert _error_payload(short)["code"] == "VALIDATION"
    assert _error_payload(secret_url)["code"] == "VALIDATION"
    assert "token" not in json.dumps(_error_payload(secret_url))
    assert _error_payload(blank)["code"] == "VALIDATION"
    assert extra.is_error is True
    assert router.calls == []


async def test_read_only_blocks_mutations() -> None:
    router = Router(lambda _request: (_ for _ in ()).throw(AssertionError("réseau")))
    client, _router = await _session(router, allow_writes=False)
    try:
        blocked = await client.call_tool(
            "cursor_create_agent",
            {"repository": "https://github.com/acme/demo", "starting_sha": _SHA, "prompt": "x"},
        )
    finally:
        await client.__aexit__(None, None, None)
    assert _error_payload(blocked)["code"] == "READ_ONLY"
    assert router.calls == []


async def test_create_conflict_returns_the_same_agent_id() -> None:
    def responder(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"error": {"code": "agent_id_conflict", "message": "existe"}})

    router = Router(responder)
    client, _router = await _session(router)
    try:
        conflict = await client.call_tool(
            "cursor_create_agent",
            {
                "repository": "https://github.com/acme/demo",
                "starting_sha": _SHA,
                "prompt": "x",
                "agent_id": _AGENT,
            },
        )
    finally:
        await client.__aexit__(None, None, None)
    payload = _error_payload(conflict)
    assert payload["code"] == "AGENT_ID_CONFLICT"
    assert payload["agent_id"] == _AGENT
    assert payload["safe_to_retry_automatically"] is False
    assert len(router.calls) == 1


async def test_continuation_refuses_incompatible_agents_and_returns_busy() -> None:
    state = {"mode": "missing"}

    def responder(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            if state["mode"] == "missing":
                agent = _agent()
                agent.pop("workOnCurrentBranch")
                return httpx.Response(200, json=agent)
            if state["mode"] == "branch":
                return httpx.Response(200, json=_agent(workOnCurrentBranch=True))
            if state["mode"] == "multi":
                return httpx.Response(
                    200,
                    json=_agent(
                        repos=[
                            {"url": "https://github.com/acme/demo"},
                            {"url": "https://github.com/acme/other"},
                        ]
                    ),
                )
            return httpx.Response(200, json=_agent(latestRunId="run-previous"))
        return httpx.Response(409, json={"error": {"code": "agent_busy", "message": "occupé"}})

    router = Router(responder)
    client, _router = await _session(router)
    try:
        missing = await client.call_tool("cursor_create_run", {"agent_id": _AGENT, "prompt": "suite"})
        state["mode"] = "branch"
        branch = await client.call_tool("cursor_create_run", {"agent_id": _AGENT, "prompt": "suite"})
        state["mode"] = "multi"
        multi = await client.call_tool("cursor_create_run", {"agent_id": _AGENT, "prompt": "suite"})
        state["mode"] = "busy"
        busy = await client.call_tool("cursor_create_run", {"agent_id": _AGENT, "prompt": "suite"})
    finally:
        await client.__aexit__(None, None, None)
    assert _error_payload(missing)["code"] == "CONTINUATION_REFUSED"
    assert _error_payload(branch)["code"] == "CONTINUATION_REFUSED"
    assert _error_payload(multi)["code"] == "CONTINUATION_REFUSED"
    busy_payload = _error_payload(busy)
    assert busy_payload["code"] == "AGENT_BUSY"
    assert busy_payload["previous_latest_run_id"] == "run-previous"
    posts = [call for call in router.calls if call[0] == "POST"]
    assert len(posts) == 1


async def test_cancel_distinguishes_accepted_and_confirmed() -> None:
    failures_left = {"n": 2}

    def responder(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"id": _RUN})
        if failures_left["n"] > 0:
            failures_left["n"] -= 1
            return httpx.Response(500, json={"error": {"code": "upstream_error", "message": "relecture"}})
        return httpx.Response(200, json=_run(status="CANCELLED", result="arrêté"))

    client, _router = await _session(Router(responder))
    try:
        unconfirmed = _data(await client.call_tool("cursor_cancel_run", {"agent_id": _AGENT, "run_id": _RUN}))
        confirmed = _data(await client.call_tool("cursor_cancel_run", {"agent_id": _AGENT, "run_id": _RUN}))
    finally:
        await client.__aexit__(None, None, None)
    assert unconfirmed["cancel_request_accepted"] is True
    assert unconfirmed["outcome_confirmed"] is False
    assert unconfirmed["commits_removed"] is False
    assert confirmed["observed_status"] == "CANCELLED"
    assert confirmed["observed_terminal"] is True

    def already_done(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/cancel"):
            return httpx.Response(409, json={"error": {"code": "run_not_cancellable", "message": "fini"}})
        raise AssertionError(request.url.path)

    client, _router = await _session(Router(already_done))
    try:
        refused = await client.call_tool("cursor_cancel_run", {"agent_id": _AGENT, "run_id": _RUN})
    finally:
        await client.__aexit__(None, None, None)
    assert _error_payload(refused)["code"] == "CANCEL_NOT_POSSIBLE"


async def test_cursor_end_of_page_and_non_json() -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/agents":
            return httpx.Response(200, json={"items": [_agent()], "nextCursor": ""})
        return httpx.Response(200, content=b"not-json", headers={"content-type": "text/plain"})

    router = Router(responder)
    client, _router = await _session(router)
    try:
        empty_cursor = await client.call_tool("cursor_list_agents", {})
        broken = await client.call_tool("cursor_get_account", {})
    finally:
        await client.__aexit__(None, None, None)
    assert _error_payload(empty_cursor)["code"] == "INCOMPATIBLE_RESPONSE"
    assert _error_payload(broken)["code"] == "INCOMPATIBLE_RESPONSE"


async def test_repository_tool_uses_the_process_cache() -> None:
    calls = {"n": 0}

    def responder(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"items": [{"url": "https://github.com/acme/demo"}]})

    router = Router(responder)
    client, _router = await _session(router)
    try:
        first = _data(await client.call_tool("cursor_list_repositories", {}))
        second = _data(await client.call_tool("cursor_list_repositories", {}))
    finally:
        await client.__aexit__(None, None, None)
    assert calls["n"] == 1
    assert first["cache_hit"] is False
    assert second["cache_hit"] is True
