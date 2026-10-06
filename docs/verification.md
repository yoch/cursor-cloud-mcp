# Vérification

## Consolidation du 6 octobre 2026 (ré-audit de `23aba59`)

Quatre écarts du ré-audit corrigés, chacun avec un test qui échoue sur `23aba59` : secret multiligne masqué avant normalisation ; réponse décrivant un autre agent ou run refusée (aucun POST) ; échéance SSE couvrant ouverture et corps d'erreur, `Content-Type` contrôlé ; dernière observation rendue avec `reread_error` après une erreur transitoire. Version 0.2.0, migration documentée, `scripts/wheel_smoke.py` en CI. Aucun appel Cursor réel : le contrôle d'identité suppose que l'API renvoie l'`id` demandé, à confirmer par `scripts/smoke_live.py` sur autorisation explicite.

## Smoke réel de la PR #2 et livraison interface du 5 octobre 2026

### Smoke réel de la PR #2 (`9db67c7`), avant toute modification

- `uv run pytest` : 70 passed ; ruff propre ; CI verte (3.12 et 3.13).
- `workOnCurrentBranch` dans `GET /v1/agents/{id}` : **présent**. Relu par GET seuls sur les 162 agents du compte, archivés compris, avec 0, 1 ou 2 dépôts : 160 à `false`, 2 à `true`, aucun absent. La règle de continuation ne bloque donc pas les agents légitimes.
- Règle de continuation sur ces agents réels, avec un transport qui bloque tout POST : `false` + `IDLE` ou `ACTIVE` atteint le POST (intercepté) ; `true` est refusé (`CONTINUATION_REFUSED`) avant tout envoi ; un agent archivé aussi.
- `scripts/smoke_live.py` : **34 sur 34 PASS**, un `WARN` (liste d'artefacts vide, limite connue). Un premier essai a échoué partout en `TIMEOUT` : le script ne transmettait au serveur que `PATH`, `HOME` et `LANG`, donc pas le proxy du bac à sable ; aucune requête n'avait atteint Cursor. Le script transmet désormais les variables proxy et CA si elles existent.

### Livraison interface (évaluation à l'usage et SDK officiel)

Mesures sur le serveur stdio réel, en lecture seule, puis smoke complet.

- 19 outils deviennent 16 : `cursor_wait_run` → `cursor_get_run(wait_seconds)`, `cursor_get_artifact_url` → `cursor_read_artifact(url_only)` (et URL rendue d'office pour un binaire ou un fichier de plus de 5 Mo), `cursor_unarchive_agent` → `cursor_archive_agent(unarchive=true)`. Retrait de `thinking` (couvert par `model_params`) et de l'alias `starting_sha`.
- Réponses en JSON compact, sans champ nul, identiques en texte et en `structuredContent`. Le SDK MCP écrivait un JSON indenté, `null` compris.
- `cursor_list_models` : **242 629 → 9 280 caractères** pour les 43 modèles du compte. `model_id` (id ou alias non ambigu) rend un modèle et ses variantes. Combinaisons absentes des variantes du modèle refusées avant POST.
- Flux : sur un run réel, le texte de l'assistant arrivait mot par mot, un événement par fragment ; les fragments consécutifs sont fusionnés, chaque appel d'outil n'apparaît plus qu'une fois (dernier état), `status` et `result` ne répètent plus leur JSON brut.
- Recherche : l'API refuse tout filtre de `GET /v1/agents` hors `limit`, `cursor`, `includeArchived` et `prUrl` (400 vérifié pour `name`, `q`, `search`, `status`, `sort`). `name` est donc filtré localement sur cinq pages au plus ; `pr_url` est transmis. `cursor_list_repositories` accepte `query`.
- Repris de la lecture du SDK Python officiel `cursor-sdk` 1.0.36, qui appelle le même REST v1 : `cost` de `GET /v1/agents/{id}/usage` (présent en réel, ignoré jusqu'ici), `Run.error`, `helpUrl` et `provider` des erreurs, consigne de reprise sur `invalid_last_event_id`. Matrice de `docs/api-contract.md` corrigée en conséquence.
- `uv run pytest` : **83 passed** sous Python 3.13 et 3.12 (nouveau `tests/test_interface.py`). Ruff propre.
- `Idempotency-Key`, test réel autorisé (`composer-2.5`) : **ignoré** par l'API, à la création (avec et sans `envVars`) comme à la continuation. Détail et conclusion dans `docs/api-contract.md`. Cinq agents créés et supprimés, 4,26 centimes.
- Changement de modèle sur une continuation, test réel autorisé : **accepté, validé et persistant** (`composer-2.5` → `claude-haiku-4-5`, puis run sans `model` resté sur Haiku). Détail dans `docs/api-contract.md`. `cursor_create_run` accepte désormais `model_id`, `model_params` et `reasoning_level`. 3,54 centimes, agent supprimé.
- `scripts/smoke_live.py` sur cette livraison : **34 sur 34 PASS**, un `WARN` (artefacts). Coût relu : 0,74 centime pour l'agent avec dépôt ; agent retrouvé par `name` parmi 163 ; annulation `CANCELLED` confirmée ; stderr sans secret. Les quatre agents créés par les deux smokes du jour ont été supprimés ; les deux agents `smoke-*` du 1er octobre, gardés volontairement, n'ont pas été touchés.

## Livraison fiabilité du 5 octobre 2026

Suite à l'audit externe du commit `f9a582d`. Aucun appel Cursor réel, aucune écriture payante.

- `mcp==2.3.0`, `httpx==0.28.1`, `pytest==8.4.2`, lockfile régénéré.
- `uv run pytest` : **70 passed** sous Python 3.13 et 3.12. Nouveaux tests dans `tests/test_reliability.py` : POST `201` coupé après quelques octets (une seule requête, `MUTATION_OUTCOME_UNKNOWN` avec `agent_id`, statut et request id), continuation coupée (`previous_latest_run_id` conservé), GET coupé, erreur de fermeture après corps complet, budget épuisé sans envoi, catalogue lent qui consomme le budget de création, continuation refusée si `workOnCurrentBranch` est absent ou le statut inconnu, désarchivage non confirmé sur statut inconnu, annulation en course avec `FINISHED`, secret reflété par Cursor masqué dans l'erreur, argument inconnu refusé sans modifier `ArgModelBase`, alias `starting_sha`, SSE UTF-8 coupé, `error` distinct de `finished`, coupure SSE avec événements partiels, curseur conservé, `clipped`, artefact en mode simulé sans réseau, abandon d'un appel MCP sans annulation distante (`outcome=cancelled` journalisé), lecteur de cache annulé sans effet sur l'autre, traceback masquée, secret court masqué comme mot entier sans casser le JSON d'erreur ni les clés, secrets permanents conservés quand les valeurs par appel sont évincées. Les tests stdio en sous-processus couvrent aussi `starting_ref`, l'argument inconnu, l'artefact simulé et un secret court passé par `env_vars`, absent de stderr en DEBUG.
- `uvx ruff@0.16.10 check src tests scripts` : propre. Une CI GitHub Actions rejoue installation verrouillée, ruff, tests (3.12 et 3.13) et installation du wheel construit.

Requalifié en réel le 5 octobre 2026 (section ci-dessus) : `workOnCurrentBranch` est présent dans `GET /v1/agents/{id}`.

## Rapport du 1er octobre 2026

Les trois familles ci-dessous ne se remplacent pas.

## Contenu testé

Empreinte SHA-256 des fichiers suivis ou non ignorés, hors `examples/resolved` et ce fichier (`git ls-files -co --exclude-standard`, tri `LC_ALL=C`, `sha256sum` de chaque fichier, puis `sha256sum` de la liste) : `c63ba4012df8044b415c7d3628fe01a93f51ceb85e00d2a85e0ec4b997534b03`.

Versions utilisées pour `uv run pytest` :

- Python `3.13.12` dans `.venv`, après restauration de cet interpréteur
- Python `3.12.13`, via `uv run --python 3.12 pytest`, puis l'environnement a été remis sur 3.13.12
- `mcp==2.2.0`
- `httpx==0.28.1`
- `pytest==8.4.2`

Les dépendances n'ont pas changé depuis l'audit `pip-audit` du 30 septembre 2026, qui n'avait trouvé aucune vulnérabilité connue.

## Tests simulés

Commande : `uv run pytest`, puis `uv run --python 3.12 pytest`. Résultat : `45 passed` sur les deux interpréteurs.

Ces tests utilisent `httpx.MockTransport` ou `CURSOR_MCP_FIXTURE=1`. Aucun n'appelle `api.cursor.com`.

Couverture ajoutée : validation du catalogue, `reasoning_level`, règles d'environnement, `forward_env`, flux SSE coupé puis repris, `STREAM_EXPIRED`, attente d'un run déjà terminal, téléchargement d'artefact sans en-tête d'autorisation, hôte refusé, suppression sans les deux garde-fous, identifiant d'annulation incohérent, filtre de logs sur un logger enfant, refus fixture plus vraie clé, délai de 1 seconde sur un 429 sans `Retry-After`. Le catalogue stdio compte 19 outils, en `2026-07-28` et `2025-11-25`.

Statut de cette famille : **PASS**.

## Appels exécutés par les clients

Les boucles fixture déjà rapportées le 30 septembre 2026 n'ont pas été rejouées. Cette session a vérifié un seul appel réel, `cursor_get_account`, qui est un GET. Aucune configuration personnelle n'a été modifiée. `CURSOR_MCP_ALLOW_WRITES` valait `0`. La clé n'apparaît pas dans les sorties conservées.

### Serveur stdio, hors LLM : PASS

`python -m cursor_cloud_mcp` lancé par le client MCP Python, avec la clé uniquement dans l'environnement du sous-processus. `cursor_get_account` a renvoyé `api_key_name` `MCP dev`. La clé n'est pas dans le texte de l'outil.

### Claude Code : PASS

`claude -p --strict-mcp-config --mcp-config /tmp/ccm-real/mcp.json`, outil autorisé `mcp__cursor_cloud__cursor_get_account`. La réponse est `MCP dev`.

### Codex CLI : PASS

`codex exec --ignore-user-config --skip-git-repo-check --ephemeral -s read-only`, serveur passé par `-c`, sans écrire `~/.codex/config.toml`. La réponse est `MCP dev`.

### OpenCode : NON CONCLUANT

`opencode debug config` dans `/tmp/ccm-opencode` charge le serveur, masque la clé et normalise `timeout` en 100000 ms pour le catalogue et pour l'exécution. Deux commandes `opencode run --model opencode/big-pickle` ont bien nommé `cursor_get_account`. Le modèle a rapporté `CONFIGURATION_MISSING`. Le corps d'erreur brut de l'outil n'était pas dans la sortie capturée. Claude Code et Codex, avec la même clé, ont réussi au même moment.

## Appels réels à Cursor

Script : `uv run python scripts/live_read.py`. Il charge `CURSOR_API_KEY` depuis `.env` pour son propre processus. Le serveur MCP ne charge pas ce fichier. Aucun POST.

- `GET /v1/me` : **PASS**, nom de clé `MCP dev`, e-mail présent et non affiché.
- `GET /v1/models` : **PASS**, 43 modèles, premier id `default`.
- `GET /v1/agents?limit=5` : **PASS**, 5 agents `IDLE`, `nextCursor` présent.
- `GET /v1/repositories` : **PASS**, 76 dépôts.
- `GET /v1/agents/{id}/artifacts` sur le premier agent de cette page : **PASS**, 52 artefacts. Les chemins ne sont pas affichés.
- `GET /v1/agents/{id}/runs/{runId}/stream` sur le dernier run de cet agent : **PASS** au sens du contrat, code `STREAM_EXPIRED`, HTTP 410. Le flux d'un run ancien n'est plus rejouable. Aucun événement n'a été lu.

L'appel stdio `cursor_get_account` ci-dessus est aussi un GET réel. Il est compté une fois, dans la famille des clients.

Session payante minimale sans dépôt, autorisée par l'utilisateur, modèle `composer-2.5`, une seule exécution, le 1er octobre 2026 :

- `cursor_create_agent` : **PARTIEL**. L'agent a bien été créé, mais la réponse a dépassé la deadline de 40 secondes et l'outil a renvoyé `MUTATION_OUTCOME_UNKNOWN`. Sans rejouer la création, l'état a été relu avec `cursor_list_agents`. Correctif : deadline de création portée à 90 secondes, avec un test.
- Lecture d'état, flux SSE et attente du run : **PASS**. Le run s'est terminé (`FINISHED`) en 37,7 secondes, 29 422 jetons au total.
- `cursor_archive_agent` : **PASS**, statut `ARCHIVED` relu. Aucun agent n'a été supprimé.
- Artefacts : **ÉCHEC côté API**. Le flux montre une écriture de `/agent/artifacts/result.txt`, mais `GET /artifacts` est resté vide, y compris lors d'une seconde lecture après archivage, et le téléchargement a répondu 404. Limite documentée dans le README et les instructions du serveur.

Création avec dépôt, hors de la session ci-dessus, rapportée le 1er octobre 2026 (issue #1) : un SHA complet dans `startingRef` a répondu `400 validation_error` et n'a rien créé. Le même appel avec le nom de branche dont la tête était ce SHA a répondu `201`. Le MCP refuse désormais un SHA complet et envoie le nom de branche.

Smoke réel de chaque outil, `scripts/smoke_live.py`, serveur stdio réel, `composer-2.5`, deux agents et neuf runs courts, le 1er octobre 2026 :

- Première exécution : 32 sur 33. L'unique échec était une assertion fausse du script : l'API ne renvoie jamais `startingRef` dans `GET /v1/agents/{id}`, vérifié sur trois agents existants. La branche est prouvée par le `201` de la création. Elle a aussi révélé un défaut réel : `cursor_cancel_run` annonçait `outcome_confirmed=True` alors que le run relu était encore `RUNNING`. Correctif : relecture jusqu'à quatre fois, confirmé seulement si l'état est terminal, avec deux tests.
- Seconde exécution, avec les correctifs : **31 sur 31 PASS**, un `WARN`. Création avec dépôt et branche (`201`), `forward_env` et `env_vars` reçus par l'agent sans recopie de la valeur secrète, continuation, flux SSE, attente, usage, annulation (`CANCELLED` confirmé), archivage, désarchivage, `include_archived`, refus de continuation sur agent archivé, refus de suppression sans la bonne confirmation, et stderr sans la clé ni la valeur transmise. Le `WARN` est la liste d'artefacts, restée vide.
- `cursor_delete_agent` : **PASS** deux fois, à la première exécution, avec les deux garde-fous. La seconde exécution a gardé ses deux agents (`SMOKE_KEEP=1`).

Observations de l'API : l'ordre de `GET /v1/agents` n'est pas celui de la date de création. `includeArchived=true` ajoute bien les agents archivés.

Non exécutés : `env_type` `pool` et `machine` (aucun worker disponible), plusieurs dépôts, `auto_create_pr`, `mode: plan`, et le niveau de réflexion sur un modèle qui en expose un (`composer-2.5` n'a que `fast`).

## Limitations

- La boucle fixture dans Claude Code, Codex et OpenCode date du 30 septembre 2026 et visait les onze premiers outils.
- OpenCode 2.0.20 n'a pas montré, dans cette session, que `{env:CURSOR_API_KEY}` arrive jusqu'au processus du serveur. `debug config` montre pourtant une valeur masquée.
- (Corrigé le 5 octobre 2026) Le filtre de logs ne masquait que les secrets d'au moins 8 caractères, ni les tracebacks, ni les `env_vars` fournies par appel. Le formateur masque désormais tous les secrets connus du processus, traceback comprise ; sous 8 caractères, comme mot entier seulement.
- Aucune publication PyPI.
