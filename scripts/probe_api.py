"""Real probes of Cursor API behaviors the spec and our observations disagree on. One JSON line per finding.

Free GETs always: the includeArchived default, the legacy v0 conversation on a v1 agent, private
worker pools and environments with this key. With `SMOKE_PAID=1` and `PROBE_MODEL=id[:param=value,...]`
(every cost option explicit), three tiny agents, each deleted at the end: a commit SHA as
`startingRef`, two repositories on an unnamed cloud environment, and whether `envVars` reaches the VM.

Raw HTTP on purpose: some endpoints are not in this server's client yet. The key comes from the
environment or `.env`; it is never printed, nor are prompt texts, conversation texts or the
probe variable's value (only its length).
"""

import asyncio
import collections
import json
import os
import secrets
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
from smoke_live import load_key

from cursor_cloud_mcp.config import API_BASE, parse_default_model

REPO = os.environ.get("SMOKE_REPO", "https://github.com/yoch/cursor-cloud-mcp")
SECOND_REPO = os.environ.get("PROBE_SECOND_REPO", "https://github.com/yoch/pykuwahara")
PROBE_PROMPT = "Probe. Do not read or print any credential. Reply only with the word OK, then end your turn."


def emit(**row: object) -> None:
    print(json.dumps(row), flush=True)


