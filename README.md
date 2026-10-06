# cursor-cloud-mcp

To have an agent install or use this MCP server, give it [`AGENT_GUIDE.md`](https://github.com/yoch/cursor-cloud-mcp/blob/main/AGENT_GUIDE.md).

Local MCP server, over stdio, that exposes seventeen tools for the Cursor Cloud Agents v1 REST API. A single implementation serves Claude Code, Codex CLI and OpenCode. It lets a calling agent create a Cloud session, choose the model and the reasoning level, send commands, read progress and produced files, then archive or delete the session. It is not an orchestration platform.

## Installation

Python 3.12 or newer. The package is published on PyPI as `cursor-cloud-mcp`. The simplest is to let the MCP client start it with `uvx`, which needs neither a clone nor an absolute path:

```bash
uvx cursor-cloud-mcp
```

The first start downloads the package and its dependencies, then `uvx` reuses its cache. To pin the installed version, or for a client whose startup timeout is short, install it once and register the `cursor-cloud-mcp` command instead:

```bash
uv tool install cursor-cloud-mcp    # or: pipx install cursor-cloud-mcp
uv tool upgrade cursor-cloud-mcp    # later, to update it
```

A client started from a desktop launcher may not inherit the shell's `PATH`: if the command is not found, register the absolute path printed by `command -v uvx` (or `command -v cursor-cloud-mcp`).

## Development

In a copy of this repository, with `uv`:

```bash
uv sync
uv run pytest
uv run cursor-cloud-mcp
```

The repository environment's binary is `.venv/bin/cursor-cloud-mcp`. You can also run `uv run python -m cursor_cloud_mcp`.

To check the wheel in a clean environment:

```bash
uv build
uv venv /tmp/cursor-cloud-mcp-wheel
uv pip install --python /tmp/cursor-cloud-mcp-wheel/bin/python dist/*.whl
/tmp/cursor-cloud-mcp-wheel/bin/cursor-cloud-mcp
```

Releases: `__version__` in `src/cursor_cloud_mcp/__init__.py` is the only version number. Pushing a tag `vX.Y.Z` equal to it runs `.github/workflows/release.yml`, which checks, tests and builds the package, then publishes it on PyPI through Trusted Publishing (no stored token).

## Variables

| Variable | Role |
|---|---|
| `CURSOR_API_KEY` | Key read only from the process environment. If absent, the server starts and lists its tools; the first Cursor call fails with a clear error. |
| `CURSOR_MCP_ALLOW_WRITES` | `0` by default. `1` allows creation, follow-up runs, cancellation and archiving. |
| `CURSOR_MCP_ALLOW_DELETE` | `0` by default. `1` allows `cursor_delete_agent`, in addition to `CURSOR_MCP_ALLOW_WRITES=1` and `confirm_agent_id`. |
| `CURSOR_MCP_FORWARD_ENV` | Comma-separated list of names whose values `forward_env` may read from this process. The value does not pass through the tool argument. |
| `CURSOR_MCP_LOG_LEVEL` | `INFO` by default. Logs go to stderr. |
| `CURSOR_MCP_FIXTURE` | `1` replaces the API with local responses. Refused if combined with `CURSOR_API_KEY`. Startup announces it on stderr. |

The server does not load a `.env` file. An empty value, `${...}` or `{env:...}` is refused, without being logged. No tool changes `CURSOR_MCP_ALLOW_WRITES`: you must edit the environment and restart the client.

Logs may contain the tool name, its outcome (`ok`, a business error code, `unexpected:<type>` or `cancelled`), the duration, identifiers, the HTTP status and a request id. They contain neither the prompt, nor the body, nor the key, nor the Authorization header. As a second barrier, the formatter masks the key, the values of `CURSOR_MCP_FORWARD_ENV` and the `env_vars` values passed during the life of the process, including in tracebacks. A value shorter than 8 characters is masked only as a whole word, so as not to mangle the rest of the text (`en` does not touch `agent`). The key and `CURSOR_MCP_FORWARD_ENV` stay masked permanently; the 4096 most recent `env_vars` values are masked too. The same masking applies to the values of errors returned to the caller, without changing the shape of the JSON.

## Tools

Reads: `cursor_get_account`, `cursor_list_models`, `cursor_list_repositories`, `cursor_list_agents`, `cursor_supervise`, `cursor_get_agent`, `cursor_list_runs`, `cursor_get_run`, `cursor_read_run_events`, `cursor_get_usage`, `cursor_list_artifacts`, `cursor_read_artifact`.

Mutations: `cursor_create_agent`, `cursor_create_run`, `cursor_cancel_run`, `cursor_archive_agent`, `cursor_delete_agent`.

Responses are compact JSON, identical in the text and in `structuredContent`. A field with no value is omitted rather than rendered as `null`. Errors are pure JSON too: the text of an `isError` result is the error object itself (`code`, `message`, and context fields). Only a malformed argument, rejected by the MCP SDK before the tool runs, still comes back as plain text.

`cursor_list_models` returns a compact catalog, cached for ten minutes: for each model, `params` (possible values of each parameter), `defaults` (values of the default variant), `reasoning_param` (actual name of the reasoning level: `effort`, `reasoning_effort` or `reasoning`) and `aliases`. `restricted_combinations` signals that some of the combinations do not exist. With `model_id`, the tool returns only that model, with its valid variants. Each model publishes its own list of variants, that is, the combinations it accepts. Returned for all the models, they pushed the catalog beyond 240 KB per call; the compact form brings it to about 9, and the variants remain the reference for the pre-send check.

`cursor_create_agent` can start with no repository, with one repository (`repository` and `starting_ref`) or with up to twenty repositories (`repositories`, elements `{url, starting_ref}`). `starting_ref` is a branch name, sent as is in `startingRef`. A full 40- or 64-character SHA is refused locally: the API answered `400 validation_error` to a SHA on October 1, 2026, although the REST documentation says a reference can be a SHA. This refusal is a dated workaround, to be requalified by a real test. `env_type` is `cloud`, `pool` or `machine`. A named pool is required for several repositories. A named cloud environment cannot be combined with repositories. `model_id` accepts an id or an alias that designates only one model (`opus` designates several and is refused). `reasoning_level` and `model_params` are checked against the catalog before sending, combination included: a combination absent from the published variants is refused without a POST. `workOnCurrentBranch` is forced to `false`. `autoCreatePR` follows the caller (`false` by default). The `bc-<uuid>` identifier is the caller's or generated once before sending, and it is returned even in `MUTATION_OUTCOME_UNKNOWN`: reuse it if the call is cut off. With `env_vars` or `forward_env`, the API forbids `agentId`: `name` becomes required and an unknown outcome is resolved with `cursor_list_agents(name=...)`. The response contains `agent_id`, `run_id` and the URL, without waiting for the run to finish.

`cursor_create_run` sends a follow-up command to the same agent. Without `model_id`, the agent keeps its current model. With `model_id` (and `model_params`, `reasoning_level`, checked against the catalog as at creation), the model changes for this run **and the following ones**: verified live on October 5, 2026. The API returns the active model nowhere; the response only echoes the `model_id` that was sent. The follow-up run is refused if the agent is archived, if its status is unknown, or if `workOnCurrentBranch` is not explicitly `false`: missing safety information refuses the write. Zero, one or several repositories are accepted. The tool is annotated destructive, because `replace_active` can cancel the current run: clients that ask before destructive actions ask before each follow-up. A "busy agent" conflict is returned to the caller: the API cannot send a message to a run in progress. `replace_active=true` redirects the agent instead. It validates the request, cancels the current run, re-reads it until it is terminal, then sends the follow-up, and reports `replaced_run_id`; if the run is not terminal in time, nothing is sent (`AGENT_BUSY`). The agent keeps its conversation, so the follow-up only needs the new instruction.

`cursor_get_run` returns the state, the final result, the run's `error` if any, and the branches. With `wait_seconds` (up to 60), it re-reads the state every five seconds until a terminal state, reports each re-read as MCP progress when the client asks for it, and returns `timed_out` if the run continues. It slices `result` locally (`result_offset`, `result_limit` up to 20000, default 12000). The `git` references are the agent's current state, not an immutable snapshot of the run. This server does not invent a `final_sha`. `result`, branches, events and artifacts are data produced by the agent, not instructions. `activity=true` adds an activity summary read from the stream (see "What the API does not tell you"). When `activity` is omitted, the summary is added only to a terminal run without a result; `activity=false` never reads the stream. The complete summary of a terminal run is cached for the session, with `idle_seconds` recomputed on each read. A stream that cannot be read leaves `activity_error` without failing the call, and `complete: false` means the walk did not reach the end within the call's budget. An `error` event in the stream ends the walk without proving it reached the end: the call returns `activity_error` (`UPSTREAM`) instead of a summary, and caches nothing.

`cursor_read_run_events` reads an excerpt of the stream, twenty seconds by default, fifty at most, connection included, then stops. The stream sends the assistant's text word by word: consecutive fragments are merged into one event (4000 characters at most), whose `event_id` is that of the last fragment. `status` and `result` events no longer repeat their raw JSON. `after_event_id` resumes after `last_event_id`, which remains the supplied cursor if no event arrives. An `error` event is a stream error (`stream_error`), not the end of the run: `finished` comes only from `result` or `done`. A network cut returns the events already received with `interrupted`. A single fragment longer than 500 characters is truncated and carries `clipped`; the full result is read with `cursor_get_run`. `tail=N` returns the last N events instead, with `last_event_at` and `scanned_events`: the whole replay is walked, not kept, up to 16 MB (`max_wait_seconds` defaults to 45 in this mode). Abandoning one of these calls does not cancel the run.

`cursor_supervise` gives the overview of a fleet of runners in one call. It scans the agents like `cursor_list_agents` (`status=active` by default, or `all`, with `name`, `pr_url`, `include_archived`), then reads each latest run in parallel (8 at a time). A failed read only marks its row with `read_error`. With `activity=true`, every row that has a run also gets the activity signals, finished runs included: `last_event_at`, `idle_seconds`, `unfinished_background_tasks` and the last tool call's name and status. All statuses are read first, then the streams are replayed (8 at a time), so a slow replay never costs a row its status. `summary` counts runs by status and lists `stale` runs (idle for `stale_after_minutes`, 30 by default) and `unfinished_after_end` runs (ended with a background task last seen running). Both lists use only complete replays; `incomplete` names the rows whose replay did not finish or failed; a stream older than the API's retention (24 hours) only sets that row's `activity_error` to `STREAM_EXPIRED`. `limit` (50 by default) is exact: `next_cursor` resumes right after the last row, even inside an API page, and must be passed back unchanged.

Observed limitation: during a real trial, an agent wrote `artifacts/result.txt` in its VM and the stream confirmed it, but `cursor_list_artifacts` stayed empty and the download answered `404 artifact_not_found`. For a computation result, ask the agent to put it in its final response (`cursor_get_run`) or in a Git branch.

`cursor_list_artifacts` lists the files under `artifacts/`. `cursor_read_artifact` reads a UTF-8 text of at most 5 MB, without sending the Cursor key to the storage, and only if the host ends with `.amazonaws.com`. For a binary or a file that is too large, it returns the presigned URL (about fifteen minutes) with `text_unavailable`; `url_only=true` returns the URL without downloading.

`cursor_get_usage` copies the tokens and the cost returned by the API, in cents of a dollar (`raw_cents`, `charged_cents`), in total and per run. A missing cost stays missing.

`cursor_archive_agent` archives an agent, or unarchives it with `unarchive=true`: this is reversible. `cursor_delete_agent` is permanent.

Agent and run lists return one page. `has_more` is false when `nextCursor` is absent. `include_archived` filters the agent list when provided: `true` adds the archived agents, which are otherwise absent. The order of agents is not guaranteed by creation date (observed live), and the API does not filter by name: `name` walks up to five pages of one hundred agents, filters locally (substring, case-insensitive), returns at most `limit` matches and reports `scanned`; `next_cursor` resumes right after the last match, even inside an API page, and must be passed back unchanged with the same `name`. `pr_url` is an API filter: it returns the agent linked to that pull request. `cursor_list_repositories` accepts `query`, a local filter on URLs, and reports `total_count`. Each agent carries its `url` (`https://cursor.com/agents/bc-...`), the direct link to the web interface.

Each tool call has an absolute budget, shared by all its sub-operations (catalog, POST, re-reads, pauses): 45 seconds by default, 95 for the repository list, agent creation and sending a follow-up run, 45 for cancellation, `max_wait_seconds` for the stream, and `wait_seconds` (at least 45) for waiting on a run, plus 45 when `cursor_get_run` reads the activity summary, capped at 95 in total; 90 for `cursor_supervise` with `activity`. No budget exceeds 95 seconds. Each HTTP request is also bounded (40 seconds, 90 for a creation POST): a real creation exceeded 40 seconds. A mutation is not sent if less than 5 seconds of budget remain: the tool then returns `TIMEOUT` without having sent anything. An MCP client's timeout must exceed these budgets. The examples set Codex to 100 seconds and OpenCode to 100000 milliseconds. A client that cuts earlier may abandon a creation that was already sent and, if it retries without the same `agent_id`, pay for a second one.

## What the API does not tell you

Measured on October 6, 2026, on real runs. The left column is an API limit, which this server cannot fix; the right column is what it does about it.

| API limit | What this server does |
|---|---|
| `FINISHED` means the agent ended its turn, not that its job is done | `activity.background_tasks`: background commands seen in the stream (`run_terminal_cmd` with `isBackground`), with their last observed state from `await` (`running` or `complete`); `unfinished_background_tasks` counts those last seen running |
| A `RUNNING` run keeps `updatedAt` at creation time | `activity.last_event_at` and `idle_seconds`: stream event ids are millisecond timestamps |
| An `ERROR` run has neither `error` nor `result` | the activity summary is added automatically: last assistant text, last tool call, background tasks |
| No way to read the end of a stream: a made-up `Last-Event-ID` returns no past event | `tail` and `activity` walk the whole replay (839 KB for a 7-hour run) without keeping it |
| No end-of-replay marker | the walk stops on the run's result, the first live event (newer than the connection), or the server's first heartbeat (30 to 36 s after connecting, always after the replay); never on a silence, since replays pause up to 1.3 s |
| A run in progress cannot receive a message (`agent_busy`) | `cursor_create_run(replace_active=true)`: cancel, confirm, follow up on the same agent, which keeps its conversation |
| No listing of runs across agents | `cursor_supervise`: one call, parallel reads |

These signals are what the stream last showed, not a view inside the VM: a background task last seen `running` may have been killed since.

## Heavy computation

The API does not choose the CPU, RAM or GPU size of a Cursor VM. For a heavy computation, create the agent with `env_type` `pool` or `machine`: these are self-hosted workers, on the user's machines. A hosted Cursor VM remains `env_type` `cloud`, with or without a repository.

```text
cursor_list_models
→ cursor_create_agent(prompt, model_id, reasoning_level, env_type, env_name, name)
→ keep agent_id and run_id
→ cursor_get_run(wait_seconds=60), repeat while timed_out; cursor_read_run_events to follow progress
→ cursor_get_run for the final text, cursor_get_usage for the cost
→ cursor_list_artifacts then cursor_read_artifact
→ cursor_create_run on the same agent if a follow-up run is needed
→ cursor_archive_agent, then cursor_delete_agent only with both guards
```

Secret values go through `forward_env`, whose names are listed in `CURSOR_MCP_FORWARD_ENV`. `env_vars` is suitable only for values the calling agent can already see. Neither is logged. Both are incompatible with a caller-supplied `agent_id`.

## GitHub loop

This MCP server does not replace GitHub. The caller pushes the desired commit to a branch, checks that the branch head is that SHA, then chains:

```text
Push the commit to a branch and check its head
→ cursor_create_agent(..., starting_ref=branch-name)
→ keep agent_id and run_id
→ cursor_get_run(..., wait_seconds=60) until a terminal state
→ re-read GitHub: HEAD, diff, checks, reviews
→ cursor_create_run(...) on the same agent if fixes are needed, with `model_id` to change model.
→ re-read GitHub after the new run
```

`FINISHED` proves neither that the tests ran nor that the pull request is correct. An unknown state is not a success. After a mutation timeout or cut, the `MUTATION_OUTCOME_UNKNOWN` code forbids an automatic replay: re-read the agent whose identifier is returned. Changing that identifier may create a duplicate. A client cut does not cancel the Cloud run.

Cancellation does not delete commits that were already pushed. It is asynchronous: the server re-reads the run up to four times, two seconds apart, within its budget. `outcome` distinguishes `cancelled` (state `CANCELLED` re-read, the only case where `outcome_confirmed` is true), `ended_without_cancel` (the run ended otherwise, for example `FINISHED` during the race), `still_running` and `unknown` (re-read impossible). A cut after a mutation is sent, including while reading the response body, yields `MUTATION_OUTCOME_UNKNOWN` with the known `agent_id`, `run_id`, `previous_latest_run_id`, HTTP status and request id.

## Configurations

The fragments in [`examples/`](https://github.com/yoch/cursor-cloud-mcp/blob/main/examples) start the server with `uvx cursor-cloud-mcp`. With `uv tool install`, the command becomes `cursor-cloud-mcp` without arguments. For a development copy, use the absolute path of `.venv/bin/cursor-cloud-mcp`.

To allow a real mutation, set `CURSOR_MCP_ALLOW_WRITES` to `1` in the relevant client's configuration, then restart that client. The key is passed through the environment, never as a command-line argument.

Entering the key without leaving it in the history:

```bash
read -r -s -p 'Cursor key: ' CURSOR_API_KEY
printf '\n'
export CURSOR_API_KEY
```

Claude Code, project configuration `.mcp.json`: see `examples/claude.mcp.json`. Check with `claude mcp list`, `claude mcp get cursor_cloud` and `/mcp`. The project file may ask for approval. For a user configuration, the form consistent with the installed help is:

```bash
claude mcp add --transport stdio --scope user cursor_cloud -- uvx cursor-cloud-mcp
```

Then provide `CURSOR_API_KEY` in the process environment, not in the command. `CURSOR_MCP_ALLOW_WRITES` goes in the JSON's `env` entry, not on the command line with the key.

Codex: `examples/codex.config.toml`. The key goes through `env_vars`, not through a `${...}` interpolation in the TOML. Check with `codex mcp list` and `/mcp`.

OpenCode: `examples/opencode.json`. The key uses `{env:CURSOR_API_KEY}`. The public documentation describes `timeout` as the tool discovery timeout. On OpenCode 2.0.20, `opencode debug config` loads a single numeric `timeout` both as the catalog timeout and as the execution timeout. The example sets it to 100000 ms, above the 90-second timeout of the repository list. Check with `opencode debug config`, then a tool call in a session. `opencode mcp list` may not display a server that the project configuration does load.

## Troubleshooting

- The server lists its tools but every call says the key is missing: the MCP process environment does not contain `CURSOR_API_KEY`. A repository `.env` is not read.
- The key is displayed as not interpolated: the value is still `${CURSOR_API_KEY}` or `{env:CURSOR_API_KEY}`.
- A mutation answers `READ_ONLY`: `CURSOR_MCP_ALLOW_WRITES` is not exactly `1`, or the client was not restarted.
- `CONTINUATION_REFUSED`: the agent is archived, its status is unknown, or `workOnCurrentBranch` is not explicitly `false` (true or absent).
- `DELETE_DISABLED`: `CURSOR_MCP_ALLOW_DELETE` is not exactly `1`.
- `STREAM_EXPIRED`: the stream can no longer be replayed. Read `cursor_get_run`.
- `MUTATION_OUTCOME_UNKNOWN`: do not resend the same creation with a new identifier. Call `cursor_get_agent` with the returned identifier. Without an `agent_id`, look for the agent with `cursor_list_agents(name=...)`.
- `GET /v1/repositories` can be slow and is heavily rate-limited (1 request per minute, 30 per hour). The five-minute cache covers only the current process.
- stderr announces `SIMULATED MODE` when `CURSOR_MCP_FIXTURE=1`. This variable with a real key prevents any call.
- stdout must remain the MCP channel. If a client reports invalid JSON, look for a `print` or a log that is not on stderr.
- "bad interpreter" or "No such file" after moving a development copy: a virtual environment keeps absolute paths in its scripts. Run `uv sync` again in the new place, or register `uv run --directory /path/to/cursor-cloud-mcp cursor-cloud-mcp` as the command.

## Out of scope

No MCP HTTP server, no database, no local shell, no reading of the local checkout, no pull request merging, no images, no remote MCP servers in the VM, no custom subagents declared in the request, no budget cap enforced by this process. The API also does not allow setting the CPU, RAM or GPU size of a Cursor VM.
