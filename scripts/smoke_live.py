"""Real, PAID smoke test of the seventeen tools, on the real stdio server.

Opt-in: `SMOKE_PAID=1`. Two `composer-2.5` agents, a few very short runs,
and both agents are deleted at the end, except with `SMOKE_KEEP=1`, which
leaves them visible in the web interface and prints their link. No creation is replayed.
The supervision tools (`cursor_supervise`, `activity`, `tail`) are checked on the runs the
script already pays for; only `replace_active` adds two short runs.
The key comes from the environment, or from `.env` read by this script only. It
is never printed. No prompt and no secret value is printed.
"""

import asyncio
import json
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path

from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client

MODEL = "composer-2.5"
REPO_URL = os.environ.get("SMOKE_REPO", "https://github.com/yoch/cursor-cloud-mcp")
BRANCH = os.environ.get("SMOKE_BRANCH", "main")
FWD_VALUE = "smoke-forwarded-value-12345678"
PUB_VALUE = "smoke-public-value-87654321"
SHA = "a" * 40
# Shared by both agent names, so cursor_supervise(name=TAG) finds exactly this run's agents.
TAG = f"smoke-{uuid.uuid4().hex[:8]}"
# Network variables passed to the server if they exist: proxy and certificate authority
# of a managed environment. Without them, the server cannot reach the API behind a proxy.
NETWORK_ENV = (
    "HTTPS_PROXY",
    "https_proxy",
    "NO_PROXY",
    "no_proxy",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
)

Result = tuple[bool, dict[str, object]]


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.rows.append((name, status, detail))
        print(f"{status:5} {name} {detail}".rstrip(), flush=True)

    def check(self, name: str, condition: bool, detail: str = "") -> bool:
        self.add(name, "PASS" if condition else "FAIL", detail)
        return condition

    @property
    def failed(self) -> list[str]:
        return [name for name, status, _ in self.rows if status == "FAIL"]


def load_key(root: Path) -> None:
    if os.environ.get("CURSOR_API_KEY"):
        return
    env_file = root / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("CURSOR_API_KEY="):
            value = line.split("=", 1)[1].strip().strip('"').strip("'")
            if value:
                os.environ["CURSOR_API_KEY"] = value
            return


async def call(
    client: Client, name: str, args: dict[str, object] | None = None
) -> Result:
    result = await client.call_tool(name, args or {})
    text = result.content[0].text if result.content else ""
    if result.is_error:
        start = (text or "").find("{")
        try:
            return False, json.loads(text[start:])
        except (ValueError, TypeError):
            return False, {"code": "UNPARSABLE", "message": (text or "")[:200]}
    return True, dict(result.structured_content or {})


def code_of(data: dict[str, object]) -> str:
    return str(data.get("code", "-"))


async def wait_terminal(
    client: Client, agent_id: str, run_id: str, rounds: int = 6
) -> dict[str, object]:
    data: dict[str, object] = {}
    for _ in range(rounds):
        ok, data = await call(
            client,
            "cursor_get_run",
            {"agent_id": agent_id, "run_id": run_id, "wait_seconds": 60},
        )
        if not ok or not data.get("timed_out"):
            return data
    return data


async def main() -> int:
    root = Path(__file__).resolve().parents[1]
    if os.environ.get("SMOKE_PAID") != "1":
        print("Paid smoke not run: set SMOKE_PAID=1.")
        return 2
    load_key(root)
    key = os.environ.get("CURSOR_API_KEY")
    if not key:
        print("CURSOR_API_KEY missing")
        return 2
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "cursor_cloud_mcp"],
        env={
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "LANG": "C.UTF-8",
            "CURSOR_API_KEY": key,
            "CURSOR_MCP_ALLOW_WRITES": "1",
            "CURSOR_MCP_ALLOW_DELETE": "1",
            "CURSOR_MCP_FORWARD_ENV": "SMOKE_FWD",
            "SMOKE_FWD": FWD_VALUE,
            **{name: os.environ[name] for name in NETWORK_ENV if name in os.environ},
        },
        cwd=root,
    )
    report = Report()
    created: list[str] = []
    stderr_path = Path(tempfile.mkdtemp()) / "stderr.txt"
    with stderr_path.open("w", encoding="utf-8") as errlog:
        async with Client(stdio_client(params, errlog=errlog)) as client:
            try:
                await run_all(client, report, created)
            finally:
                await cleanup(client, report, created)
    stderr = stderr_path.read_text(encoding="utf-8")
    leaked = [
        label
        for label, value in (("key", key), ("secret", FWD_VALUE))
        if value in stderr
    ]
    report.check("stderr without secret", not leaked, f"leaks={leaked}")
    stderr_path.unlink()
    print(f"\nSummary: {len(report.rows) - len(report.failed)}/{len(report.rows)} PASS")
    if report.failed:
        print("FAILURES:", ", ".join(report.failed))
        return 1
    return 0


