# Vérification

Rapport du 1er octobre 2026. Les trois familles ci-dessous ne se remplacent pas.

## Contenu testé

Empreinte SHA-256 des fichiers suivis ou non ignorés, hors `examples/resolved` et ce fichier (`git ls-files -co --exclude-standard`, tri `LC_ALL=C`, `sha256sum` de chaque fichier, puis `sha256sum` de la liste) : `3613b7223fce4b0d0ce340a41f5eeba1c99c48fe644324d60d1feaaa685b85bf`.

Versions utilisées pour `uv run pytest` :

- Python `3.13.12` dans `.venv`, après restauration de cet interpréteur
- Python `3.12.13`, via `uv run --python 3.12 pytest`, puis l'environnement a été remis sur 3.13.12
- `mcp==2.2.0`
- `httpx==0.28.1`
- `pytest==8.4.2`

Les dépendances n'ont pas changé depuis l'audit `pip-audit` du 30 septembre 2026, qui n'avait trouvé aucune vulnérabilité connue.

## Tests simulés

Commande : `uv run pytest`, puis `uv run --python 3.12 pytest`. Résultat : `44 passed` sur les deux interpréteurs.

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

Autres écritures Cloud (continuation, suppression, `envVars`, dépôts) : **NON EXÉCUTÉES**.

## Limitations

- La boucle fixture dans Claude Code, Codex et OpenCode date du 30 septembre 2026 et visait les onze premiers outils.
- OpenCode 2.0.20 n'a pas montré, dans cette session, que `{env:CURSOR_API_KEY}` arrive jusqu'au processus du serveur. `debug config` montre pourtant une valeur masquée.
- Le filtre de logs masque la clé et les valeurs transférées d'au moins 8 caractères. Il ne masque pas une valeur plus courte.
- Aucune publication PyPI.
