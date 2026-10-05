"""Smoke test réel, PAYANT, des dix-neuf outils, sur le vrai serveur stdio.

Opt-in : `SMOKE_PAID=1`. Deux agents `composer-2.5`, quelques runs très courts,
et suppression de ces deux agents à la fin, sauf avec `SMOKE_KEEP=1` qui les
laisse visibles dans l'interface web et affiche leur lien. Aucune création n'est rejouée.
La clé vient de l'environnement, ou de `.env` lu par ce script seulement. Elle
n'est jamais affichée. Aucun prompt, aucune valeur secrète n'est imprimé.
"""

import asyncio
import json
import os
import sys
import tempfile
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
            "cursor_wait_run",
            {"agent_id": agent_id, "run_id": run_id, "max_wait_seconds": 60},
        )
        if not ok or not data.get("timed_out"):
            return data
    return data


async def main() -> int:
    root = Path(__file__).resolve().parents[1]
    if os.environ.get("SMOKE_PAID") != "1":
        print("Smoke payant non lancé : définir SMOKE_PAID=1.")
        return 2
    load_key(root)
    key = os.environ.get("CURSOR_API_KEY")
    if not key:
        print("CURSOR_API_KEY absente")
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
        for label, value in (("clé", key), ("secret", FWD_VALUE))
        if value in stderr
    ]
    report.check("stderr sans secret", not leaked, f"fuites={leaked}")
    stderr_path.unlink()
    print(f"\nRésumé : {len(report.rows) - len(report.failed)}/{len(report.rows)} PASS")
    if report.failed:
        print("ÉCHECS :", ", ".join(report.failed))
        return 1
    return 0


async def run_all(client: Client, report: Report, created: list[str]) -> None:
    listed = await client.list_tools()
    report.check("tools/list", len(listed.tools) == 19, f"outils={len(listed.tools)}")

    ok, data = await call(client, "cursor_get_account")
    report.check("cursor_get_account", ok, f"key_name_present={'api_key_name' in data}")

    ok, data = await call(client, "cursor_list_models")
    models = {str(item["id"]): item for item in data.get("items", [])} if ok else {}  # type: ignore[union-attr]
    composer = models.get(MODEL)
    report.check(
        "cursor_list_models",
        composer is not None,
        f"modèles={len(models)} {MODEL}=présent",
    )
    params = [item["id"] for item in (composer or {}).get("parameters") or []]  # type: ignore[index]
    fast_values = [
        value["value"]
        for item in (composer or {}).get("parameters") or []  # type: ignore[union-attr]
        if item["id"] == "fast"
        for value in item["values"]
    ]
    print(f"      {MODEL} paramètres={params} fast={fast_values}")

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
        f"dépôts={len(repos)} cible_présente={present}",
    )

    # Garde-fous locaux, gratuits.
    ok, data = await call(
        client,
        "cursor_create_agent",
        {"prompt": "x", "repository": REPO_URL, "starting_ref": SHA, "model_id": MODEL},
    )
    report.check(
        "SHA refusé localement", not ok and code_of(data) == "VALIDATION", code_of(data)
    )
    ok, data = await call(
        client,
        "cursor_create_agent",
        {"prompt": "x", "model_id": MODEL, "reasoning_level": "high"},
    )
    report.check(
        "reasoning_level inconnu refusé",
        not ok and code_of(data) == "VALIDATION",
        code_of(data),
    )

    agent_a = await smoke_repo_agent(client, report, created, fast_values)
    await smoke_env_agent(client, report, created)
    if agent_a is not None:
        await smoke_lifecycle(client, report, agent_a)