async def run_all(client: Client, report: Report, created: list[str]) -> None:
    listed = await client.list_tools()
    report.check("tools/list", len(listed.tools) == 17, f"tools={len(listed.tools)}")
    tools = {tool.name: tool for tool in listed.tools}
    follow_up = tools.get("cursor_create_run")
    supervise = tools.get("cursor_supervise")
    report.check(
        "annotations (create_run destructive, supervise read-only)",
        follow_up is not None
        and follow_up.annotations is not None
        and follow_up.annotations.destructive_hint is True
        and supervise is not None
        and supervise.annotations is not None
        and supervise.annotations.read_only_hint is True,
    )

    # Errors are pure JSON: the text parses as is, with no "Error executing tool" prefix.
    raw = await client.call_tool(
        "cursor_get_agent", {"agent_id": "bc-00000000-0000-0000-0000-000000000000"}
    )
    text = raw.content[0].text if raw.content else ""
    try:
        error_code = json.loads(text).get("code")
    except ValueError:
        error_code = None
    report.check("error as pure JSON", raw.is_error and error_code == "NOT_FOUND", f"code={error_code}")

    ok, data = await call(client, "cursor_get_account")
    report.check("cursor_get_account", ok, f"key_name_present={'api_key_name' in data}")

    ok, data = await call(client, "cursor_list_models")
    models = {str(item["id"]): item for item in data.get("items", [])} if ok else {}  # type: ignore[union-attr]
    composer = models.get(MODEL)
    report.check(
        "cursor_list_models",
        composer is not None,
        f"models={len(models)} {MODEL}={'present' if composer else 'absent'}",
    )
    model_params: dict[str, list[str]] = (composer or {}).get("params") or {}  # type: ignore[assignment]
    fast_values = model_params.get("fast", [])
    print(f"      {MODEL} parameters={sorted(model_params)} fast={fast_values}")

    ok, data = await call(client, "cursor_list_repositories")
    repos = [str(item) for item in data.get("items", [])] if ok else []  # type: ignore[union-attr]
    wanted = REPO_URL.removeprefix("https://")
    present = any(
        item.rstrip("/")
        .removesuffix(".git")
        .endswith(wanted.removeprefix("github.com"))
        for item in repos
    )
    report.check(
        "cursor_list_repositories",
        ok and present,
        f"repositories={len(repos)} target_present={present}",
    )

    # Local guards, free.
    ok, data = await call(
        client,
        "cursor_create_agent",
        {"prompt": "x", "repository": REPO_URL, "starting_ref": SHA, "model_id": MODEL},
    )
    report.check(
        "SHA refused locally", not ok and code_of(data) == "VALIDATION", code_of(data)
    )
    ok, data = await call(
        client,
        "cursor_create_agent",
        {"prompt": "x", "model_id": MODEL, "reasoning_level": "high"},
    )
    report.check(
        "unknown reasoning_level refused",
        not ok and code_of(data) == "VALIDATION",
        code_of(data),
    )

    first = await smoke_repo_agent(client, report, created, fast_values)
    agent_b = await smoke_env_agent(client, report, created)
    if first is not None:
        await smoke_supervision(client, report, agent_a=first[0], run_a=first[1], agent_b=agent_b)
        await smoke_lifecycle(client, report, first[0])


