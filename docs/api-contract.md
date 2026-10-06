# API contract used

Consulted on September 30, 2026, re-read on October 1, 2026. The raw file's fingerprint is unchanged.

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
| `POST /v1/agents/{id}/runs` | 201 `CreateRunResponse` | `cursor_create_run` |
| `POST /v1/agents/{id}/runs/{runId}/cancel` | 200 `IdResponse` | `cursor_cancel_run` |
| `GET /v1/agents/{id}/runs/{runId}/stream` | 200 `text/event-stream` | `cursor_read_run_events`, `cursor_get_run(activity)`, `cursor_supervise(activity)` |
| `GET /v1/agents/{id}/usage` | 200 `AgentUsageResponse` | `cursor_get_usage` |
| `GET /v1/agents/{id}/artifacts` | 200 `ListArtifactsResponse` | `cursor_list_artifacts` |
| `GET /v1/agents/{id}/artifacts/download` | 200 `DownloadArtifactResponse` | `cursor_read_artifact` |
| `POST /v1/agents/{id}/archive` | 200 `IdResponse` | `cursor_archive_agent` |
| `POST /v1/agents/{id}/unarchive` | 200 `IdResponse` | `cursor_archive_agent` with `unarchive=true` |
| `DELETE /v1/agents/{id}` | 200 `IdResponse` | `cursor_delete_agent` |

## Fields sent

`POST /v1/agents`, only the keys provided, never `null`:

- `prompt.text`: required, non-empty
- `repos`: zero to twenty elements `{ "url", "startingRef" }`. Absent if there is no repository. `startingRef` is a branch name. The OpenAPI schema types it as `string`, but a real trial on October 1, 2026 received `400 validation_error` for a full 40-character SHA, and `201` for the branch name whose head was that SHA
- `workOnCurrentBranch`: always `false`
- `autoCreatePR`: boolean, `false` unless the caller asks for `true`
- `agentId`: `bc-` followed by a UUID, supplied or generated once before sending. Absent when `envVars` is sent: the API forbids both together
- `name`, `model` (`id` and optionally `params[{id,value}]`), `mode` (`agent` or `plan`): only if provided
- `env`: `{ "type": "cloud" | "pool" | "machine", "name"? }` only if provided
- `envVars`: object of strings, at most 50, only if provided. Names do not start with `CURSOR_`

`model.id` is a catalog id; an alias is accepted only if it designates a single model, and it is sent as that id. `model.params` is checked against `GET /v1/models` before sending: each value, then the combination, which must fit in at least one published variant (the real catalog of October 5, 2026 omits 5 combinations out of 20 for `gpt-5.5`). `reasoning_level` is placed in the first parameter present among `effort`, `reasoning_effort` and `reasoning`.

`POST /v1/agents/{id}/runs`: `prompt.text`, and `mode` and `model` (same shape and same check as for creation) only if provided.

`GET /v1/agents` and `GET /v1/agents/{id}/runs`: `limit` (1 to 100) and `cursor` only if provided. `GET /v1/agents` adds `includeArchived` and `prUrl` only if provided. The API refuses any other filter (`400`, "Unrecognized key(s)", verified on October 5, 2026 for `name`, `q`, `search`, `status`, `sort`): the name search of `cursor_list_agents` therefore walks at most five pages of one hundred and filters locally.

`GET /v1/agents/{id}/runs/{runId}/stream`: `Last-Event-ID` header only if provided. Reading stops on `done`, `result` or `error`, at the local deadline, or at 1 MB. `error` is a stream error, returned as `stream_error`: only `result` and `done` mark `finished`. UTF-8 decoding is incremental, so that a character split between two network chunks stays intact. `heartbeat` and `interaction_update` are not returned to the caller. Consecutive `assistant` (and `thinking`) fragments, sent word by word by the API, are merged into one event of at most 4000 characters, which carries the identifier of the last fragment. The `tail` mode and the activity summary send no `Last-Event-ID`: they walk the whole replay (see "Stream replay and supervision" below).

`GET /v1/agents/{id}/artifacts/download`: `path`, relative, `artifacts/` prefix, no `..`.

`GET /v1/agents/{id}/usage`: `runId` only if provided.

## Fields read

