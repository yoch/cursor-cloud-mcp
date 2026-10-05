# cursor-cloud-mcp

Pour faire installer ou utiliser ce MCP par un agent, donne-lui [`MODE-D-EMPLOI.md`](MODE-D-EMPLOI.md).

Serveur MCP local, sur stdio, qui expose seize outils pour l'API REST Cursor Cloud Agents v1. Une seule implémentation sert Claude Code, Codex CLI et OpenCode. Il permet à un agent appelant de créer une session Cloud, choisir le modèle et le niveau de réflexion, envoyer des commandes, lire la progression et les fichiers produits, puis archiver ou supprimer la session. Ce n'est pas une plateforme d'orchestration, et le paquet n'est pas publié sur PyPI.

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
| `CURSOR_MCP_ALLOW_WRITES` | `0` par défaut. `1` autorise création, continuation, annulation et archivage. |
| `CURSOR_MCP_ALLOW_DELETE` | `0` par défaut. `1` autorise `cursor_delete_agent`, en plus de `CURSOR_MCP_ALLOW_WRITES=1` et de `confirm_agent_id`. |
| `CURSOR_MCP_FORWARD_ENV` | Liste de noms, séparés par des virgules, dont `forward_env` a le droit de lire la valeur dans ce processus. La valeur ne passe pas par l'argument de l'outil. |
| `CURSOR_MCP_LOG_LEVEL` | `INFO` par défaut. Les logs vont sur stderr. |
| `CURSOR_MCP_FIXTURE` | `1` remplace l'API par des réponses locales. Refusé s'il est combiné à `CURSOR_API_KEY`. Le démarrage l'annonce sur stderr. |

Le serveur ne charge pas de fichier `.env`. Une valeur vide, `${...}` ou `{env:...}` est refusée, sans être journalisée. Aucun outil ne change `CURSOR_MCP_ALLOW_WRITES` : il faut modifier l'environnement et redémarrer le client.

Les logs peuvent contenir le nom de l'outil, son issue (`ok`, code d'erreur métier, `unexpected:<type>` ou `cancelled`), la durée, les identifiants, le statut HTTP et un request id. Ils ne contiennent ni le prompt, ni le corps, ni la clé, ni l'en-tête Authorization. En seconde barrière, le formateur masque la clé, les valeurs de `CURSOR_MCP_FORWARD_ENV` et les valeurs `env_vars` transmises pendant la vie du processus, y compris dans les tracebacks. Une valeur de moins de 8 caractères n'est masquée que comme mot entier, pour ne pas mutiler le reste du texte (`en` ne touche pas `agent`). La clé et `CURSOR_MCP_FORWARD_ENV` restent masquées en permanence ; les 4096 valeurs `env_vars` les plus récentes le sont aussi. Le même masquage s'applique aux valeurs des erreurs renvoyées à l'appelant, sans toucher à la forme du JSON.

## Outils

Lectures : `cursor_get_account`, `cursor_list_models`, `cursor_list_repositories`, `cursor_list_agents`, `cursor_get_agent`, `cursor_list_runs`, `cursor_get_run`, `cursor_read_run_events`, `cursor_get_usage`, `cursor_list_artifacts`, `cursor_read_artifact`.

Mutations : `cursor_create_agent`, `cursor_create_run`, `cursor_cancel_run`, `cursor_archive_agent`, `cursor_delete_agent`.

Les réponses sont du JSON compact, identique dans le texte et dans `structuredContent`. Un champ sans valeur est omis plutôt que rendu `null`.

`cursor_list_models` rend un catalogue compact, mis en cache dix minutes : pour chaque modèle, `params` (valeurs possibles de chaque paramètre), `defaults` (valeurs de la variante par défaut), `reasoning_param` (nom réel du niveau de réflexion : `effort`, `reasoning_effort` ou `reasoning`) et `aliases`. `restricted_combinations` signale qu'une partie des combinaisons n'existe pas. Avec `model_id`, l'outil ne rend que ce modèle, avec ses variantes valides. Chaque modèle publie sa propre liste de variantes, c'est-à-dire les combinaisons qu'il accepte. Rendues pour les 43 modèles, elles portaient le catalogue au-delà de 240 Ko par appel ; la forme compacte en fait environ 9, et les variantes restent la référence du contrôle avant envoi.