async def smoke_repo_agent(
    client: Client,
    report: Report,
    created: list[str],
    fast_values: list[str],
) -> tuple[str, str] | None:
    """Agent A. Returns its id and its first run, read again later by the supervision checks."""
    agent_id = f"bc-{uuid.uuid4()}"
    repo_name = f"{TAG}-repo"
    args: dict[str, object] = {
        "prompt": "Reply only with the word OK. Run no command and modify no file.",
        "repository": REPO_URL,
        "starting_ref": BRANCH,
        "name": repo_name,
        "model_id": MODEL,
        "agent_id": agent_id,
    }
    if "false" in fast_values:
        args["model_params"] = [{"id": "fast", "value": "false"}]
    ok, data = await call(client, "cursor_create_agent", args)
    if ok:
        created.append(agent_id)
    if not report.check(
        "cursor_create_agent (repository + branch)",
        ok,
        f"status={data.get('run_status', '-')}"
        if ok
        else f"code={code_of(data)} http={data.get('http_status')}",
    ):
        return None
    run_id = str(data["run_id"])
    print(f"      url={data.get('url')}")

    ok, data = await call(client, "cursor_get_agent", {"agent_id": agent_id})
    # The API does not return startingRef on read: the branch is proven by the 201 at creation.
    repos = data.get("repos") or []
    same_repo = len(repos) == 1 and str(repos[0].get("url", "")).endswith(
        REPO_URL.removeprefix("https://")
    )  # type: ignore[index]
    report.check(
        "cursor_get_agent",
        ok and same_repo,
        f"repository_attached={same_repo} url_present={'url' in data}",
    )
    # The follow-up run is refused if this field is missing: the real contract must provide it.
    report.check(
        "workOnCurrentBranch returned",
        ok and data.get("work_on_current_branch") is False,
        f"work_on_current_branch={data.get('work_on_current_branch')}",
    )

    ok, data = await call(
        client,
        "cursor_read_run_events",
        {
            "agent_id": agent_id,
            "run_id": run_id,
            "max_wait_seconds": 20,
            "max_events": 50,
        },
    )
    kinds = sorted({event["kind"] for event in data.get("events", [])}) if ok else []  # type: ignore[union-attr]
    report.check(
        "cursor_read_run_events",
        ok,
        f"events={len(data.get('events', []))} types={kinds}",
    )  # type: ignore[arg-type]

    data = await wait_terminal(client, agent_id, run_id)
    report.check(
        "cursor_get_run (wait_seconds)",
        data.get("status") == "FINISHED",
        f"status={data.get('status')} timed_out={data.get('timed_out')}",
    )

    ok, data = await call(
        client, "cursor_get_run", {"agent_id": agent_id, "run_id": run_id}
    )
    report.check(
        "cursor_get_run",
        ok and data.get("terminal") is True and bool(data.get("result_present")),
        f"status={data.get('status')} result_present={data.get('result_present')}",
    )
    ok, data = await call(client, "cursor_list_runs", {"agent_id": agent_id})
    report.check(
        "cursor_list_runs",
        ok and len(data.get("items", [])) >= 1,
        f"runs={len(data.get('items', []))}",
    )  # type: ignore[arg-type]
    ok, data = await call(client, "cursor_get_usage", {"agent_id": agent_id})
    if ok:
        total = data["total_usage"]["total_tokens"]  # type: ignore[index]
        cost = data.get("total_cost") or {}
        report.check(
            "cursor_get_usage",
            total > 0,
            f"tokens={total} charged_cost_cents={cost.get('charged_cents', 'absent')}",  # type: ignore[union-attr]
        )
    else:
        report.add(
            "cursor_get_usage",
            "WARN",
            f"code={code_of(data)} (early-access feature)",
        )

    ok, data = await call(client, "cursor_list_agents", {"name": repo_name})
    names = {item["agent_id"] for item in data.get("items", [])} if ok else set()  # type: ignore[union-attr]
    report.check(
        "cursor_list_agents (name)",
        agent_id in names,
        f"found={agent_id in names} scanned={data.get('scanned')}",
    )

    ok, data = await call(
        client,
        "cursor_create_run",
        {
            "agent_id": agent_id,
            "prompt": "Reply only with the word OK2. Modify no file.",
        },
    )
    run2 = str(data.get("run_id", ""))
    report.check(
        "cursor_create_run (follow-up run)",
        ok and run2 not in {"", run_id},
        f"new_run={run2 != run_id}",
    )
    if ok:
        data = await wait_terminal(client, agent_id, run2)
        report.check(
            "follow-up run finished",
            data.get("status") == "FINISHED",
            f"status={data.get('status')}",
        )
    return agent_id, run_id