- Account: `apiKeyName`, `createdAt`, and if present `userId`, `userEmail`, `userFirstName`, `userLastName`. No key secret.
- Models: `items[]` with `id`, `displayName`, and if present `description`, `aliases`, `parameters`, `variants`. Variants are specific to each model: they are the combinations it accepts. On October 5, 2026, they covered all combinations for most models of the catalog; `gpt-5.5`, `gpt-5.4` and `claude-opus-5` excluded some, and some carried an unpublished parameter (`cyber`), ignored here. They serve the combination check, model by model, and `defaults`, and are returned only with `model_id`.
- Repositories: `items[].url`. No cursor in this schema.
- Agents, page: `items[]` (`id`, `status`, `env`, `url`, `createdAt`, `updatedAt`, optional `name` and `latestRunId`) and `nextCursor` if present. Its absence means end of list, not a `null` value.
- Agent: the page's fields, plus `repos`, `workOnCurrentBranch`, `autoCreatePR` when present. An absence stays an absence.
- Runs, page: `items[]` of the `Run` schema and `nextCursor` under the same rule.
- Run: `id`, `agentId`, `status`, `createdAt`, `updatedAt`, and if present `durationMs`, `result`, `error` (free form; `code: message` if it is an object), `git.branches[]` (`repoUrl`, `branch`, `prUrl`). `git` is the agent's current state, not an immutable snapshot of the run. `repoUrl` is returned without a scheme. No `final_sha` is invented.
- Cancellation, archiving, unarchiving, deletion: `id`. It is compared to the requested identifier when present.
- Usage: `totalUsage` and `runs[].usage` with `inputTokens`, `outputTokens`, `cacheWriteTokens`, `cacheReadTokens`, `totalTokens`. `usageUuid` if present. `cost` and `runs[].cost` (`rawCostCents`, `chargedCents`) if present: absent from the September 30 OpenAPI, but returned by the real API on October 5, 2026 and typed by the Cursor SDK. Rendered in cents, rounded to 4 decimals, never estimated.
- Artifacts: `items[]` with `path`, `sizeBytes`, `updatedAt`. The download returns `url` and `expiresAt`. The presigned URL is followed only if it is HTTPS and the host ends with `.amazonaws.com`, with no redirect and no `Authorization` header.
- Stream: events `status`, `assistant`, `tool_call`, `result`, `error`, `done`, and `thinking` only on request. The `X-Cursor-Stream-Retention-Seconds` header is kept if present. For the activity summary: event ids read as millisecond timestamps (`<ms>-<sequence>`, otherwise ignored), `run_terminal_cmd` calls with `args.isBackground` or `result.isBackground` and their `result.success.shellId`, and `await` results `success.stillRunning` / `success.complete` (`taskId`, `runtimeMs`). `heartbeat` ends a replay walk.

Known run states: `CREATING`, `RUNNING`, `FINISHED`, `ERROR`, `CANCELLED`, `EXPIRED`. The last four are terminal. Any other state is kept and is not a success.

Agent states: the endpoints page describes `ACTIVE`, `IDLE` and `ARCHIVED`. The OpenAPI enumerates only `ACTIVE` and `ARCHIVED`. `IDLE` is therefore accepted and displayed. An unknown state stays visible.

Created `agentId`: `^bc-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$`.

## Remote errors

Documented body: `{ "error": { "code", "message", "helpUrl"?, "provider"? } }`. `helpUrl` (HTTPS only) and `provider` are rendered in `help_url` and `provider` when present, for example for `integration_not_connected`. `invalid_last_event_id` carries a resume instruction without a cursor.

Codes cited by the schema: `unauthorized`, `api_key_not_found`, `plan_required`, `role_forbidden`, `feature_unavailable`, `integration_not_connected`, `validation_error`, `missing_body`, `invalid_model`, `invalid_branch_name`, `repository_required`, `repository_access`, `pr_resolution_failed`, `artifact_not_found`, `service_account_required`, `agent_not_found`, `run_not_found`, `agent_busy`, `agent_archived`, `agent_id_conflict`, `run_not_cancellable`, `rate_limit_exceeded`, `usage_limit_exceeded`, `stream_expired`, `stream_unavailable`, `invalid_last_event_id`, `client_cancelled`, `not_implemented`, `upstream_error`, `internal_error`.

Statuses used for classification: 400 validation, 401 authentication, 403 permissions (`feature_unavailable` included), 404 missing resource, 409 conflict, 410 expired stream, 429 quota. The 429 may carry `Retry-After`. Without this header, a single GET retry waits one second, within the deadline. The OpenAPI also mentions `X-RateLimit-Limit`, `X-RateLimit-Remaining` and `X-RateLimit-Reset`. No request identifier is specified: the `x-request-id`, `request-id` and `x-cursor-request-id` headers are kept only if present.

`GET /v1/agents/{id}/usage` may answer `403 feature_unavailable` (early access).

`GET /v1/repositories` is limited to 1 request per user per minute, and 30 per hour. The page states that the response may take tens of seconds.

## Accepted adjustments

