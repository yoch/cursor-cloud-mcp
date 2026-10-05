"""Client HTTP sans aucun appel réseau réel."""

import asyncio

import httpx
import pytest

from cursor_cloud_mcp.client import CursorCloudClient
from cursor_cloud_mcp.config import load_settings
from cursor_cloud_mcp.errors import CursorFailure, ErrorCode
from cursor_cloud_mcp.slicing import slice_text

pytestmark = pytest.mark.anyio

_ACCOUNT = {"apiKeyName": "demo", "createdAt": "2026-09-30T00:00:00Z"}
_AGENT_ID = "bc-11111111-1111-1111-1111-111111111111"


async def _open(transport: httpx.AsyncBaseTransport, **kwargs: object) -> CursorCloudClient:
    client = CursorCloudClient(api_key="test-secret-key", transport=transport, **kwargs)
    await client.open()
    return client


def test_slice_covers_the_text_without_gap_or_overlap() -> None:
    text = "abcdefghijklmnopqrstuvwxyz"
    offset = 0
    pieces: list[str] = []
    while True:
        chunk, truncated, nxt = slice_text(text, offset, 4)
        pieces.append(chunk)
        if not truncated:
            assert nxt is None
            break
        assert nxt == offset + len(chunk)
        offset = nxt
    assert "".join(pieces) == text
    assert pieces == ["abcd", "efgh", "ijkl", "mnop", "qrst", "uvwx", "yz"]


def test_settings_reject_unexpanded_keys(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("CURSOR_API_KEY=from-dotenv-file\n", encoding="utf-8")
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    assert load_settings().api_key is None
    monkeypatch.setenv("CURSOR_API_KEY", "")
    assert load_settings().config_error is not None
    monkeypatch.setenv("CURSOR_API_KEY", "${CURSOR_API_KEY}")
    assert "interpol" in (load_settings().config_error or "")
    monkeypatch.setenv("CURSOR_API_KEY", "{env:CURSOR_API_KEY}")
    assert load_settings().config_error is not None
    monkeypatch.setenv("CURSOR_MCP_ALLOW_WRITES", "true")
    assert load_settings().allow_writes is False
    monkeypatch.setenv("CURSOR_MCP_ALLOW_WRITES", "1")
    assert load_settings().allow_writes is True


async def test_get_retries_429_once_inside_the_deadline() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                429,
                headers={"retry-after": "0", "x-request-id": "req-429"},
                json={"error": {"code": "rate_limit_exceeded", "message": "slow"}},
            )
        return httpx.Response(200, json=_ACCOUNT)

    client = await _open(httpx.MockTransport(handler), deadline_seconds=2)
    try:
        account = await client.get_account()
    finally:
        await client.aclose()
    assert calls["n"] == 2
    assert account.apiKeyName == "demo"


async def test_retry_after_beyond_deadline_is_not_waited() -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            429,
            headers={"retry-after": "30"},
            json={"error": {"code": "rate_limit_exceeded", "message": "later"}},
        )

    client = await _open(httpx.MockTransport(handler), deadline_seconds=1)
    try:
        with pytest.raises(CursorFailure) as caught:
            await client.get_account()
    finally:
        await client.aclose()
    error = caught.value
    assert calls["n"] == 1
    assert error.body.code is ErrorCode.QUOTA
    assert error.body.retry_after_seconds == 30.0
    assert error.body.safe_to_retry_automatically is False


async def test_get_retries_5xx_once_then_reports_upstream() -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(502, json={"error": {"code": "upstream_error", "message": "down"}})

    client = await _open(httpx.MockTransport(handler), deadline_seconds=2)
    try:
        with pytest.raises(CursorFailure) as caught:
            await client.get_account()
    finally:
        await client.aclose()
    assert calls["n"] == 2
    assert caught.value.body.code is ErrorCode.UPSTREAM
    assert caught.value.body.remote_code == "upstream_error"


async def test_post_is_not_retried_on_5xx_and_keeps_the_agent_id() -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, json={"error": {"code": "internal_error", "message": "test-secret-key"}})

    client = await _open(httpx.MockTransport(handler), deadline_seconds=2)
    try:
        with pytest.raises(CursorFailure) as caught:
            await client.create_agent({"prompt": {"text": "hi"}}, agent_id=_AGENT_ID)
    finally:
        await client.aclose()
    error = caught.value
    assert calls["n"] == 1
    assert error.body.code is ErrorCode.MUTATION_OUTCOME_UNKNOWN
    assert error.body.agent_id == _AGENT_ID
    assert error.body.safe_to_retry_automatically is False
    assert "test-secret-key" not in error.body.message
    assert "[redacted]" in error.body.message