async def smoke_env_agent(client: Client, report: Report, created: list[str]) -> str | None:
    """Agent B. Returns its id once created, even if a later check fails."""
    ok, data = await call(
        client,
        "cursor_create_agent",
        {
            "prompt": (
                'Run `test -n "$SMOKE_FWD" && echo FWD_SET || echo FWD_MISSING; '
                'test -n "$SMOKE_PUB" && echo PUB_SET || echo PUB_MISSING` '
                "then reply with the two words obtained. Never display the value of the variables."
            ),
            "name": f"{TAG}-env",
            "model_id": MODEL,
            "env_vars": {"SMOKE_PUB": PUB_VALUE},
            "forward_env": ["SMOKE_FWD"],
        },
    )
    if not report.check(
        "cursor_create_agent (env_vars + forward_env, without agentId)",
        ok,
        f"status={data.get('run_status', '-')}"
        if ok
        else f"code={code_of(data)} http={data.get('http_status')}",
    ):
        return None
    agent_id = str(data["agent_id"])
    created.append(agent_id)
    run_id = str(data["run_id"])
    print(f"      url={data.get('url')}")
    data = await wait_terminal(client, agent_id, run_id)
    ok, data = await call(
        client, "cursor_get_run", {"agent_id": agent_id, "run_id": run_id}
    )
    text = str(data.get("result") or "")
    report.check(
        "variables passed to the agent",
        ok and "FWD_SET" in text and "PUB_SET" in text and FWD_VALUE not in text,
        f"FWD_SET={'FWD_SET' in text} PUB_SET={'PUB_SET' in text} value_echoed={FWD_VALUE in text}",
    )

    ok, data = await call(
        client,
        "cursor_create_agent",
        {"prompt": "x", "forward_env": ["NOT_ALLOWED"], "name": "n"},
    )
    report.check(
        "forward_env outside the allowlist refused",
        not ok and code_of(data) == "VALIDATION",
        code_of(data),
    )

    ok, data = await call(
        client,
        "cursor_create_run",
        {
            "agent_id": agent_id,
            "prompt": "Write the word ARTIFACT in /agent/artifacts/smoke.txt then reply with ARTIFACT.",
        },
    )
    run2 = str(data.get("run_id", ""))
    report.check(
        "follow-up run (artifact)", ok, f"status={data.get('status', code_of(data))}"
    )
    if ok:
        await wait_terminal(client, agent_id, run2)
    ok, data = await call(client, "cursor_list_artifacts", {"agent_id": agent_id})
    items = [item["path"] for item in data.get("items", [])] if ok else []  # type: ignore[union-attr]
    report.check("cursor_list_artifacts", ok, f"artifacts={len(items)}")
    if items:
        path = items[0]
        ok, data = await call(
            client,
            "cursor_read_artifact",
            {"agent_id": agent_id, "path": path, "url_only": True},
        )
        report.check("cursor_read_artifact (url_only)", ok, f"url_present={'url' in data}")
        ok, data = await call(
            client, "cursor_read_artifact", {"agent_id": agent_id, "path": path}
        )
        report.check(
            "cursor_read_artifact", ok and "ARTIFACT" in str(data.get("text", "")), ""
        )
    else:
        report.add(
            "cursor_read_artifact (url_only)",
            "WARN",
            "no artifact listed (known API limitation)",
        )
        ok, data = await call(
            client,
            "cursor_read_artifact",
            {"agent_id": agent_id, "path": "artifacts/smoke.txt"},
        )
        report.check(
            "cursor_read_artifact (absent → clean error)",
            not ok
            and code_of(data) in {"NOT_FOUND", "ARTIFACT_NOT_FOUND", "UPSTREAM_ERROR"},
            f"code={code_of(data)}",
        )

    ok, data = await call(
        client,
        "cursor_create_run",
        {
            "agent_id": agent_id,
            "prompt": "Run `sleep 120` in the shell and wait for it to finish before replying.",
        },
    )
    run3 = str(data.get("run_id", ""))
    if not report.check(
        "follow-up run (cancellable)", ok, f"status={data.get('status', code_of(data))}"
    ):
        return agent_id
    await asyncio.sleep(8)
    ok, data = await call(
        client, "cursor_cancel_run", {"agent_id": agent_id, "run_id": run3}
    )
    report.check(
        "cursor_cancel_run",
        ok
        and bool(data.get("cancel_request_accepted"))
        and bool(data.get("outcome_confirmed")) == (data.get("observed_status") == "CANCELLED"),
        f"accepted={data.get('cancel_request_accepted')} outcome={data.get('outcome')} "
        f"status={data.get('observed_status')}",
    )
    await smoke_replace_active(client, report, agent_id)
    return agent_id


