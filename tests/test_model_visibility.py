"""Model visibility: effective selection, server default, local journal, fleet spend."""

import json
from pathlib import Path

import httpx
import pytest

from cursor_cloud_mcp.config import parse_default_model
from cursor_cloud_mcp.journal import ModelJournal
from cursor_cloud_mcp.models import ModelSelectionView
from tests.test_tools import _AGENT, Router, _agent, _data, _error_payload, _run, _session

pytestmark = pytest.mark.anyio

_RUN = "run-00000000-0000-0000-0000-000000000001"
_RUN2 = "run-00000000-0000-0000-0000-000000000002"
_CATALOG = {
    "items": [
        {
            "id": "gpt-5.5",
            "displayName": "GPT-5.5",
            "aliases": ["gpt-5-5"],
            "parameters": [
                {"id": "reasoning", "values": [{"value": "low"}, {"value": "high"}]},
                {"id": "fast", "values": [{"value": "false"}, {"value": "true"}]},
            ],
            "variants": [
                {
                    "params": [{"id": "reasoning", "value": "low"}, {"id": "fast", "value": "true"}],
                    "displayName": "GPT-5.5 Fast",
                    "isDefault": True,
                },
                {"params": [{"id": "reasoning", "value": "high"}, {"id": "fast", "value": "false"}], "displayName": "x"},
                {"params": [{"id": "reasoning", "value": "high"}, {"id": "fast", "value": "true"}], "displayName": "x"},
            ],
        },
        {"id": "small-1", "displayName": "Small One", "aliases": ["tiny"]},
    ]
}


def _created(agent_id: str, run_id: str = _RUN) -> httpx.Response:
    run = _run(status="CREATING", result=None, id=run_id, agentId=agent_id)
    return httpx.Response(201, json={"agent": _agent(agent_id, status="ACTIVE", latestRunId=run_id), "run": run})


