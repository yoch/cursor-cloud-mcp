# cursor-cloud-mcp

Local MCP server, over stdio, exposing the Cursor Cloud Agents v1 REST API as seventeen tools: create a Cloud agent, choose its model, send follow-up runs, supervise a fleet, read results, usage and artifacts, then archive or delete. One implementation serves Claude Code, Codex CLI and OpenCode. It is not an orchestration platform.

To have an agent install or use it, give it [`AGENT_GUIDE.md`](https://github.com/yoch/cursor-cloud-mcp/blob/main/AGENT_GUIDE.md): client setup, usage patterns, recovery and troubleshooting. Each tool's description, seen by every MCP client, is the reference for its parameters and fields.

## Installation

Python 3.12 or newer. The package is on PyPI as `cursor-cloud-mcp`; the simplest is to let the MCP client start it with `uvx`:

```bash
uvx cursor-cloud-mcp
```

To pin the version, or for a client with a short startup timeout, install it once and register the `cursor-cloud-mcp` command:

```bash
uv tool install cursor-cloud-mcp    # or: pipx install cursor-cloud-mcp
```

Client fragments for Claude Code, Codex and OpenCode are in [`examples/`](https://github.com/yoch/cursor-cloud-mcp/blob/main/examples). **Set the client's tool timeout to at least 100 seconds**: no call takes more than 95, but a client that gives up earlier may abandon a creation that was already sent, and pay for a second one on retry.

## Variables

The server reads the process environment only, never a `.env` file. No tool changes these settings: edit the environment and restart the client.

| Variable | Role |
|---|---|
| `CURSOR_API_KEY` | The Cursor key. Without it the server still lists its tools; calls fail with a clear error. |
| `CURSOR_MCP_ALLOW_WRITES` | `1` allows creation, follow-up runs, cancellation and archiving. `0` by default. |
| `CURSOR_MCP_ALLOW_DELETE` | `1` also allows `cursor_delete_agent`, which further requires `confirm_agent_id`. `0` by default. |
| `CURSOR_MCP_FORWARD_ENV` | Comma-separated names whose values `forward_env` may pass to an agent, without the value going through a tool argument. |
| `CURSOR_MCP_DEFAULT_MODEL` | `id` or `id:param=value,...`, used by `cursor_create_agent` without `model_id`. Never applied to a follow-up run. |
| `CURSOR_MCP_STATE_DIR` | Directory of the local model journal, `~/.local/state/cursor-cloud-mcp` by default; `off` disables it. |
| `CURSOR_MCP_LOG_LEVEL` | `INFO` by default. Logs go to stderr; they never contain prompts, bodies or the key, and secret values are masked, in error responses too. |
| `CURSOR_MCP_FIXTURE` | `1` replaces the API with simulated responses, for trying the tools without spending. Refused with a real key. |

## Tools

| Tool | Does |
|---|---|
| `cursor_get_account` | Checks the key. |
| `cursor_list_models` | Compact model catalog: parameters, defaults, reasoning parameter (`brief`, `query`). |
| `cursor_list_repositories` | GitHub repositories visible to Cursor (rate-limited by the API, cached 5 min). |
| `cursor_list_agents` | Agents, one page at a time; `name` filters locally. |
| `cursor_supervise` | A fleet in one call: each agent's latest run, optionally stream activity and spend. |
| `cursor_get_agent` | Agent metadata, url, latest run, recorded model. |
| `cursor_create_agent` | **Paid.** Creates an agent and starts its first run. |
| `cursor_create_run` | **Paid.** Follow-up run on the same agent; `replace_active` cancels a run in progress first. |
| `cursor_list_runs` | An agent's runs, newest first. |
| `cursor_get_run` | State, final result, branches; waits up to 60 s; `activity` adds what the stream shows. |
| `cursor_read_run_events` | Stream excerpt, resumable, or the last N events (`tail`). |
| `cursor_cancel_run` | Cancels, then re-reads to confirm. Pushed commits stay. |
| `cursor_get_usage` | Tokens and cost in US cents, per agent or per run. |
| `cursor_list_artifacts` / `cursor_read_artifact` | Files under `artifacts/` (the API list is often empty: ask agents for their result in the final answer). |
| `cursor_archive_agent` | Archives or unarchives (reversible). |
| `cursor_delete_agent` | Permanent deletion, behind both guards. |

Responses are compact JSON, without `null` fields. Errors are JSON objects with a stable `code`, a `message` and, when useful, a `recovery`. `MUTATION_OUTCOME_UNKNOWN` means a paid call may have gone through: re-read the returned `agent_id`, never retry with a new one.

## What the API does not tell you

The left column is a limit of the API, measured on real runs; the right column is what this server does about it. Details and dates: [`docs/api-contract.md`](https://github.com/yoch/cursor-cloud-mcp/blob/main/docs/api-contract.md).

| API limit | What this server does |
|---|---|
| `FINISHED` means the agent ended its turn, not that its job is done | `activity.background_tasks` and `unfinished_background_tasks`: commands the agent started in the background, with their last observed state |
| A `RUNNING` run keeps `updatedAt` at creation time | `activity.last_event_at` and `idle_seconds`, from the stream's event ids |
| An `ERROR` run carries no cause | the activity summary (last events, last tool call, background tasks) is added automatically |
| A stream can only be replayed from its start, with no end marker | `tail` and `activity` walk the whole replay without keeping it, and know where it ends |
| A run in progress cannot receive a message | `cursor_create_run(replace_active=true)`: cancel, confirm, then follow up on the same agent |
| No listing of runs across agents | `cursor_supervise`: one call, parallel reads |
| The model of an agent or a run is never returned | creations return the effective selection and warn about catalog defaults (such as `fast=true`); a local journal records each model sent and reads it back as `model` |
| No quota or account spend | `cursor_supervise(usage=true)` sums a fleet's charged cost |
| `starting_ref` as a full SHA is refused by the API | refused locally, before sending: use a branch name |

Activity is what the stream last showed, not a view inside the VM. The API cannot choose the CPU, RAM or GPU of a Cursor VM: `env_type` `pool` or `machine` targets the user's own workers.

## Agent skill

[`skills/cursor-agents/`](https://github.com/yoch/cursor-cloud-mcp/blob/main/skills/cursor-agents) is a Claude Code skill for driving Cursor agents through this server: fleets of compute workers (job design, launch, supervision, nudges, stop) and coding agents on their own branch. Copy the directory to `~/.claude/skills/` or to a project's `.claude/skills/`. It ships prompt templates, `scripts/ccm.py` (tool calls from scripts that outlive a session) and `scripts/push_watch.py` (a no-LLM wake-up check on worker branches).

Private conventions (default model, caps, naming) go in `references/field-notes.md` next to `SKILL.md`: the skill reads it when present, and this repository ignores it. In a project's `.claude/skills/`, add `.claude/skills/*/references/field-notes.md` to that project's `.gitignore`.

## Development

```bash
uv sync
uv run pytest       # never calls Cursor
uv run cursor-cloud-mcp
```

Releases: `__version__` in `src/cursor_cloud_mcp/__init__.py` is the only version number; pushing the tag `v<version>` tests, builds and publishes to PyPI through Trusted Publishing.

## Out of scope

No HTTP transport, no database, no local shell, no reading of the local checkout, no pull request merging, no images, no remote MCP servers or custom subagents in the VM, no budget cap enforced by this process.
