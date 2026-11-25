# API contract used

Consulted on September 30, 2026, re-read on October 1, 2026. Re-read on October 19, 2026: the spec had doubled; re-read on November 10, 2026 and again on November 16, 2026 (see the last three sections). The parts below are updated where this server now uses them.

This document does not copy the OpenAPI. It fixes the fields this MCP server sends or reads. The officially read contract prevails over any other source. The discrepancies are listed below.

## Source

- Endpoints page: <https://cursor.com/docs/cloud-agent/api/endpoints>
- OpenAPI link followed from that page: <https://cursor.com/docs-static/cloud-agents-openapi.yaml>
- HTTP response of the download: `200`, `content-type: text/yaml; charset=utf-8`, `date: Wed, 30 Sep 2026 19:13:31 GMT`, `etag: "fb90bd68a38f8414b4d85c52e6bfa7e0"`, 59344 bytes
- `info.title`: Cursor Cloud Agents API
- `info.version`: `1.0.0`
- `openapi`: `3.0.3`
- Server: `https://api.cursor.com`
- SHA-256 fingerprint of the raw file: `7fb350f40e928721afaba13d44377880f235f087734bc26ef51b54f7323ad209`
- October 19, 2026: `etag: "57a4cf20b2db5ce669b8c9a8beed34d6"`, 114038 bytes, SHA-256 `d6bf8a5740df6ac252ab14f9858a7406830bc175a05528f21e60468b559dc5a3`
- November 10, 2026: `etag: "ea6fc66ea63a13ad6c768de490b1276f"`, `last-modified: Mon, 09 Nov 2026 23:26:24 GMT`, 146464 bytes, SHA-256 `dd3b836c4e95ad68892274280e53c348619cdd2051988793d2a85e8213d9a489`. The endpoints page is also served at <https://cursor.com/docs/cloud-agent/api/overview>, byte for byte
- November 16, 2026: `etag: "1c8bc6320e442c2c7e20d13a7b21fcba"`, `last-modified: Mon, 16 Nov 2026 14:29:43 GMT`, 166185 bytes, SHA-256 `738f84ce36282fbbfac2dde96cdb4d059ed15e1f92d1c4ccc27ef0574e44a1d9`. The spec changed the day it was read.
- Both files are kept locally and git-ignored: `docs/cloud-agents-openapi.yaml` (November 16) and `docs/cloud-agents-openapi.previous.yaml` (November 10, SHA-256 checked against the line above), with the response headers in `docs/openapi-headers.txt`. This document still does not copy the OpenAPI. Keep the previous file before replacing the current one: a diff needs it, and the November 10 file was only found again in a previous session's scratch directory.

Authentication chosen: `Authorization: Bearer`. The OpenAPI also accepts Basic with the key as the username and an empty password. Both schemes are documented as equivalent. No redirect is followed.

## Endpoints used

| Method and path | OpenAPI success | Role in this MCP server |
|---|---|---|
| `GET /v1/me` | 200 `ApiKeyInfo` | `cursor_get_account` |
| `GET /v1/models` | 200 `ListModelsResponse` | `cursor_list_models` |
| `GET /v1/repositories` | 200 `ListRepositoriesResponse` | `cursor_list_repositories` |
| `GET /v1/agents` | 200 `ListAgentsResponse` | `cursor_list_agents` |
| `GET /v1/agents/{id}` | 200 `Agent` | `cursor_get_agent` |
| `POST /v1/agents` | 201 `CreateAgentResponse` | `cursor_create_agent` |
| `GET /v1/agents/{id}/runs` | 200 `ListRunsResponse` | `cursor_list_runs` |
| `GET /v1/agents/{id}/runs/{runId}` | 200 `Run` | `cursor_get_run` |
| `POST /v1/agents/{id}/runs` | 201 `CreateRunResponse`, or 202 `SteerRunResponse` with `delivery: "steer"` | `cursor_create_run` |
| `POST /v1/agents/{id}/runs/{runId}/cancel` | 200 `IdResponse` | `cursor_cancel_run` |
| `GET /v1/agents/{id}/runs/{runId}/stream` | 200 `text/event-stream` | `cursor_read_run_events`, `cursor_get_run(activity)`, `cursor_supervise(activity)` |
| `GET /v1/agents/{id}/usage` | 200 `AgentUsageResponse` | `cursor_get_usage` |
| `GET /v1/agents/{id}/artifacts` | 200 `ListArtifactsResponse` | `cursor_list_artifacts` |
| `GET /v1/agents/{id}/artifacts/download` | 200 `DownloadArtifactResponse` | `cursor_read_artifact` |
| `POST /v1/agents/{id}/archive` | 200 `IdResponse` | `cursor_archive_agent` |
| `POST /v1/agents/{id}/unarchive` | 200 `IdResponse` | `cursor_archive_agent` with `unarchive=true` |
| `DELETE /v1/agents/{id}` | 200 `IdResponse` | `cursor_delete_agent` |
| `GET /v1/environments` | 200 `ListEnvironmentsResponse` | `cursor_list_environments` |
| `GET /v1/environments/{id}` | 200 `GetEnvironmentResponse` | `cursor_list_environments(environment_id)` |
| `GET /v1/environments/{id}/builds/active` | 200 `EnvironmentActiveBuild` | same |
| `GET /v1/environments/{id}/builds/{buildId}` | 200 `EnvironmentBuild` | same |
| `GET /v1/projects` | 200 `ListProjectsResponse` | `cursor_list_projects` |
| `GET /v1/projects/{id}` | 200 `Project` | `cursor_list_projects(project_id)` |
| `GET /v1/projects/{id}/agents` | 200 `ListProjectAgentsResponse` | same, the agents of the caller's coordinator |
| `GET /v0/private-workers/pools` | 200, endpoints page only | `cursor_list_environments`, first page, best effort |
| `GET /v0/agents/{id}/conversation` | 200, legacy v0 page only | `cursor_read_conversation` |