`cursor_create_agent` peut démarrer sans dépôt, avec un dépôt (`repository` et `starting_ref`) ou avec jusqu'à vingt dépôts (`repositories`, éléments `{url, starting_ref}`). `starting_ref` est un nom de branche, envoyé tel quel dans `startingRef`. Un SHA complet de 40 ou 64 caractères est refusé localement : l'API a répondu `400 validation_error` à un SHA le 1er octobre 2026, alors que la documentation REST annonce qu'une référence peut être un SHA. Ce refus est un contournement daté, à requalifier par un test réel. `env_type` vaut `cloud`, `pool` ou `machine`. Un pool nommé est exigé pour plusieurs dépôts. Un environnement cloud nommé ne se combine pas à des dépôts. `model_id` accepte un id ou un alias qui ne désigne qu'un modèle (`opus` en désigne plusieurs et est refusé). `reasoning_level` et `model_params` sont vérifiés contre le catalogue avant l'envoi, combinaison comprise : une combinaison absente des variantes publiées est refusée sans POST. `workOnCurrentBranch` est imposé à `false`. `autoCreatePR` suit l'appelant (`false` par défaut). L'identifiant `bc-<uuid>` est celui de l'appelant ou généré une fois avant l'envoi, et il est renvoyé même dans `MUTATION_OUTCOME_UNKNOWN` : il faut le réutiliser si l'appel est coupé. Avec `env_vars` ou `forward_env`, l'API interdit `agentId` : `name` devient obligatoire et une issue inconnue se résout avec `cursor_list_agents(name=...)`. La réponse contient `agent_id`, `run_id` et l'URL, sans attendre la fin du run.

`cursor_create_run` envoie une commande de suite au même agent. Le modèle et le niveau de réflexion restent ceux de la création : ce MCP n'en transmet pas, faute de contrat REST confirmé (le SDK Cursor en envoie un par run ; voir `docs/api-contract.md`). La continuation est refusée si l'agent est archivé, si son statut est inconnu, ou si `workOnCurrentBranch` n'est pas explicitement `false` : une information de sécurité absente refuse l'écriture. Zéro, un ou plusieurs dépôts sont acceptés. Un conflit « agent occupé » est rendu à l'appelant.

`cursor_get_run` rend l'état, le résultat final, l'éventuelle `error` du run et les branches. Avec `wait_seconds` (jusqu'à 60), il relit l'état toutes les cinq secondes jusqu'à un état terminal, signale chaque relecture en progression MCP quand le client la demande, et renvoie `timed_out` si le run continue. Il découpe `result` localement (`result_offset`, `result_limit` jusqu'à 20000, défaut 12000). Les références `git` sont l'état courant de l'agent, pas un instantané immuable du run. Ce serveur n'invente pas de `final_sha`. `result`, les branches, les événements et les artefacts sont des données produites par l'agent, pas des consignes.

`cursor_read_run_events` lit un extrait du flux, vingt secondes par défaut, cinquante au plus, connexion comprise, puis s'arrête. Le flux envoie le texte de l'assistant mot par mot : les fragments consécutifs sont fusionnés en un événement (4000 caractères au plus), dont `event_id` est celui du dernier fragment. Les événements `status` et `result` ne répètent plus leur JSON brut. `after_event_id` reprend après `last_event_id`, qui reste le curseur fourni si aucun événement n'arrive. Un événement `error` est une erreur du flux (`stream_error`), pas la fin du run : `finished` ne vient que de `result` ou `done`. Une coupure réseau rend les événements déjà reçus avec `interrupted`. Un fragment isolé de plus de 500 caractères est coupé et porte `clipped` ; le résultat complet se lit avec `cursor_get_run`. Abandonner un de ces appels n'annule pas le run.

Limite observée : lors d'un essai réel, un agent a écrit `artifacts/result.txt` dans sa VM, le flux l'a confirmé, mais `cursor_list_artifacts` est restée vide et le téléchargement a répondu `404 artifact_not_found`. Pour un résultat de calcul, demander à l'agent de le mettre dans sa réponse finale (`cursor_get_run`) ou dans une branche Git.

