"""Transport synthétique. Il ne remplace pas l'API et ne s'active que si CURSOR_MCP_FIXTURE=1."""

import json
import uuid
from typing import Any

import httpx

SEEDED_AGENT_ID = "bc-00000000-0000-0000-0000-000000000001"
BUSY_AGENT_ID = "bc-00000000-0000-0000-0000-000000000099"
INCOMPATIBLE_AGENT_ID = "bc-00000000-0000-0000-0000-000000000088"
SEEDED_RUN_ID = "run-00000000-0000-0000-0000-000000000001"
ERROR_PROMPT = "ERREUR_ATTENDUE"
_NOW = "2026-09-30T12:00:00.000Z"
_SHA = "a" * 40
_ARTIFACT_HOST = "cloud-agent-artifacts.s3.us-east-1.amazonaws.com"


class FixtureTransport(httpx.AsyncBaseTransport):
    """Réponses locales conformes au contrat, sans socket."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.agents: dict[str, dict[str, Any]] = {
            SEEDED_AGENT_ID: _agent(SEEDED_AGENT_ID, "IDLE", False, SEEDED_RUN_ID),
            BUSY_AGENT_ID: _agent(BUSY_AGENT_ID, "ACTIVE", False, "run-busy"),
            INCOMPATIBLE_AGENT_ID: _agent(INCOMPATIBLE_AGENT_ID, "IDLE", True, "run-current-branch"),
        }
        self.runs: dict[tuple[str, str], dict[str, Any]] = {
            (SEEDED_AGENT_ID, SEEDED_RUN_ID): _run(
                SEEDED_RUN_ID,
                SEEDED_AGENT_ID,
                "FINISHED",
                "Résultat fictif. " + ("abcde" * 3000),
            ),
            (BUSY_AGENT_ID, "run-busy"): _run("run-busy", BUSY_AGENT_ID, "RUNNING", None),
            (INCOMPATIBLE_AGENT_ID, "run-current-branch"): _run(
                "run-current-branch",
                INCOMPATIBLE_AGENT_ID,
                "FINISHED",
                "Déjà terminé.",
            ),
        }

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        if request.url.host != "api.cursor.com":
            return self._download(request)
        if request.method == "GET" and path == "/v1/me":
            return _json(200, {"apiKeyName": "fixture", "createdAt": _NOW})
        if request.method == "GET" and path == "/v1/models":
            return _json(200, {"items": [_models()]})
        if request.method == "GET" and path == "/v1/repositories":
            return _json(200, {"items": [{"url": "https://github.com/example/demo"}]})
        if request.method == "GET" and path == "/v1/agents":
            return self._list_agents(request)
        if request.method == "POST" and path == "/v1/agents":
            return self._create_agent(request)
        parts = [part for part in path.split("/") if part]
        if len(parts) >= 3 and parts[0] == "v1" and parts[1] == "agents":
            return self._agent_route(request, parts)
        return _error(404, "agent_not_found", "Chemin inconnu du fixture.")

    def _agent_route(self, request: httpx.Request, parts: list[str]) -> httpx.Response:
        agent_id = parts[2]
        if request.method == "DELETE" and len(parts) == 3:
            return self._delete(agent_id)
        if request.method == "GET" and len(parts) == 3:
            agent = self.agents.get(agent_id)
            if agent is None:
                return _error(404, "agent_not_found", "Agent inconnu.")
            return _json(200, agent)
        if len(parts) == 4 and parts[3] == "archive" and request.method == "POST":
            return self._set_status(agent_id, "ARCHIVED")
        if len(parts) == 4 and parts[3] == "unarchive" and request.method == "POST":
            return self._set_status(agent_id, "IDLE")
        if len(parts) == 4 and parts[3] == "artifacts" and request.method == "GET":
            return self._artifacts(agent_id)
        if len(parts) == 5 and parts[3] == "artifacts" and parts[4] == "download" and request.method == "GET":
            return self._artifact_url(agent_id, request.url.params.get("path"))
        if len(parts) == 4 and parts[3] == "runs" and request.method == "GET":
            return self._list_runs(request, agent_id)
        if len(parts) == 4 and parts[3] == "runs" and request.method == "POST":
            return self._create_run(request, agent_id)
        if len(parts) == 4 and parts[3] == "usage" and request.method == "GET":
            return self._usage(request, agent_id)
        if len(parts) == 5 and parts[3] == "runs" and request.method == "GET":
            run = self.runs.get((agent_id, parts[4]))
            if run is None:
                return _error(404, "run_not_found", "Run inconnu.")
            return _json(200, run)
        if len(parts) == 6 and parts[3] == "runs" and parts[5] == "cancel" and request.method == "POST":
            return self._cancel(agent_id, parts[4])
        if len(parts) == 6 and parts[3] == "runs" and parts[5] == "stream" and request.method == "GET":
            return self._stream(request, agent_id, parts[4])
        return _error(404, "agent_not_found", "Chemin inconnu du fixture.")

    def _list_agents(self, request: httpx.Request) -> httpx.Response:
        limit, cursor, error = _page_args(request)
        if error is not None:
            return error
        include_archived = request.url.params.get("includeArchived", "true") != "false"
        ordered = sorted(self.agents.values(), key=lambda item: str(item["createdAt"]), reverse=True)
        if not include_archived:
            ordered = [item for item in ordered if item.get("status") != "ARCHIVED"]
        start = cursor or 0
        window = ordered[start : start + limit]
        payload: dict[str, Any] = {"items": [_summary(item) for item in window]}
        if start + limit < len(ordered):
            payload["nextCursor"] = str(start + limit)
        return _json(200, payload)

    def _list_runs(self, request: httpx.Request, agent_id: str) -> httpx.Response:
        if agent_id not in self.agents:
            return _error(404, "agent_not_found", "Agent inconnu.")
        limit, cursor, error = _page_args(request)
        if error is not None:
            return error
        ordered = [run for (owner, _run_id), run in self.runs.items() if owner == agent_id]
        ordered.sort(key=lambda item: str(item["createdAt"]), reverse=True)
        start = cursor or 0
        window = ordered[start : start + limit]
        payload: dict[str, Any] = {"items": window}
        if start + limit < len(ordered):
            payload["nextCursor"] = str(start + limit)
        return _json(200, payload)

    def _create_agent(self, request: httpx.Request) -> httpx.Response:
        body = _body(request)
        prompt = str(body.get("prompt", {}).get("text", "")) if isinstance(body.get("prompt"), dict) else ""
        if prompt == ERROR_PROMPT:
            return _error(400, "validation_error", "Prompt refusé par le fixture.")
        agent_id = body.get("agentId")
        if not isinstance(agent_id, str):
            if not body.get("envVars"):
                return _error(400, "validation_error", "agentId absent.")
            agent_id = f"bc-{uuid.uuid4()}"
        if agent_id in self.agents:
            return _error(409, "agent_id_conflict", "Identifiant déjà utilisé.")
        run_id = f"run-{uuid.uuid4()}"
        repos = body.get("repos") if isinstance(body.get("repos"), list) else []
        env = body.get("env") if isinstance(body.get("env"), dict) else {"type": "cloud"}
        agent = {
            "id": agent_id,
            "name": body.get("name") or "Agent fictif",
            "status": "ACTIVE",
            "env": env,
            "repos": repos,
            "workOnCurrentBranch": body.get("workOnCurrentBranch", False),
            "autoCreatePR": body.get("autoCreatePR", False),
            "url": f"https://cursor.com/agents/{agent_id}",
            "createdAt": _NOW,
            "updatedAt": _NOW,
            "latestRunId": run_id,
        }
        self.agents[agent_id] = agent
        self.runs[(agent_id, run_id)] = _run(run_id, agent_id, "CREATING", None)
        return _json(201, {"agent": agent, "run": self.runs[(agent_id, run_id)]})

    def _create_run(self, request: httpx.Request, agent_id: str) -> httpx.Response:
        agent = self.agents.get(agent_id)
        if agent is None:
            return _error(404, "agent_not_found", "Agent inconnu.")
        if agent.get("status") == "ARCHIVED":
            return _error(409, "agent_archived", "Agent archivé.")
        if agent_id == BUSY_AGENT_ID:
            return _error(409, "agent_busy", "Un run est déjà actif.")
        body = _body(request)
        prompt = ""
        prompt_body = body.get("prompt")
        if isinstance(prompt_body, dict):
            prompt = str(prompt_body.get("text", ""))
        if prompt == ERROR_PROMPT:
            return _error(400, "validation_error", "Prompt refusé par le fixture.")
        run_id = f"run-{uuid.uuid4()}"
        self.runs[(agent_id, run_id)] = _run(run_id, agent_id, "FINISHED", "Continuation fictive appliquée.")
        agent["latestRunId"] = run_id
        agent["status"] = "IDLE"
        return _json(201, {"run": self.runs[(agent_id, run_id)]})

    def _cancel(self, agent_id: str, run_id: str) -> httpx.Response:
        run = self.runs.get((agent_id, run_id))
        if run is None:
            return _error(404, "run_not_found", "Run inconnu.")
        if run["status"] in {"FINISHED", "ERROR", "CANCELLED", "EXPIRED"}:
            return _error(409, "run_not_cancellable", "Le run est déjà terminal.")
        run["status"] = "CANCELLED"
        run["result"] = "Annulé par le fixture."
        return _json(200, {"id": run_id})

    def _set_status(self, agent_id: str, status: str) -> httpx.Response:
        agent = self.agents.get(agent_id)
        if agent is None:
            return _error(404, "agent_not_found", "Agent inconnu.")
        agent["status"] = status
        return _json(200, {"id": agent_id})

    def _delete(self, agent_id: str) -> httpx.Response:
        if agent_id not in self.agents:
            return _error(404, "agent_not_found", "Agent inconnu.")
        del self.agents[agent_id]
        for key in [key for key in self.runs if key[0] == agent_id]:
            del self.runs[key]
        return _json(200, {"id": agent_id})

    def _artifacts(self, agent_id: str) -> httpx.Response:
        if agent_id not in self.agents:
            return _error(404, "agent_not_found", "Agent inconnu.")
        return _json(
            200,
            {
                "items": [
                    {"path": "artifacts/result.txt", "sizeBytes": 17, "updatedAt": _NOW},
                ]
            },
        )

    def _download(self, request: httpx.Request) -> httpx.Response:
        """Stockage simulé : le mode fixture n'ouvre jamais de connexion externe."""
        if request.method == "GET" and request.url.host == _ARTIFACT_HOST and request.url.path == "/fixture/result.txt":
            return httpx.Response(200, content=b"fixture artefact\n")
        return httpx.Response(404, text="Hôte ou chemin inconnu du fixture.")

    def _artifact_url(self, agent_id: str, path: str | None) -> httpx.Response:
        if agent_id not in self.agents:
            return _error(404, "agent_not_found", "Agent inconnu.")
        if path != "artifacts/result.txt":
            return _error(404, "artifact_not_found", "Artefact inconnu.")
        return _json(
            200,
            {
                "url": f"https://{_ARTIFACT_HOST}/fixture/result.txt",
                "expiresAt": "2026-09-30T12:15:00.000Z",
            },
        )

    def _stream(self, request: httpx.Request, agent_id: str, run_id: str) -> httpx.Response:
        if (agent_id, run_id) not in self.runs:
            return _error(404, "run_not_found", "Run inconnu.")
        last = request.headers.get("last-event-id")
        if last == "expired":
            return _error(410, "stream_expired", "Flux expiré.")
        lines = [
            f'id: 1\nevent: status\ndata: {{"runId":"{run_id}","status":"RUNNING"}}\n\n',
            'id: 2\nevent: assistant\ndata: {"text":"calcul fictif"}\n\n',
            f'id: 3\nevent: result\ndata: {{"runId":"{run_id}","status":"FINISHED","text":"terminé"}}\n\n',
            "id: 4\nevent: done\ndata: {}\n\n",
        ]
        if last in {"1", "2", "3", "4"}:
            lines = lines[int(last) :]
        body = "".join(lines)
        return httpx.Response(
            200,
            content=body.encode(),
            headers={
                "content-type": "text/event-stream",
                "x-cursor-stream-retention-seconds": "3600",
            },
        )

    def _usage(self, request: httpx.Request, agent_id: str) -> httpx.Response:
        if agent_id not in self.agents:
            return _error(404, "agent_not_found", "Agent inconnu.")
        selected = request.url.params.get("runId")
        rows = [run for (owner, _run_id), run in self.runs.items() if owner == agent_id]
        if selected is not None:
            rows = [run for run in rows if run["id"] == selected]
            if not rows:
                return _error(404, "run_not_found", "Run inconnu.")
        usage = {
            "inputTokens": 3,
            "outputTokens": 2,
            "cacheWriteTokens": 0,
            "cacheReadTokens": 0,
            "totalTokens": 5,
        }
        cost = {"rawCostCents": 0.5, "chargedCents": 0.5}
        total = len(rows) * 0.5
        return _json(
            200,
            {
                "totalUsage": usage,
                "cost": {"rawCostCents": total, "chargedCents": total},
                "runs": [{"id": row["id"], "usage": usage, "cost": cost} for row in rows],
            },
        )


