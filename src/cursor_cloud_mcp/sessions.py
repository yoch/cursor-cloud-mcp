"""Création, continuation et cycle de vie d'un agent Cloud."""

import os
import uuid
from typing import Literal, Never

from cursor_cloud_mcp import redaction
from cursor_cloud_mcp.catalog import resolve_model_selection
from cursor_cloud_mcp.client import CursorCloudClient
from cursor_cloud_mcp.config import ENV_MAX_COUNT, REPO_MAX_COUNT, Settings
from cursor_cloud_mcp.errors import CursorFailure, ErrorCode, failure
from cursor_cloud_mcp.models import (
    ArchiveView,
    CreateAgentView,
    CreateRunView,
    DeleteView,
    ModelParam,
    RepositoryInput,
    agent_status_known,
)
from cursor_cloud_mcp.present import (
    create_agent_view,
    create_run_view,
    ensure_continuation_allowed,
)
from cursor_cloud_mcp.validation import (
    normalize_repository,
    require_agent_id,
    require_env_name,
    require_env_value,
    require_mode,
    require_prompt,
    require_starting_ref,
)


async def perform_create(
    client: CursorCloudClient,
    settings: Settings,
    *,
    repository: str | None,
    starting_ref: str | None,
    repositories: list[RepositoryInput] | None,
    prompt: str,
    name: str | None,
    model_id: str | None,
    model_params: list[ModelParam] | None,
    reasoning_level: str | None,
    mode: str | None,
    auto_create_pr: bool,
    agent_id: str | None,
    env_type: str | None,
    env_name: str | None,
    env_vars: dict[str, str] | None,
    forward_env: list[str] | None,
) -> CreateAgentView:
    checked_prompt = require_prompt(prompt)
    checked_mode = require_mode(mode)
    if name is not None and name.strip() == "":
        raise failure(ErrorCode.VALIDATION, "name est vide.")
    repos = _repositories(repository, starting_ref, repositories)
    _check_environment(env_type, env_name, len(repos))
    merged = _environment_variables(settings, env_vars, forward_env)
    if merged is not None:
        # Valeurs fournies par appel : masquées dans les logs et les erreurs dès maintenant.
        redaction.register(*merged.values())
    chosen_id, lookup_name = _identity(agent_id, name, merged is not None)
    model_body = await _model_body(
        client,
        model_id=model_id,
        model_params=model_params,
        reasoning_level=reasoning_level,
    )
    body: dict[str, object] = {
        "prompt": {"text": checked_prompt},
        "workOnCurrentBranch": False,
        "autoCreatePR": auto_create_pr,
    }
    if repos:
        body["repos"] = repos
    if env_type is not None:
        env: dict[str, str] = {"type": env_type}
        if env_name is not None:
            env["name"] = env_name
        body["env"] = env
    if chosen_id is not None:
        body["agentId"] = chosen_id
    if name is not None:
        body["name"] = name
    if model_body is not None:
        body["model"] = model_body
    if checked_mode is not None:
        body["mode"] = checked_mode
    if merged is not None:
        body["envVars"] = merged
    remote = await client.create_agent(body, agent_id=chosen_id, lookup_name=lookup_name)
    return create_agent_view(remote)


async def perform_followup(
    client: CursorCloudClient,
    *,
    agent_id: str,
    prompt: str,
    mode: str | None,
) -> CreateRunView:
    checked_prompt = require_prompt(prompt)
    checked_mode = require_mode(mode)
    agent = await client.get_agent(agent_id)
    ensure_continuation_allowed(agent)
    body: dict[str, object] = {"prompt": {"text": checked_prompt}}
    if checked_mode is not None:
        body["mode"] = checked_mode
    remote = await client.create_run(agent.id, body, previous_latest_run_id=agent.latestRunId)
    return create_run_view(remote, previous_latest_run_id=agent.latestRunId, url=agent.url)


async def perform_archive(
    client: CursorCloudClient,
    agent_id: str,
    *,
    action: Literal["archive", "unarchive"],
) -> ArchiveView:
    if action == "archive":
        remote_id = await client.archive_agent(agent_id)
    elif action == "unarchive":
        remote_id = await client.unarchive_agent(agent_id)
    else:
        unexpected: Never = action
        raise AssertionError(unexpected)
    _same_id(remote_id.id, agent_id)
    try:
        agent = await client.get_agent(agent_id)
    except CursorFailure as exc:
        return ArchiveView(
            agent_id=agent_id,
            action=action,
            request_accepted=True,
            outcome_confirmed=False,
            reread_error=exc.body.message,
        )
    # Un statut inconnu ne confirme rien : seul un état connu et attendu compte.
    if action == "archive":
        confirmed = agent.status == "ARCHIVED"
    else:
        confirmed = agent_status_known(agent.status) and agent.status != "ARCHIVED"
    return ArchiveView(
        agent_id=agent_id,
        action=action,
        request_accepted=True,
        outcome_confirmed=confirmed,
        observed_status=agent.status,
    )