async def smoke_replace_active(client: Client, report: Report, agent_id: str) -> None:
    """Two short runs: one that starts a background job then waits, and its replacement."""
    ok, data = await call(
        client,
        "cursor_create_run",
        {
            "agent_id": agent_id,
            "prompt": (
                "Start `sleep 600` as a background command and do not wait for it. "
                "Then run `sleep 120` in the foreground and wait for it before replying."
            ),
        },
    )
    busy_run = str(data.get("run_id", ""))
    if not report.check("follow-up run (to replace)", ok, f"status={data.get('status', code_of(data))}"):
        return
    # Leave the agent time to start its background job before the run is replaced.
    await asyncio.sleep(30)
    ok, data = await call(
        client,
        "cursor_create_run",
        {"agent_id": agent_id, "prompt": "Reply only with the word OK3. Run no command.", "replace_active": True},
    )
    new_run = str(data.get("run_id", ""))
    report.check(
        "cursor_create_run (replace_active)",
        ok and data.get("replaced_run_id") == busy_run and new_run not in {"", busy_run},
        f"replaced={data.get('replaced_run_id') == busy_run}"
        if ok
        else f"code={code_of(data)} message={str(data.get('message', ''))[:120]}",
    )
    if not ok:
        return
    data = await wait_terminal(client, agent_id, new_run)
    report.check("replacement run finished", data.get("status") == "FINISHED", f"status={data.get('status')}")
    # The replaced run ended CANCELLED without a result: its activity summary comes on its own.
    ok, data = await call(client, "cursor_get_run", {"agent_id": agent_id, "run_id": busy_run})
    activity = data.get("activity") or {}
    report.check(
        "activity added to the replaced run",
        ok and data.get("status") == "CANCELLED" and bool(activity.get("complete")),
        f"status={data.get('status')} complete={activity.get('complete')} error={data.get('activity_error')}",
    )
    tasks = activity.get("background_tasks") or []
    if tasks:
        report.check(
            "background task detected",
            (activity.get("unfinished_background_tasks") or 0) >= 1,
            f"tasks={len(tasks)} last_state={tasks[-1].get('last_state')}",  # type: ignore[union-attr]
        )
    else:
        # Depends on how the agent ran the command, not on this server.
        report.add("background task detected", "WARN", "the agent did not start a background command")