def _models() -> dict[str, Any]:
    return {
        "id": "composer-2",
        "displayName": "Composer 2",
        "parameters": [
            {
                "id": "fast",
                "displayName": "Fast",
                "values": [{"value": "true", "displayName": "Fast"}, {"value": "false"}],
            },
            {
                "id": "effort",
                "displayName": "Effort",
                "values": [{"value": "low"}, {"value": "high"}],
            },
            {
                "id": "thinking",
                "displayName": "Thinking",
                "values": [{"value": "true"}, {"value": "false"}],
            },
        ],
    }


def _agent(agent_id: str, status: str, work_on_current_branch: bool, latest_run_id: str) -> dict[str, Any]:
    return {
        "id": agent_id,
        "name": "Agent fictif",
        "status": status,
        "env": {"type": "cloud"},
        "repos": [{"url": "https://github.com/example/demo", "startingRef": _SHA}],
        "workOnCurrentBranch": work_on_current_branch,
        "autoCreatePR": False,
        "url": f"https://cursor.com/agents/{agent_id}",
        "createdAt": _NOW,
        "updatedAt": _NOW,
        "latestRunId": latest_run_id,
    }


def _summary(agent: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": agent["id"],
        "name": agent.get("name"),
        "status": agent["status"],
        "env": agent["env"],
        "url": agent["url"],
        "createdAt": agent["createdAt"],
        "updatedAt": agent["updatedAt"],
        "latestRunId": agent.get("latestRunId"),
    }


