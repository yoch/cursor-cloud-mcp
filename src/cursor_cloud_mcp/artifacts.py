"""Liste, URL et lecture texte des artefacts. Le téléchargement n'envoie pas la clé Cursor."""

import asyncio
import logging

import httpx

from cursor_cloud_mcp import budget
from cursor_cloud_mcp.client import CursorCloudClient, ResponseTooLarge, close_quietly, read_bounded
from cursor_cloud_mcp.config import ARTIFACT_MAX_BYTES, DEFAULT_DEADLINE_SECONDS
from cursor_cloud_mcp.errors import ErrorCode, failure
from cursor_cloud_mcp.models import (
    ArtifactItemView,
    ArtifactListView,
    ArtifactTextView,
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
) -> ArtifactTextView:
    located = await artifact_url(client, agent_id, path)
    raw = await fetch_presigned(
        located.url,
        transport=client.download_transport,
        max_bytes=ARTIFACT_MAX_BYTES,
        deadline=DEFAULT_DEADLINE_SECONDS,
    )
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        raise failure(
            ErrorCode.INCOMPATIBLE_RESPONSE,
            "L'artefact n'est pas du texte UTF-8. Utiliser cursor_get_artifact_url pour le récupérer autrement.",
        ) from None
    chunk, truncated, next_offset = slice_text(text, offset, limit)
    return ArtifactTextView(
        path=located.path,
        text=chunk,
        offset=offset,
        limit=limit,
        total_chars=len(text),
        truncated=truncated,
        next_offset=next_offset,
        expires_at=located.expires_at,
    )


async def fetch_presigned(
    url: str,
    *,
    transport: httpx.AsyncBaseTransport | None,
    max_bytes: int,
    deadline: float,
) -> bytes:
    """Téléchargement borné de bout en bout : connexion, lecture et fermeture."""
    _require_presigned(url)
    deadline_at = budget.deadline_at(deadline)
    remaining = deadline_at - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise failure(ErrorCode.TIMEOUT, "Budget de l'outil épuisé avant le téléchargement de l'artefact.")
    try:
        async with asyncio.timeout_at(deadline_at):
            async with httpx.AsyncClient(
                transport=transport,
                follow_redirects=False,
                timeout=httpx.Timeout(remaining),
            ) as http:
                request = http.build_request("GET", url)
                if "authorization" in {name.lower() for name in request.headers}:
                    raise failure(ErrorCode.VALIDATION, "Le téléchargement ne doit pas porter la clé Cursor.")
                response = await http.send(request, stream=True)
                try:
                    return await _read_download(response, max_bytes)
                finally:
                    await close_quietly(response)
    except (TimeoutError, httpx.TimeoutException):
        raise failure(ErrorCode.TIMEOUT, "Délai dépassé pendant le téléchargement de l'artefact.") from None
    except httpx.RequestError:
        raise failure(ErrorCode.TIMEOUT, "Téléchargement de l'artefact interrompu.") from None


async def _read_download(response: httpx.Response, max_bytes: int) -> bytes:
    logger.info("artifact_download status=%s", response.status_code)
    if response.status_code in _REDIRECTS:
        raise failure(ErrorCode.INCOMPATIBLE_RESPONSE, "Redirection de téléchargement refusée.")
    if response.status_code != 200:
        raise failure(
            ErrorCode.UPSTREAM,
            f"Téléchargement de l'artefact refusé ({response.status_code}).",
        )
    try:
        return await read_bounded(response, max_bytes)
    except ResponseTooLarge:
        raise failure(
            ErrorCode.INCOMPATIBLE_RESPONSE,
            "L'artefact dépasse 5 Mo. Utiliser cursor_get_artifact_url.",
        ) from None


def _require_presigned(url: str) -> None:
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL:
        raise failure(ErrorCode.VALIDATION, "URL d'artefact invalide.") from None
    host = parsed.host
    if parsed.scheme != "https" or not host.endswith(".amazonaws.com"):
        raise failure(
            ErrorCode.VALIDATION,
            "Le téléchargement n'accepte qu'une URL HTTPS dont l'hôte se termine par .amazonaws.com.",
        )
    if parsed.username or parsed.password:
        raise failure(ErrorCode.VALIDATION, "L'URL d'artefact ne doit pas contenir d'identifiants.")