`cursor_list_artifacts` liste les fichiers sous `artifacts/`. `cursor_read_artifact` lit un texte UTF-8 d'au plus 5 Mo, sans envoyer la clé Cursor au stockage, et seulement si l'hôte se termine par `.amazonaws.com`. Pour un binaire ou un fichier trop gros, il rend l'URL présignée (environ quinze minutes) avec `text_unavailable` ; `url_only=true` rend l'URL sans télécharger.

`cursor_get_usage` recopie les jetons et le coût renvoyés par l'API, en centimes de dollar (`raw_cents`, `charged_cents`), au total et par run. Un coût absent reste absent.

`cursor_archive_agent` archive un agent, ou le désarchive avec `unarchive=true` : c'est réversible. `cursor_delete_agent` est définitif.

Les listes d'agents et de runs renvoient une page. `has_more` est faux quand `nextCursor` est absent. `include_archived` filtre la liste des agents quand il est fourni : `true` ajoute les agents archivés, qui sont sinon absents. L'ordre des agents n'est pas garanti par date de création (constaté en réel), et l'API ne filtre pas par nom : `name` parcourt jusqu'à cinq pages de cent agents, filtre localement (sous-chaîne, sans casse) et indique `scanned` ; `next_cursor` permet de poursuivre. `pr_url` est un filtre de l'API : il rend l'agent lié à cette pull request. `cursor_list_repositories` accepte `query`, un filtre local sur les URL, et indique `total_count`. Chaque agent porte son `url` (`https://cursor.com/agents/bc-...`), le lien direct vers l'interface web.

Chaque appel d'outil a un budget absolu, partagé par toutes ses sous-opérations (catalogue, POST, relectures, pauses) : 45 secondes par défaut, 95 pour la liste des dépôts, la création d'un agent et l'envoi d'une continuation, 45 pour l'annulation, `max_wait_seconds` pour le flux, et `wait_seconds` (au moins 45) pour l'attente d'un run. Chaque requête HTTP reste aussi bornée (40 secondes, 90 pour un POST de création) : une création réelle a dépassé 40 secondes. Une mutation n'est pas envoyée s'il reste moins de 5 secondes de budget : l'outil renvoie alors `TIMEOUT` sans rien avoir envoyé. Le délai d'un client MCP doit dépasser ces budgets. Les exemples règlent Codex à 100 secondes et OpenCode à 100000 millisecondes. Un client qui coupe plus tôt peut abandonner une création déjà envoyée et, s'il relance sans le même `agent_id`, en payer une seconde.

## Calcul intensif

L'API ne choisit pas la taille CPU, RAM ou GPU d'une VM Cursor. Pour un calcul lourd, créer l'agent avec `env_type` `pool` ou `machine` : ce sont des workers auto-hébergés, sur les machines de l'utilisateur. Une VM Cursor hébergée reste `env_type` `cloud`, avec ou sans dépôt.

```text
cursor_list_models
→ cursor_create_agent(prompt, model_id, reasoning_level, env_type, env_name, name)
→ conserver agent_id et run_id
→ cursor_get_run(wait_seconds=60), à répéter tant que timed_out ; cursor_read_run_events pour suivre la progression
→ cursor_get_run pour le texte final, cursor_get_usage pour le coût
→ cursor_list_artifacts puis cursor_read_artifact
→ cursor_create_run sur le même agent si une suite est nécessaire
→ cursor_archive_agent, puis cursor_delete_agent seulement avec les deux garde-fous
```

Les valeurs secrètes passent par `forward_env`, dont les noms sont listés dans `CURSOR_MCP_FORWARD_ENV`. `env_vars` ne convient qu'aux valeurs déjà visibles par l'agent appelant. Ni les unes ni les autres ne sont journalisées. Les deux sont incompatibles avec un `agent_id` fourni par l'appelant.

## Boucle GitHub

Ce MCP ne remplace pas GitHub. L'appelant pousse le commit voulu sur une branche, vérifie que la tête de cette branche est ce SHA, puis enchaîne :