async def smoke_repo_agent(
    client: Client,
    report: Report,
    created: list[str],
    fast_values: list[str],
) -> str | None:
    agent_id = f"bc-{uuid.uuid4()}"
    args: dict[str, object] = {
        "prompt": "Réponds uniquement par le mot OK. N'exécute aucune commande et ne modifie aucun fichier.",
        "repository": REPO_URL,
        "starting_ref": BRANCH,
        "name": "smoke-repo",
        "model_id": MODEL,
        "agent_id": agent_id,
    }
    if "false" in fast_values:
        args["model_params"] = [{"id": "fast", "value": "false"}]
    ok, data = await call(client, "cursor_create_agent", args)
    if ok:
        created.append(agent_id)
    if not report.check(
        "cursor_create_agent (dépôt + branche)",
        ok,
        f"statut={data.get('run_status', '-')}"
        if ok
        else f"code={code_of(data)} http={data.get('http_status')}",
    ):
        return None
    run_id = str(data["run_id"])
    print(f"      url={data.get('url')}")

    ok, data = await call(client, "cursor_get_agent", {"agent_id": agent_id})
    # L'API ne renvoie pas startingRef en lecture : la branche est prouvée par le 201 à la création.
    repos = data.get("repos") or []
    same_repo = len(repos) == 1 and str(repos[0].get("url", "")).endswith(
        REPO_URL.removeprefix("https://")
    )  # type: ignore[index]
    report.check(
        "cursor_get_agent",
        ok and same_repo,
        f"dépôt_rattaché={same_repo} url_présente={'url' in data}",
    )
    # La continuation est refusée si ce champ manque : le contrat réel doit le fournir.
    report.check(
        "workOnCurrentBranch renvoyé",
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
        f"événements={len(data.get('events', []))} types={kinds}",
    )  # type: ignore[arg-type]

    data = await wait_terminal(client, agent_id, run_id)
    report.check(
        "cursor_wait_run",
        data.get("status") == "FINISHED",
        f"statut={data.get('status')} timed_out={data.get('timed_out')}",
    )

    ok, data = await call(
        client, "cursor_get_run", {"agent_id": agent_id, "run_id": run_id}
    )
    report.check(
        "cursor_get_run",
        ok and data.get("terminal") is True and bool(data.get("result_present")),
        f"statut={data.get('status')} résultat_présent={data.get('result_present')}",
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
        report.check("cursor_get_usage", total > 0, f"jetons={total}")
    else:
        report.add(
            "cursor_get_usage",
            "WARN",
            f"code={code_of(data)} (fonction à accès anticipé)",
        )

    ok, data = await call(client, "cursor_list_agents", {"limit": 10})
    names = {item["agent_id"] for item in data.get("items", [])} if ok else set()  # type: ignore[union-attr]
    report.check("cursor_list_agents", agent_id in names, f"trouvé={agent_id in names}")

    ok, data = await call(
        client,
        "cursor_create_run",
        {
            "agent_id": agent_id,
            "prompt": "Réponds uniquement par le mot OK2. Ne modifie aucun fichier.",
        },
    )
    run2 = str(data.get("run_id", ""))
    report.check(
        "cursor_create_run (continuation)",
        ok and run2 not in {"", run_id},
        f"nouveau_run={run2 != run_id}",
    )
    if ok:
        data = await wait_terminal(client, agent_id, run2)
        report.check(
            "continuation terminée",
            data.get("status") == "FINISHED",
            f"statut={data.get('status')}",
        )
    return agent_id


async def smoke_env_agent(client: Client, report: Report, created: list[str]) -> None:
    ok, data = await call(
        client,
        "cursor_create_agent",
        {
            "prompt": (
                'Exécute `test -n "$SMOKE_FWD" && echo FWD_SET || echo FWD_MISSING; '
                'test -n "$SMOKE_PUB" && echo PUB_SET || echo PUB_MISSING` '
                "puis réponds avec les deux mots obtenus. N'affiche jamais la valeur des variables."
            ),
            "name": f"smoke-env-{uuid.uuid4().hex[:8]}",
            "model_id": MODEL,
            "env_vars": {"SMOKE_PUB": PUB_VALUE},
            "forward_env": ["SMOKE_FWD"],
        },
    )
    if not report.check(
        "cursor_create_agent (env_vars + forward_env, sans agentId)",
        ok,
        f"statut={data.get('run_status', '-')}"
        if ok
        else f"code={code_of(data)} http={data.get('http_status')}",
    ):
        return
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
        "variables transmises à l'agent",
        ok and "FWD_SET" in text and "PUB_SET" in text and FWD_VALUE not in text,
        f"FWD_SET={'FWD_SET' in text} PUB_SET={'PUB_SET' in text} valeur_recopiée={FWD_VALUE in text}",
    )

    ok, data = await call(
        client,
        "cursor_create_agent",
        {"prompt": "x", "forward_env": ["PAS_AUTORISEE"], "name": "n"},
    )
    report.check(
        "forward_env hors liste refusé",
        not ok and code_of(data) == "VALIDATION",
        code_of(data),
    )

    ok, data = await call(
        client,
        "cursor_create_run",
        {
            "agent_id": agent_id,
            "prompt": "Écris le mot ARTEFACT dans /agent/artifacts/smoke.txt puis réponds par ARTEFACT.",
        },
    )
    run2 = str(data.get("run_id", ""))
    report.check(
        "continuation (artefact)", ok, f"statut={data.get('status', code_of(data))}"
    )
    if ok:
        await wait_terminal(client, agent_id, run2)
    ok, data = await call(client, "cursor_list_artifacts", {"agent_id": agent_id})
    items = [item["path"] for item in data.get("items", [])] if ok else []  # type: ignore[union-attr]
    report.check("cursor_list_artifacts", ok, f"artefacts={len(items)}")
    if items:
        path = items[0]
        ok, data = await call(
            client, "cursor_get_artifact_url", {"agent_id": agent_id, "path": path}
        )
        report.check("cursor_get_artifact_url", ok, f"url_présente={'url' in data}")
        ok, data = await call(
            client, "cursor_read_artifact", {"agent_id": agent_id, "path": path}
        )
        report.check(
            "cursor_read_artifact", ok and "ARTEFACT" in str(data.get("text", "")), ""
        )
    else:
        report.add(
            "cursor_get_artifact_url",
            "WARN",
            "aucun artefact listé (limite connue de l'API)",
        )
        ok, data = await call(
            client,
            "cursor_read_artifact",
            {"agent_id": agent_id, "path": "artifacts/smoke.txt"},
        )
        report.check(
            "cursor_read_artifact (absent → erreur propre)",
            not ok
            and code_of(data) in {"NOT_FOUND", "ARTIFACT_NOT_FOUND", "UPSTREAM_ERROR"},
            f"code={code_of(data)}",
        )

    ok, data = await call(
        client,
        "cursor_create_run",
        {
            "agent_id": agent_id,
            "prompt": "Exécute `sleep 120` dans le shell et attends la fin avant de répondre.",
        },
    )
    run3 = str(data.get("run_id", ""))
    if not report.check(
        "continuation (annulable)", ok, f"statut={data.get('status', code_of(data))}"
    ):
        return
    await asyncio.sleep(8)
    ok, data = await call(
        client, "cursor_cancel_run", {"agent_id": agent_id, "run_id": run3}
    )
    report.check(
        "cursor_cancel_run",
        ok
        and bool(data.get("cancel_request_accepted"))
        and bool(data.get("outcome_confirmed")) == (data.get("observed_status") == "CANCELLED"),
        f"accepté={data.get('cancel_request_accepted')} issue={data.get('outcome')} "
        f"statut={data.get('observed_status')}",
    )