def _responder(posts: list[object]):
    def respond(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/models":
            return httpx.Response(200, json=_CATALOG)
        if request.method == "POST" and path == "/v1/agents":
            body = json.loads(request.content.decode())
            posts.append(body)
            return _created(body["agentId"])
        if request.method == "POST" and path.endswith("/runs"):
            posts.append(json.loads(request.content.decode()))
            run = _run(status="CREATING", result=None, id=_RUN2, createdAt="2999-01-01T00:00:00.000Z")
            return httpx.Response(201, json={"run": run})
        if path.endswith(f"/runs/{_RUN2}"):
            return httpx.Response(200, json=_run(id=_RUN2, createdAt="2999-01-01T00:00:00.000Z"))
        if path.endswith(f"/runs/{_RUN}"):
            return httpx.Response(200, json=_run())
        return httpx.Response(200, json=_agent(_AGENT, status="IDLE"))

    return respond


async def test_default_variant_is_echoed_and_warned_and_journaled(tmp_path: Path) -> None:
    posts: list[object] = []
    client, _router = await _session(Router(_responder(posts)), state_dir=str(tmp_path))
    try:
        created = _data(
            await client.call_tool("cursor_create_agent", {"prompt": "go", "model_id": "gpt-5-5", "agent_id": _AGENT})
        )
        agent = _data(await client.call_tool("cursor_get_agent", {"agent_id": _AGENT}))
        run = _data(await client.call_tool("cursor_get_run", {"agent_id": _AGENT, "run_id": _RUN}))
        followed = _data(await client.call_tool("cursor_create_run", {"agent_id": _AGENT, "prompt": "more"}))
        inherited = _data(await client.call_tool("cursor_get_run", {"agent_id": _AGENT, "run_id": _RUN2}))
    finally:
        await client.__aexit__(None, None, None)
    assert posts[0]["model"] == {"id": "gpt-5.5"}  # type: ignore[index]
    assert created["model"] == {
        "id": "gpt-5.5",
        "params": {"reasoning": "low", "fast": "true"},
        "defaulted": ["reasoning", "fast"],
        "source": "request",
    }
    assert "fast=true" in created["warnings"][0]
    assert agent["model"]["source"] == "local_journal" and agent["model"]["params"]["fast"] == "true"
    assert run["model"]["id"] == "gpt-5.5" and "inherited" not in run["model"]
    # The follow-up sent no model: the agent keeps it, and the run inherits it.
    assert posts[1] == {"prompt": {"text": "more"}}
    assert followed["model"]["source"] == "local_journal"
    assert "last one this MCP recorded" in followed["warnings"][0]
    assert inherited["model"]["inherited"] is True
    lines = (tmp_path / "models.jsonl").read_text().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["run_id"] == _RUN


async def test_unfixed_parameter_is_reported_unset() -> None:
    posts: list[object] = []
    client, _router = await _session(Router(_responder(posts)))
    try:
        created = _data(
            await client.call_tool(
                "cursor_create_agent",
                {"prompt": "go", "model_id": "gpt-5.5", "reasoning_level": "high", "agent_id": _AGENT},
            )
        )
        explicit = _data(
            await client.call_tool(
                "cursor_create_agent",
                {
                    "prompt": "go",
                    "model_id": "gpt-5.5",
                    "reasoning_level": "high",
                    "model_params": [{"id": "fast", "value": "false"}],
                    "agent_id": _AGENT,
                },
            )
        )
    finally:
        await client.__aexit__(None, None, None)
    # The default variant (low, fast) does not fit reasoning=high, and two variants do.
    assert created["model"]["unset"] == ["fast"]
    assert "defaulted" not in created["model"]
    assert "fast" in created["warnings"][0]
    assert explicit["model"]["params"] == {"reasoning": "high", "fast": "false"}
    assert "warnings" not in explicit


async def test_server_default_applies_to_creation_only(tmp_path: Path) -> None:
    posts: list[object] = []
    client, _router = await _session(
        Router(_responder(posts)),
        default_model=("gpt-5.5", (("reasoning", "high"), ("fast", "false"))),
        state_dir=str(tmp_path),
    )
    try:
        created = _data(await client.call_tool("cursor_create_agent", {"prompt": "go", "agent_id": _AGENT}))
        _data(await client.call_tool("cursor_create_run", {"agent_id": _AGENT, "prompt": "more"}))
    finally:
        await client.__aexit__(None, None, None)
    assert posts[0]["model"] == {  # type: ignore[index]
        "id": "gpt-5.5",
        "params": [{"id": "reasoning", "value": "high"}, {"id": "fast", "value": "false"}],
    }
    assert created["model"]["source"] == "server_default"
    assert "warnings" not in created
    assert "model" not in posts[1]  # type: ignore[operator]


async def test_no_model_warns_and_bad_server_default_is_named() -> None:
    posts: list[object] = []
    client, _router = await _session(Router(_responder(posts)))
    try:
        bare = _data(await client.call_tool("cursor_create_agent", {"prompt": "go", "agent_id": _AGENT}))
    finally:
        await client.__aexit__(None, None, None)
    assert "model" not in posts[0]  # type: ignore[operator]
    assert "model" not in bare
    assert "CURSOR_MCP_DEFAULT_MODEL" in bare["warnings"][0]

    client, _router = await _session(Router(_responder(posts)), default_model=("nope", ()))
    try:
        refused = await client.call_tool("cursor_create_agent", {"prompt": "go", "agent_id": _AGENT})
    finally:
        await client.__aexit__(None, None, None)
    payload = _error_payload(refused)
    assert payload["code"] == "VALIDATION"
    assert "CURSOR_MCP_DEFAULT_MODEL" in str(payload["message"])
    assert len(posts) == 1


def test_default_model_syntax() -> None:
    assert parse_default_model(None) == (None, None)
    assert parse_default_model(" m-1 ") == (("m-1", ()), None)
    assert parse_default_model("m-1:fast=false, effort=high") == (
        ("m-1", (("fast", "false"), ("effort", "high"))),
        None,
    )
    for bad in (":fast=false", "m-1:fast", "m-1:=x", "m-1:fast="):
        assert parse_default_model(bad)[0] is None
        assert "CURSOR_MCP_DEFAULT_MODEL" in str(parse_default_model(bad)[1])


def test_journal_skips_damaged_lines_and_never_raises(tmp_path: Path) -> None:
    model = ModelSelectionView(id="m-1", source="request")
    journal = ModelJournal(str(tmp_path))
    (tmp_path / "models.jsonl").write_text('not json\n{"ts": "x"}\n')
    journal.record(agent_id="a", run_id="r1", run_created_at="2026-01-01T00:00:00Z", model=model)
    assert journal.for_agent("a") is not None
    assert journal.for_run("a", "r1", None) is not None
    # Before any record, a run's model is unknown; after, it is inherited.
    assert journal.for_run("a", "r0", "2000-01-01T00:00:00Z") is None
    later = journal.for_run("a", "r2", "2999-01-01T00:00:00Z")
    assert later is not None and later.inherited is True
    assert ModelJournal(None).for_agent("a") is None
    blocker = tmp_path / "file"
    blocker.write_text("")
    ModelJournal(str(blocker)).record(agent_id="a", run_id=None, run_created_at=None, model=model)


async def test_brief_and_query_catalog() -> None:
    client, _router = await _session(Router(lambda _request: httpx.Response(200, json=_CATALOG)))
    try:
        brief = _data(await client.call_tool("cursor_list_models", {"brief": True}))
        found = _data(await client.call_tool("cursor_list_models", {"query": "TINY"}))
    finally:
        await client.__aexit__(None, None, None)
    assert brief["items"][0] == {
        "id": "gpt-5.5",
        "aliases": ["gpt-5-5"],
        "defaults": {"reasoning": "low", "fast": "true"},
    }
    assert [item["id"] for item in found["items"]] == ["small-1"]


async def test_supervise_sums_usage_and_rereads_a_failed_status(tmp_path: Path) -> None:
    second = "bc-33333333-3333-3333-3333-333333333333"
    # Two 503s: the client's own GET retry absorbs one, the supervision retry the other.
    failures = {"left": 2}

    def responder(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/agents":
            return httpx.Response(200, json={"items": [_agent(_AGENT), _agent(second, name="Demo 2")]})
        if path.endswith("/usage"):
            cents = 2.5 if _AGENT in path else 1.25
            tokens = {
                "inputTokens": 1,
                "outputTokens": 1,
                "cacheWriteTokens": 0,
                "cacheReadTokens": 0,
                "totalTokens": 2,
            }
            return httpx.Response(
                200,
                json={
                    "totalUsage": tokens,
                    "cost": {"rawCostCents": cents, "chargedCents": cents},
                    "runs": [],
                },
            )
        if second in path and failures["left"]:
            failures["left"] -= 1
            return httpx.Response(503, json={"error": {"code": "unavailable", "message": "busy"}})
        owner = second if second in path else _AGENT
        return httpx.Response(200, json=_run(status="RUNNING", result=None, agentId=owner))

    client, _router = await _session(Router(responder))
    try:
        view = _data(await client.call_tool("cursor_supervise", {"status": "all", "usage": True}))
    finally:
        await client.__aexit__(None, None, None)
    assert [row["status"] for row in view["items"]] == ["RUNNING", "RUNNING"]
    assert [row["charged_cents"] for row in view["items"]] == [2.5, 1.25]
    assert view["summary"]["charged_cents_total"] == 3.75
    assert "read_errors" not in view["summary"]


async def test_usage_limit_is_told_apart_from_a_rate_limit() -> None:
    def responder(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"code": "usage_limit_exceeded", "message": "limit"}})

    client, _router = await _session(Router(responder))
    try:
        result = await client.call_tool("cursor_get_account", {})
    finally:
        await client.__aexit__(None, None, None)
    payload = _error_payload(result)
    assert payload["code"] == "QUOTA"
    assert "spend limit" in str(payload["message"])
    assert "account owner" in str(payload["recovery"])


