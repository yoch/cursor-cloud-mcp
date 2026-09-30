# Vérification

Rapport du 30 septembre 2026. Les trois familles ci-dessous ne se remplacent pas.

## Contenu testé

Aucun commit Git : `git rev-parse HEAD` répond qu'il n'y a pas de révision. `.env` n'est pas suivi.

Empreinte SHA-256 du contenu présent au moment de `uv run pytest`, hors `.venv`, `.env`, `dist`, `examples/resolved` et ce fichier : `0a264fea002c57e83af97f0bda68a617863cd740df98153a1064b987f96e8a88`.

Versions réellement installées dans `.venv` :

- Python `3.13.12` (le paquet exige `>=3.12` ; le Python système `3.12.3` est aussi présent)
- `mcp==2.2.0`
- `httpx==0.28.1` pour le client REST de ce projet
- `httpx2==2.13.1` uniquement comme dépendance du SDK MCP
- `pytest==8.4.2`

Clients :

- Claude Code `2.1.285`
- Codex CLI `0.153.4`
- OpenCode `2.0.20`

`uvx pip-audit -r` sur l'export runtime `uv export --no-dev --no-emit-project` : « No known vulnerabilities found ». Des avertissements de cache `cachecontrol` ont été ignorés par l'outil. Cet audit ne prouve pas l'absence de tout défaut.

Le wheel `dist/cursor_cloud_mcp-0.1.0-py3-none-any.whl` s'installe dans un venv vierge `/tmp/cursor-cloud-mcp-wheel`. Le script `cursor-cloud-mcp` de ce venv, avec `CURSOR_MCP_FIXTURE=1`, a répondu à un `initialize` `2025-11-25` par une ligne JSON-RPC commençant par `{`.

## Tests simulés

Commande : `uv run pytest`. Résultat : `29 passed`.

Ces tests utilisent `httpx.MockTransport` ou `CURSOR_MCP_FIXTURE=1`. Aucun n'appelle `api.cursor.com`.

Couverture : les onze outils, le corps REST de création (`workOnCurrentBranch` faux, `autoCreatePR`, `startingRef`, modèle), SHA, URL, lecture seule, clé non interpolée, `.env` ignoré, états inconnus, résultat long sans trou, deadline sur un corps bloqué, 429, 5xx, POST non rejoué, identifiant conservé, conflit `agent_busy`, stdio du vrai point d'entrée en `2026-07-28` et `2025-11-25`, EOF, stdout sans bannière.

Statut de cette famille : **PASS**.

## Appels exécutés par les clients

Backend : le binaire `.venv/bin/cursor-cloud-mcp` avec `CURSOR_MCP_FIXTURE=1`. Aucun de ces appels n'est une requête vers Cursor. Les configurations personnelles n'ont pas été modifiées. Les exemples livrés gardent `CURSOR_MCP_ALLOW_WRITES` à `0`. Les sessions de test ont autorisé explicitement les outils de cette invocation (`--allowedTools` pour Claude, `--approve-for-me` et `-c` éphémère pour les mutations Codex, projet temporaire pour OpenCode). `--dangerously-skip-permissions` et `--dangerously-bypass-approvals-and-sandbox` n'ont pas été utilisés.

### Claude Code : PASS

Commandes `claude -p --strict-mcp-config --mcp-config … --permission-mode default`, depuis `/tmp/ccm-clients`.

- Lecture : `cursor_get_account` a renvoyé `api_key_name=fixture`.
- Boucle : création fictive, `cursor_get_run` sur ce run (`CREATING`), continuation du même `agent_id` avec `previous_latest_run_id` égal au premier run, puis `VALIDATION` / `validation_error` sur le prompt `ERREUR_ATTENDUE`.
- Lecture seule : `cursor_create_agent` avec `CURSOR_MCP_ALLOW_WRITES=0` a renvoyé `READ_ONLY`.

Ces tours ont consommé le quota Claude de la session (environ 0,13 à 0,19 USD par commande d'après le JSON du client). Ce n'est pas un coût Cursor Cloud.

### Codex CLI : PASS

`codex exec --ignore-user-config --skip-git-repo-check --ephemeral`, serveur MCP passé par `-c`, sans écrire `~/.codex/config.toml`.

- Lecture, sandbox `-s read-only` : l'événement JSONL `mcp_tool_call` `cursor_get_account` est `completed` avec `api_key_name=fixture`.
- Mutations : le même sandbox a refusé les outils non marqués lecture seule (« MCP tool call requires approval, but approval policy is never »). Relance avec `--approve-for-me`, toujours sans le contournement dangereux : création, lecture du run, continuation du même agent, puis erreur `VALIDATION` du fixture.
- Lecture seule : `cursor_create_agent` a échoué côté outil avec le code `READ_ONLY`.

### OpenCode : PASS

`opencode mcp list` dans `/tmp/ccm-opencode` a répondu « No MCP servers configured ». `opencode debug config` charge pourtant `opencode.json` et normalise `timeout: 10000` en délais de catalogue et d'exécution, tous deux à 10000 ms. Le modèle Go par défaut a répondu HTTP 403 : un abonnement OpenCode Go est requis. Les appels suivants utilisent `--model opencode/big-pickle`.

- Lecture : l'événement `toolCalls` nomme `cursor_cloud.cursor_get_account` et le texte est le compte fixture.
- Boucle : `cursor_create_agent`, `cursor_get_run` (`CREATING`), `cursor_create_run` sur le même agent avec le `previous_latest_run_id` du premier run, puis `cursor_create_run` en erreur `VALIDATION`.
- Lecture seule : `cursor_create_agent` a le statut d'appel `error` et le message contient `READ_ONLY`. Une tentative précédente du même modèle a inventé une erreur JSON-RPC sans appeler l'outil ; elle n'est pas comptée.

## Appels réels à Cursor

Script : `uv run python scripts/live_read.py`. Il exporte `CURSOR_API_KEY` depuis `.env` pour son processus. Le serveur MCP ne charge pas ce fichier. Aucun POST.

- `GET /v1/me` : **PASS**, nom de clé `MCP dev`, e-mail présent et non affiché.
- `GET /v1/models` : **PASS**, 43 modèles, premier id `default`.
- `GET /v1/agents?limit=5` : **PASS**, 5 agents, `nextCursor` présent, statuts `IDLE`.
- `GET /v1/repositories` : **PASS**, 75 dépôts.

Statut des lectures réelles : **PASS**.

Écritures Cloud (création, continuation, annulation réelles) : **NON EXÉCUTÉE**. Aucun dépôt de test ni budget d'écriture n'a été autorisé.

## Limitations

- Pas de commit, donc pas de SHA Git à citer.
- Le fixture n'est pas l'API. Les succès clients ci-dessus sont des succès de protocole MCP sur réponses synthétiques.
- OpenCode 2.0.20 ne liste pas forcément dans `opencode mcp list` un serveur que `debug config` et `run` utilisent. Son `timeout` numérique alimente aussi le délai d'exécution, d'après le chargeur, sans essai d'un outil plus long que 10 secondes.
- Les mutations Codex non interactives demandent `--approve-for-me` ou une politique équivalente. Le mode read-only du sandbox ne suffit pas.
- L'usage réel `GET /v1/agents/{id}/usage` n'a pas été appelé, pour ne pas choisir un agent au-delà de la page déjà lue.
- Aucune publication PyPI.