- Creations are specified as `201`. A `200` with the same JSON is accepted, because success is judged on the schema, not on a neighboring status.
- Per-tool budget: a single absolute deadline per MCP call, passed as remaining time to each request, re-read and pause (45 seconds by default, 95 for creation, follow-up runs and the repository list, 45 for cancellation, `max_wait_seconds` for the stream, `wait_seconds` (at least 45) for waiting on a run). A mutation is not sent if less than 5 seconds remain, or less than half of its own timeout.
- Per-request deadline, within this budget: 40 seconds per call, 90 seconds for `POST /v1/agents` and `POST /v1/agents/{id}/runs` (a real creation exceeded 40 seconds before succeeding), 90 seconds for `GET /v1/repositories`, because the official contract warns that this call can take tens of seconds. `cursor_get_run` with `wait_seconds` chains reads within a deadline of at most 60 seconds. `cursor_read_run_events` bounds its wait to 50 seconds.
- `IDLE` is not in the agent's OpenAPI enum, but the endpoints page defines it. It is not rejected.
- The SSE stream is read once, with no internal reconnection. `410 stream_expired` becomes `STREAM_EXPIRED`.
- Artifacts, archiving, unarchiving and deletion are exposed. `prUrl` serves as a read filter (`cursor_list_agents`), not for creation. Images, `mcpServers`, `customSubagents` and `POST /v1/sub-tokens` remain out of this MCP server.
- `startingRef` is ignored by Cursor when `prUrl` is supplied. This MCP server does not send `prUrl`.
- A full SHA is not sent in `startingRef`. The caller pushes the commit to a branch and passes that branch's name in `starting_ref` (the old `starting_sha` alias was removed on October 5, 2026). This format check does not prove that the branch exists. It is a workaround dated from the October 1, 2026 observation: the REST documentation and the SDK v1.0.36 bridge say a reference may include a SHA. It will be removed only after an explicitly authorized real test.
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
| `startingRef` as a full SHA | refused locally | announced | announced | refused (400) on October 1, 2026 |
| `prUrl` filter of `GET /v1/agents` | yes (`pr_url`) | yes | yes | yes (October 5, 2026) |
| Raw / charged cost (`cost`) | yes (`cursor_get_usage`) | no | yes | yes (October 5, 2026) |
| Run error (`Run.error`) | yes, if present | no | yes | never observed: absent from the `ERROR` runs re-read on October 5, 2026 |
| `helpUrl`, `provider` of errors | yes | yes | yes | no |
| Images in the prompt | no | yes | yes | no |
| Remote MCP servers (`mcpServers`) | no | yes | yes | no |
| Custom subagents | no | yes | yes | no |
| Idempotency key (`Idempotency-Key`, creation and send) | no (`agentId` fixed before sending instead) | no | yes (uuid4 per creation) | **ignored** (October 5, 2026, see below) |
| Model per send (`model` on `POST .../runs`) | yes (`model_id` of `cursor_create_run`) | no | yes | **yes, and persistent** (October 5, 2026, see below) |
| Environment variables limited to one run | no | no | yes | no |
| Agent metadata (`metadata`) | no | no | yes | no |
| Conversation of a run | no (SSE stream and `cursor_get_run`) | no | yes, rebuilt client-side from `interaction_update` | no |
| Observation with resume by ordinal (`observe`) | no | no | bridge only | no |
| Liveness of a running run | yes (`last_event_at` from event ids) | no (`updatedAt` frozen) | no | yes (October 6, 2026) |
| Reading the end of a stream | yes (full replay walk) | no | no | no server-side tail (October 6, 2026) |
| Message to a run in progress | no (`replace_active`: cancel, then follow up) | no | no (`steer` reverts to a follow-up for cloud agents) | `agent_busy` |

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
- **No end-of-replay marker**: no SSE comment, no `retry:` field, no dedicated event type; `interaction_update` carries only progress types (`token-delta`, `step-started`, `tool-call-completed`…). A replay can pause 1.3 s before its first event and 1.1 s in the middle; under 6 parallel reads, an 851 KB replay took 15 s. On the 6 active runs read in parallel, the first `heartbeat` came 30 to 36 s after connecting, always after the replay, then every 15 s. A replay walk therefore stops only on `result`/`done`, on an event newer than the connection, or on `heartbeat`, never on a silence.
- **An `ERROR` run carries no cause**: the only one younger than 24 hours ended its stream with `status: ERROR`, a `result` event without text or error, and `done`, after 4 h 27 min. Its last tool call was an `await` on a background task, still `running`.
- **Background tasks are visible**: a `run_terminal_cmd` launched in the background returns `success.shellId` (the task id), `pid` and the command; `await` returns `success.stillRunning {taskId, runtimeMs, …}` or `success.complete {taskId, runtimeMs, …}`.
- **No run listing across agents**: `GET /v1/agents` returns `latestRunId`, not the run's status.