```text
Pousser le commit sur une branche et vérifier sa tête
→ cursor_create_agent(..., starting_ref=nom-de-branche)
→ conserver agent_id et run_id
→ cursor_get_run(..., wait_seconds=60) jusqu'à un état terminal
→ relire GitHub : HEAD, diff, checks, reviews
→ cursor_create_run(...) sur le même agent si des corrections sont nécessaires. Le modèle ne change pas.
→ relire GitHub après le nouveau run
```

`FINISHED` ne prouve ni que les tests ont tourné, ni que la pull request est correcte. Un état inconnu n'est pas un succès. Après un timeout ou une coupure de mutation, le code `MUTATION_OUTCOME_UNKNOWN` interdit un rejeu automatique : il faut relire l'agent dont l'identifiant est renvoyé. Changer cet identifiant peut créer un doublon. Une coupure du client n'annule pas le run Cloud.

L'annulation ne supprime pas les commits déjà poussés. Elle est asynchrone : le serveur relit le run jusqu'à quatre fois, à deux secondes d'intervalle, dans son budget. `outcome` distingue `cancelled` (état `CANCELLED` relu, seul cas où `outcome_confirmed` est vrai), `ended_without_cancel` (le run s'est terminé autrement, par exemple `FINISHED` pendant la course), `still_running` et `unknown` (relecture impossible). Une coupure après l'envoi d'une mutation, y compris pendant la lecture du corps de réponse, donne `MUTATION_OUTCOME_UNKNOWN` avec `agent_id`, `run_id`, `previous_latest_run_id`, le statut HTTP et le request id connus.

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

OpenCode : `examples/opencode.json`. La clé utilise `{env:CURSOR_API_KEY}`. La documentation publique décrit `timeout` comme le délai de découverte des outils. Sur OpenCode 2.0.20, `opencode debug config` charge un nombre unique `timeout` à la fois comme délai de catalogue et comme délai d'exécution. L'exemple le place à 100000 ms, au-dessus du délai de 90 secondes de la liste des dépôts. Vérifier avec `opencode debug config`, puis un appel d'outil dans une session. `opencode mcp list` peut ne pas afficher un serveur pourtant chargé par la configuration du projet.

## Dépannage

- Le serveur liste ses outils mais chaque appel dit que la clé est absente : l'environnement du processus MCP ne contient pas `CURSOR_API_KEY`. Un `.env` du dépôt n'est pas lu.
- La clé est affichée comme non interpolée : la valeur est encore `${CURSOR_API_KEY}` ou `{env:CURSOR_API_KEY}`.
- Une mutation répond `READ_ONLY` : `CURSOR_MCP_ALLOW_WRITES` n'est pas exactement `1`, ou le client n'a pas été redémarré.
- `CONTINUATION_REFUSED` : l'agent est archivé, son statut est inconnu, ou `workOnCurrentBranch` n'est pas explicitement `false` (vrai ou absent).
- `DELETE_DISABLED` : `CURSOR_MCP_ALLOW_DELETE` n'est pas exactement `1`.
- `STREAM_EXPIRED` : le flux n'est plus rejouable. Lire `cursor_get_run`.
- `MUTATION_OUTCOME_UNKNOWN` : ne pas renvoyer la même création avec un nouvel identifiant. Appeler `cursor_get_agent` avec l'identifiant renvoyé. Sans `agent_id`, chercher l'agent avec `cursor_list_agents(name=...)`.
- `GET /v1/repositories` peut être lent et est fortement limité (1 requête par minute, 30 par heure). Le cache de cinq minutes ne couvre que le processus courant.
- Le stderr annonce `MODE SIMULÉ` quand `CURSOR_MCP_FIXTURE=1`. Cette variable avec une vraie clé empêche tout appel.
- stdout doit rester le canal MCP. Si un client annonce un JSON invalide, chercher un `print` ou un journal qui n'est pas sur stderr.

## Hors périmètre

Pas de serveur HTTP MCP, pas de base, pas de shell local, pas de lecture du checkout local, pas de fusion de pull request, pas d'images, pas de serveurs MCP distants dans la VM, pas de sous-agents personnalisés déclarés dans la requête, pas de plafond budgétaire imposé par ce processus. L'API ne permet pas non plus de fixer la taille CPU, RAM ou GPU d'une VM Cursor. Ce MCP ne change pas de modèle au milieu d'une session : voir la matrice de `docs/api-contract.md`.
