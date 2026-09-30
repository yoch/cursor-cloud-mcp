# Vérification

Rapport du 1er octobre 2026. Les trois familles ci-dessous ne se remplacent pas.

## Contenu testé

Empreinte SHA-256 du contenu hors `.git`, `.venv`, `dist`, `examples/resolved`, `.env` et ce fichier : `bacb94e49a8b9914b76d6c9b971dc4fa31ece01cf6fad90bdb2740301f31f34a`.

Versions utilisées pour `uv run pytest` :

- Python `3.13.12` dans `.venv`, après restauration de cet interpréteur
- Python `3.12.13`, via `uv run --python 3.12 pytest`, puis l'environnement a été remis sur 3.13.12
- `mcp==2.2.0`
- `httpx==0.28.1`
- `pytest==8.4.2`

Les dépendances n'ont pas changé depuis l'audit `pip-audit` du 30 septembre 2026, qui n'avait trouvé aucune vulnérabilité connue.

## Tests simulés

Commande : `uv run pytest`, puis `uv run --python 3.12 pytest`. Résultat : `43 passed` sur les deux interpréteurs.

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

Écritures Cloud, y compris la session payante minimale sans dépôt : **NON EXÉCUTÉE**. Elle attend une autorisation explicite.

## Limitations

- La boucle fixture dans Claude Code, Codex et OpenCode date du 30 septembre 2026 et visait les onze premiers outils.
- OpenCode 2.0.20 n'a pas montré, dans cette session, que `{env:CURSOR_API_KEY}` arrive jusqu'au processus du serveur. `debug config` montre pourtant une valeur masquée.
- Le filtre de logs masque la clé et les valeurs transférées d'au moins 8 caractères. Il ne masque pas une valeur plus courte.
- Aucune publication PyPI, aucun push dans cette session.
