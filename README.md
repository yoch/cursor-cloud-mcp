# cursor-cloud-mcp

Serveur MCP local, sur stdio, qui expose onze outils pour l'API REST Cursor Cloud Agents v1. Une seule implémentation sert Claude Code, Codex CLI et OpenCode. Ce n'est pas une plateforme d'orchestration, et le paquet n'est pas publié sur PyPI.

## Installation locale

Python 3.12 ou plus récent, et `uv`.

```bash
uv sync
uv run pytest
uv run cursor-cloud-mcp
```

Le binaire de l'environnement du dépôt est `.venv/bin/cursor-cloud-mcp`. On peut aussi lancer `uv run python -m cursor_cloud_mcp`. Ces commandes supposent une copie de ce dépôt, pas un paquet public.

Pour vérifier le wheel dans un environnement vierge :

```bash
uv build
uv venv /tmp/cursor-cloud-mcp-wheel
uv pip install --python /tmp/cursor-cloud-mcp-wheel/bin/python dist/*.whl
/tmp/cursor-cloud-mcp-wheel/bin/cursor-cloud-mcp
```

## Variables

| Variable | Rôle |
|---|---|
| `CURSOR_API_KEY` | Clé lue uniquement dans l'environnement du processus. Absente : le serveur démarre et liste ses outils ; le premier appel Cursor échoue clairement. |
| `CURSOR_MCP_ALLOW_WRITES` | `0` par défaut. `1` autorise création, continuation et annulation. |
| `CURSOR_MCP_LOG_LEVEL` | `INFO` par défaut. Les logs vont sur stderr. |

Le serveur ne charge pas de fichier `.env`. Une valeur vide, `${...}` ou `{env:...}` est refusée, sans être journalisée. Aucun outil ne change `CURSOR_MCP_ALLOW_WRITES` : il faut modifier l'environnement et redémarrer le client.

Les logs peuvent contenir le nom de l'outil, la durée, les identifiants, le statut HTTP et un request id. Ils ne contiennent ni le prompt, ni le corps, ni la clé, ni l'en-tête Authorization.

## Outils

Lectures : `cursor_get_account`, `cursor_list_models`, `cursor_list_repositories`, `cursor_list_agents`, `cursor_get_agent`, `cursor_list_runs`, `cursor_get_run`, `cursor_get_usage`.

Mutations : `cursor_create_agent`, `cursor_create_run`, `cursor_cancel_run`.

`cursor_create_agent` envoie un seul dépôt GitHub HTTPS, `repos[].startingRef` égal au SHA complet fourni, `workOnCurrentBranch=false` et `autoCreatePR` selon l'appelant (`false` par défaut). L'identifiant `bc-<uuid>` est celui de l'appelant ou généré une fois avant l'envoi. La réponse contient `agent_id`, `run_id` et l'URL, sans attendre la fin du run.

`cursor_create_run` relit l'agent et refuse la continuation s'il n'est pas mono-dépôt avec `workOnCurrentBranch` explicitement `false`. Un conflit « agent occupé » est rendu à l'appelant.

