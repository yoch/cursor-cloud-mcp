"""Input schemas, MCP views and remote payloads used by the tools."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from cursor_cloud_mcp.config import (
    NAME_MAX_CHARS,
    PROMPT_MAX_CHARS,
    RESULT_DEFAULT_LIMIT,
    RESULT_MAX_LIMIT,
)


class ModelParam(BaseModel):
    """Parameter ``model.params[]`` as ``GET /v1/models`` describes it."""

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
    error: object | None = None
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


class RemoteCost(BaseModel):
    model_config = ConfigDict(extra="ignore")

    rawCostCents: float
    chargedCents: float


class RemoteRunUsage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    usage: RemoteTokenUsage
    usageUuid: str | None = None
    cost: RemoteCost | None = None


class RemoteUsage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    totalUsage: RemoteTokenUsage
    runs: list[RemoteRunUsage]
    cost: RemoteCost | None = None


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
    """Repository requested at creation. ``starting_ref`` is a branch name."""

    model_config = ConfigDict(extra="forbid")

    url: str
    starting_ref: str

    @property
    def ref(self) -> str:
        return self.starting_ref


class AccountView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key_name: str
    created_at: str
    user_id: int | None = None
    user_email: str | None = None
    user_first_name: str | None = None
    user_last_name: str | None = None


class ModelView(BaseModel):
    """Compact form: the values of each parameter; the model variants only on request."""

    model_config = ConfigDict(extra="forbid")

    id: str
    display_name: str
    description: str | None = None
    aliases: list[str] | None = None
    params: dict[str, list[str]] | None = None
    defaults: dict[str, str] | None = None
    reasoning_param: str | None = None
    restricted_combinations: bool | None = None
    variants: list[dict[str, str]] | None = None


class ModelListView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ModelView]


class RepositoryListView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[str]
    total_count: int
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
    scanned: int | None = None


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
    terminal: bool | None = None
    created_at: str
    updated_at: str
    duration_ms: int | None = None


class RunPageView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[RunSummaryView]
    next_cursor: str | None = None
    has_more: bool


class ToolCallSummaryView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    status: str | None = None
    args: str | None = None
    result: str | None = None


class BackgroundTaskView(BaseModel):
    """A background task the agent started, as last observed in the stream (not proof it is alive)."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    command: str | None = None
    last_state: Literal["running", "complete", "unknown"]
    runtime_ms: int | None = None
    observed_at: str | None = None


class ActivityView(BaseModel):
    """What the stream shows about a run: liveness, last action, background tasks."""

    model_config = ConfigDict(extra="forbid")

    last_event_id: str | None = None
    last_event_at: str | None = None
    idle_seconds: int | None = None
    last_assistant_text: str | None = None
    last_tool_call: ToolCallSummaryView | None = None
    background_tasks: list[BackgroundTaskView] | None = None
    background_tasks_total: int | None = None
    unfinished_background_tasks: int | None = None
    run_terminal: bool | None = None
    scanned_events: int
    complete: bool = True


class RunView(RunSummaryView):
    result_present: bool
    result: str | None = None
    result_offset: int | None = None
    result_limit: int | None = None
    result_total_chars: int | None = None
    result_truncated: bool | None = None
    next_result_offset: int | None = None
    error: str | None = None
    git: GitView | None = None
    timed_out: bool | None = None
    reread_error: str | None = None
    activity: ActivityView | None = None
    activity_error: str | None = None


class SupervisedAgentView(BaseModel):
    """One agent and its latest run, as seen in a single supervision pass."""

    model_config = ConfigDict(extra="forbid")

    agent_id: str
    name: str | None = None
    url: str
    agent_status: str
    run_id: str | None = None
    status: str | None = None
    terminal: bool | None = None
    created_at: str | None = None
    duration_ms: int | None = None
    result_present: bool | None = None
    error: str | None = None
    read_error: str | None = None
    activity: ActivityView | None = None
    activity_error: str | None = None


class SuperviseSummaryView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agents: int
    by_status: dict[str, int]
    stale: list[str] | None = None
    unfinished_after_end: list[str] | None = None
    incomplete: list[str] | None = None
    read_errors: int | None = None


class SuperviseView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[SupervisedAgentView]
    summary: SuperviseSummaryView
    scanned: int | None = None
    has_more: bool
    next_cursor: str | None = None


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
    model_id: str | None = None
    replaced_run_id: str | None = None
    next_step: str


class CancelView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str
    run_id: str
    cancel_request_accepted: bool
    outcome: Literal["cancelled", "ended_without_cancel", "still_running", "unknown"]
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


class CostView(BaseModel):
    """Cost as the API returns it, in US cents. Absent if the API does not provide it."""

    model_config = ConfigDict(extra="forbid")

    raw_cents: float
    charged_cents: float


class RunUsageView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    usage_uuid: str | None = None
    usage: TokenUsageView
    cost: CostView | None = None


class UsageView(BaseModel):
    """Tokens and cost actually returned. Nothing is estimated."""

    model_config = ConfigDict(extra="forbid")

    total_usage: TokenUsageView
    total_cost: CostView | None = None
    runs: list[RunUsageView]


class RunEventView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str | None = None
    kind: Literal["status", "assistant", "tool_call", "thinking", "result", "error", "done"]
    text: str | None = None
    status: str | None = None
    call_id: str | None = None
    tool_name: str | None = None
    tool_status: str | None = None
    tool_args: str | None = None
    tool_result: str | None = None
    clipped: bool | None = None


class RunEventsView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str
    run_id: str
    events: list[RunEventView]
    last_event_id: str | None = None
    finished: bool
    stream_error: bool = False
    interrupted: bool = False
    run_status: str | None = None
    retention_seconds: int | None = None
    truncated: bool
    last_event_at: str | None = None
    scanned_events: int | None = None


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


class ArtifactReadView(BaseModel):
    """Text of an artifact, or its presigned URL if it is not readable as text or if the URL is requested."""

    model_config = ConfigDict(extra="forbid")

    path: str
    expires_at: str
    url: str | None = None
    text_unavailable: Literal["not_utf8", "too_large"] | None = None
    text: str | None = None
    offset: int | None = None
    limit: int | None = None
    total_chars: int | None = None
    truncated: bool | None = None
    next_offset: int | None = None


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
    """A missing cursor ends the page. An empty string is a broken contract."""
    if value is None:
        return None, False
    if value == "":
        raise ValueError("empty cursor")
    return value, True


RESULT_LIMIT_FIELD = Field(default=RESULT_DEFAULT_LIMIT, ge=1, le=RESULT_MAX_LIMIT)
NAME_FIELD = Field(default=None, max_length=NAME_MAX_CHARS)
PROMPT_FIELD = Field(min_length=1, max_length=PROMPT_MAX_CHARS)
