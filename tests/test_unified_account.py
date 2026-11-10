"""November 10, 2026 re-read: unified-account keys, unrecorded usage, Max Mode variants, environment filters."""

import importlib.util
import json
from pathlib import Path

import httpx
import pytest
from mcp import Client

from cursor_cloud_mcp.fixture import BUSY_AGENT_ID, SEEDED_AGENT_ID
from cursor_cloud_mcp.server import build_server
from tests.test_tools import _AGENT, Router, _agent, _data, _error_payload, _run, _session, _settings

pytestmark = pytest.mark.anyio

_RUN = "run-00000000-0000-0000-0000-000000000001"
_RUN2 = "run-00000000-0000-0000-0000-000000000002"
_ENV = "00000000-0000-4000-8000-0000000000e1"
_ZERO = {"inputTokens": 0, "outputTokens": 0, "cacheWriteTokens": 0, "cacheReadTokens": 0, "totalTokens": 0}
_SOME = {"inputTokens": 4, "outputTokens": 1, "cacheWriteTokens": 0, "cacheReadTokens": 0, "totalTokens": 5}
_FREE = {"rawCostCents": 0, "chargedCents": 0}


def _variant(context: str, fast: str, **flags: bool) -> dict[str, object]:
    params = [{"id": "context", "value": context}, {"id": "fast", "value": fast}]
    return {"params": params, "displayName": "Wide", **flags}


_CATALOG = {
    "items": [
        {
            "id": "wide-1",
            "displayName": "Wide One",
            "parameters": [
                {"id": "context", "values": [{"value": "256k"}, {"value": "500k"}]},
                {"id": "fast", "values": [{"value": "false"}, {"value": "true"}]},
            ],
            "variants": [
                _variant("256k", "false"),
                _variant("256k", "true"),
                _variant("500k", "false", isMaxMode=True),
                _variant("500k", "true", isMaxMode=True, isDefault=True),
            ],
        },
        {"id": "plain-1", "displayName": "Plain One"},
    ]
}


async def test_runs_without_recorded_usage_are_flagged_not_priced_at_zero() -> None:
    def responder(_request: httpx.Request) -> httpx.Response:
        runs = [
            {"id": _RUN, "usageUuid": "u1", "usage": _ZERO, "cost": _FREE},
            # Covered by the plan: real tokens, charged zero. Not flagged.
            {"id": _RUN2, "usageUuid": "u2", "usage": _SOME, "cost": {"rawCostCents": 3.5, "chargedCents": 0}},
        ]
        return httpx.Response(200, json={"totalUsage": _SOME, "cost": {"rawCostCents": 3.5, "chargedCents": 0}, "runs": runs})

    client, _router = await _session(Router(responder))
    try:
        view = _data(await client.call_tool("cursor_get_usage", {"agent_id": _AGENT}))
    finally:
        await client.__aexit__(None, None, None)
    assert view["runs"][0]["no_usage_recorded"] is True
    assert "no_usage_recorded" not in view["runs"][1]
    assert view["runs_without_usage"] == 1
    assert "1 of 2 run(s) report no usage" in view["warnings"][0]


async def test_usage_fully_recorded_carries_no_flag() -> None:
    def responder(_request: httpx.Request) -> httpx.Response:
        runs = [{"id": _RUN, "usage": _SOME, "cost": {"rawCostCents": 1.0, "chargedCents": 1.0}}]
        return httpx.Response(200, json={"totalUsage": _SOME, "cost": {"rawCostCents": 1.0, "chargedCents": 1.0}, "runs": runs})

    client, _router = await _session(Router(responder))
    try:
        view = _data(await client.call_tool("cursor_get_usage", {"agent_id": _AGENT}))
    finally:
        await client.__aexit__(None, None, None)
    assert "runs_without_usage" not in view
    assert "warnings" not in view
    assert "no_usage_recorded" not in view["runs"][0]