## Fields sent

`POST /v1/agents`, only the keys provided, never `null`:

- `prompt.text`: required, non-empty
- `repos`: zero to 500 elements `{ "url", "startingRef" }`. Absent if there is no repository. `startingRef` is a branch name or a full commit SHA: a SHA got `400 validation_error` on October 1, 2026 and `201` on October 19, 2026. On self-hosted workers, only a named any-repo pool takes more than one repository; a hosted VM took two on October 19
- `prompt.images`: up to 5, each `{url}` or `{data, mimeType}` (png, jpeg, gif, webp, 15 MB decoded), with an optional `dimension`; checked locally, never logged
- `skipReviewerRequest`: `true` only, and only with `autoCreatePR`
- `workOnCurrentBranch`: always `false`
- `autoCreatePR`: boolean, `false` unless the caller asks for `true`
- `agentId`: `bc-` followed by a UUID, supplied or generated once before sending. Absent when `envVars` is sent: the API forbids both together
- `name`, `model` (`id` and optionally `params[{id,value}]`), `mode` (`agent` or `plan`): only if provided
- `env`: `{ "type": "cloud" | "pool" | "machine", "name"? }` only if provided
- `envVars`: object of strings, at most 50, only if provided. Names do not start with `CURSOR_`. The October 19 spec marks it beta, "silently ignored" where not enabled; it reached the VM on the account tested that day

`model.id` is a catalog id; an alias is accepted only if it designates a single model, and it is sent as that id. `model.params` is checked against `GET /v1/models` before sending: each value, then the combination, which must fit in at least one published variant (the real catalog of October 5, 2026 omits 5 combinations out of 20 for `gpt-5.5`). `reasoning_level` is placed in the first parameter present among `effort`, `reasoning_effort` and `reasoning`.

`POST /v1/agents/{id}/runs`: `prompt.text`, and `prompt.images`, `mode`, `model` (same shape and same check as for creation) and `envVars` (same rules as for creation, for that run only) only if provided. A steer sends `prompt` and `delivery: "steer"` and nothing else: the API answers `400 validation_error` to any other top-level field, so this server refuses them before sending.

`GET /v1/environments`: `limit`, `cursor`, and the filters `query`, `owner` (`personal` or `team`), `named` (`true` or `false`) and `repo` (repeated, up to 100) only if provided. `userId` is not sent.

`GET /v1/agents` and `GET /v1/agents/{id}/runs`: `limit` (1 to 100) and `cursor` only if provided. `GET /v1/agents` adds `includeArchived` and `prUrl` only if provided. `includeArchived` defaults to `true`: archived agents are listed unless `false` is sent (spec and observation of October 19, 2026). The API refuses any other filter (`400`, "Unrecognized key(s)", verified on October 5, 2026 for `name`, `q`, `search`, `status`, `sort`): the name search of `cursor_list_agents` therefore walks at most five pages of one hundred and filters locally.