async def smoke_lifecycle(client: Client, report: Report, agent_id: str) -> None:
    ok, data = await call(client, "cursor_archive_agent", {"agent_id": agent_id})
    report.check(
        "cursor_archive_agent",
        ok and bool(data.get("outcome_confirmed")),
        f"statut={data.get('observed_status')}",
    )
    ok, data = await call(
        client, "cursor_list_agents", {"include_archived": True, "limit": 20}
    )
    names = {item["agent_id"] for item in data.get("items", [])} if ok else set()  # type: ignore[union-attr]
    report.check(
        "cursor_list_agents (include_archived)",
        agent_id in names,
        f"trouvé={agent_id in names}",
    )
    ok, data = await call(
        client, "cursor_create_run", {"agent_id": agent_id, "prompt": "x"}
    )
    report.check(
        "continuation refusée sur agent archivé",
        not ok and code_of(data) == "CONTINUATION_REFUSED",
        code_of(data),
    )
    ok, data = await call(client, "cursor_unarchive_agent", {"agent_id": agent_id})
    report.check(
        "cursor_unarchive_agent",
        ok and bool(data.get("outcome_confirmed")),
        f"statut={data.get('observed_status')}",
    )
    ok, data = await call(
        client,
        "cursor_delete_agent",
        {
            "agent_id": agent_id,
            "confirm_agent_id": "bc-00000000-0000-0000-0000-000000000000",
        },
    )
    report.check("suppression sans bonne confirmation refusée", not ok, code_of(data))


async def cleanup(client: Client, report: Report, created: list[str]) -> None:
    if os.environ.get("SMOKE_KEEP") == "1":
        for agent_id in created:
            print(f"      conservé : https://cursor.com/agents/{agent_id}")
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