async def test_supervise_leaves_unrecorded_usage_out_of_the_fleet_total() -> None:
    silent = "bc-33333333-3333-3333-3333-333333333333"

    def responder(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/agents":
            return httpx.Response(200, json={"items": [_agent(_AGENT), _agent(silent, name="Demo 2")]})
        if path.endswith("/usage"):
            if silent in path:
                return httpx.Response(200, json={"totalUsage": _ZERO, "cost": _FREE, "runs": []})
            return httpx.Response(
                200, json={"totalUsage": _SOME, "cost": {"rawCostCents": 2.5, "chargedCents": 2.5}, "runs": []}
            )
        owner = silent if silent in path else _AGENT
        return httpx.Response(200, json=_run(agentId=owner))

    client, _router = await _session(Router(responder))
    try:
        view = _data(await client.call_tool("cursor_supervise", {"status": "all", "usage": True}))
    finally:
        await client.__aexit__(None, None, None)
    recorded, unrecorded = view["items"]
    assert recorded["charged_cents"] == 2.5
    assert unrecorded["no_usage_recorded"] is True
    assert "charged_cents" not in unrecorded
    assert view["summary"]["charged_cents_total"] == 2.5
    assert view["summary"]["no_usage_recorded"] == [silent]


@pytest.mark.parametrize(
    ("status", "runs", "total", "charged", "expected"),
    [
        # An earlier run recorded, a later ended one did not: flagged, and what was charged stays visible.
        ("FINISHED", [("run-a", 5), (_RUN, 0)], 5, 2.5, {"no_usage_recorded": True, "charged_cents": 2.5}),
        # The only zero is the run still going: it may not have reported yet.
        ("RUNNING", [("run-a", 5), (_RUN, 0)], 5, 2.5, {"charged_cents": 2.5}),
        ("CREATING", [(_RUN, 0)], 0, 0, {"charged_cents": 0.0}),
        # An ended zero run behind an active one is still missing.
        ("RUNNING", [("run-a", 0), (_RUN, 0)], 0, 0, {"no_usage_recorded": True}),
    ],
)
async def test_supervise_counts_only_ended_runs_as_missing_usage(
    status: str, runs: list[tuple[str, int]], total: int, charged: float, expected: dict[str, object]
) -> None:
    def tokens(count: int) -> dict[str, int]:
        return {**_ZERO, "inputTokens": count, "totalTokens": count}

    def responder(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/agents":
            return httpx.Response(200, json={"items": [_agent(_AGENT)]})
        if path.endswith("/usage"):
            rows = [{"id": run_id, "usage": tokens(count)} for run_id, count in runs]
            cost = {"rawCostCents": charged, "chargedCents": charged}
            return httpx.Response(200, json={"totalUsage": tokens(total), "cost": cost, "runs": rows})
        return httpx.Response(200, json=_run(status=status, result=None))

    client, _router = await _session(Router(responder))
    try:
        view = _data(await client.call_tool("cursor_supervise", {"status": "all", "usage": True}))
    finally:
        await client.__aexit__(None, None, None)
    row = view["items"][0]
    assert {key: row[key] for key in ("no_usage_recorded", "charged_cents") if key in row} == expected
    assert ("no_usage_recorded" in view["summary"]) is ("no_usage_recorded" in expected)


async def test_max_mode_is_shown_in_the_catalog_and_on_a_creation() -> None:
    posts: list[dict[str, object]] = []

    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json=_CATALOG)
        body = json.loads(request.content.decode())
        posts.append(body)
        run = _run(status="CREATING", result=None, agentId=body["agentId"])
        return httpx.Response(201, json={"agent": _agent(body["agentId"], status="ACTIVE"), "run": run})

    client, _router = await _session(Router(responder), state_dir=None)
    try:
        brief = _data(await client.call_tool("cursor_list_models", {"brief": True}))
        detail = _data(await client.call_tool("cursor_list_models", {"model_id": "wide-1"}))
        default = _data(await client.call_tool("cursor_create_agent", {"prompt": "x", "model_id": "wide-1"}))
        narrow = _data(
            await client.call_tool(
                "cursor_create_agent",
                {
                    "prompt": "x",
                    "model_id": "wide-1",
                    "model_params": [{"id": "context", "value": "256k"}, {"id": "fast", "value": "false"}],
                },
            )
        )
        plain = _data(await client.call_tool("cursor_create_agent", {"prompt": "x", "model_id": "plain-1"}))
    finally:
        await client.__aexit__(None, None, None)
    assert brief["items"][0]["default_max_mode"] is True
    assert "default_max_mode" not in brief["items"][1]
    assert detail["items"][0]["default_max_mode"] is True
    assert detail["items"][0]["max_mode_variants"] == [
        {"context": "500k", "fast": "false"},
        {"context": "500k", "fast": "true"},
    ]
    assert default["model"]["max_mode"] is True
    assert any("Max Mode" in warning for warning in default["warnings"])
    assert "max_mode" not in narrow["model"]
    assert not any("Max Mode" in warning for warning in narrow.get("warnings", []))
    assert "max_mode" not in plain["model"]
    # The flag is read from the catalog only: nothing about it is sent.
    assert posts[0]["model"] == {"id": "wide-1"}


async def test_max_mode_needs_every_variant_behind_a_selection_to_be_flagged() -> None:
    # Two variants project onto effort=high: they differ only by an unpublished parameter.
    hidden = {
        "id": "hidden-1",
        "displayName": "Hidden One",
        "parameters": [{"id": "effort", "values": [{"value": "high"}]}],
        "variants": [
            {"params": [{"id": "effort", "value": "high"}], "displayName": "x", "isDefault": True},
            {
                "params": [{"id": "effort", "value": "high"}, {"id": "cyber", "value": "true"}],
                "displayName": "x",
                "isMaxMode": True,
            },
        ],
    }
    bare = {"id": "bare-1", "displayName": "Bare One", "variants": [{"params": [], "displayName": "x", "isDefault": True, "isMaxMode": True}]}

    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"items": [hidden, bare]})
        body = json.loads(request.content.decode())
        run = _run(status="CREATING", result=None, agentId=body["agentId"])
        return httpx.Response(201, json={"agent": _agent(body["agentId"], status="ACTIVE"), "run": run})

    client, _router = await _session(Router(responder), state_dir=None)
    try:
        hidden_view = _data(await client.call_tool("cursor_list_models", {"model_id": "hidden-1"}))["items"][0]
        bare_view = _data(await client.call_tool("cursor_list_models", {"model_id": "bare-1"}))["items"][0]
        on_hidden = _data(await client.call_tool("cursor_create_agent", {"prompt": "x", "model_id": "hidden-1"}))
        on_bare = _data(await client.call_tool("cursor_create_agent", {"prompt": "x", "model_id": "bare-1"}))
    finally:
        await client.__aexit__(None, None, None)
    assert "max_mode_variants" not in hidden_view and "default_max_mode" not in hidden_view
    assert "max_mode" not in on_hidden["model"]
    assert not any("Max Mode" in warning for warning in on_hidden.get("warnings", []))
    # No parameter to list: the default flag alone tells it.
    assert bare_view["default_max_mode"] is True and "max_mode_variants" not in bare_view
    assert on_bare["model"]["max_mode"] is True