async def test_usage_rows_carry_ratio_and_recorded_model(tmp_path: Path) -> None:
    journal = ModelJournal(str(tmp_path))
    journal.record(
        agent_id=_AGENT,
        run_id=_RUN,
        run_created_at="2026-09-30T00:00:00.000Z",
        model=ModelSelectionView(id="m-1", source="request"),
    )
    tokens = {"inputTokens": 1, "outputTokens": 1, "cacheWriteTokens": 0, "cacheReadTokens": 0, "totalTokens": 200_000}

    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/runs"):
            return httpx.Response(200, json={"items": [_run(), _run(id=_RUN2, createdAt="2999-01-01T00:00:00.000Z")]})
        cost = {"rawCostCents": 2.0, "chargedCents": 2.0}
        return httpx.Response(
            200,
            json={
                "totalUsage": tokens,
                "cost": cost,
                "runs": [{"id": _RUN, "usage": tokens, "cost": cost}, {"id": _RUN2, "usage": tokens, "cost": cost}],
            },
        )

    client, _router = await _session(Router(responder), state_dir=str(tmp_path))
    try:
        usage = _data(await client.call_tool("cursor_get_usage", {"agent_id": _AGENT}))
    finally:
        await client.__aexit__(None, None, None)
    assert usage["charged_cents_per_million_tokens"] == 10.0
    first, following = usage["runs"]
    assert first["model"]["id"] == "m-1" and "inherited" not in first["model"]
    assert following["model"]["inherited"] is True


