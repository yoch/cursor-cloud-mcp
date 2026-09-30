"""Lectures Cursor réelles, sans mutation.

Le serveur MCP ne charge pas `.env`. Ce script, lancé à part, peut le faire
pour exporter la clé dans son propre processus. Il n'affiche jamais la clé.
"""

import asyncio
import os
import sys
from pathlib import Path

from cursor_cloud_mcp.client import CursorCloudClient
from cursor_cloud_mcp.errors import CursorFailure


def load_key(root: Path) -> None:
    if os.environ.get("CURSOR_API_KEY"):
        return
    env_file = root / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if not line.startswith("CURSOR_API_KEY="):
            continue
        value = line.split("=", 1)[1].strip().strip('"').strip("'")
        if value:
            os.environ["CURSOR_API_KEY"] = value
        return


async def main() -> int:
    root = Path(__file__).resolve().parents[1]
    load_key(root)
    key = os.environ.get("CURSOR_API_KEY")
    if not key:
        print("CURSOR_API_KEY absente")
        return 2
    client = CursorCloudClient(api_key=key)
    await client.open()
    try:
        await _read("me", client.get_account, _account)
        await _read("models", client.list_models, _models)
        await _read("agents", lambda: client.list_agents(limit=5, cursor=None), _agents)
        await _read("repositories", client.list_repositories, _repositories)
    finally:
        await client.aclose()
    return 0


async def _read(name: str, call, present) -> None:
    try:
        payload = await call()
    except CursorFailure as exc:
        print(
            f"{name} FAIL code={exc.body.code.value} http={exc.body.http_status} remote={exc.body.remote_code}"
        )
        return
    print(f"{name} PASS {present(payload)}")


def _account(payload: object) -> str:
    return f"api_key_name={payload.apiKeyName!r} user_email_present={payload.userEmail is not None}"


def _models(payload: object) -> str:
    first = payload.items[0].id if payload.items else "-"
    return f"count={len(payload.items)} first_id={first}"


def _agents(payload: object) -> str:
    statuses = ",".join(item.status for item in payload.items)
    return f"count={len(payload.items)} has_next={payload.nextCursor is not None} statuses={statuses}"


def _repositories(payload: object) -> str:
    items, _hit = payload
    return f"count={len(items.items)}"


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