async def test_max_mode_survives_the_local_journal(tmp_path: Path) -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json=_CATALOG)
        if request.method == "GET":
            return httpx.Response(200, json=_agent(_AGENT, status="IDLE"))
        run = _run(status="CREATING", result=None)
        return httpx.Response(201, json={"agent": _agent(_AGENT, status="ACTIVE"), "run": run})

    client, _router = await _session(Router(responder), state_dir=str(tmp_path))
    try:
        created = _data(
            await client.call_tool("cursor_create_agent", {"prompt": "x", "model_id": "wide-1", "agent_id": _AGENT})
        )
        read_back = _data(await client.call_tool("cursor_get_agent", {"agent_id": _AGENT}))
    finally:
        await client.__aexit__(None, None, None)
    assert created["model"]["max_mode"] is True
    assert read_back["model"]["source"] == "local_journal"
    assert read_back["model"]["params"] == {"context": "500k", "fast": "true"}
    # Not written to the journal: an older server sharing the file refuses unknown fields.
    assert "max_mode" not in read_back["model"]
    assert "max_mode" not in (tmp_path / "models.jsonl").read_text()


@pytest.mark.parametrize(
    ("key", "expected", "noted"),
    [
        ("crsr_0123456789abcdef", "crsr", False),
        ("grok_0123456789abcdef", "grok", True),
        ("key_0123456789abcdef", "other", False),
    ],
)
async def test_account_names_the_key_type_without_the_key(key: str, expected: str, noted: bool) -> None:
    def responder(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"apiKeyName": "k", "createdAt": "2026-11-10T00:00:00.000Z", "userId": 7})

    client, _router = await _session(Router(responder), api_key=key)
    try:
        result = await client.call_tool("cursor_get_account", {})
    finally:
        await client.__aexit__(None, None, None)
    view = _data(result)
    assert view["api_key_type"] == expected
    assert ("notes" in view) is noted
    assert key not in json.dumps(view)
    assert key[5:] not in json.dumps(view)


