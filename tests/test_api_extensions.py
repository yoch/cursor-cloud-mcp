"""API surface added from the October 19, 2026 spec: images, environments, conversation, diagnostics."""

import base64
import json

import httpx
import pytest

from tests.test_tools import _AGENT, Router, _agent, _data, _error_payload, _run, _session

pytestmark = pytest.mark.anyio

_ENV = "00000000-0000-4000-8000-0000000000e1"
_BUILD = "bld-20261019-00000000-0000-4000-8000-0000000000b1"
_PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * 32).decode()


def _created(agent_id: str) -> httpx.Response:
    run = _run(status="CREATING", result=None, agentId=agent_id)
    return httpx.Response(201, json={"agent": _agent(agent_id, status="ACTIVE"), "run": run})


def _posting(posts: list[dict[str, object]]):
    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/agents":
            body = json.loads(request.content.decode())
            posts.append(body)
            return _created(body.get("agentId", _AGENT))
        if request.method == "POST":
            posts.append(json.loads(request.content.decode()))
            return httpx.Response(201, json={"run": _run(status="CREATING", result=None)})
        return httpx.Response(200, json=_agent(_AGENT, status="IDLE"))

    return respond


async def test_images_are_checked_locally_and_sent_as_prompt_images() -> None:
    posts: list[dict[str, object]] = []
    client, _router = await _session(Router(_posting(posts)))
    images = [
        {"url": "https://example.com/shot.png"},
        {"data": _PNG, "mime_type": "image/png", "width": 4, "height": 3},
    ]
    try:
        _data(await client.call_tool("cursor_create_agent", {"prompt": "look", "images": images, "agent_id": _AGENT}))
        _data(await client.call_tool("cursor_create_run", {"agent_id": _AGENT, "prompt": "again", "images": images[:1]}))
        refused = [
            await client.call_tool("cursor_create_agent", {"prompt": "x", "images": bad, "agent_id": _AGENT})
            for bad in (
                [{"url": "https://example.com/a.png"}] * 6,
                [{"url": "https://example.com/a.png", "data": _PNG, "mime_type": "image/png"}],
                [{"data": _PNG}],
                [{"data": "not base64 at all!", "mime_type": "image/png"}],
                [{"url": "https://example.com/a.png", "mime_type": "image/png"}],
                [{"url": "ftp://example.com/a.png"}],
                [{"url": "https://user:secret@example.com/a.png"}],
                [{"data": _PNG, "mime_type": "image/bmp"}],
                [{"data": _PNG, "mime_type": "image/png", "width": 4}],
            )
        ]
    finally:
        await client.__aexit__(None, None, None)
    assert posts[0]["prompt"] == {
        "text": "look",
        "images": [
            {"url": "https://example.com/shot.png"},
            {"data": _PNG, "mimeType": "image/png", "dimension": {"width": 4, "height": 3}},
        ],
    }
    assert posts[1]["prompt"] == {"text": "again", "images": [{"url": "https://example.com/shot.png"}]}
    assert len(posts) == 2
    for result in refused:
        payload = _error_payload(result)
        assert payload["code"] == "VALIDATION"
        assert "not base64 at all" not in json.dumps(payload)
        assert "secret" not in json.dumps(payload)


async def test_oversized_image_is_refused_by_size_not_content(monkeypatch: pytest.MonkeyPatch) -> None:
    from cursor_cloud_mcp import validation

    monkeypatch.setattr(validation, "IMAGE_MAX_BYTES", 16)
    client, router = await _session(Router(_posting([])))
    try:
        result = await client.call_tool(
            "cursor_create_agent",
            {"prompt": "x", "images": [{"data": _PNG, "mime_type": "image/png"}], "agent_id": _AGENT},
        )
    finally:
        await client.__aexit__(None, None, None)
    payload = _error_payload(result)
    assert payload["code"] == "VALIDATION" and "Image 1" in str(payload["message"])
    assert _PNG not in json.dumps(payload)
    assert router.calls == []