async def test_post_transport_error_is_unknown_and_not_retried() -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("coupé")

    client = await _open(httpx.MockTransport(handler), deadline_seconds=2)
    try:
        with pytest.raises(CursorFailure) as caught:
            await client.create_run(_AGENT_ID, {"prompt": {"text": "suite"}}, previous_latest_run_id="run-old")
    finally:
        await client.aclose()
    error = caught.value
    assert calls["n"] == 1
    assert error.body.code is ErrorCode.MUTATION_OUTCOME_UNKNOWN
    assert error.body.previous_latest_run_id == "run-old"
    assert error.body.agent_id == _AGENT_ID


async def test_deadline_covers_a_stalled_body() -> None:
    class SlowBody(httpx.AsyncByteStream):
        async def __aiter__(self):  # type: ignore[override]
            yield b'{"apiKeyName":'
            await asyncio.sleep(2)
            yield b'"demo","createdAt":"2026-09-30T00:00:00Z"}'

    class Stall(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.calls = 0

        async def handle_async_request(self, _request: httpx.Request) -> httpx.Response:
            self.calls += 1
            return httpx.Response(200, stream=SlowBody(), headers={"content-type": "application/json"})

    stall = Stall()
    client = await _open(stall, deadline_seconds=0.3)
    try:
        with pytest.raises(CursorFailure) as caught:
            await client.get_account()
    finally:
        await client.aclose()
    assert stall.calls == 1
    assert caught.value.body.code is ErrorCode.TIMEOUT


async def test_html_and_missing_fields_are_incompatible() -> None:
    def html(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>no</html>", headers={"content-type": "text/html"})

    client = await _open(httpx.MockTransport(html))
    try:
        with pytest.raises(CursorFailure) as caught:
            await client.list_models()
    finally:
        await client.aclose()
    assert caught.value.body.code is ErrorCode.INCOMPATIBLE_RESPONSE

    def incomplete(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [{"id": "composer-2"}]})

    client = await _open(httpx.MockTransport(incomplete))
    try:
        with pytest.raises(CursorFailure) as caught:
            await client.list_models()
    finally:
        await client.aclose()
    assert caught.value.body.code is ErrorCode.INCOMPATIBLE_RESPONSE


async def test_auth_redirect_to_another_host_is_refused() -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(302, headers={"location": "https://evil.example/collect"})

    client = await _open(httpx.MockTransport(handler))
    try:
        with pytest.raises(CursorFailure) as caught:
            await client.get_account()
    finally:
        await client.aclose()
    assert calls["n"] == 1
    assert "autre domaine" in caught.value.body.message
    assert "evil.example" not in caught.value.body.message


async def test_repository_cache_shares_one_request() -> None:
    calls = {"n": 0}

    async def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={"items": [{"url": "https://github.com/acme/demo"}]})

    client = await _open(httpx.MockTransport(handler), repository_cache_ttl_seconds=300)
    try:
        first, second = await asyncio.gather(client.list_repositories(), client.list_repositories())
        third, hit = await client.list_repositories()
    finally:
        await client.aclose()
    assert calls["n"] == 1
    assert sorted([first[1], second[1]]) == [False, True]
    assert hit is True
    assert third.items[0].url == "https://github.com/acme/demo"


async def test_cancelling_one_cache_reader_does_not_cancel_the_other() -> None:
    calls = {"n": 0}

    async def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        await asyncio.sleep(0.2)
        return httpx.Response(200, json={"items": [{"url": "https://github.com/acme/demo"}]})

    client = await _open(httpx.MockTransport(handler), repository_cache_ttl_seconds=300)
    try:
        first = asyncio.create_task(client.list_repositories())
        await asyncio.sleep(0.05)
        second = asyncio.create_task(client.list_repositories())
        await asyncio.sleep(0.01)
        first.cancel()
        payload, _hit = await second
    finally:
        await client.aclose()
    assert first.cancelled()
    assert payload.items[0].url == "https://github.com/acme/demo"
    assert calls["n"] == 2


async def test_agent_busy_conflict_is_not_retried() -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(409, json={"error": {"code": "agent_busy", "message": "occupé"}})

    client = await _open(httpx.MockTransport(handler))
    try:
        with pytest.raises(CursorFailure) as caught:
            await client.create_run(_AGENT_ID, {"prompt": {"text": "suite"}}, previous_latest_run_id="run-9")
    finally:
        await client.aclose()
    assert calls["n"] == 1
    assert caught.value.body.code is ErrorCode.AGENT_BUSY
    assert caught.value.body.previous_latest_run_id == "run-9"


def test_import_does_not_open_a_connection() -> None:
    import subprocess
    import sys

    script = """
import socket
def blocked(*args, **kwargs):
    raise AssertionError("network")
socket.create_connection = blocked
import cursor_cloud_mcp.server
import cursor_cloud_mcp.client
print("ok")
"""
    completed = subprocess.run([sys.executable, "-c", script], check=True, capture_output=True, text=True)
    assert completed.stdout.strip() == "ok"
    assert "Authorization" not in completed.stderr