`GET /v1/agents/{id}/runs/{runId}/stream`: `Last-Event-ID` header only if provided. Reading stops on `done`, `result` or `error`, at the local deadline, or at 1 MB. `error` is a stream error, returned as `stream_error`: only `result` and `done` mark `finished`. UTF-8 decoding is incremental, so that a character split between two network chunks stays intact. `heartbeat` and `interaction_update` are not returned to the caller. Consecutive `assistant` (and `thinking`) fragments, sent word by word by the API, are merged into one event of at most 4000 characters, which carries the identifier of the last fragment. The `tail` mode and the activity summary send no `Last-Event-ID`: they walk the whole replay (see "Stream replay and supervision" below).

`GET /v1/agents/{id}/artifacts/download`: `path`, relative, `artifacts/` prefix, no `..`.

`GET /v1/agents/{id}/usage`: `runId` only if provided.

## Fields read

- Account: `apiKeyName`, `createdAt`, and if present `userId`, `userEmail`, `userFirstName`, `userLastName`. No key secret.
- Steer: `delivery` (`steered` or `queued`) and, if present, `runId`. Any other `delivery` is an unknown outcome, not a failure to retry.
- Models: `items[]` with `id`, `displayName`, and if present `description`, `aliases`, `parameters`, `variants`. A variant may carry `isMaxMode`, returned by the real API on November 10, 2026 and absent from the OpenAPI: it only feeds `default_max_mode`, `max_mode_variants` and `model.max_mode`, and nothing about it is sent. Variants are specific to each model: they are the combinations it accepts. On October 5, 2026, they covered all combinations for most models of the catalog; `gpt-5.5`, `gpt-5.4` and `claude-opus-5` excluded some, and some carried an unpublished parameter (`cyber`), ignored here. They serve the combination check, model by model, and `defaults`, and are returned only with `model_id`.
- Repositories: `items[].url`. No cursor in this schema.
- Agents, page: `items[]` (`id`, `status`, `env`, `url`, `createdAt`, `updatedAt`, optional `name` and `latestRunId`) and `nextCursor` if present. Its absence means end of list, not a `null` value.
- Agent: the page's fields, plus `repos`, `workOnCurrentBranch`, `autoCreatePR` when present. An absence stays an absence.
- Projects, page: `items[]` and `nextCursor` under the same rule, which may be set on an empty page. Project: `id` (the only field relied on), and if present `status` (`ACTIVE` or `ARCHIVED`), `name`, `coordinatorAgentId`, `appearance` (`icon`, `color`), `createdAt`, `updatedAt`, `url`. Query: `limit`, `cursor`, `includeArchived` (sent only when given). The Project's agents are `Agent` rows read as agent summaries; `GET /v1/projects/{id}` also accepts the caller's own coordinator id, whose response carries the Project's id, so that one id is compared to the requested one only when it is not the coordinator's.
- Runs, page: `items[]` of the `Run` schema and `nextCursor` under the same rule.
- Run: `id`, `agentId`, `status`, `createdAt`, `updatedAt`, and if present `durationMs`, `result`, `error` (free form; `code: message` if it is an object), `git.branches[]` (`repoUrl`, `branch`, `prUrl`). `git` is the agent's current state, not an immutable snapshot of the run. `repoUrl` is returned without a scheme. No `final_sha` is invented.
- Cancellation, archiving, unarchiving, deletion: `id`. It is compared to the requested identifier when present.
- Usage: `totalUsage` and `runs[].usage` with `inputTokens`, `outputTokens`, `cacheWriteTokens`, `cacheReadTokens`, `totalTokens`. `usageUuid` if present. A run whose `totalTokens` is zero is reported as `no_usage_recorded` (the spec: "Runs without any recorded token usage report zeros"), whatever its cost fields say. `cost` and `runs[].cost` (`rawCostCents`, `chargedCents`) if present: absent from the September 30 OpenAPI, but returned by the real API on October 5, 2026 and typed by the Cursor SDK. Rendered in cents, rounded to 4 decimals, never estimated.
- Artifacts: `items[]` with `path`, `sizeBytes`, `updatedAt`. The download returns `url` and `expiresAt`. The presigned URL is followed only if it is HTTPS and the host ends with `.amazonaws.com`, with no redirect and no `Authorization` header.
- Stream: events `status`, `assistant`, `tool_call`, `result`, `error`, `done`, and `thinking` only on request. The `X-Cursor-Stream-Retention-Seconds` header is kept if present. A `tool_call` payload may carry `truncated.args` / `truncated.result`: the field is omitted upstream and this server reports it as `tool_args_omitted` / `tool_result_omitted`, never from a local clip. Verified live on October 9, 2026: an 8 MB terminal output produced `truncated: {result: true}` with `result` absent. For the activity summary: event ids read as millisecond timestamps (`<ms>-<sequence>`, otherwise ignored), `run_terminal_cmd` calls with `args.isBackground` or `result.isBackground` and their `result.success.shellId`, and `await` results `success.stillRunning` / `success.complete` (`taskId`, `runtimeMs`). `heartbeat` ends a replay walk.