async def test_skip_reviewer_needs_auto_pr_and_a_sha_ref_is_sent() -> None:
    posts: list[dict[str, object]] = []
    client, _router = await _session(Router(_posting(posts)))
    sha = "b" * 40
    try:
        refused = await client.call_tool(
            "cursor_create_agent", {"prompt": "x", "skip_reviewer_request": True, "agent_id": _AGENT}
        )
        _data(
            await client.call_tool(
                "cursor_create_agent",
                {
                    "prompt": "x",
                    "repository": "https://github.com/acme/demo",
                    "starting_ref": sha,
                    "auto_create_pr": True,
                    "skip_reviewer_request": True,
                    "agent_id": _AGENT,
                },
            )
        )
        _data(
            await client.call_tool(
                "cursor_create_agent",
                {
                    "prompt": "x",
                    "repositories": [
                        {"url": "https://github.com/acme/demo", "starting_ref": "main"},
                        {"url": "https://github.com/acme/other", "starting_ref": "main"},
                    ],
                    "env_type": "cloud",
                    "agent_id": _AGENT,
                },
            )
        )
        long_name = await client.call_tool(
            "cursor_create_agent", {"prompt": "x", "env_type": "pool", "env_name": "p" * 256, "agent_id": _AGENT}
        )
    finally:
        await client.__aexit__(None, None, None)
    assert _error_payload(refused)["code"] == "VALIDATION"
    assert posts[0]["repos"] == [{"url": "https://github.com/acme/demo", "startingRef": sha}]
    assert posts[0]["skipReviewerRequest"] is True and posts[0]["autoCreatePR"] is True
    assert len(posts[1]["repos"]) == 2  # type: ignore[arg-type]
    assert _error_payload(long_name)["code"] == "VALIDATION"
    assert len(posts) == 2


async def test_quota_carries_rate_limit_headers_and_stream_unavailable_is_expired() -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/me":
            headers = {"X-RateLimit-Limit": "60", "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1760900000"}
            return httpx.Response(
                429, headers=headers, json={"error": {"code": "rate_limit_exceeded", "message": "slow down"}}
            )
        return httpx.Response(404, json={"error": {"code": "stream_unavailable", "message": "gone"}})

    client, _router = await _session(Router(responder))
    try:
        limited = _error_payload(await client.call_tool("cursor_get_account", {}))
        events = _error_payload(
            await client.call_tool(
                "cursor_read_run_events",
                {"agent_id": _AGENT, "run_id": "run-00000000-0000-0000-0000-000000000001", "max_wait_seconds": 2},
            )
        )
    finally:
        await client.__aexit__(None, None, None)
    assert limited["code"] == "QUOTA"
    assert (limited["rate_limit_limit"], limited["rate_limit_remaining"], limited["rate_limit_reset"]) == (
        60,
        0,
        "1760900000",
    )
    assert events["code"] == "STREAM_EXPIRED"


def _environment(name: str = "Release workspace") -> dict[str, object]:
    return {
        "id": _ENV,
        "name": name,
        "owner": "team",
        "repos": [{"url": "https://github.com/acme/demo"}],
        "createdAt": "2026-10-01T00:00:00.000Z",
        "updatedAt": "2026-10-02T00:00:00.000Z",
    }


