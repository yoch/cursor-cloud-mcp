# TODO

Improvement tracks from a real supervision session (an autoqueue supervisor, `*_job.py` scripts, eight runners). Recommended order: **A → D → B → C**. Target release: 0.4.0.

Each track is a server-side *means*. None prescribes how a calling agent structures its protocol (result markers, branch naming, state files): that stays agent-side, and the MCP only exposes the facts that make such conventions possible.

**Status (2026-10-09): all four tracks are implemented on `main` (version 0.4.0, unreleased). This file stays as the contract of record.**

## A. Error diagnosis: more of the stream in the activity summary (small)

Gap: an ERROR run carries neither `error` nor `result` (API limitation), and the caller cannot tell a dead session from a failed job without side effects.

- `ActivityView.last_events`: the last ~10 events **observed during this read** (not necessarily the true last available when `complete: false`). Built from the walk the activity summary already pays for (`supervision.read_activity` calls `walk_replay(keep=0)` → `keep=N`).
- A stream ending on an `error` event returns the partial summary with `stream_error: true` (present only when true), `complete: false`, no cache, and no `activity_error`. The error event itself stays visible in `last_events` with the information it carries.
- Widen the last tool call's clip in the activity summary (new `ACTIVITY_TOOL_TEXT_MAX_CHARS`, ~2000). The limit must be applied in `_simplify()` **during the walk**: the tracker stores the already-clipped view, so raising the limit when producing the summary would recover nothing. A and B share the same parameterizable-limit mechanism.
- `cursor_supervise` rows stay light: strip `last_events` like the other heavy fields (`last_assistant_text`, `background_tasks`, tool args/result). Keep `activity.stream_error` and `activity.complete`; do not duplicate `stream_error` at row level. `stale`/`unfinished_after_end` remain conditioned on `complete` summaries; a `stream_error` row is counted `incomplete` through `complete: false`.

## D. Fleet state: `git` in list and supervision views (small)

Gap: `git.branches` is in every upstream `Run` payload but dropped by `present.run_page_view` and `supervision.read_status`; seeing which branch/PR a runner is on costs one `cursor_get_run` per agent.

- Add `git` to `RunSummaryView` (`cursor_list_runs` items) and to `SupervisedAgentView` (`cursor_supervise` rows). No extra API calls; the fixture already serves `git`.
- Preserve `git.scope = "agent_current_state"`: branches and PRs describe the agent's current pushed state, not the run's checkout nor the commit of a past run. Response size stays small; the MCP attaches no meaning to a branch name.

## B. Logs: wider command outputs from the stream (medium)

Gap: `tool_call` events carry `tool_args`/`tool_result` clipped at 500 chars (`EVENT_TEXT_MAX_CHARS`); a supervisor cannot read a runner's recent terminal output without asking the agent to recopy it into its reply.

- Single parameter on `cursor_read_run_events`, applicable to `tail` and incremental modes: `tool_output_limit` (default 500, max 4000). One read interface, same cursor conventions.
- Head + tail clip for tool `args` and `result` **only** (never assistant/thinking text): half head, half tail, joined by an explicit marker; total length, marker included, respects the limit; values within the limit stay intact. Shared function with A.
- **Two distinct truncations.** The MCP may clip what it received (own limit); Cursor may omit a field upstream because of its size (verified live on 2026-10-09: `truncated.result` with `result` absent). The second is not recoverable by raising the MCP limit.
  - Parse the upstream signal; expose `tool_args_omitted` / `tool_result_omitted` (booleans, present only when true) on `RunEventView` and `ToolCallSummaryView`.
  - Definition to document: *the upstream API explicitly reported that this field was omitted from the stream because of its size*. Only the upstream signal sets them: an absent value, a local clip, or a field stripped in `cursor_supervise` never does.
- Explicit global cap on rendered tool text per call. On reaching it: `truncated: true`, `last_event_id` = last event actually returned, resume with `after_event_id` for the following events. In `tail` mode the same cap bounds the returned window from its oldest side (the newest events are kept) and still sets `truncated`; the walk itself always runs to its usual stop criterion.
- Contract: events are paged, outputs are bounded; no intra-`tool_result` resume, no promise of full recovery of an output (local cap or upstream omission). The agent-side convention "ask the runner to tail its log" is out of scope.

## C. Creation recovery by `agent_id`: `on_conflict="reuse"` (small)

Gap: the API ignores `Idempotency-Key` (verified 2026-10-05: three POSTs with the same key → three agents, 4.26 cents). A creation retried after a cut or crash can duplicate a paid agent unless the caller holds its `agent_id`.

- `on_conflict: Literal["error","reuse"] = "error"` on `cursor_create_agent`. Requires a caller-supplied `agent_id` (otherwise `VALIDATION`); inapplicable with `env_vars`/`forward_env` (the existing rule forbids `agent_id` there).
- On 409 `agent_id_conflict`: re-read the agent, then return the widened view:

| Field | Normal creation | Reuse |
|---|---|---|
| `agent_id` | present | present, after re-read |
| `run_id`, `run_status` | present as today | absent |
| `latest_run_id` | current behavior | observed value, if available |
| `reused` | absent | `true` |

- `run_id` stays absent even when `latest_run_id` is known; `next_step` adapts (point to the observed last run when it exists, without assuming a new run was created). The normal path's JSON is unchanged.
- The new prompt and parameters are **not** applied on reuse (documented). A failed re-read raises the original `AGENT_ID_CONFLICT` with a `recovery` saying that retrying the same call is safe (`reuse` is idempotent) or to read the agent with `cursor_get_agent` — never with another identifier.

## Not planned

- Guaranteed `error_reason`, formal dead-session detection: the API gives neither; only heuristics (`idle_seconds`, `background_tasks`, side-effect heartbeats).
- Structured output/status guarantees: agent-side protocol; the means already exist (`result`, `git.branches`, artifacts).
- Deterministic exec primitive ("run this command at this commit"): the product is an agent; a template tool would be false safety.
- VM/process/CPU visibility, arbitrary file reads: no API surface.
- Push notifications: out of scope for the current implementation. Run monitoring uses polling, with `wait_seconds ≤ 60` per call.
- API-side idempotency; steering a run in progress.
- `dry_run` on mutations: deferred. Validation already runs before the POST (a refusal costs nothing), so it only adds a pre-approval display or a writes-disabled check; revisit only if a human-approval workflow needs it.