def _run(run_id: str, agent_id: str, status: str, result: str | None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": run_id,
        "agentId": agent_id,
        "status": status,
        "createdAt": _NOW,
        "updatedAt": _NOW,
    }
    if status in {"FINISHED", "ERROR", "CANCELLED", "EXPIRED"}:
        payload["durationMs"] = 15
    if result is not None:
        payload["result"] = result
        payload["git"] = {
            "branches": [{"repoUrl": "github.com/example/demo", "branch": "cursor/fixture-demo"}]
        }
    return payload


def _page_args(request: httpx.Request) -> tuple[int, int | None, httpx.Response | None]:
    raw_limit = request.url.params.get("limit")
    limit = 20
    if raw_limit is not None:
        try:
            limit = int(raw_limit)
        except ValueError:
            return 0, None, _error(400, "validation_error", "limit invalide.")
    raw_cursor = request.url.params.get("cursor")
    cursor = None
    if raw_cursor is not None:
        try:
            cursor = int(raw_cursor)
        except ValueError:
            return 0, None, _error(400, "validation_error", "cursor invalide.")
    return limit, cursor, None


def _body(request: httpx.Request) -> dict[str, Any]:
    if not request.content:
        return {}
    payload = json.loads(request.content.decode())
    if not isinstance(payload, dict):
        return {}
    return payload


def _json(status: int, payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(status, json=payload)


def _error(status: int, code: str, message: str) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": message}})