Known run states: `CREATING`, `RUNNING`, `WAITING_FOR_BACKGROUND_WORK` (spec of November 9, 2026, not observed), `FINISHED`, `ERROR`, `CANCELLED`, `EXPIRED`. The last four are terminal. Any other state is kept and is not a success.

Agent states: the endpoints page describes `ACTIVE`, `IDLE` and `ARCHIVED`. The OpenAPI enumerates only `ACTIVE` and `ARCHIVED`. `IDLE` is therefore accepted and displayed. An unknown state stays visible.

Created `agentId`: `^bc-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$`.

## Remote errors

Documented body: `{ "error": { "code", "message", "helpUrl"?, "provider"? } }`. `helpUrl` (HTTPS only) and `provider` are rendered in `help_url` and `provider` when present, for example for `integration_not_connected`. `invalid_last_event_id` carries a resume instruction without a cursor.

Codes cited by the schema: `unauthorized`, `api_key_not_found`, `plan_required`, `role_forbidden`, `feature_unavailable`, `integration_not_connected`, `validation_error`, `missing_body`, `invalid_model`, `invalid_branch_name`, `repository_required`, `repository_access`, `pr_resolution_failed`, `artifact_not_found`, `service_account_required`, `agent_not_found`, `run_not_found`, `agent_busy`, `agent_archived`, `agent_id_conflict`, `run_not_cancellable`, `rate_limit_exceeded`, `usage_limit_exceeded`, `stream_expired`, `stream_unavailable`, `invalid_last_event_id`, `client_cancelled`, `not_implemented`, `upstream_error`, `internal_error`. Added by October 19: `environment_not_found`, `build_not_found`, `team_admin_required`, `environment_name_conflict`, `duplicate_json_key`, plus secret-specific codes on the secret endpoints. `stream_expired` and `stream_unavailable` are classified by code before the status.

Statuses used for classification: 400 validation, 401 authentication, 403 permissions (`feature_unavailable` included), 404 missing resource, 409 conflict, 410 expired stream, 429 quota. The 429 may carry `Retry-After`. Without this header, a single GET retry waits one second, within the deadline. The OpenAPI also mentions `X-RateLimit-Limit`, `X-RateLimit-Remaining` and `X-RateLimit-Reset`: a `QUOTA` error carries them as `rate_limit_limit`, `rate_limit_remaining` and `rate_limit_reset` when present. No request identifier is specified: the `x-request-id`, `request-id` and `x-cursor-request-id` headers are kept only if present.

`GET /v1/agents/{id}/usage` may answer `403 feature_unavailable` (early access).

An account-state refusal can come as `400 validation_error` with a message starting with "Blocked by privacy setting" (observed on `GET /v1/environments` with a `grok_` key, November 10, 2026; the same call answered `200` on November 12). It is classified `PERMISSION`, not `VALIDATION`: no argument can fix it. A failed authentication answers `401` with a top-level `code` and `message` instead of an `error` object; both shapes are read.

`GET /v1/repositories` is limited to 1 request per user per minute, and 30 per hour. The page states that the response may take tens of seconds.

## Accepted adjustments

- Creations are specified as `201`. A `200` with the same JSON is accepted, because success is judged on the schema, not on a neighboring status.
- Per-tool budget: a single absolute deadline per MCP call, passed as remaining time to each request, re-read and pause (45 seconds by default, 95 for creation, follow-up runs and the repository list, 45 for cancellation, `max_wait_seconds` for the stream, `wait_seconds` (at least 45) for waiting on a run). A mutation is not sent if less than 5 seconds remain, or less than half of its own timeout.
- Per-request deadline, within this budget: 40 seconds per call, 90 seconds for `POST /v1/agents` and `POST /v1/agents/{id}/runs` (a real creation exceeded 40 seconds before succeeding), 90 seconds for `GET /v1/repositories`, because the official contract warns that this call can take tens of seconds. `cursor_get_run` with `wait_seconds` chains reads within a deadline of at most 60 seconds. `cursor_read_run_events` bounds its wait to 50 seconds.
- `IDLE` is not in the agent's OpenAPI enum, but the endpoints page defines it. It is not rejected.
- The SSE stream is read once, with no internal reconnection. `410 stream_expired` becomes `STREAM_EXPIRED`.
- Artifacts, archiving, unarchiving and deletion are exposed. `prUrl` serves as a read filter (`cursor_list_agents`), not for creation. `mcpServers`, `customSubagents`, `POST /v1/sub-tokens`, environment writes and secrets remain out of this MCP server.
- `startingRef` is ignored by Cursor when `prUrl` is supplied. This MCP server does not send `prUrl`.
- A full SHA is sent as is in `startingRef` since the real test of October 19, 2026 (it was refused locally after the October 1 observation). The format check proves neither that the branch nor that the commit exists.
- A follow-up run requires `workOnCurrentBranch` explicitly `false` and a known agent status. The schema allows this field to be absent; this MCP server treats its absence as a refusal, not as an authorization. A read, on the contrary, keeps an unknown state as is.
- A cut after a mutation is sent — headers received or not, during the reading, decoding or closing of the body — yields `MUTATION_OUTCOME_UNKNOWN` with the known identifiers, HTTP status and request id. No mutation is replayed. A close error after a complete body (according to `Content-Length`) does not overwrite the result.
- No schema field chooses the CPU, RAM or GPU size of a Cursor VM. `env.type` `pool` or `machine` targets a self-hosted worker.
- `CreateRunRequest` has no `model` field in the consulted OpenAPI, but the real API accepts and validates it (test of October 5, 2026 below). This MCP server passes it when `model_id` is supplied.