async def test_environment_filters_are_sent_and_refused_with_an_environment_id() -> None:
    queries: list[list[tuple[str, str]]] = []

    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v0/private-workers/pools":
            return httpx.Response(200, json={"pools": []})
        queries.append(list(request.url.params.multi_items()))
        return httpx.Response(200, json={"items": []})

    client, router = await _session(Router(responder))
    filters = {
        "query": "release",
        "owner": "team",
        "named": True,
        "repositories": ["github.com/acme/api", "github.com/acme/web"],
    }
    try:
        _data(await client.call_tool("cursor_list_environments", filters))
        _data(await client.call_tool("cursor_list_environments", {**filters, "cursor": "p2", "limit": 5}))
        _data(await client.call_tool("cursor_list_environments", {"named": False}))
        sent = len(router.calls)
        refused = [
            await client.call_tool("cursor_list_environments", {"environment_id": _ENV, "owner": "team"}),
            await client.call_tool("cursor_list_environments", {"repositories": ["github.com/acme/api", " "]}),
        ]
    finally:
        await client.__aexit__(None, None, None)
    expected = [
        ("query", "release"),
        ("owner", "team"),
        ("named", "true"),
        ("repo", "github.com/acme/api"),
        ("repo", "github.com/acme/web"),
    ]
    assert queries[0] == expected
    assert queries[1] == [*expected, ("limit", "5"), ("cursor", "p2")]
    assert queries[2] == [("named", "false")]
    assert len(router.calls) == sent
    for result in refused:
        assert _error_payload(result)["code"] == "VALIDATION"


def _steer_router(answer: httpx.Response, *, latest: str | None = "run-old") -> Router:
    def responder(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=_agent(_AGENT, status="ACTIVE", latestRunId=latest))
        return answer

    return Router(responder)


def _posted(router: Router) -> list[object]:
    return [call[2] for call in router.calls if call[0] == "POST"]


async def test_steer_sends_only_prompt_and_delivery_and_reports_the_running_run() -> None:
    router = _steer_router(httpx.Response(202, json={"delivery": "steered", "runId": "run-old"}))
    client, _ = await _session(router)
    try:
        view = _data(
            await client.call_tool(
                "cursor_create_run",
                {
                    "agent_id": _AGENT,
                    "prompt": "also cover the error path",
                    "delivery": "steer",
                    "images": [{"url": "https://example.com/shot.png"}],
                },
            )
        )
    finally:
        await client.__aexit__(None, None, None)
    assert _posted(router) == [
        {
            "prompt": {"text": "also cover the error path", "images": [{"url": "https://example.com/shot.png"}]},
            "delivery": "steer",
        }
    ]
    assert view["delivery"] == "steered"
    assert view["run_id"] == "run-old"
    assert "status" not in view
    assert "no new run" in view["next_step"]


@pytest.mark.parametrize("answer", [{"delivery": "queued"}, {"delivery": "steered"}])
async def test_a_steer_without_a_run_id_invents_none(answer: dict[str, str]) -> None:
    router = _steer_router(httpx.Response(202, json=answer), latest=None)
    client, _ = await _session(router)
    try:
        view = _data(await client.call_tool("cursor_create_run", {"agent_id": _AGENT, "prompt": "go", "delivery": "steer"}))
    finally:
        await client.__aexit__(None, None, None)
    assert view["delivery"] == answer["delivery"]
    assert "run_id" not in view
    assert "cursor_get_agent" in view["next_step"]
    assert view["next_step"].startswith("steered" if answer["delivery"] == "steered" else "No running turn")
    assert len(_posted(router)) == 1