def error_code(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        return str(error.get("code"))
    return str(body.get("code")) if isinstance(body, dict) and body.get("code") else None


async def free_probes(http: httpx.AsyncClient) -> None:
    pages: dict[str, list[dict[str, object]]] = {}
    for label, params in (("default", {}), ("false", {"includeArchived": "false"}), ("true", {"includeArchived": "true"})):
        response = await http.get("/v1/agents", params={"limit": "100", **params})
        items = response.json().get("items", []) if response.status_code == 200 else []
        pages[label] = items
        archived = sum(1 for item in items if item.get("status") == "ARCHIVED")
        emit(probe="include_archived", param=label, http=response.status_code, agents=len(items), archived=archived)
    candidates = [item for item in pages["default"] if item.get("status") != "ARCHIVED" and item.get("latestRunId")]
    if candidates:
        agent_id = str(candidates[0]["id"])
        response = await http.get(f"/v0/agents/{agent_id}/conversation", timeout=120)
        row: dict[str, object] = {"probe": "v0_conversation", "http": response.status_code, "code": error_code(response)}
        if response.status_code == 200:
            messages = response.json().get("messages", [])
            row["messages"] = len(messages)
            row["types"] = dict(collections.Counter(str(item.get("type")) for item in messages))
            row["fields"] = sorted({key for item in messages for key in item})
            row["injected"] = sum(1 for item in messages if "<system_notification>" in str(item.get("text", "")))
        emit(**row)
    else:
        emit(probe="v0_conversation", skipped="no non-archived agent with a run")
    for path in ("/v0/private-workers/pools", "/v0/private-workers/summary", "/v1/environments"):
        response = await http.get(path)
        row = {"probe": path, "http": response.status_code, "code": error_code(response)}
        if response.status_code == 200:
            body = response.json()
            row["keys"] = sorted(body) if isinstance(body, dict) else type(body).__name__
            for field in ("pools", "items", "workers"):
                if isinstance(body.get(field), list):
                    row[f"{field}_count"] = len(body[field])
                    if body[field]:
                        row[f"{field}_item_keys"] = sorted(body[field][0])
        emit(**row)


def head_sha(repo: str) -> str:
    listed = subprocess.run(["git", "ls-remote", repo, "HEAD"], capture_output=True, text=True, check=True, timeout=60)
    return listed.stdout.split()[0]


def default_branch(repo: str) -> str:
    listed = subprocess.run(
        ["git", "ls-remote", "--symref", repo, "HEAD"], capture_output=True, text=True, check=True, timeout=60
    )
    first = listed.stdout.splitlines()[0]
    return first.split()[1].removeprefix("refs/heads/")


async def wait_result(http: httpx.AsyncClient, agent_id: str, run_id: str, seconds: float = 360) -> dict[str, object]:
    deadline = time.monotonic() + seconds
    run: dict[str, object] = {}
    while time.monotonic() < deadline:
        response = await http.get(f"/v1/agents/{agent_id}/runs/{run_id}")
        if response.status_code == 200:
            run = response.json()
            if run.get("status") in {"FINISHED", "ERROR", "CANCELLED", "EXPIRED"}:
                return run
        await asyncio.sleep(10)
    return run


async def paid_probes(http: httpx.AsyncClient, model: dict[str, object]) -> None:
    # Every agent is known before its POST: by agentId, or by a unique name when envVars forbids
    # agentId. A creation cut off after reaching Cursor is still found and deleted.
    created: list[str] = []
    named: list[str] = []
    try:
        agent_id = f"bc-{uuid.uuid4()}"
        created.append(agent_id)
        response = await http.post(
            "/v1/agents",
            json={
                "prompt": {"text": PROBE_PROMPT},
                "model": model,
                "agentId": agent_id,
                "repos": [{"url": REPO, "startingRef": head_sha(REPO)}],
            },
            timeout=120,
        )
        emit(probe="sha_starting_ref", http=response.status_code, code=error_code(response))

        repos = [
            {"url": REPO, "startingRef": default_branch(REPO)},
            {"url": SECOND_REPO, "startingRef": default_branch(SECOND_REPO)},
        ]
        agent_id = f"bc-{uuid.uuid4()}"
        created.append(agent_id)
        response = await http.post(
            "/v1/agents",
            json={"prompt": {"text": PROBE_PROMPT}, "model": model, "agentId": agent_id, "repos": repos},
            timeout=120,
        )
        emit(probe="multi_repo_unnamed_cloud", http=response.status_code, code=error_code(response))

        value = secrets.token_hex(12)
        name = f"probe-env-{uuid.uuid4().hex[:12]}"
        named.append(name)
        prompt = (
            "Probe. Do not print the variable itself. Run exactly: printf 'len=%s' \"${#PROBE_VAR}\" "
            "and reply only with that output, then end your turn."
        )
        response = await http.post(
            "/v1/agents",
            json={"prompt": {"text": prompt}, "model": model, "name": name, "envVars": {"PROBE_VAR": value}},
            timeout=120,
        )
        row: dict[str, object] = {"probe": "env_vars", "http": response.status_code, "code": error_code(response)}
        if response.status_code == 201:
            payload = response.json()
            run = await wait_result(http, payload["agent"]["id"], payload["run"]["id"])
            result = str(run.get("result") or "")
            row.update(status=run.get("status"), expected=f"len={len(value)}", reported=result.strip()[:20])
        emit(**row)
    finally:
        await cleanup(http, created, named)


async def cleanup(http: httpx.AsyncClient, agent_ids: list[str], names: list[str]) -> None:
    """Delete each agent on its own: one failure never leaves the others running."""
    targets = list(agent_ids)
    if names:
        try:
            listed = await http.get("/v1/agents", params={"limit": "100"})
            targets += [str(item["id"]) for item in listed.json().get("items", []) if item.get("name") in names]
        except (httpx.HTTPError, ValueError) as exc:
            emit(probe="cleanup", error=f"name lookup failed: {type(exc).__name__}", names=names)
    for agent_id in targets:
        try:
            response = await http.delete(f"/v1/agents/{agent_id}")
            # 404: the creation never went through, nothing is left.
            emit(probe="cleanup", agent_id=agent_id, http=response.status_code)
        except httpx.HTTPError as exc:
            emit(probe="cleanup", agent_id=agent_id, error=type(exc).__name__)


async def main() -> int:
    root = Path(__file__).resolve().parents[1]
    load_key(root)
    key = os.environ.get("CURSOR_API_KEY")
    if not key:
        print("CURSOR_API_KEY missing")
        return 2
    model: dict[str, object] | None = None
    if os.environ.get("SMOKE_PAID") == "1":
        parsed, _error = parse_default_model(os.environ.get("PROBE_MODEL"))
        if parsed is None:
            print("PROBE_MODEL must be id[:param=value,...], with every cost option explicit.")
            return 2
        model = {"id": parsed[0]}
        if parsed[1]:
            model["params"] = [{"id": name, "value": value} for name, value in parsed[1]]
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    async with httpx.AsyncClient(base_url=API_BASE, headers=headers, timeout=60) as http:
        await free_probes(http)
        if model is not None:
            await paid_probes(http, model)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
