"""List, URL and text reading of artifacts. The download does not send the Cursor key."""

import asyncio
import logging
from typing import Literal

import httpx

from cursor_cloud_mcp import budget
from cursor_cloud_mcp.client import CursorCloudClient, ResponseTooLarge, close_quietly, read_bounded
from cursor_cloud_mcp.config import ARTIFACT_MAX_BYTES, DEFAULT_DEADLINE_SECONDS
from cursor_cloud_mcp.errors import ErrorCode, failure
from cursor_cloud_mcp.models import (
    ArtifactItemView,
    ArtifactListView,
    ArtifactReadView,
    ArtifactUrlView,
)
from cursor_cloud_mcp.slicing import slice_text
from cursor_cloud_mcp.validation import require_artifact_path

logger = logging.getLogger(__name__)
_REDIRECTS = {301, 302, 303, 307, 308}


async def list_artifacts(client: CursorCloudClient, agent_id: str) -> ArtifactListView:
    remote = await client.list_artifacts(agent_id)
    return ArtifactListView(
        items=[
            ArtifactItemView(path=item.path, size_bytes=item.sizeBytes, updated_at=item.updatedAt)
            for item in remote.items
        ]
    )


async def artifact_url(client: CursorCloudClient, agent_id: str, path: str) -> ArtifactUrlView:
    checked = require_artifact_path(path)
    remote = await client.artifact_download(agent_id, checked)
    _require_presigned(remote.url)
    return ArtifactUrlView(path=checked, url=remote.url, expires_at=remote.expiresAt)


async def read_artifact(
    client: CursorCloudClient,
    agent_id: str,
    path: str,
    *,
    offset: int,
    limit: int,
    url_only: bool = False,
) -> ArtifactReadView:
    """UTF-8 text of at most 5 MB. Otherwise, or on request, the presigned URL to download elsewhere."""
    located = await artifact_url(client, agent_id, path)
    if url_only:
        return ArtifactReadView(path=located.path, expires_at=located.expires_at, url=located.url)
    try:
        raw = await fetch_presigned(
            located.url,
            transport=client.download_transport,
            max_bytes=ARTIFACT_MAX_BYTES,
            deadline=DEFAULT_DEADLINE_SECONDS,
        )
    except _TooLarge:
        return _unreadable(located, "too_large")
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        return _unreadable(located, "not_utf8")
    chunk, truncated, next_offset = slice_text(text, offset, limit)
    return ArtifactReadView(
        path=located.path,
        text=chunk,
        offset=offset,
        limit=limit,
        total_chars=len(text),
        truncated=truncated,
        next_offset=next_offset,
        expires_at=located.expires_at,
    )


class _TooLarge(Exception):
    """Artifact beyond the text-reading limit: the URL is returned instead."""


def _unreadable(located: ArtifactUrlView, reason: Literal["not_utf8", "too_large"]) -> ArtifactReadView:
    return ArtifactReadView(
        path=located.path,
        expires_at=located.expires_at,
        url=located.url,
        text_unavailable=reason,
    )


async def fetch_presigned(
    url: str,
    *,
    transport: httpx.AsyncBaseTransport | None,
    max_bytes: int,
    deadline: float,
) -> bytes:
    """Download bounded end to end: connection, reading and closing."""
    _require_presigned(url)
    deadline_at = budget.deadline_at(deadline)
    remaining = deadline_at - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise failure(ErrorCode.TIMEOUT, "Tool budget exhausted before the artifact download.")
    try:
        async with asyncio.timeout_at(deadline_at):
            async with httpx.AsyncClient(
                transport=transport,
                follow_redirects=False,
                timeout=httpx.Timeout(remaining),
            ) as http:
                request = http.build_request("GET", url)
                if "authorization" in {name.lower() for name in request.headers}:
                    raise failure(ErrorCode.VALIDATION, "The download must not carry the Cursor key.")
                response = await http.send(request, stream=True)
                try:
                    return await _read_download(response, max_bytes)
                finally:
                    await close_quietly(response)
    except (TimeoutError, httpx.TimeoutException):
        raise failure(ErrorCode.TIMEOUT, "Timed out during the artifact download.") from None
    except httpx.RequestError:
        raise failure(ErrorCode.TIMEOUT, "Artifact download interrupted.") from None


async def _read_download(response: httpx.Response, max_bytes: int) -> bytes:
    logger.info("artifact_download status=%s", response.status_code)
    if response.status_code in _REDIRECTS:
        raise failure(ErrorCode.INCOMPATIBLE_RESPONSE, "Download redirect refused.")
    if response.status_code != 200:
        raise failure(
            ErrorCode.UPSTREAM,
            f"Artifact download refused ({response.status_code}).",
        )
    try:
        return await read_bounded(response, max_bytes)
    except ResponseTooLarge:
        raise _TooLarge from None


def _require_presigned(url: str) -> None:
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL:
        raise failure(ErrorCode.VALIDATION, "Invalid artifact URL.") from None
    host = parsed.host
    if parsed.scheme != "https" or not host.endswith(".amazonaws.com"):
        raise failure(
            ErrorCode.VALIDATION,
            "The download only accepts an HTTPS URL whose host ends with .amazonaws.com.",
        )
    if parsed.username or parsed.password:
        raise failure(ErrorCode.VALIDATION, "The artifact URL must not contain credentials.")