@pytest.mark.parametrize(
    "extra",
    [
        {"mode": "plan"},
        {"model_id": "wide-1"},
        {"reasoning_level": "high"},
        {"model_params": [{"id": "fast", "value": "true"}]},
        {"replace_active": True},
        {"env_vars": {"TOKEN": "steer-secret-value"}},
        {"forward_env": ["TOKEN"]},
    ],
)
async def test_a_steer_with_any_other_option_is_refused_before_any_request(extra: dict[str, object]) -> None:
    router = _steer_router(httpx.Response(202, json={"delivery": "queued"}))
    client, _ = await _session(router)
    try:
        result = await client.call_tool(
            "cursor_create_run", {"agent_id": _AGENT, "prompt": "go", "delivery": "steer", **extra}
        )
    finally:
        await client.__aexit__(None, None, None)
    payload = _error_payload(result)
    assert payload["code"] == "VALIDATION"
    assert "steer-secret-value" not in json.dumps(payload)
    # Nothing read, cancelled or sent.
    assert router.calls == []


async def test_a_steer_the_account_does_not_have_is_a_permission_error_and_is_not_resent() -> None:
    refusal = {"error": {"code": "feature_unavailable", "message": "Steering is not available for your account."}}
    router = _steer_router(httpx.Response(403, json=refusal))
    client, _ = await _session(router)
    try:
        result = await client.call_tool("cursor_create_run", {"agent_id": _AGENT, "prompt": "go", "delivery": "steer"})
    finally:
        await client.__aexit__(None, None, None)
    payload = _error_payload(result)
    assert payload["code"] == "PERMISSION"
    assert payload["remote_code"] == "feature_unavailable"
    assert len(_posted(router)) == 1


async def test_an_unreadable_steer_answer_is_an_unknown_outcome_not_a_failure_to_retry() -> None:
    router = _steer_router(httpx.Response(202, json={"delivery": "something-else"}))
    client, _ = await _session(router)
    try:
        result = await client.call_tool("cursor_create_run", {"agent_id": _AGENT, "prompt": "go", "delivery": "steer"})
    finally:
        await client.__aexit__(None, None, None)
    assert _error_payload(result)["code"] == "MUTATION_OUTCOME_UNKNOWN"
    assert len(_posted(router)) == 1


async def test_follow_up_env_vars_are_sent_for_the_run_and_kept_out_of_the_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUN_TOKEN", "forwarded-run-secret")

    def responder(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=_agent(_AGENT, status="IDLE"))
        return httpx.Response(201, json={"run": _run(status="CREATING", result=None)})

    router = Router(responder)
    client, _ = await _session(router, forward_env=frozenset({"RUN_TOKEN"}))
    try:
        result = await client.call_tool(
            "cursor_create_run",
            {"agent_id": _AGENT, "prompt": "next", "env_vars": {"SHARD": "inline-run-secret"}, "forward_env": ["RUN_TOKEN"]},
        )
        refused = [
            await client.call_tool("cursor_create_run", {"agent_id": _AGENT, "prompt": "next", **bad})
            for bad in (
                {"forward_env": ["NOT_ALLOWED"]},
                {"env_vars": {"CURSOR_TOKEN": "x"}},
                {"env_vars": {"MY_cursor_sandbox_FLAG": "x"}},
            )
        ]
    finally:
        await client.__aexit__(None, None, None)
    view = _data(result)
    assert _posted(router) == [
        {"prompt": {"text": "next"}, "envVars": {"SHARD": "inline-run-secret", "RUN_TOKEN": "forwarded-run-secret"}}
    ]
    assert "secret" not in json.dumps(view)
    for item in refused:
        assert _error_payload(item)["code"] == "VALIDATION"


async def test_waiting_for_background_work_is_a_known_active_state() -> None:
    def responder(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_run(status="WAITING_FOR_BACKGROUND_WORK", result=None))

    client, _router = await _session(Router(responder))
    try:
        run = _data(await client.call_tool("cursor_get_run", {"agent_id": _AGENT, "run_id": _RUN, "activity": False}))
    finally:
        await client.__aexit__(None, None, None)
    assert run["status"] == "WAITING_FOR_BACKGROUND_WORK"
    assert run["status_known"] is True
    assert run["terminal"] is False


async def test_the_smoke_scripts_local_guard_sends_nothing() -> None:
    # The paid smoke script counts on this value being refused before any request.
    spec = importlib.util.spec_from_file_location("smoke_live", Path(__file__).parents[1] / "scripts" / "smoke_live.py")
    assert spec is not None and spec.loader is not None
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)
    router = Router(lambda _request: httpx.Response(500, json={"error": {"code": "internal_error", "message": "x"}}))
    client, _ = await _session(router)
    try:
        result = await client.call_tool(
            "cursor_create_agent",
            {"prompt": "x", "repository": smoke.REPO_URL, "starting_ref": smoke.BAD_REF, "model_id": smoke.MODEL},
        )
    finally:
        await client.__aexit__(None, None, None)
    assert _error_payload(result)["code"] == "VALIDATION"
    assert router.calls == []


