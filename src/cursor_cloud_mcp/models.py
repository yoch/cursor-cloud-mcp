"""Schémas d'entrée, vues MCP et payloads distants utilisés par les onze outils."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from cursor_cloud_mcp.config import (
    NAME_MAX_CHARS,
    PROMPT_MAX_CHARS,
    RESULT_DEFAULT_LIMIT,
    RESULT_MAX_LIMIT,
)


class ModelParam(BaseModel):
    """Paramètre ``model.params[]`` tel que ``GET /v1/models`` le décrit."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    value: str = Field(min_length=1)


class RemoteEnv(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: str
    name: str | None = None


class RemoteRepo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    url: str
    startingRef: str | None = None
    prUrl: str | None = None


class RemoteAgentSummary(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    status: str
    env: RemoteEnv
    url: str
    createdAt: str
    updatedAt: str
    name: str | None = None
    latestRunId: str | None = None


class RemoteAgent(RemoteAgentSummary):
    repos: list[RemoteRepo] | None = None
    workOnCurrentBranch: bool | None = None
    autoCreatePR: bool | None = None


class RemoteGitBranch(BaseModel):
    model_config = ConfigDict(extra="ignore")

    repoUrl: str
    branch: str | None = None
    prUrl: str | None = None


class RemoteGit(BaseModel):
    model_config = ConfigDict(extra="ignore")

    branches: list[RemoteGitBranch]


class RemoteRun(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    agentId: str
    status: str
    createdAt: str
    updatedAt: str
    durationMs: int | None = None
    result: str | None = None
    git: RemoteGit | None = None


class RemoteCreateAgent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    agent: RemoteAgent
    run: RemoteRun


class RemoteCreateRun(BaseModel):
    model_config = ConfigDict(extra="ignore")

    run: RemoteRun


class RemoteAgentPage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    items: list[RemoteAgentSummary]
    nextCursor: str | None = None


class RemoteRunPage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    items: list[RemoteRun]
    nextCursor: str | None = None


class RemoteAccount(BaseModel):
    model_config = ConfigDict(extra="ignore")

    apiKeyName: str
    createdAt: str
    userId: int | None = None
    userEmail: str | None = None
    userFirstName: str | None = None
    userLastName: str | None = None


class RemoteModelValue(BaseModel):
    model_config = ConfigDict(extra="ignore")

    value: str
    displayName: str | None = None


class RemoteModelParameter(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    values: list[RemoteModelValue]
    displayName: str | None = None


class RemoteModelSelection(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    value: str


class RemoteModelVariant(BaseModel):
    model_config = ConfigDict(extra="ignore")

    params: list[RemoteModelSelection]
    displayName: str
    description: str | None = None
    isDefault: bool | None = None


class RemoteModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    displayName: str
    description: str | None = None
    aliases: list[str] | None = None
    parameters: list[RemoteModelParameter] | None = None
    variants: list[RemoteModelVariant] | None = None


class RemoteModelList(BaseModel):
    model_config = ConfigDict(extra="ignore")

    items: list[RemoteModel]


class RemoteRepository(BaseModel):
    model_config = ConfigDict(extra="ignore")

    url: str


class RemoteRepositoryList(BaseModel):
    model_config = ConfigDict(extra="ignore")

    items: list[RemoteRepository]


class RemoteTokenUsage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    inputTokens: int
    outputTokens: int
    cacheWriteTokens: int
    cacheReadTokens: int
    totalTokens: int


class RemoteRunUsage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    usage: RemoteTokenUsage
    usageUuid: str | None = None


class RemoteUsage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    totalUsage: RemoteTokenUsage
    runs: list[RemoteRunUsage]


class RemoteId(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str | None = None


class RemoteArtifact(BaseModel):
    model_config = ConfigDict(extra="ignore")

    path: str
    sizeBytes: int
    updatedAt: str


class RemoteArtifactList(BaseModel):
    model_config = ConfigDict(extra="ignore")

    items: list[RemoteArtifact]


class RemoteArtifactDownload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    url: str
    expiresAt: str


class RepositoryInput(BaseModel):
    """Dépôt demandé à la création. Le SHA n'est pas vérifié chez GitHub."""

    model_config = ConfigDict(extra="forbid")

    url: str
    starting_sha: str


class AccountView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key_name: str
    created_at: str
    user_id: int | None = None
    user_email: str | None = None
    user_first_name: str | None = None
    user_last_name: str | None = None


class ModelValueView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str
    display_name: str | None = None


class ModelParameterView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    display_name: str | None = None
    values: list[ModelValueView]


class ModelSelectionView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    value: str


class ModelVariantView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    params: list[ModelSelectionView]
    display_name: str
    description: str | None = None
    is_default: bool | None = None


class ReasoningParamView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    values: list[str]


class ModelView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    display_name: str
    description: str | None = None
    aliases: list[str] | None = None
    parameters: list[ModelParameterView] | None = None
    variants: list[ModelVariantView] | None = None
    reasoning_param: ReasoningParamView | None = None


class ModelListView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ModelView]


class RepositoryListView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[str]
    cache_ttl_seconds: int
    cache_hit: bool


class RepoView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    starting_ref: str | None = None
    pr_url: str | None = None


class EnvView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    name: str | None = None


class AgentSummaryView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str
    name: str | None = None
    status: str
    status_known: bool
    url: str
    created_at: str
    updated_at: str
    latest_run_id: str | None = None
    env: EnvView


class AgentPageView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[AgentSummaryView]
    next_cursor: str | None = None
    has_more: bool


class AgentView(AgentSummaryView):
    repos: list[RepoView] | None = None
    repo_count: int | None = None
    work_on_current_branch: bool | None = None
    auto_create_pr: bool | None = None


class GitBranchView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo_url: str
    branch: str | None = None
    pr_url: str | None = None


class GitView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    branches: list[GitBranchView]
    scope: Literal["agent_current_state"] = "agent_current_state"


class RunSummaryView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    agent_id: str
    status: str
    status_known: bool
    terminal: bool | None
    created_at: str
    updated_at: str
    duration_ms: int | None = None


class RunPageView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[RunSummaryView]
    next_cursor: str | None = None
    has_more: bool


class RunView(RunSummaryView):
    result_present: bool
    result: str | None = None
    result_offset: int = 0
    result_limit: int = RESULT_DEFAULT_LIMIT
    result_total_chars: int | None = None
    result_truncated: bool = False
    next_result_offset: int | None = None
    git: GitView | None = None


class CreateAgentView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str
    run_id: str
    agent_status: str
    run_status: str
    url: str | None = None
    name: str | None = None
    latest_run_id: str | None = None
    next_step: str


class CreateRunView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str
    run_id: str
    status: str
    previous_latest_run_id: str | None = None
    url: str | None = None
    next_step: str


class CancelView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str
    run_id: str
    cancel_request_accepted: bool
    outcome_confirmed: bool
    observed_status: str | None = None
    observed_terminal: bool | None = None
    reread_error: str | None = None
    commits_removed: Literal[False] = False


class TokenUsageView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_tokens: int
    output_tokens: int
    cache_write_tokens: int
    cache_read_tokens: int
    total_tokens: int


class RunUsageView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    usage_uuid: str | None = None
    usage: TokenUsageView


class UsageView(BaseModel):
    """Jetons réellement renvoyés. Aucun champ de coût : l'API n'en fournit pas."""

    model_config = ConfigDict(extra="forbid")

    total_usage: TokenUsageView
    runs: list[RunUsageView]


class RunEventView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str | None = None
    kind: Literal["status", "assistant", "tool_call", "thinking", "result", "error", "done"]
    text: str | None = None
    status: str | None = None
    tool_name: str | None = None
    tool_status: str | None = None
    tool_args: str | None = None
    tool_result: str | None = None


class RunEventsView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str
    run_id: str
    events: list[RunEventView]
    last_event_id: str | None = None
    finished: bool
    run_status: str | None = None
    retention_seconds: int | None = None
    truncated: bool


class WaitRunView(RunView):
    timed_out: bool


class ArtifactItemView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    size_bytes: int
    updated_at: str


class ArtifactListView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ArtifactItemView]


class ArtifactUrlView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    url: str
    expires_at: str


class ArtifactTextView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    text: str
    offset: int
    limit: int
    total_chars: int
    truncated: bool
    next_offset: int | None = None
    expires_at: str


class ArchiveView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str
    action: Literal["archive", "unarchive"]
    request_accepted: bool
    outcome_confirmed: bool
    observed_status: str | None = None
    reread_error: str | None = None


class DeleteView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str
    deleted: Literal[True] = True


_KNOWN_AGENT_STATUSES = {"ACTIVE", "IDLE", "ARCHIVED"}
_RUN_TERMINAL = {"FINISHED": True, "ERROR": True, "CANCELLED": True, "EXPIRED": True}
_RUN_OPEN = {"CREATING": False, "RUNNING": False}


def agent_status_known(status: str) -> bool:
    return status in _KNOWN_AGENT_STATUSES


def run_terminal(status: str) -> bool | None:
    if status in _RUN_TERMINAL:
        return True
    if status in _RUN_OPEN:
        return False
    return None


def tokens_from(remote: RemoteTokenUsage) -> TokenUsageView:
    return TokenUsageView(
        input_tokens=remote.inputTokens,
        output_tokens=remote.outputTokens,
        cache_write_tokens=remote.cacheWriteTokens,
        cache_read_tokens=remote.cacheReadTokens,
        total_tokens=remote.totalTokens,
    )


def env_from(remote: RemoteEnv) -> EnvView:
    return EnvView(type=remote.type, name=remote.name)


def summary_from(remote: RemoteAgentSummary) -> AgentSummaryView:
    return AgentSummaryView(
        agent_id=remote.id,
        name=remote.name,
        status=remote.status,
        status_known=agent_status_known(remote.status),
        url=remote.url,
        created_at=remote.createdAt,
        updated_at=remote.updatedAt,
        latest_run_id=remote.latestRunId,
        env=env_from(remote.env),
    )


def cursor_of(value: str | None) -> tuple[str | None, bool]:
    """L'absence de curseur termine la page. Une chaîne vide est un contrat cassé."""
    if value is None:
        return None, False
    if value == "":
        raise ValueError("curseur vide")
    return value, True


RESULT_LIMIT_FIELD = Field(default=RESULT_DEFAULT_LIMIT, ge=1, le=RESULT_MAX_LIMIT)
NAME_FIELD = Field(default=None, max_length=NAME_MAX_CHARS)
PROMPT_FIELD = Field(min_length=1, max_length=PROMPT_MAX_CHARS)