## Support matrix

Re-read on October 5, 2026 against the code of the official Python SDK `cursor-sdk` 1.0.36 and its Node bridge. For a cloud agent, the bridge calls the same REST v1 (`https://api.cursor.com`, `CloudApiClient` client) as this MCP server: the "SDK → REST" column shows what the SDK actually sends to this REST API, even when the September 30 OpenAPI does not document it. "Bridge only" means no REST equivalent.

| Capability | This MCP server | September 30 OpenAPI | SDK → REST | Verified live |
|---|---|---|---|---|
| Creation, follow-up run, cancellation, archiving, deletion | yes | yes | yes | yes (October 1 and 5, 2026) |
| `startingRef` as a branch name | yes | yes | yes | yes |
| `startingRef` as a full SHA | yes | announced | announced | refused (400) on October 1, 2026; accepted (201) on October 19, 2026 |
| `prUrl` filter of `GET /v1/agents` | yes (`pr_url`) | yes | yes | yes (October 5, 2026) |
| Raw / charged cost (`cost`) | yes (`cursor_get_usage`) | no | yes | yes (October 5, 2026) |
| Run error (`Run.error`) | yes, if present | no | yes | never observed: absent from the `ERROR` runs re-read on October 5, 2026 |
| `helpUrl`, `provider` of errors | yes | yes | yes | no |
| Images in the prompt | yes (`images`) | yes | yes | no |
| Projects (`GET /v1/projects`, one Project, its agents) | yes, read-only (`cursor_list_projects`) | November 16 spec | not read | list, one Project and its coordinator as an agent: yes (November 16, 2026, both key types); the agents under a Project and a coordinator's stream: no |
| Projects, creation, rename, archive | no, on purpose | November 16 spec | not read | no |
| Remote MCP servers (`mcpServers`) | no | yes | yes | no |
| Custom subagents | no | yes | yes | no |
| Idempotency key (`Idempotency-Key`, creation and send) | no (`agentId` fixed before sending instead) | no | yes (uuid4 per creation) | **ignored** (October 5, 2026, see below) |
| Model per send (`model` on `POST .../runs`) | yes (`model_id` of `cursor_create_run`) | no | yes | **yes, and persistent** (October 5, 2026, see below) |
| Environment variables limited to one run | yes (`env_vars`, `forward_env` of `cursor_create_run`) | in the November 9, 2026 spec | yes | no |
| Agent metadata (`metadata`) | no | no | yes | no |
| Conversation of an agent | yes (`cursor_read_conversation`, legacy v0 endpoint) | no | yes, rebuilt client-side from `interaction_update` | v0 answered for a v1 agent (October 19, 2026) |
| Observation with resume by ordinal (`observe`) | no | no | bridge only | no |
| Liveness of a running run | yes (`last_event_at` from event ids) | no (`updatedAt` frozen) | no | yes (October 6, 2026) |
| Reading the end of a stream | yes (full replay walk) | no | no | no server-side tail (October 6, 2026) |
| Message to a run in progress | yes (`delivery="steer"`), and `replace_active`: cancel, then follow up | in the November 9, 2026 spec (`delivery: "steer"`, 202) | no in 1.0.36 (`steer` reverted to a follow-up for cloud agents) | body validation only (November 10, 2026): a steer with `mode` answered `400`, `"mode" is not allowed with delivery "steer"` |

### `Idempotency-Key`: real test of October 5, 2026