`cursor_get_run` découpe `result` localement (`result_offset`, `result_limit` jusqu'à 20000, défaut 12000) et indique `result_total_chars`, `result_truncated`, `next_result_offset`. Les références `git` sont l'état courant de l'agent, pas un instantané immuable du run. Ce serveur n'invente pas de `final_sha`.

`cursor_get_usage` recopie les jetons renvoyés. Un coût absent reste absent.

Les listes d'agents et de runs renvoient une page. `has_more` est faux quand `nextCursor` est absent.

## Boucle GitHub

Ce MCP ne remplace pas GitHub. L'appelant lit le dépôt, la branche de base et le SHA exact, puis enchaîne :

```text
Lire GitHub : dépôt, base, SHA exact
→ cursor_create_agent(..., starting_sha=SHA)
→ conserver agent_id et run_id
→ cursor_get_run(...) jusqu'à un état terminal
→ relire GitHub : HEAD, diff, checks, reviews
→ cursor_create_run(...) sur le même agent si des corrections sont nécessaires
→ relire GitHub après le nouveau run
```

`FINISHED` ne prouve ni que les tests ont tourné, ni que la pull request est correcte. Un état inconnu n'est pas un succès. Après un timeout ou une coupure de mutation, le code `MUTATION_OUTCOME_UNKNOWN` interdit un rejeu automatique : il faut relire l'agent dont l'identifiant est renvoyé. Changer cet identifiant peut créer un doublon. Une coupure du client n'annule pas le run Cloud.

L'annulation ne supprime pas les commits déjà poussés. Si la relecture après annulation échoue, la demande est acceptée mais son résultat n'est pas confirmé.

## Configurations

Les fragments dans `examples/` utilisent un chemin absolu de remplacement. Les copies résolues vers le binaire de cette machine sont dans `examples/resolved/` après installation, et ne modifient aucun profil personnel.

Pour autoriser une mutation réelle, passer `CURSOR_MCP_ALLOW_WRITES` à `1` dans la configuration du client concerné, puis redémarrer ce client. La clé se transmet par l'environnement, jamais en argument de ligne de commande.

Saisie sans laisser la clé dans l'historique :

```bash
read -r -s -p 'Clé Cursor : ' CURSOR_API_KEY
printf '\n'
export CURSOR_API_KEY
```

Claude Code, configuration projet `.mcp.json` : voir `examples/claude.mcp.json`. Vérifier avec `claude mcp list`, `claude mcp get cursor_cloud` et `/mcp`. Le fichier projet peut demander une approbation. Pour une configuration utilisateur, la forme conforme à l'aide installée est :

```bash
claude mcp add --transport stdio --scope user cursor_cloud -- /CHEMIN/ABSOLU/.venv/bin/cursor-cloud-mcp
```

Puis renseigner `CURSOR_API_KEY` dans l'environnement du processus, pas dans la commande. `CURSOR_MCP_ALLOW_WRITES` se pose dans l'entrée `env` du JSON, pas sur la ligne de commande avec la clé.

Codex : `examples/codex.config.toml`. La clé passe par `env_vars`, pas par une interpolation `${...}` dans le TOML. Vérifier avec `codex mcp list` et `/mcp`.

OpenCode : `examples/opencode.json`. La clé utilise `{env:CURSOR_API_KEY}`. La documentation publique décrit `timeout` comme le délai de découverte des outils. Sur OpenCode 2.0.20, `opencode debug config` charge un nombre unique `timeout: 10000` à la fois comme délai de catalogue et comme délai d'exécution, tous deux à 10000 ms. Ce constat vient du chargeur de cette version, pas d'un chronomètre sur un outil lent. Vérifier avec `opencode debug config`, puis un appel d'outil dans une session. `opencode mcp list` peut ne pas afficher un serveur pourtant chargé par la configuration du projet.

## Dépannage

- Le serveur liste ses outils mais chaque appel dit que la clé est absente : l'environnement du processus MCP ne contient pas `CURSOR_API_KEY`. Un `.env` du dépôt n'est pas lu.
- La clé est affichée comme non interpolée : la valeur est encore `${CURSOR_API_KEY}` ou `{env:CURSOR_API_KEY}`.
- Une mutation répond `READ_ONLY` : `CURSOR_MCP_ALLOW_WRITES` n'est pas exactement `1`, ou le client n'a pas été redémarré.
- `CONTINUATION_REFUSED` : l'agent n'a pas exactement un dépôt, ou `workOnCurrentBranch` n'est pas `false`.
- `MUTATION_OUTCOME_UNKNOWN` : ne pas renvoyer la même création avec un nouvel identifiant. Appeler `cursor_get_agent` avec l'identifiant renvoyé.
- `GET /v1/repositories` peut être lent et est fortement limité (1 requête par minute, 30 par heure). Le cache de cinq minutes ne couvre que le processus courant.
- stdout doit rester le canal MCP. Si un client annonce un JSON invalide, chercher un `print` ou un journal qui n'est pas sur stderr.

## Hors périmètre

Pas de serveur HTTP MCP, pas de base, pas de suivi continu, pas de flux SSE, pas de shell, pas de lecture du checkout local, pas de suppression d'agent, pas de fusion de pull request, pas de multi-dépôt, pas de plafond budgétaire imposé par ce processus.
