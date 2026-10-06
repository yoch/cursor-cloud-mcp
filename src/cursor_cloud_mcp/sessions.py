"""Creation, follow-up runs and lifecycle of a Cloud agent."""

import asyncio
import os
import uuid
from collections.abc import Awaitable, Callable
from typing import Literal, Never

from cursor_cloud_mcp import budget, redaction
from cursor_cloud_mcp.catalog import resolve_model_selection
from cursor_cloud_mcp.client import CursorCloudClient
from cursor_cloud_mcp.config import (
    CANCEL_REREAD_PAUSE_SECONDS,
    CANCEL_REREADS,
    ENV_MAX_COUNT,
    REPO_MAX_COUNT,
    TOOL_BUDGET_SECONDS,
    Settings,
)
from cursor_cloud_mcp.errors import CursorFailure, ErrorCode, failure
from cursor_cloud_mcp.models import (
    ArchiveView,
    CreateAgentView,
    CreateRunView,
    DeleteView,
    ModelParam,
    RepositoryInput,
    agent_status_known,
    run_terminal,
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
    require_segment,
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
        raise failure(ErrorCode.VALIDATION, "name is empty.")
    repos = _repositories(repository, starting_ref, repositories)
    _check_environment(env_type, env_name, len(repos))
    merged = _environment_variables(settings, env_vars, forward_env)
    if merged is not None:
        # Per-call values: redacted from logs and errors from now on.
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
    model_id: str | None = None,
    model_params: list[ModelParam] | None = None,
    reasoning_level: str | None = None,
    replace_active: bool = False,
) -> CreateRunView:
    checked_prompt = require_prompt(prompt)
    checked_mode = require_mode(mode)
    agent = await client.get_agent(agent_id)
    ensure_continuation_allowed(agent)
    # Verified against the real API on October 5, 2026: model on POST /runs changes the model, and the choice persists.
    # Validated before anything is cancelled: a rejected model must not stop the current run.
    model_body = await _model_body(
        client,
        model_id=model_id,
        model_params=model_params,
        reasoning_level=reasoning_level,
    )
    body: dict[str, object] = {"prompt": {"text": checked_prompt}}
    if checked_mode is not None:
        body["mode"] = checked_mode
    if model_body is not None:
        body["model"] = model_body
    replaced: str | None = None
    if replace_active and agent.latestRunId is not None:
        # The API cannot steer a running cloud run (agent_busy): stop it, then follow up on the
        # same agent, which keeps its conversation, so no full resume prompt is needed.
        replaced = await _stop_active_run(client, agent_id, agent.latestRunId)
    try:
        remote = await client.create_run(agent_id, body, previous_latest_run_id=agent.latestRunId)
    except CursorFailure as exc:
        if replaced is None:
            raise
        note = f"Run {replaced} had already been cancelled before this failure."
        recovery = f"{exc.body.recovery} {note}" if exc.body.recovery else note
        raise CursorFailure(exc.body.model_copy(update={"recovery": recovery})) from None
    view = create_run_view(remote, previous_latest_run_id=agent.latestRunId, url=agent.url)
    updates: dict[str, object] = {}
    if model_body is not None:
        updates["model_id"] = model_body["id"]
    if replaced is not None:
        updates["replaced_run_id"] = replaced
    return view.model_copy(update=updates) if updates else view


async def cancel_and_observe(
    client: CursorCloudClient,
    agent_id: str,
    run_id: str,
    progress: Callable[[int, str], Awaitable[None]] | None = None,
) -> tuple[str | None, str | None]:
    """Cancel a run, then re-read it. Returns the last observed status and a re-read error, if any.

    Cancellation is asynchronous: the run can stay RUNNING for a moment after acceptance,
    or end otherwise during the race. Only a re-read CANCELLED confirms the cancellation.
    """
    require_segment(agent_id, label="agent_id")
    require_segment(run_id, label="run_id")
    cancelled = await client.cancel_run(agent_id, run_id)
    if cancelled.id is not None and cancelled.id.lower() != run_id.lower():
        raise failure(
            ErrorCode.INCOMPATIBLE_RESPONSE,
            "The identifier returned by the cancellation does not match the requested run.",
        )
    observed: str | None = None
    reread_error: str | None = None
    for attempt in range(CANCEL_REREADS):
        try:
            remote = await client.get_run(agent_id, run_id)
        except CursorFailure as exc:
            reread_error = exc.body.message
            break
        observed = remote.status
        if progress is not None:
            await progress(attempt + 1, f"Status re-read: {observed}")
        if run_terminal(observed) is True or attempt == CANCEL_REREADS - 1:
            break
        # A pause only makes sense if there is time left to re-read afterwards.
        if budget.remaining(TOOL_BUDGET_SECONDS) < CANCEL_REREAD_PAUSE_SECONDS + 1.0:
            reread_error = "Tool budget exhausted before a terminal state."
            break
        await asyncio.sleep(CANCEL_REREAD_PAUSE_SECONDS)
    return observed, reread_error