Authorized paid test, `composer-2.5`, header exactly as the SDK sends it (`Idempotency-Key: <uuid4>`), direct REST v1:

- Creation with `envVars` and without `agentId`, three POSTs with the same key: three `201`, **three distinct agents**. The third POST had a different body: neither refusal nor replay.
- Creation without `envVars`, two POSTs with the same key: two `201`, **two distinct agents**.
- Follow-up run, two POSTs with the same key, back to back: the first creates a run, the second receives `409 agent_busy`, not the same run.
- No response header mentions idempotency. Five agents created, all deleted; total cost re-read: 4.26 cents.

Conclusion: the REST API ignores this header. This MCP server does not send it. The only guard against a duplicate creation remains `agentId`, fixed before sending; with `envVars`, which the API refuses together with `agentId`, an unknown outcome is resolved by `cursor_list_agents(name=...)`. A creation replay therefore remains forbidden after `MUTATION_OUTCOME_UNKNOWN`.

### Model change mid-session: real test of October 5, 2026

Authorized paid test, direct REST v1. An agent with no repository, created with `composer-2.5`, receives the same question three times: name the model it is.

| Run | Send | Response | Clues |
|---|---|---|---|
| 1 | creation, `composer-2.5` | "Composer 2.5" | 48 s; 13,662 input tokens; 0.81 cents |
| — | follow-up run, nonexistent `model.id` | `400 validation_error` "Model '…' is not available or invalid" | the field is read and validated; no run created |
| 2 | follow-up run, `model: {"id": "claude-haiku-4-5"}` | "Claude Haiku 4.5" | 7 s; 18,618 tokens written to cache; 2.45 cents |
| 3 | follow-up run **without** `model` | "Claude Haiku 4.5" | re-reads exactly the 18,618 tokens cached at run 2; 0.28 cents |

Conclusion: `model` on a follow-up run changes the model, and the choice persists for the following runs. Neither the agent, nor the run, nor the stream mentions the active model: the caller must remember the one it chose. Total cost: 3.54 cents, agent deleted.

### Stream replay and supervision: real observations of October 6, 2026

GET requests only, on the account's real runs.

- **`updatedAt` of a `RUNNING` run stays at creation + 1 s**, even after 7 hours of activity (3 runs). It is no liveness signal.
- **Event ids are millisecond timestamps**: `1791250392023-0` came 7 s after the run's creation. `heartbeat` and the first `status` event carry no id.
- **No server-side tail.** A made-up `Last-Event-ID` of the form `<ms>-0` is accepted but returns no past event, 5, 30 or 60 minutes back; without the `-0` suffix, or invalid, it answers `400 invalid_last_event_id` ("Last-Event-ID must refer to an event within the requested run"). The only way to the latest events is the full replay: 2,426 events and 839 KB for a 7-hour run.
- **No end-of-replay marker**: no SSE comment, no `retry:` field, no dedicated event type; `interaction_update` carries only progress types (`token-delta`, `step-started`, `tool-call-completed`…). A replay can pause 1.3 s before its first event and 1.1 s in the middle; under 6 parallel reads, an 851 KB replay took 15 s. On the 6 active runs read in parallel, the first `heartbeat` came 30 to 36 s after connecting, always after the replay, then every 15 s. A replay walk therefore stops only on `result`/`done`, on an event newer than the connection, or on `heartbeat`, never on a silence. An `error` event also stops it, but leaves it incomplete: it proves nothing about the rest of the replay.
- **An `ERROR` run carries no cause**: the only one younger than 24 hours ended its stream with `status: ERROR`, a `result` event without text or error, and `done`, after 4 h 27 min. Its last tool call was an `await` on a background task, still `running`.
- **Background tasks are visible**: a `run_terminal_cmd` launched in the background returns `success.shellId` (the task id), `pid` and the command; `await` returns `success.stillRunning {taskId, runtimeMs, …}` or `success.complete {taskId, runtimeMs, …}`.
- **No run listing across agents**: `GET /v1/agents` returns `latestRunId`, not the run's status.

## October 19, 2026 re-read: what the spec added

The OpenAPI doubled (59 KB to 114 KB). What this server uses is in the sections above; the rest is listed here, with the reason it is left out.