async def test_a_permanent_status_error_is_not_retried() -> None:
    reads = {"count": 0}

    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/agents":
            return httpx.Response(200, json={"items": [_agent(_AGENT)]})
        reads["count"] += 1
        return httpx.Response(404, json={"error": {"code": "not_found", "message": "gone"}})

    client, _router = await _session(Router(responder))
    try:
        view = _data(await client.call_tool("cursor_supervise", {"status": "all"}))
    finally:
        await client.__aexit__(None, None, None)
    assert reads["count"] == 1
    assert view["summary"]["by_status"] == {"UNREAD": 1}


def test_inheritance_uses_cursor_time_not_the_local_clock(tmp_path: Path) -> None:
    journal = ModelJournal(str(tmp_path))
    first = ModelSelectionView(id="model-a", source="request")
    switched = ModelSelectionView(id="model-b", source="request")
    journal.record(agent_id="a", run_id="r1", run_created_at="2026-01-01T00:00:00Z", model=first)
    # Written "now" by the local clock, long after the run it describes started on Cursor's.
    journal.record(agent_id="a", run_id="r2", run_created_at="2026-01-01T01:00:00Z", model=switched)
    inherited = journal.for_run("a", "r3", "2026-01-01T01:00:20Z")
    assert inherited is not None and inherited.id == "model-b" and inherited.inherited is True
    before = journal.for_run("a", "r0", "2026-01-01T00:30:00Z")
    assert before is not None and before.id == "model-a"


async def test_an_uncertain_model_switch_is_journaled_unconfirmed(tmp_path: Path) -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json=_CATALOG)
        if request.method == "POST":
            return httpx.Response(500, json={"error": {"code": "internal", "message": "oops"}})
        return httpx.Response(200, json=_agent(_AGENT, status="IDLE"))

    client, _router = await _session(Router(responder), state_dir=str(tmp_path))
    try:
        sent = await client.call_tool(
            "cursor_create_run",
            {
                "agent_id": _AGENT,
                "prompt": "switch",
                "model_id": "small-1",
            },
        )
        agent = _data(await client.call_tool("cursor_get_agent", {"agent_id": _AGENT}))
    finally:
        await client.__aexit__(None, None, None)
    assert _error_payload(sent)["code"] == "MUTATION_OUTCOME_UNKNOWN"
    assert agent["model"]["id"] == "small-1" and agent["model"]["unconfirmed"] is True


async def test_a_malformed_server_default_refuses_only_the_creations_that_use_it() -> None:
    posts: list[object] = []
    error = "CURSOR_MCP_DEFAULT_MODEL must be id or id:param=value,param=value."
    client, _router = await _session(Router(_responder(posts)), default_model_error=error)
    try:
        models = _data(await client.call_tool("cursor_list_models", {"brief": True}))
        refused = await client.call_tool("cursor_create_agent", {"prompt": "go", "agent_id": _AGENT})
        explicit = _data(
            await client.call_tool("cursor_create_agent", {"prompt": "go", "model_id": "small-1", "agent_id": _AGENT})
        )
    finally:
        await client.__aexit__(None, None, None)
    assert models["items"]
    assert _error_payload(refused)["code"] == "VALIDATION"
    assert "CURSOR_MCP_DEFAULT_MODEL" in str(_error_payload(refused)["message"])
    assert explicit["model"]["id"] == "small-1"
    assert len(posts) == 1


def test_a_malformed_server_default_is_not_a_configuration_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from cursor_cloud_mcp.config import load_settings

    monkeypatch.setenv("CURSOR_MCP_DEFAULT_MODEL", "m-1:fast")
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    settings = load_settings()
    assert settings.config_error is None
    assert settings.default_model is None and settings.default_model_error is not None