async def smoke_supervision(
    client: Client,
    report: Report,
    *,
    agent_a: str,
    run_a: str,
    agent_b: str | None,
) -> None:
    """Free checks: reads only, on the runs already paid for."""
    ok, data = await call(
        client, "cursor_read_run_events", {"agent_id": agent_a, "run_id": run_a, "tail": 3}
    )
    kinds = [event["kind"] for event in data.get("events", [])] if ok else []  # type: ignore[union-attr]
    report.check(
        "cursor_read_run_events (tail)",
        ok and data.get("finished") is True and data.get("truncated") is False and bool(data.get("last_event_at")),
        f"kinds={kinds} scanned={data.get('scanned_events')}",
    )

    started = time.monotonic()
    ok, data = await call(
        client, "cursor_get_run", {"agent_id": agent_a, "run_id": run_a, "activity": True}
    )
    first_seconds = time.monotonic() - started
    activity = data.get("activity") or {}
    report.check(
        "cursor_get_run (activity)",
        ok and bool(activity.get("complete")) and bool(activity.get("last_event_at")),
        f"complete={activity.get('complete')} idle={activity.get('idle_seconds')} {first_seconds:.1f}s",
    )
    started = time.monotonic()
    ok, data = await call(
        client, "cursor_get_run", {"agent_id": agent_a, "run_id": run_a, "activity": True}
    )
    second_seconds = time.monotonic() - started
    report.check(
        "cursor_get_run (activity, cached)",
        ok and bool((data.get("activity") or {}).get("complete")) and second_seconds < max(first_seconds, 1.0),
        f"{second_seconds:.1f}s",
    )
    ok, data = await call(
        client, "cursor_get_run", {"agent_id": agent_a, "run_id": run_a, "activity": False}
    )
    report.check("cursor_get_run (activity=false)", ok and "activity" not in data)

    expected = {agent_a} | ({agent_b} if agent_b else set())
    ok, data = await call(client, "cursor_supervise", {"status": "all", "name": TAG})
    rows = {row["agent_id"]: row for row in data.get("items", [])} if ok else {}  # type: ignore[union-attr]
    summary = data.get("summary") or {}
    report.check(
        "cursor_supervise",
        ok and set(rows) == expected and all(row.get("status") for row in rows.values()),
        f"rows={len(rows)} by_status={summary.get('by_status')}",
    )
    if agent_b:
        ok, data = await call(client, "cursor_supervise", {"status": "all", "name": TAG, "limit": 1})
        cursor = data.get("next_cursor")
        first_page = {row["agent_id"] for row in data.get("items", [])} if ok else set()  # type: ignore[union-attr]
        ok2, data2 = (False, {})
        if ok and cursor:
            ok2, data2 = await call(
                client, "cursor_supervise", {"status": "all", "name": TAG, "limit": 1, "cursor": cursor}
            )
        second_page = {row["agent_id"] for row in data2.get("items", [])} if ok2 else set()  # type: ignore[union-attr]
        report.check(
            "cursor_supervise (exact limit, cursor)",
            len(first_page) == 1 and len(second_page) == 1 and first_page | second_page == expected,
            f"pages={len(first_page)}+{len(second_page)}",
        )
    ok, data = await call(client, "cursor_supervise", {"status": "all", "name": TAG, "activity": True})
    rows = data.get("items", []) if ok else []
    summary = data.get("summary") or {}
    report.check(
        "cursor_supervise (activity)",
        ok
        and len(rows) == len(expected)  # type: ignore[arg-type]
        and all((row.get("activity") or {}).get("complete") for row in rows)  # type: ignore[union-attr]
        and "incomplete" not in summary,
        f"by_status={summary.get('by_status')} unfinished_after_end={len(summary.get('unfinished_after_end') or [])}",  # type: ignore[arg-type]
    )


async def smoke_lifecycle(client: Client, report: Report, agent_id: str) -> None:
    ok, data = await call(client, "cursor_archive_agent", {"agent_id": agent_id})
    report.check(
        "cursor_archive_agent",
        ok and bool(data.get("outcome_confirmed")),
        f"status={data.get('observed_status')}",
    )
    ok, data = await call(
        client, "cursor_list_agents", {"include_archived": True, "limit": 20}
    )
    names = {item["agent_id"] for item in data.get("items", [])} if ok else set()  # type: ignore[union-attr]
    report.check(
        "cursor_list_agents (include_archived)",
        agent_id in names,
        f"found={agent_id in names}",
    )
    ok, data = await call(
        client, "cursor_create_run", {"agent_id": agent_id, "prompt": "x"}
    )
    report.check(
        "follow-up run refused on archived agent",
        not ok and code_of(data) == "CONTINUATION_REFUSED",
        code_of(data),
    )
    ok, data = await call(
        client, "cursor_archive_agent", {"agent_id": agent_id, "unarchive": True}
    )
    report.check(
        "cursor_archive_agent (unarchive)",
        ok and bool(data.get("outcome_confirmed")),
        f"status={data.get('observed_status')}",
    )
    ok, data = await call(
        client,
        "cursor_delete_agent",
        {
            "agent_id": agent_id,
            "confirm_agent_id": "bc-00000000-0000-0000-0000-000000000000",
        },
    )
    report.check("deletion without correct confirmation refused", not ok, code_of(data))


async def cleanup(client: Client, report: Report, created: list[str]) -> None:
    if os.environ.get("SMOKE_KEEP") == "1":
        for agent_id in created:
            print(f"      kept: https://cursor.com/agents/{agent_id}")
        return
    for agent_id in created:
        ok, data = await call(
            client,
            "cursor_delete_agent",
            {"agent_id": agent_id, "confirm_agent_id": agent_id},
        )
        report.check(
            "cursor_delete_agent",
            ok and data.get("deleted") is True,
            "" if ok else f"code={code_of(data)}",
        )


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