async def _stop_active_run(client: CursorCloudClient, agent_id: str, run_id: str) -> str | None:
    """Cancel the agent's current run if it is still going. Returns the id of the run it stopped.

    Nothing is sent afterwards unless the run is re-read terminal: AGENT_BUSY otherwise.
    """
    current = await client.get_run(agent_id, run_id)
    if run_terminal(current.status) is True:
        return None
    try:
        observed, reread_error = await cancel_and_observe(client, agent_id, run_id)
    except CursorFailure as exc:
        if exc.body.code is not ErrorCode.CANCEL_NOT_POSSIBLE:
            raise
        # The run ended on its own during the race: nothing left to replace.
        observed, reread_error = (await client.get_run(agent_id, run_id)).status, None
        if run_terminal(observed) is True:
            return None
    if run_terminal(observed) is not True:
        seen = observed or "unknown"
        detail = f" ({reread_error})" if reread_error else ""
        raise failure(
            ErrorCode.AGENT_BUSY,
            f"Run {run_id} was asked to cancel but is still {seen}{detail}. Nothing was sent: "
            "re-read it with cursor_get_run, then send the follow-up again.",
            agent_id=agent_id,
            run_id=run_id,
        )
    return run_id


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
    # An unknown status confirms nothing: only a known, expected state counts.
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
                "model_id is required with model_params or reasoning_level. "
                "No model identifier is invented.",
            )
        return None
    if model_id.strip() == "" or len(model_id) > 128:
        raise failure(ErrorCode.VALIDATION, "model_id is empty or too long.")
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
            "Provide either repository and starting_ref, or repositories, not both.",
        )
    if single:
        if repository is None or starting_ref is None:
            raise failure(ErrorCode.VALIDATION, "repository and starting_ref go together.")
        return [
            {
                "url": normalize_repository(repository),
                "startingRef": require_starting_ref(starting_ref),
            }
        ]
    if not repositories:
        return []
    if len(repositories) > REPO_MAX_COUNT:
        raise failure(ErrorCode.VALIDATION, "At most 20 repositories.")
    return [
        {
            "url": normalize_repository(item.url),
            "startingRef": require_starting_ref(item.ref),
        }
        for item in repositories
    ]


def _check_environment(env_type: str | None, env_name: str | None, repo_count: int) -> None:
    if env_name is not None and env_type is None:
        raise failure(ErrorCode.VALIDATION, "env_name requires env_type.")
    if env_name is not None and (env_name.strip() == "" or len(env_name) > 128):
        raise failure(ErrorCode.VALIDATION, "env_name is empty or too long.")
    named_cloud = env_type == "cloud" and env_name is not None
    named_pool = env_type == "pool" and env_name is not None
    if named_cloud and repo_count:
        raise failure(
            ErrorCode.VALIDATION,
            "A named cloud environment cannot be combined with repositories.",
        )
    if repo_count > 1 and not named_pool:
        raise failure(
            ErrorCode.VALIDATION,
            "Multiple repositories require a named pool (env_type=pool and env_name).",
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
                f"{name} is not in CURSOR_MCP_FORWARD_ENV. The value is not read.",
            )
        if name in merged:
            raise failure(ErrorCode.VALIDATION, f"{name} is provided in both env_vars and forward_env.")
        raw = os.environ.get(name)
        if raw is None or raw == "":
            raise failure(
                ErrorCode.VALIDATION,
                f"{name} is missing from the process environment. The value is not returned.",
            )
        require_env_value(raw)
        merged[name] = raw
    if len(merged) > ENV_MAX_COUNT:
        raise failure(ErrorCode.VALIDATION, "At most 50 environment variables.")
    return merged or None


def _identity(agent_id: str | None, name: str | None, has_env: bool) -> tuple[str | None, str | None]:
    if has_env:
        if agent_id is not None:
            raise failure(
                ErrorCode.VALIDATION,
                "agent_id is incompatible with env_vars and forward_env. "
                "Omit agent_id, provide name, then find the agent with cursor_list_agents if the creation is uncertain.",
            )
        if name is None:
            raise failure(
                ErrorCode.VALIDATION,
                "name is required when environment variables are sent.",
            )
        return None, name
    if agent_id is not None:
        return require_agent_id(agent_id), None
    return f"bc-{uuid.uuid4()}", None


def _same_id(returned: str | None, expected: str) -> None:
    if returned is not None and returned != expected:
        raise failure(
            ErrorCode.INCOMPATIBLE_RESPONSE,
            "The returned identifier does not match the requested agent.",
        )
