# Verification

## Runner supervision, version 0.3.0 (October 6, 2026)

Following the feedback of an agent that supervised about ten runners overnight with version 0.1. What belongs to the API and what this server does about it is in the README ("What the API does not tell you"); the raw observations are in `docs/api-contract.md` ("Stream replay and supervision").

- New `cursor_supervise`; `activity` on `cursor_get_run`; `tail` on `cursor_read_run_events`; `replace_active` on `cursor_create_run`; errors returned as pure JSON. 17 tools.
- `uv run pytest`: **110 passed** under Python 3.13 and 3.12, including the new `tests/test_supervision.py` (stream shapes recorded from a real run, anonymized). Ruff clean, `uv lock --check` OK, `scripts/wheel_smoke.py` OK on the built wheel.
- Real API, read-only (8 active runners of the account):
  - `cursor_supervise`: 8.4 s without `activity`, 44 s with it, all 8 walks `complete`. Idle times from 3 to 48 minutes (the API's `updated_at` stays at creation), 3 runs flagged `stale`, background tasks last seen running.
  - `cursor_get_run(activity=true)`: 27 to 33 s on an idle run (until its first heartbeat), `complete: true`.
  - `cursor_read_run_events(tail=…)`: last events of an 851 KB replay, `truncated: false`.
- A first version ended a replay walk after 1.5 s, then 5 s, of silence. On the real API it stopped after 118 of 2,426 events: replays pause mid-way. The walk now stops only on deterministic signals (result, live event, heartbeat), as measured above.
- Not run: `replace_active` against the real API (paid; covered by tests: one cancel, exactly one follow-up after `CANCELLED`, none if the run does not stop, model validated before anything is cancelled).


## Consolidation of October 6, 2026 (re-audit of `23aba59`)

Four re-audit gaps fixed, each with a test that fails on `23aba59`: multiline secret masked before normalization; response describing a different agent or run refused (no POST); SSE deadline covering the opening and the error body, `Content-Type` checked; last observation returned with `reread_error` after a transient error. Version 0.2.0, migration documented, `scripts/wheel_smoke.py` in CI. Confirmed live on October 6, 2026 on `258ec02`, with explicit authorization:

- `scripts/live_read.py`: all reads PASS (account, full model catalog, agents, the account's repositories, artifacts, stream).
- `scripts/smoke_live.py`: **34 out of 34 PASS**, one `WARN` (empty artifact list, known limitation). No `INCOMPATIBLE_RESPONSE`: `cursor_get_agent`, `cursor_get_run` (with and without `wait_seconds`), the cancellation and archiving re-reads and the follow-up run accept the API's responses. Cost re-read: 0.78 cents for the agent with a repository; both test agents are deleted (404 re-read).
- Re-read by GET: the API accepts an agent or run identifier whose hexadecimal digits are uppercase, and responds with the lowercase form. The identity check compared byte for byte and would have refused this legitimate request, whereas local validation accepts uppercase. It now compares case-insensitively; a test covers it (it fails on `258ec02`). `uv run pytest`: **92 passed** under 3.13 and 3.12.

## Live smoke of PR #2 and interface delivery of October 5, 2026

### Live smoke of PR #2 (`9db67c7`), before any change

- `uv run pytest`: 70 passed; ruff clean; CI green (3.12 and 3.13).
- `workOnCurrentBranch` in `GET /v1/agents/{id}`: **present**. Re-read by GETs only on all agents of the test account, archived included, with 0, 1 or 2 repositories: almost all at `false`, a few at `true`, none absent. The follow-up rule therefore does not block legitimate agents.
- Follow-up rule on these real agents, with a transport that blocks any POST: `false` + `IDLE` or `ACTIVE` reaches the POST (intercepted); `true` is refused (`CONTINUATION_REFUSED`) before any send; an archived agent too.
- `scripts/smoke_live.py`: **34 out of 34 PASS**, one `WARN` (empty artifact list, known limitation). A first attempt failed everywhere with `TIMEOUT`: the script passed only `PATH`, `HOME` and `LANG` to the server, so not the sandbox's proxy; no request had reached Cursor. The script now passes the proxy and CA variables if they exist.

### Interface delivery (usage evaluation and official SDK)

Measurements on the real stdio server, read-only, then the full smoke.

- 19 tools become 16: `cursor_wait_run` → `cursor_get_run(wait_seconds)`, `cursor_get_artifact_url` → `cursor_read_artifact(url_only)` (and URL returned automatically for a binary or a file over 5 MB), `cursor_unarchive_agent` → `cursor_archive_agent(unarchive=true)`. Removal of `thinking` (covered by `model_params`) and of the `starting_sha` alias.
- Responses in compact JSON, with no null field, identical in text and in `structuredContent`. The MCP SDK wrote indented JSON, `null` included.
- `cursor_list_models`: **242,629 → 9,280 characters** for the full model catalog. `model_id` (id or unambiguous alias) returns one model and its variants. Combinations absent from the model's variants refused before POST.
- Stream: on a real run, the assistant's text arrived word by word, one event per fragment; consecutive fragments are merged, each tool call now appears only once (last state), `status` and `result` no longer repeat their raw JSON.
- Search: the API refuses any `GET /v1/agents` filter other than `limit`, `cursor`, `includeArchived` and `prUrl` (400 verified for `name`, `q`, `search`, `status`, `sort`). `name` is therefore filtered locally over at most five pages; `pr_url` is passed through. `cursor_list_repositories` accepts `query`.
- Taken from reading the official Python SDK `cursor-sdk` 1.0.36, which calls the same REST v1: `cost` of `GET /v1/agents/{id}/usage` (present live, ignored until now), `Run.error`, `helpUrl` and `provider` of errors, resume instruction on `invalid_last_event_id`. Matrix in `docs/api-contract.md` corrected accordingly.
- `uv run pytest`: **83 passed** under Python 3.13 and 3.12 (new `tests/test_interface.py`). Ruff clean.
- `Idempotency-Key`, authorized real test (`composer-2.5`): **ignored** by the API, at creation (with and without `envVars`) as at follow-up run. Details and conclusion in `docs/api-contract.md`. Five agents created and deleted, 4.26 cents.
- Model change on a follow-up run, authorized real test: **accepted, validated and persistent** (`composer-2.5` → `claude-haiku-4-5`, then a run without `model` stayed on Haiku). Details in `docs/api-contract.md`. `cursor_create_run` now accepts `model_id`, `model_params` and `reasoning_level`. 3.54 cents, agent deleted.
- `scripts/smoke_live.py` on this delivery: **34 out of 34 PASS**, one `WARN` (artifacts). Cost re-read: 0.74 cents for the agent with a repository; agent found by `name` among all the test account's agents; cancellation `CANCELLED` confirmed; stderr without secrets. The four agents created by the day's two smokes were deleted; the two `smoke-*` agents from October 1, kept on purpose, were not touched.

## Reliability delivery of October 5, 2026

Following the external audit of commit `f9a582d`. No real Cursor call, no paid write.

- `mcp==2.3.0`, `httpx==0.28.1`, `pytest==8.4.2`, lockfile regenerated.
- `uv run pytest`: **70 passed** under Python 3.13 and 3.12. New tests in `tests/test_reliability.py`: `201` POST cut after a few bytes (a single request, `MUTATION_OUTCOME_UNKNOWN` with `agent_id`, status and request id), follow-up run cut (`previous_latest_run_id` kept), GET cut, close error after a complete body, budget exhausted without sending, slow catalog that consumes the creation budget, follow-up run refused if `workOnCurrentBranch` is absent or the status unknown, unarchiving not confirmed on unknown status, cancellation racing with `FINISHED`, secret reflected by Cursor masked in the error, unknown argument refused without modifying `ArgModelBase`, `starting_sha` alias, UTF-8 SSE cut, `error` distinct from `finished`, SSE cut with partial events, cursor kept, `clipped`, artifact in simulated mode without network, abandoned MCP call without remote cancellation (`outcome=cancelled` logged), cancelled cache reader with no effect on the other, masked traceback, short secret masked as a whole word without breaking the error JSON or the keys, permanent secrets kept when per-call values are evicted. The stdio subprocess tests also cover `starting_ref`, the unknown argument, the simulated artifact and a short secret passed through `env_vars`, absent from stderr at DEBUG.
- `uvx ruff@0.16.10 check src tests scripts`: clean. A GitHub Actions CI replays locked install, ruff, tests (3.12 and 3.13) and installation of the built wheel.

Requalified live on October 5, 2026 (section above): `workOnCurrentBranch` is present in `GET /v1/agents/{id}`.

## Report of October 1, 2026

The three families below do not replace one another.

## Content tested

SHA-256 fingerprint of the tracked or non-ignored files, excluding `examples/resolved` and this file (`git ls-files -co --exclude-standard`, `LC_ALL=C` sort, `sha256sum` of each file, then `sha256sum` of the list): `c63ba4012df8044b415c7d3628fe01a93f51ceb85e00d2a85e0ec4b997534b03`.

Versions used for `uv run pytest`:

- Python `3.13.12` in `.venv`, after restoring that interpreter
- Python `3.12.13`, via `uv run --python 3.12 pytest`, then the environment was put back on 3.13.12
- `mcp==2.2.0`
- `httpx==0.28.1`
- `pytest==8.4.2`

Dependencies have not changed since the `pip-audit` audit of September 30, 2026, which found no known vulnerability.

## Simulated tests

Command: `uv run pytest`, then `uv run --python 3.12 pytest`. Result: `45 passed` on both interpreters.

These tests use `httpx.MockTransport` or `CURSOR_MCP_FIXTURE=1`. None calls `api.cursor.com`.

Coverage added: catalog validation, `reasoning_level`, environment rules, `forward_env`, SSE stream cut then resumed, `STREAM_EXPIRED`, waiting on an already-terminal run, artifact download without an authorization header, refused host, deletion without both guards, inconsistent cancellation identifier, log filter on a child logger, fixture plus real key refusal, 1-second delay on a 429 without `Retry-After`. The stdio catalog has 19 tools, in `2026-07-28` and `2025-11-25`.

Status of this family: **PASS**.

## Calls executed by the clients

The fixture loops already reported on September 30, 2026 were not replayed. This session verified a single real call, `cursor_get_account`, which is a GET. No personal configuration was modified. `CURSOR_MCP_ALLOW_WRITES` was `0`. The key does not appear in the retained outputs.

### stdio server, no LLM: PASS

`python -m cursor_cloud_mcp` launched by the Python MCP client, with the key only in the subprocess environment. `cursor_get_account` returned an `api_key_name` equal to the test key's name. The key is not in the tool's text.

### Claude Code: PASS

`claude -p --strict-mcp-config --mcp-config /tmp/ccm-real/mcp.json`, allowed tool `mcp__cursor_cloud__cursor_get_account`. The response is the test key's name.

### Codex CLI: PASS

`codex exec --ignore-user-config --skip-git-repo-check --ephemeral -s read-only`, server passed through `-c`, without writing `~/.codex/config.toml`. The response is the test key's name.

### OpenCode: INCONCLUSIVE

`opencode debug config` in `/tmp/ccm-opencode` loads the server, masks the key and normalizes `timeout` to 100000 ms for the catalog and for execution. Two `opencode run --model opencode/big-pickle` commands did name `cursor_get_account`. The model reported `CONFIGURATION_MISSING`. The tool's raw error body was not in the captured output. Claude Code and Codex, with the same key, succeeded at the same time.

## Real calls to Cursor

Script: `uv run python scripts/live_read.py`. It loads `CURSOR_API_KEY` from `.env` for its own process. The MCP server does not load that file. No POST.

- `GET /v1/me`: **PASS**, key name returned, email present and not displayed.
- `GET /v1/models`: **PASS**, full model catalog, first id `default`.
- `GET /v1/agents?limit=5`: **PASS**, 5 `IDLE` agents, `nextCursor` present.
- `GET /v1/repositories`: **PASS**, the account's repositories.
- `GET /v1/agents/{id}/artifacts` on the first agent of that page: **PASS**, several artifacts. The paths are not displayed.
- `GET /v1/agents/{id}/runs/{runId}/stream` on that agent's latest run: **PASS** in the contract's sense, code `STREAM_EXPIRED`, HTTP 410. The stream of an old run can no longer be replayed. No event was read.

The stdio `cursor_get_account` call above is also a real GET. It is counted once, in the clients family.

Minimal paid session with no repository, authorized by the user, model `composer-2.5`, a single execution, on October 1, 2026:

- `cursor_create_agent`: **PARTIAL**. The agent was indeed created, but the response exceeded the 40-second deadline and the tool returned `MUTATION_OUTCOME_UNKNOWN`. Without replaying the creation, the state was re-read with `cursor_list_agents`. Fix: creation deadline raised to 90 seconds, with a test.
- State read, SSE stream and waiting on the run: **PASS**. The run finished (`FINISHED`) in 37.7 seconds, 29,422 tokens in total.
- `cursor_archive_agent`: **PASS**, `ARCHIVED` status re-read. No agent was deleted.
- Artifacts: **FAIL on the API side**. The stream shows a write of `/agent/artifacts/result.txt`, but `GET /artifacts` stayed empty, including on a second read after archiving, and the download answered 404. Limitation documented in the README and in the server's instructions.

Creation with a repository, outside the session above, reported on October 1, 2026 (issue #1): a full SHA in `startingRef` answered `400 validation_error` and created nothing. The same call with the branch name whose head was that SHA answered `201`. The MCP server now refuses a full SHA and sends the branch name.

Real smoke of each tool, `scripts/smoke_live.py`, real stdio server, `composer-2.5`, two agents and nine short runs, on October 1, 2026:

- First execution: 32 out of 33. The only failure was a wrong assertion in the script: the API never returns `startingRef` in `GET /v1/agents/{id}`, verified on three existing agents. The branch is proven by the creation's `201`. It also revealed a real defect: `cursor_cancel_run` reported `outcome_confirmed=True` while the re-read run was still `RUNNING`. Fix: re-read up to four times, confirmed only if the state is terminal, with two tests.
- Second execution, with the fixes: **31 out of 31 PASS**, one `WARN`. Creation with repository and branch (`201`), `forward_env` and `env_vars` received by the agent without copying the secret value, follow-up run, SSE stream, wait, usage, cancellation (`CANCELLED` confirmed), archiving, unarchiving, `include_archived`, follow-up refusal on an archived agent, deletion refusal without the right confirmation, and stderr without the key or the passed value. The `WARN` is the artifact list, which stayed empty.
- `cursor_delete_agent`: **PASS** twice, on the first execution, with both guards. The second execution kept its two agents (`SMOKE_KEEP=1`).

API observations: the order of `GET /v1/agents` is not creation-date order. `includeArchived=true` does add the archived agents.

Not executed: `env_type` `pool` and `machine` (no worker available), several repositories, `auto_create_pr`, `mode: plan`, and the reasoning level on a model that exposes one (`composer-2.5` has only `fast`).

## Limitations

- The fixture loop in Claude Code, Codex and OpenCode dates from September 30, 2026 and targeted the first eleven tools.
- OpenCode 2.0.20 did not show, in this session, that `{env:CURSOR_API_KEY}` reaches the server process. `debug config` nonetheless shows a masked value.
- (Fixed on October 5, 2026) The log filter masked only secrets of at least 8 characters, and neither tracebacks nor per-call `env_vars`. The formatter now masks all of the process's known secrets, traceback included; below 8 characters, as a whole word only.
- No PyPI publication.