- **Used:** `GET /v1/environments`, `GET /v1/environments/{id}`, `.../builds/active`, `.../builds/{buildId}` (read-only `cursor_list_environments`); `prompt.images` on creation and follow-up; `skipReviewerRequest`; the `X-RateLimit-*` headers.
- **Left out, mutations or secrets:** `POST`/`PATCH`/`DELETE /v1/environments[/{id}]`; environment history and build lists; `GET`/`PUT`/`DELETE` on environment and team secrets, and the `GET /v1/secrets` inventory; `POST /v1/sub-tokens` (agent-scoped service-account keys only). `mcpServers` (stdio or remote, with headers or OAuth) and `customSubagents` (up to 20) on creation, and `mcpServers` on follow-ups.
- **Legacy v0 still documented:** `/v0/private-workers` (workers, summary, pools, pending requests, claims, tokens) for self-hosted machines; the v0 agents page, labeled legacy, "available during the migration window", with webhooks (`webhook{url, secret}`, event `statusChange`, HMAC-SHA256 signature) and `GET /v0/agents/{id}/conversation`. v1 webhooks are "coming soon".
- **Agent statuses:** the enum is `ACTIVE`, `ARCHIVED`; `IDLE`, still observed, stays accepted.
- **Stream event ids:** the spec now calls them opaque ("Do not parse"). `last_event_at` and `idle_seconds` still read them as millisecond timestamps, an observed format; an id that does not match yields no time rather than a wrong one.
- **Still absent:** the model in agent, run and usage responses; `cost` in the usage schema (returned by the real API); `Run.error`.


## November 10, 2026 re-read: what the spec added

The OpenAPI grew from 114 KB to 146 KB. What this server uses is in the sections above; the rest is listed here.

- **Used:** `delivery: "steer"` on `POST /v1/agents/{id}/runs` (`SteerRunRequest`, 202 `SteeredRunResponse` or `QueuedRunResponse`); `envVars` on a follow-up run; the run status `WAITING_FOR_BACKGROUND_WORK`; the `query`, `owner`, `named` and `repo` filters of `GET /v1/environments`; the `CURSOR_SANDBOX` rule on environment variable names.
- **Steer, as specified:** it requires `prompt` and `delivery` and accepts no other top-level field. `steered` may include `runId`, the run that was running when the API got the prompt; `queued` never does. A steer that fails with a server or network error may still have reached the agent, and Cursor treats a retry as a new prompt. Where the account does not have it, a valid steer answers `403 feature_unavailable`; the body is checked first.
- **Key types:** the spec names API keys that start with `grok_`. The secrets endpoints and `POST /v1/environments/{id}/builds` answer them `403 key_not_supported`; none of these is called by this server. See `docs/verification.md` for what a `grok_` key was refused beyond the spec.
- **Left out, as before:** environment writes, build starts, history and build lists; every secrets endpoint; `POST /v1/sub-tokens`; `mcpServers` and `customSubagents`; `prUrl` on a repository entry; the `userId` filter of `GET /v1/environments`.
- **New error codes, all on endpoints left out:** `key_not_supported`, `user_required`, `team_membership_required`, `personal_secrets_disabled`, and the `secret_*` and `repo_file_*` families. A follow-up or steer to a subagent answers `400 validation_error`.
- **Returned but not specified:** `isMaxMode` on model variants; `openAsCursorGithubApp` and `skipReviewerRequest` on agents (ignored); `cost` in usage, still.
- **Still absent:** the model in agent, run and usage responses; `Run.error`; a server-side tail of the stream; v1 webhooks.

## November 16, 2026 re-read: what the spec added

The OpenAPI grew from 146 KB to 166 KB. The November 10 file was found again on disk (SHA-256 checked), so this is a real line diff: 519 lines added and 4 removed, in 8 hunks. Nothing used by the existing tools changed shape.