async def test_server_default_merges_with_the_calls_own_parameters() -> None:
    posts: list[object] = []
    client, _router = await _session(
        Router(_responder(posts)),
        default_model=("gpt-5.5", (("reasoning", "low"), ("fast", "false"))),
    )
    try:
        created = _data(
            await client.call_tool(
                "cursor_create_agent", {"prompt": "go", "reasoning_level": "high", "agent_id": _AGENT}
            )
        )
    finally:
        await client.__aexit__(None, None, None)
    assert posts[0]["model"] == {  # type: ignore[index]
        "id": "gpt-5.5",
        "params": [{"id": "fast", "value": "false"}, {"id": "reasoning", "value": "high"}],
    }
    assert created["model"]["source"] == "server_default"


def test_effective_selection_checks_the_default_against_what_was_set() -> None:
    from cursor_cloud_mcp.catalog import effective_selection
    from cursor_cloud_mcp.models import RemoteModelList

    def variant(default: bool = False, **values: str) -> dict[str, object]:
        return {
            "params": [{"id": key, "value": value} for key, value in values.items()],
            "displayName": "v",
            "isDefault": default,
        }

    catalog = RemoteModelList.model_validate(
        {
            "items": [
                {
                    "id": "m",
                    "displayName": "M",
                    "parameters": [
                        {"id": "fast", "values": [{"value": "true"}, {"value": "false"}]},
                        {"id": "thinking", "values": [{"value": "true"}, {"value": "false"}]},
                        {"id": "effort", "values": [{"value": "low"}, {"value": "high"}]},
                    ],
                    "variants": [
                        variant(True, fast="true", thinking="true", effort="high"),
                        variant(fast="false", thinking="true", effort="low"),
                        variant(fast="false", thinking="true", effort="high"),
                    ],
                }
            ]
        }
    )
    # effort=low does not fit the default variant: its fast=true must not be reported.
    params, defaulted, unset = effective_selection(catalog, {"id": "m", "params": [{"id": "effort", "value": "low"}]})
    assert params == {"effort": "low", "fast": "false", "thinking": "true"}
    assert defaulted == ["fast", "thinking"] and unset == []
    # fast=false fits two variants: they agree on thinking, not on effort.
    params, defaulted, unset = effective_selection(catalog, {"id": "m", "params": [{"id": "fast", "value": "false"}]})
    assert params == {"fast": "false", "thinking": "true"}
    assert defaulted == ["thinking"] and unset == ["effort"]


async def test_usage_dates_come_from_later_pages_or_from_the_single_run(tmp_path: Path) -> None:
    journal = ModelJournal(str(tmp_path))
    journal.record(
        agent_id=_AGENT,
        run_id=_RUN,
        run_created_at="2026-09-30T00:00:00.000Z",
        model=ModelSelectionView(id="m-1", source="request"),
    )
    tokens = {"inputTokens": 1, "outputTokens": 1, "cacheWriteTokens": 0, "cacheReadTokens": 0, "totalTokens": 2}
    late = _run(id=_RUN2, createdAt="2999-01-01T00:00:00.000Z")

    def responder(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/runs"):
            if request.url.params.get("cursor") is None:
                return httpx.Response(200, json={"items": [_run()], "nextCursor": "p2"})
            return httpx.Response(200, json={"items": [late]})
        if path.endswith(f"/runs/{_RUN2}"):
            return httpx.Response(200, json=late)
        return httpx.Response(200, json={"totalUsage": tokens, "runs": [{"id": _RUN2, "usage": tokens}]})

    client, router = await _session(Router(responder), state_dir=str(tmp_path))
    try:
        whole = _data(await client.call_tool("cursor_get_usage", {"agent_id": _AGENT}))
        listed = len([call for call in router.calls if call[1].endswith("/runs")])
        one = _data(await client.call_tool("cursor_get_usage", {"agent_id": _AGENT, "run_id": _RUN2}))
    finally:
        await client.__aexit__(None, None, None)
    assert whole["runs"][0]["model"]["inherited"] is True
    assert listed == 2
    assert one["runs"][0]["model"]["id"] == "m-1"
    assert len([call for call in router.calls if call[1].endswith("/runs")]) == listed