async def test_environments_list_pools_best_effort_and_detail_with_build() -> None:
    pools = {"status": 200}

    def responder(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/environments":
            payload: dict[str, object] = {"items": [_environment()]}
            if request.url.params.get("cursor") is None:
                payload["nextCursor"] = "p2"
            return httpx.Response(200, json=payload)
        if path == "/v0/private-workers/pools":
            if pools["status"] != 200:
                return httpx.Response(403, json={"error": {"code": "service_account_required", "message": "no"}})
            pool = {"poolName": "gpu", "scope": "team", "connectedWorkerCount": 2, "inUseWorkerCount": 1}
            return httpx.Response(200, json={"pools": [pool]})
        if path == f"/v1/environments/{_ENV}":
            return httpx.Response(200, json=_environment())
        if path == f"/v1/environments/{_ENV}/builds/active":
            return httpx.Response(200, json={"type": "build", "buildId": _BUILD})
        if path == f"/v1/environments/{_ENV}/builds/{_BUILD}":
            build = {
                "id": _BUILD,
                "environmentId": _ENV,
                "status": "FAILED",
                "trigger": "CONFIG_CHANGE",
                "draft": False,
                "failure": {"type": "INSTALL_FAILED", "code": "npm_ci"},
                "createdAt": "2026-10-02T00:00:00.000Z",
                "updatedAt": "2026-10-02T00:00:00.000Z",
            }
            return httpx.Response(200, json=build)
        return httpx.Response(404, json={"error": {"code": "environment_not_found", "message": "?"}})

    client, router = await _session(Router(responder))
    try:
        first = _data(await client.call_tool("cursor_list_environments", {}))
        second = _data(await client.call_tool("cursor_list_environments", {"cursor": "p2"}))
        pools["status"] = 403
        refused = _data(await client.call_tool("cursor_list_environments", {}))
        detail = _data(await client.call_tool("cursor_list_environments", {"environment_id": _ENV}))
    finally:
        await client.__aexit__(None, None, None)
    assert first["items"][0] == {
        "environment_id": _ENV,
        "name": "Release workspace",
        "owner": "team",
        "repos": ["https://github.com/acme/demo"],
        "updated_at": "2026-10-02T00:00:00.000Z",
    }
    assert first["pools"] == [{"name": "gpu", "scope": "team", "connected_workers": 2, "in_use_workers": 1}]
    assert first["next_cursor"] == "p2" and first["has_more"] is True
    assert "pools" not in second and second["has_more"] is False
    assert "pools" not in refused and refused["pools_error"].startswith("PERMISSION")
    assert detail["items"][0]["active_build"] == {
        "type": "build",
        "build_id": _BUILD,
        "status": "FAILED",
        "trigger": "CONFIG_CHANGE",
        "draft": False,
        "failure": "INSTALL_FAILED: npm_ci",
    }
    assert len([call for call in router.calls if call[1] == "/v0/private-workers/pools"]) == 2


async def test_account_reports_the_server_settings_without_secret_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FWD_TOKEN", "forwarded-secret-value")

    def responder(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"apiKeyName": "ci", "createdAt": "2026-10-01T00:00:00.000Z"})

    client, _router = await _session(
        Router(responder),
        allow_writes=False,
        forward_env=frozenset({"FWD_TOKEN"}),
        default_model=("m-1", (("fast", "false"),)),
        state_dir="/tmp/journal-dir",
    )
    try:
        account = _data(await client.call_tool("cursor_get_account", {}))
    finally:
        await client.__aexit__(None, None, None)
    from cursor_cloud_mcp import __version__

    assert account["service_account_key"] is True
    assert account["server"] == {
        "version": __version__,
        "writes_enabled": False,
        "delete_enabled": False,
        "fixture": False,
        "default_model": "m-1:fast=false",
        "journal_dir": "/tmp/journal-dir",
        "journal": "on",
        "forward_env": ["FWD_TOKEN"],
    }
    text = json.dumps(account)
    assert "forwarded-secret-value" not in text and "test-secret-key" not in text


