# AGENTS.md

Dev repo for `cursor-cloud-mcp`: stdio MCP server (Python 3.12+) for Cursor Cloud Agents v1. `src/cursor_cloud_mcp/` is the server; `server.py` only wires the 17 tools, logic lives in `sessions.py` (mutations), `stream.py`/`supervision.py` (stream replay, activity), `catalog.py`+`validation.py` (model/repo checks), `client.py`, `present.py`/`models.py`/`shapes.py`, `artifacts.py`, `budget.py`, `config.py`, `errors.py`, `redaction.py`, `fixture.py`.

## Commands (uv only)

- `uv sync` then `uv run pytest` (CI uses `uv sync --locked --python 3.12|3.13` + `uv run --locked pytest`, `testpaths=["tests"]`, `-q`).
- Lint: `uvx ruff@0.16.10 check src tests scripts` (line-length 120, target py312).
- Run server: `uv run cursor-cloud-mcp` or `uv run python -m cursor_cloud_mcp`; binary is `.venv/bin/cursor-cloud-mcp`.
- Wheel check (as CI): `uv build --wheel`, fresh venv, `pip install dist/*.whl`, then from `/tmp` run `python <repo>/scripts/wheel_smoke.py`.
- A moved checkout breaks `.venv` (absolute shebangs): re-run `uv sync`, or launch via `uv run --directory <repo> cursor-cloud-mcp`.

## Env / secrets

- Server reads process env only, never `.env`. `.env` in repo root holds `CURSOR_API_KEY` for the smoke scripts only — never log/print/copy it.
- `CURSOR_API_KEY` (absent = reads fail, server still lists tools), `CURSOR_MCP_ALLOW_WRITES=1` (mutations), `CURSOR_MCP_ALLOW_DELETE=1` + `confirm_agent_id` (delete), `CURSOR_MCP_FORWARD_ENV` (allowlist for `forward_env`), `CURSOR_MCP_LOG_LEVEL`, `CURSOR_MCP_FIXTURE=1` (simulated mode; refuses any real key). No tool flips these — change env and restart.
- stdout is the MCP channel: only stderr logging. Key/forwarded values are masked in logs and error JSON — don't bypass `redaction`.

## Gotchas that break work

- Single version source: `__version__` in `src/cursor_cloud_mcp/__init__.py`. Release tag must be exactly `v<version>` (`release.yml` enforces it, publishes via Trusted Publishing).
- Tool errors must be pure JSON: raise `failure(ErrorCode, ...)` (becomes `ToolFailure`); only SDK-level arg rejection stays plain text. Never emit `null` fields in views (`present.py` omits).
- Pre-send guards live in code, not the API: `starting_ref` is a branch name (40/64-char SHA refused), `model_id`/`reasoning_level`/`model_params` checked against the cached catalog (`catalog.py`), `workOnCurrentBranch` forced `false`, follow-ups refused on archived/unknown-status agents.
- API limits encoded in behavior (don't "fix" by simplifying): `FINISHED` ≠ job done, `updatedAt` frozen while `RUNNING`, no name/status filter server-side (`name` = local scan ≤5 pages, `next_cursor` must round-trip unchanged), artifact list often empty (result must come from `cursor_get_run`), stream has no tail marker (full replay walk, budgets ≤95s so client timeout must be ~100s).

## Tests

- `uv run pytest` never touches Cursor (MockTransport / `CURSOR_MCP_FIXTURE=1`). `tests/test_supervision.py` uses anonymized recorded streams.
- Paid/real scripts — run only on explicit request: `SMOKE_PAID=1 uv run python scripts/smoke_live.py` (creates 2 agents + runs, deletes them unless `SMOKE_KEEP=1`; ~cents), `uv run python scripts/live_read.py` (read-only GETs). Both read `.env` themselves; the server still must get the key via process env.
- `docs/api-contract.md` + `docs/verification.md` are dated observations/history. `README.md`/`AGENT_GUIDE.md` describe downstream MCP use, not this repo's dev workflow — trust `src/` + `config.py` + CI when they conflict.