async def test_fixture_mode_answers_a_steer() -> None:
    server = build_server(_settings(api_key=None, fixture=True))
    async with Client(server) as client:
        steered = _data(
            await client.call_tool("cursor_create_run", {"agent_id": BUSY_AGENT_ID, "prompt": "go", "delivery": "steer"})
        )
        queued = _data(
            await client.call_tool("cursor_create_run", {"agent_id": SEEDED_AGENT_ID, "prompt": "go", "delivery": "steer"})
        )
        busy = await client.call_tool("cursor_create_run", {"agent_id": BUSY_AGENT_ID, "prompt": "go"})
    assert steered["delivery"] == "steered" and steered["run_id"] == "run-busy"
    assert queued["delivery"] == "queued" and "run_id" not in queued
    assert _error_payload(busy)["code"] == "AGENT_BUSY"


async def test_a_steer_with_empty_option_containers_is_still_a_bare_steer() -> None:
    router = _steer_router(httpx.Response(202, json={"delivery": "queued"}))
    client, _ = await _session(router)
    try:
        view = _data(
            await client.call_tool(
                "cursor_create_run",
                {"agent_id": _AGENT, "prompt": "go", "delivery": "steer", "env_vars": {}, "forward_env": [], "model_params": []},
            )
        )
    finally:
        await client.__aexit__(None, None, None)
    assert view["delivery"] == "queued"
    assert _posted(router) == [{"prompt": {"text": "go"}, "delivery": "steer"}]


async def test_a_steer_cut_off_says_not_to_resend_and_where_to_look() -> None:
    router = _steer_router(httpx.Response(502, json={"error": {"code": "upstream_error", "message": "bad gateway"}}))
    client, _ = await _session(router)
    try:
        result = await client.call_tool("cursor_create_run", {"agent_id": _AGENT, "prompt": "go", "delivery": "steer"})
    finally:
        await client.__aexit__(None, None, None)
    payload = _error_payload(result)
    assert payload["code"] == "MUTATION_OUTCOME_UNKNOWN"
    assert "Do not send this steer again" in payload["recovery"]
    assert "cursor_read_conversation" in payload["recovery"]
    assert len(_posted(router)) == 1


async def test_a_steer_answered_with_a_new_run_keeps_its_run_id() -> None:
    run = _run(id=_RUN2, status="CREATING", result=None)
    router = _steer_router(httpx.Response(201, json={"run": run}))
    client, _ = await _session(router)
    try:
        view = _data(await client.call_tool("cursor_create_run", {"agent_id": _AGENT, "prompt": "go", "delivery": "steer"}))
    finally:
        await client.__aexit__(None, None, None)
    assert view["run_id"] == _RUN2 and view["status"] == "CREATING"
    assert "delivery" not in view
    assert "new run" in view["warnings"][0]


async def test_202_is_a_success_for_a_steer_only() -> None:
    router = _steer_router(httpx.Response(202, json={"delivery": "queued"}))
    client, _ = await _session(router)
    try:
        result = await client.call_tool("cursor_create_run", {"agent_id": _AGENT, "prompt": "go"})
    finally:
        await client.__aexit__(None, None, None)
    assert _error_payload(result)["code"] in {"INCOMPATIBLE_RESPONSE", "MUTATION_OUTCOME_UNKNOWN"}


async def test_agent_busy_recovery_names_the_steer_before_the_cancellation() -> None:
    busy = {"error": {"code": "agent_busy", "message": "A run is already active."}}
    router = _steer_router(httpx.Response(409, json=busy))
    client, _ = await _session(router)
    try:
        result = await client.call_tool("cursor_create_run", {"agent_id": _AGENT, "prompt": "go"})
    finally:
        await client.__aexit__(None, None, None)
    payload = _error_payload(result)
    assert payload["code"] == "AGENT_BUSY"
    recovery = str(payload["recovery"])
    assert recovery.index("delivery=steer") < recovery.index("replace_active=true")