- **Used:** `GET /v1/projects`, `GET /v1/projects/{id}` and `GET /v1/projects/{id}/agents`, read-only, behind `cursor_list_projects`. `GET /v1/projects` is also read by the agent tools (`cursor_list_agents`, `cursor_get_agent`, `cursor_supervise`): up to five pages of 100, capped at 8 seconds and cached 60 seconds (since 0.7.1 a failure never replaces a list already read), to put `coordinator_of_project` on the row of an agent that is the caller's coordinator in a Project. A marker only; the server refuses nothing.
- **Projects, as specified:** a Project is created with a coordinator agent. The Project's `id` is the owner's coordinator agent id; `coordinatorAgentId` is the caller's own coordinator and is omitted when they joined a Project and their coordinator there was archived or stopped. `POST /v1/projects` enqueues the coordinator's first run. To message it, send a follow-up run to the coordinator (`POST /v1/agents/{id}/runs`); replies come on the stream, where each message the coordinator sends is one whole `assistant` event and `Run.result` is its last message. A user key is required (`403 user_required` for a service-account key, which lists no Project); a team can turn Projects off (`403 projects_disabled`); renaming, archiving and unarchiving are owner-only (`403 owner_required`); an archived Project answers `409 project_archived`. `includeArchived` defaults to `true`. A page can hold fewer than `limit` Projects, or none, while `nextCursor` is still set.
- **Observed on November 16 (free GETs, one `crsr_` and one `grok_` key, same account):** both keys read the same single Project with the spec's fields and nothing else (`appearance`, `coordinatorAgentId`, `createdAt`, `id`, `name`, `status`, `updatedAt`, `url`); `includeArchived` `false` and `true` return the same page; `GET /v1/projects/{id}` is `200`; `GET /v1/projects/{id}/agents` is `200` with 0 agents; an unknown id is `404 project_not_found`. `GET /v1/agents/{id}` with the id of the account's own Project, which it owns, answers `200` (`IDLE`), and `GET /v1/agents` lists the coordinator, so the existing agent tools already see it. A Project the caller joined was not seen: there `id` is the owner's coordinator and `coordinatorAgentId` the caller's own, per the spec. Through the tools, with both key types, `cursor_get_agent` and `cursor_get_run` read the coordinator and its last run; `cursor_read_run_events` on that run answered `410 stream_expired`, as an old run does, so the coordinator stream described by the spec (one whole `assistant` event per message it sends) was not seen. The Project had no agent under it: the rows of `GET /v1/projects/{id}/agents` are specified, not observed. Run `scripts/probe_api.py` to repeat the reads.
- **Spec only, not tried:** `cursor_create_run` on a coordinator is not refused by the local guards (the coordinator is `IDLE`, `workOnCurrentBranch` is `false`) but it would be a paid run. The spec says a Project is `ARCHIVED` when its owner's coordinator was stopped, so `cursor_archive_agent` or `cursor_delete_agent` on a Project's id very likely archives or removes the Project: not tried on the account's only Project.
- **Left out:** `POST /v1/projects`, `PATCH /v1/projects/{id}`, Project archive and unarchive. Creation is paid, cannot be tried while the account answers `QUOTA`, and a coordinator starts agents of its own, outside this server's cost guards. `GET /v1/agents/{id}/stream` is new: one stream across all of an agent's runs, open until the client leaves or the agent is archived, `status` events and no `result`, resumed with `Last-Event-ID` or `lastEventId`, `X-Cursor-Stream-Retention-Seconds`, `410 stream_expired`, `429 too_many_open_streams` (50 per caller by default). Without a cursor it starts where the newest run started, which might help with the missing tail marker; it was not probed.
- **New error codes:** `owner_required`, `projects_disabled`, `project_not_found`, `project_id_conflict`, `project_archived`, `too_many_open_streams`. They land in existing classes by status (`403` `PERMISSION`, `404` `NOT_FOUND`, `409` `CONFLICT`, `429` `QUOTA`) and the remote code stays in `remote_code`.
- **Not documented anywhere in the spec:** why `POST /v1/agents` answers `usage_limit_exceeded`, or whether API-created agents are billed differently from app-created ones. The code is only listed among the possible values, with no endpoint attached.

## November 25, 2026 re-read: what the spec and the SDK added

The OpenAPI grew from 166 KB to 169 KB (etag `50837eb2…`, modified 08:49 GMT). The November 16 file was still on disk: 99 diff lines, 3 hunks, all on `POST /v1/projects`.

- **Spec, left out:** `POST /v1/projects` takes `files` (beta): up to 50 files, 256 KiB each, 2 MiB in total, written to the Project's shared context before the coordinator's first run, as `content` (UTF-8) or `contentBase64`, with an optional `mimeType`. Not combinable with a client-supplied `projectId`. The response lists the written files (`path`, `sizeBytes`, `etag`). New errors: `400 files_not_supported` (beta not enabled on the account), `403 storage_not_allowed` (privacy settings forbid storing data). This server does not wire Project creation, so nothing changes in the tools.
- **SDK, used:** the official Python `cursor-sdk` 1.5.0 (November 24) calls the same REST paths as 1.4.2 and sends a header the spec does not list on `GET /v1/agents/{id}/runs/{runId}/stream`: `x-cursor-sdk-shell-tool-call-delta: true`. With it, Cursor adds an `interaction_update` of type `shell-tool-call-delta` (`callId`, `modelCallId`, `stream` `stdout` or `stderr`, `content`) for each chunk a shell command prints. Checked live on November 25: the same finished run replayed with the header carried 16 of them for one command (8 `stdout`, 8 `stderr`), without it none, every other event identical. Behind `cursor_read_run_events(include_shell_output=true)`.