async def perform_delete(client: CursorCloudClient, agent_id: str) -> DeleteView:
    remote_id = await client.delete_agent(agent_id)
    _same_id(remote_id.id, agent_id)
    return DeleteView(agent_id=agent_id, deleted=True)


async def _model_body(
    client: CursorCloudClient,
    *,
    model_id: str | None,
    model_params: list[ModelParam] | None,
    reasoning_level: str | None,
) -> dict[str, object] | None:
    if model_id is None:
        if model_params or reasoning_level is not None:
            raise failure(
                ErrorCode.VALIDATION,
                "model_id est requis avec model_params ou reasoning_level. "
                "Aucun identifiant de modèle n'est inventé.",
            )
        return None
    if model_id.strip() == "" or len(model_id) > 128:
        raise failure(ErrorCode.VALIDATION, "model_id est vide ou trop long.")
    catalog, _hit = await client.cached_models()
    return resolve_model_selection(
        catalog,
        model_id=model_id,
        model_params=model_params,
        reasoning_level=reasoning_level,
    )


def _repositories(
    repository: str | None,
    starting_ref: str | None,
    repositories: list[RepositoryInput] | None,
) -> list[dict[str, str]]:
    single = repository is not None or starting_ref is not None
    if single and repositories:
        raise failure(
            ErrorCode.VALIDATION,
            "Fournir soit repository et starting_ref, soit repositories, pas les deux.",
        )
    if single:
        if repository is None or starting_ref is None:
            raise failure(ErrorCode.VALIDATION, "repository et starting_ref vont ensemble.")
        return [
            {
                "url": normalize_repository(repository),
                "startingRef": require_starting_ref(starting_ref),
            }
        ]
    if not repositories:
        return []
    if len(repositories) > REPO_MAX_COUNT:
        raise failure(ErrorCode.VALIDATION, "20 dépôts au plus.")
    return [
        {
            "url": normalize_repository(item.url),
            "startingRef": require_starting_ref(item.ref),
        }
        for item in repositories
    ]


def _check_environment(env_type: str | None, env_name: str | None, repo_count: int) -> None:
    if env_name is not None and env_type is None:
        raise failure(ErrorCode.VALIDATION, "env_name exige env_type.")
    if env_name is not None and (env_name.strip() == "" or len(env_name) > 128):
        raise failure(ErrorCode.VALIDATION, "env_name est vide ou trop long.")
    named_cloud = env_type == "cloud" and env_name is not None
    named_pool = env_type == "pool" and env_name is not None
    if named_cloud and repo_count:
        raise failure(
            ErrorCode.VALIDATION,
            "Un environnement cloud nommé ne se combine pas avec des dépôts.",
        )
    if repo_count > 1 and not named_pool:
        raise failure(
            ErrorCode.VALIDATION,
            "Plusieurs dépôts exigent un pool nommé (env_type=pool et env_name).",
        )


def _environment_variables(
    settings: Settings,
    env_vars: dict[str, str] | None,
    forward_env: list[str] | None,
) -> dict[str, str] | None:
    merged: dict[str, str] = {}
    for name, value in (env_vars or {}).items():
        require_env_name(name)
        require_env_value(value)
        merged[name] = value
    for name in forward_env or []:
        require_env_name(name)
        if name not in settings.forward_env:
            raise failure(
                ErrorCode.VALIDATION,
                f"{name} n'est pas dans CURSOR_MCP_FORWARD_ENV. La valeur n'est pas lue.",
            )
        if name in merged:
            raise failure(ErrorCode.VALIDATION, f"{name} est fourni à la fois dans env_vars et forward_env.")
        raw = os.environ.get(name)
        if raw is None or raw == "":
            raise failure(
                ErrorCode.VALIDATION,
                f"{name} est absent de l'environnement du processus. La valeur n'est pas renvoyée.",
            )
        require_env_value(raw)
        merged[name] = raw
    if len(merged) > ENV_MAX_COUNT:
        raise failure(ErrorCode.VALIDATION, "50 variables d'environnement au plus.")
    return merged or None


def _identity(agent_id: str | None, name: str | None, has_env: bool) -> tuple[str | None, str | None]:
    if has_env:
        if agent_id is not None:
            raise failure(
                ErrorCode.VALIDATION,
                "agent_id est incompatible avec env_vars et forward_env. "
                "Omettre agent_id, fournir name, puis retrouver l'agent avec cursor_list_agents si la création est incertaine.",
            )
        if name is None:
            raise failure(
                ErrorCode.VALIDATION,
                "name est obligatoire quand des variables d'environnement sont envoyées.",
            )
        return None, name
    if agent_id is not None:
        return require_agent_id(agent_id), None
    return f"bc-{uuid.uuid4()}", None


def _same_id(returned: str | None, expected: str) -> None:
    if returned is not None and returned != expected:
        raise failure(
            ErrorCode.INCOMPATIBLE_RESPONSE,
            "L'identifiant renvoyé ne correspond pas à l'agent demandé.",
        )