async def test_conversation_windows_clips_and_flags_injected_turns() -> None:
    messages = [{"id": f"m{index}", "type": "assistant_message", "text": f"reply {index}"} for index in range(30)]
    messages[3] = {"id": "m3", "type": "user_message", "text": "<system_notification>\nThe following task has finished."}
    messages[29] = {"id": "m29", "type": "assistant_message", "text": "A" * 500 + "B" * 500}

    def responder(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/v0/agents/{_AGENT}/conversation"
        return httpx.Response(200, json={"id": _AGENT, "messages": messages})

    client, _router = await _session(Router(responder))
    try:
        last = _data(await client.call_tool("cursor_read_conversation", {"agent_id": _AGENT, "text_limit": 100}))
        early = _data(await client.call_tool("cursor_read_conversation", {"agent_id": _AGENT, "offset": 0, "limit": 5}))
        beyond = await client.call_tool("cursor_read_conversation", {"agent_id": _AGENT, "offset": 31})
    finally:
        await client.__aexit__(None, None, None)
    assert last["total_messages"] == 30 and last["offset"] == 10
    assert [item["index"] for item in last["messages"]] == list(range(10, 30))
    assert "next_offset" not in last and last["injected_turns"] == 1
    tail = last["messages"][-1]
    assert tail["clipped"] is True and tail["text"].startswith("A") and tail["text"].endswith("B")
    assert len(tail["text"]) <= 100
    assert early["next_offset"] == 5
    assert early["messages"][3] == {
        "index": 3,
        "type": "user_message",
        "text": "<system_notification>\nThe following task has finished.",
        "injected": True,
    }
    assert _error_payload(beyond)["code"] == "VALIDATION"


async def test_malformed_image_urls_are_json_validation_errors() -> None:
    client, router = await _session(Router(_posting([])))
    try:
        results = [
            await client.call_tool("cursor_create_agent", {"prompt": "x", "images": [{"url": url}], "agent_id": _AGENT})
            for url in ("http://[::1/x.png", "https://example.com:99999/x.png")
        ]
    finally:
        await client.__aexit__(None, None, None)
    for result in results:
        assert _error_payload(result)["code"] == "VALIDATION"
    assert router.calls == []


async def test_non_ascii_rate_limit_digits_are_dropped_not_fatal() -> None:
    def responder(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers=[(b"X-RateLimit-Remaining", "²".encode("latin-1")), (b"X-RateLimit-Limit", b"60")],
            json={"error": {"code": "rate_limit_exceeded", "message": "slow down"}},
        )

    client, _router = await _session(Router(responder))
    try:
        payload = _error_payload(await client.call_tool("cursor_get_account", {}))
    finally:
        await client.__aexit__(None, None, None)
    assert payload["code"] == "QUOTA"
    assert payload["rate_limit_limit"] == 60 and "rate_limit_remaining" not in payload


async def test_only_a_user_turn_opening_with_the_tag_is_injected(monkeypatch: pytest.MonkeyPatch) -> None:
    from cursor_cloud_mcp import present

    monkeypatch.setattr(present, "CONVERSATION_TEXT_TOTAL_MAX_CHARS", 250)
    messages = [
        {"type": "assistant_message", "text": "I saw a <system_notification> tag earlier."},
        {"type": "user_message", "text": "Please explain <system_notification>."},
        {"type": "user_message", "text": "  <system_notification>\nThe following task has finished."},
    ] + [{"type": "assistant_message", "text": "x" * 100} for _ in range(5)]

    def responder(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": _AGENT, "messages": messages})

    client, _router = await _session(Router(responder))
    try:
        first = _data(await client.call_tool("cursor_read_conversation", {"agent_id": _AGENT, "offset": 0}))
    finally:
        await client.__aexit__(None, None, None)
    assert [item.get("injected") for item in first["messages"][:3]] == [None, None, True]
    assert first["injected_turns"] == 1
    # The texts reach the total after a few messages: the window stops there and says so.
    assert first["truncated"] is True and first["next_offset"] == len(first["messages"]) < len(messages)


async def test_a_conversation_above_the_general_body_cap_is_read() -> None:
    big = [{"type": "assistant_message", "text": "y" * 1_000_000} for _ in range(9)]

    def responder(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": _AGENT, "messages": big})

    client, _router = await _session(Router(responder))
    try:
        view = _data(await client.call_tool("cursor_read_conversation", {"agent_id": _AGENT, "limit": 1}))
    finally:
        await client.__aexit__(None, None, None)
    assert view["total_messages"] == 9 and view["messages"][0]["clipped"] is True


async def test_an_incomplete_environment_row_does_not_hide_the_page() -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/environments":
            return httpx.Response(200, json={"items": [{"id": _ENV}, _environment("Other")]})
        return httpx.Response(200, json={"pools": []})

    client, _router = await _session(Router(responder))
    try:
        view = _data(await client.call_tool("cursor_list_environments", {}))
    finally:
        await client.__aexit__(None, None, None)
    assert view["items"][0] == {"environment_id": _ENV}
    assert view["items"][1]["name"] == "Other" and view["pools"] == []
