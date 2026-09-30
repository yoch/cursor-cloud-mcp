# Mission — MCP Cursor Cloud minimal pour Claude Code, Codex CLI et OpenCode

**Date de préparation et de consultation des références : 30 septembre 2026.**

Ce document est une mission d'implémentation autonome. Il ne constitue pas un MCP déjà développé ni une preuve de compatibilité exécutée. Les décisions « v1 » ci-dessous désignent la première version de notre MCP, pas une ancienne version du protocole MCP.

## 1. Objectif et décisions de départ

Implémente un petit serveur MCP local permettant à Claude Code, Codex CLI et OpenCode de piloter Cursor Cloud Agents : découvrir les modèles et dépôts, lancer un travail, consulter son état et son résultat, envoyer une continuation au même agent, annuler un run et consulter l'usage.

Architecture cible :

```text
Claude Code / Codex CLI / OpenCode
               │ MCP stdio
               ▼
       cursor-cloud-mcp (Python)
               │ HTTPS
               ▼
      Cursor Cloud Agents API v1
               │
               ▼
          dépôt GitHub
```

Choix imposés pour limiter le périmètre : Python 3.12 minimum pour ce projet, SDK Python MCP officiel, client HTTP asynchrone `httpx`, API REST Cursor v1. Une seule implémentation pour les trois clients. Pas de serveur HTTP MCP à héberger.

Utilise la ligne stable actuelle du SDK MCP, vérifie sa version exacte puis verrouille-la. Au moment de la préparation, sa documentation présente v2 comme stable et utilise `MCPServer` depuis `mcp.server`. Ne mélange pas des exemples de v1/FastMCP avec une installation v2. Laisse le SDK gérer la négociation du protocole et la compatibilité avec les clients. [S3, S4]

Pour cette petite surface, appelle REST directement : cela permet de contrôler les timeouts et de vérifier qu'aucune mutation n'est rejouée automatiquement. N'empile pas `cursor-sdk` et une deuxième implémentation REST.

Ne reprends pas le wrapper communautaire précédemment discuté. Ne transforme pas cette mission en plateforme d'orchestration.

## 2. Démarrage et vérification des contrats

Lis les instructions du dépôt (`AGENTS.md`, etc.). Si tu travailles dans un dépôt existant, revalide son owner/repo, la branche de travail et le SHA distant ; préserve les changements étrangers à la mission. Ne pousse pas sur la branche de base et ne publie rien sans autorisation.

Relis les références [S1–S9]. Depuis [S1], suis le lien « OpenAPI specification ». Note URL réellement obtenue, date, version disponible et éventuellement empreinte du fichier dans `docs/api-contract.md`. En cas de conflit avec ce document, le contrat officiel relu fait foi : explique et teste l'ajustement.

Ne recopie pas toute l'OpenAPI et ne génère pas un client géant. Documente seulement les champs et les endpoints utilisés. En particulier, vérifie les formats de réponse, la pagination, les identifiants, les états terminaux et les erreurs de conflit.

L'API Cursor distingue l'agent durable et ses runs. La création REST renvoie les deux ; une continuation cible le même agent. Les métadonnées Git exposées dans un run représentent l'état courant de l'agent, pas un instantané Git immuable du run. [S1]

## 3. Périmètre fonctionnel : onze outils

Les noms ci-dessous constituent notre interface MCP. La colonne HTTP est le contrat de référence à revalider ; les schémas doivent être dérivés de la documentation, pas devinés.

| Outil MCP | Rôle | Endpoint de référence |
|---|---|---|
| `cursor_get_account` | Vérifier l'authentification | `GET /v1/me` |
| `cursor_list_models` | Modèles et paramètres disponibles | `GET /v1/models` |
| `cursor_list_repositories` | Dépôts accessibles via Cursor | `GET /v1/repositories` |
| `cursor_list_agents` | Une page d'agents | `GET /v1/agents` |
| `cursor_get_agent` | Métadonnées d'un agent | `GET /v1/agents/{id}` |
| `cursor_create_agent` | Nouvel agent et premier run | `POST /v1/agents` |
| `cursor_list_runs` | Une page de runs d'un agent | `GET /v1/agents/{id}/runs` |
| `cursor_get_run` | État, résultat et références Git rapportées | `GET /v1/agents/{id}/runs/{runId}` |
| `cursor_create_run` | Continuation sur le même agent | `POST /v1/agents/{id}/runs` |
| `cursor_cancel_run` | Annulation explicite d'un run | `POST /v1/agents/{id}/runs/{runId}/cancel` |
| `cursor_get_usage` | Compteurs d'usage disponibles | `GET /v1/agents/{id}/usage` |

Source des endpoints : [S1].

### Création d'un agent

Expose : `repository`, `starting_sha`, `prompt`, et éventuellement `name`, `model_id`, `model_params`, `mode`, `auto_create_pr`, `agent_id`.

Notre contrat v1 impose :

- Un seul dépôt GitHub HTTPS explicitement fourni, sans identifiants intégrés dans l'URL.
- `starting_sha` obligatoire : SHA complet, vérifié au préalable par l'appelant sur GitHub. Valider son format, sans prétendre que ce contrôle prouve son existence distante.
- `workOnCurrentBranch=false`, imposé par le serveur et non exposé comme option.
- `auto_create_pr=false` par défaut ; l'appelant peut explicitement demander `true`.
- Prompt non vide ; aucune lecture de fichier à partir d'un `plan_file`.
- Modèle facultatif ; lorsqu'il est fourni, transmettre le format officiel et ne pas inventer d'identifiants ou de paramètres.

Envoie les clés REST exactes, notamment `repos[].startingRef` et `autoCreatePR`. N'envoie pas des options absentes comme des valeurs nulles sans justification du schéma.

Conserve un identifiant de création stable. La référence actuelle permet `agentId=bc-<uuid>` et documente un conflit lorsqu'il existe déjà. Accepte un ID fourni par l'appelant ou génère-le une seule fois avant l'envoi. Sur erreur ambiguë, rends cet ID disponible pour la réconciliation. Ne transforme pas un conflit en succès prouvé : il faut relire l'agent. [S1]

Retourne les identifiants agent/run et l'URL utile dès réception de la réponse. Ne fais pas de `wait()` jusqu'à la fin du travail Cloud.

### Reprise, état et résultat

`cursor_create_run` accepte l'ID de l'agent, le prompt et un mode facultatif. Relis l'agent avant l'envoi pour vérifier que cette mutation entre dans notre périmètre, et garde l'ancien `latestRunId` pour aider à diagnostiquer un résultat ambigu.

Pour la v1, autorise les continuations seulement sur un agent mono-dépôt explicitement configuré avec `workOnCurrentBranch=false`. Si la configuration est absente ou incompatible, refuse la mutation avec une explication précise. Les outils de lecture peuvent néanmoins inspecter d'autres agents.

Pas de création d'une nouvelle PR ou d'un nouvel agent comme solution automatique à un problème de continuation. Un conflit « agent occupé » doit être rendu à l'appelant, pas résolu en lançant un autre agent.

`cursor_get_run` doit permettre de lire la totalité du résultat par tranches : par exemple `result_offset=0`, `result_limit=12000`, plafond 20000 caractères. Retourne `result_total_chars`, `result_truncated` et `next_result_offset` si nécessaire. Cette pagination est locale au texte du résultat ; elle n'est pas un curseur REST Cursor.

Garde les états inconnus visibles. Un état nouveau ou un champ absent ne doit jamais devenir un faux succès. Différencie l'état de l'agent de celui du run.

### Listes et usage

Expose `limit` et le curseur uniquement là où l'API les supporte. Une page par appel ; aucune boucle de pagination implicite. Préserve les curseurs reçus et l'indication de fin.

Pour les dépôts, mets en place un petit cache mémoire de cinq minutes et une requête partagée entre appels simultanés d'un même processus. Ne fais pas de découverte des dépôts au démarrage ni avant chaque création. Le quota étant partagé entre processus/clients, le cache n'est pas une garantie globale. [S1, S2]

Présente les tokens réellement retournés. Un coût absent reste absent : ne calcule pas une facture à partir d'un prix supposé et ne présente pas ce MCP comme imposant un plafond budgétaire dur.

## 4. Hors périmètre de la v1

Pas de HTTP MCP distant, OAuth serveur, Docker obligatoire, base de données, scheduler, webhook, suivi perpétuel ou worker local. Pas de SSE ni de promesse de flux live dans les appels MCP ; le polling explicite suffit.

Pas de shell, `git`, recherche de checkout local, lecture de fichiers arbitraires ou outil HTTP générique dans le MCP. Pas de téléchargement d'artifacts, suppression d'agents, fusion de PR, multi-repo, MCP imbriqués, custom subagents ou injection de secrets dans la VM.

Le rattachement d'un nouvel agent à une PR préexistante est différé. Il ne faut pas improviser une garantie « démarrage au SHA exact » en combinant `prUrl` et `startingRef` : la documentation précise que le second est ignoré lorsque le premier est fourni. La reprise d'un agent déjà créé par ce MCP couvre notre boucle initiale. [S1]

## 5. GitHub : responsabilité séparée

Le MCP pilote Cursor ; il ne remplace pas le connecteur ou le CLI GitHub de l'agent appelant et ne demande pas de token GitHub supplémentaire.

Documente ce workflow d'utilisation :

```text
Lire GitHub : repo, base, SHA exact
→ cursor_create_agent(..., starting_sha=SHA)
→ conserver agent_id et run_id
→ cursor_get_run(...) jusqu'à un état terminal
→ relire GitHub : HEAD, diff, checks, reviews
→ cursor_create_run(...) sur le même agent si corrections
→ relire GitHub après le nouveau run
```

Un test, une CI et une review ne valident que leur contenu/SHA. `FINISHED` ne prouve ni que les tests ont été exécutés, ni que la PR est correcte. Ne fabrique pas de `final_sha` absent de l'API. N'assimile pas les références Git rapportées par Cursor à une vérification distante indépendante.

Lire avant une mutation n'équivaut pas à une écriture conditionnelle atomique. N'invente pas d'`expected_sha` dans l'API Cursor. Les garanties conditionnelles disponibles sur GitHub restent du ressort de l'appelant.

## 6. Configuration et garde-fous minimaux

Variables applicatives :

```text
CURSOR_API_KEY                 obligatoire pour les appels réels
CURSOR_MCP_ALLOW_WRITES        "0" par défaut ; "1" pour autoriser les mutations
CURSOR_MCP_LOG_LEVEL           optionnel, INFO par défaut
```

La lecture seule doit être contrôlée dans le serveur, pas uniquement par les descriptions des tools. Garde le catalogue des onze outils stable et retourne une erreur explicite lorsqu'une mutation est désactivée.

La clé provient exclusivement de l'environnement du processus. Ni argument MCP, ni argument de commande, ni valeur dans un fichier d'exemple. Ne charge pas automatiquement un `.env` du projet courant. Rejette une clé vide ou manifestement non interpolée (`${...}` ou `{env:...}`), sans en imprimer la valeur.

Le serveur doit pouvoir s'initialiser et lister ses outils sans clé. Le premier outil dépendant de Cursor renvoie alors une erreur de configuration claire. Aucun réseau dans l'import du module ou dans `tools/list`.

Base HTTPS de production fixe : `https://api.cursor.com`. Pas de paramètre d'outil modifiant le domaine, les headers d'authentification ou le chemin HTTP. Valide et encode les identifiants comme des segments, pas comme des fragments d'URL. N'accepte pas de redirection d'authentification vers un autre domaine.

Logs sur stderr exclusivement. Aucune bannière, aucun JSON de debug et aucun `print()` sur stdout lorsque le serveur MCP tourne. Ne loggue pas prompts, réponses intégrales, clé ou header Authorization. Les logs peuvent contenir outil, durée, identifiants de ressources, statut HTTP et request ID. [S4]

Les annotations de tools doivent distinguer lecture et mutation. Déclare notamment lecture seule pour les GET ; indique les effets de bord de création, continuation et annulation. Utilise les annotations fournies par le SDK pour le protocole négocié. Elles ne remplacent ni les permissions du client ni le contrôle serveur.

N'abaisse pas les permissions de Claude Code, Codex ou OpenCode pour faire réussir une démonstration.

## 7. Transport HTTP et gestion des erreurs

Utilise un unique `httpx.AsyncClient` géré dans le lifecycle du serveur. Ferme-le proprement à l'arrêt. Injection de transport pour les tests, sans endpoint arbitraire configurable depuis un tool.

Fixe une deadline totale par appel (par exemple 40 secondes), couvrant connexion, lecture du corps, backoff et retries. Un simple timeout de lecture par bloc n'est pas une deadline de bout en bout. Aucun timeout désactivé. Les appels de création ne couvrent que l'acceptation de la requête, pas la durée du run Cloud. [S10]

Politique de retries : GET seulement, au maximum une reprise automatique et uniquement dans la deadline. Respecte `Retry-After` lorsqu'il existe ; si l'attente dépasserait la deadline, rends l'erreur avec l'information de reprise. Pas de boucle serrée.

Aucun replay automatique de POST après timeout, coupure ou 5xx. N'utilise pas de décorateur global réessayant toutes les méthodes. N'invente pas un header d'idempotence pris en charge par Cursor.

Sur mutation ambiguë :

```text
code = MUTATION_OUTCOME_UNKNOWN
safe_to_retry_automatically = false
agent_id / run_id connus
previous_latest_run_id si disponible
recovery = relire l'état distant avant toute nouvelle mutation
```

Pour une création, conserve l'ID stable ; changer l'ID peut créer un doublon. Pour une continuation, n'affirme pas que la nouvelle requête n'a pas été prise en compte parce que sa réponse manque. Une coupure du client MCP ne doit pas déclencher une annulation Cloud implicite.

Différencie au minimum : configuration manquante, lecture seule, validation, authentification, permissions, ressource absente, quota, agent occupé, annulation impossible, réponse incompatible, timeout et résultat de mutation inconnu.

Après une demande d'annulation, relis le run et distingue demande acceptée et état terminal observé. Si la relecture échoue, rends la demande comme acceptée mais son résultat non confirmé. Aucune annulation ne supprime les commits ou effets déjà produits.

Conserve les codes d'erreur distants et les request IDs disponibles. Nettoie le message, n'expose pas le corps d'erreur brut ni des traces contenant des données sensibles. Une API qui renvoie du HTML, un JSON malformé ou des champs essentiels absents doit produire une erreur, pas un objet « succès » vide.

## 8. Contrat MCP et compacité

Décris brièvement chaque outil : objectif, effet de bord, données requises, identifiants retournés et étape suivante. Ajoute des instructions de serveur expliquant que les créations peuvent coûter de l'argent, qu'il faut conserver les IDs et que la validation GitHub reste externe.

Entrées typées, champs inconnus rejetés, pas de `dict` générique transmettant tous les paramètres à Cursor. Les évolutions additives des réponses peuvent être tolérées sans les recopier aveuglément dans les résultats.

Retourne un objet structuré documenté, accompagné de son JSON dans un bloc texte pour les clients qui ne rendent pas `structuredContent`. Évite un résumé textuel contredisant les données structurées. Les erreurs opérationnelles doivent être des résultats MCP avec `isError=true`, pas simplement un champ `error` noyé dans un succès. Teste les schémas de succès et d'erreur. [S5]

Borne les résultats et rends toute troncature explicite. Ne supprime pas un identifiant, curseur, code d'erreur ou indicateur de troncature pour tenir dans une limite. Refuse un prompt trop grand plutôt que de le tronquer silencieusement.

Ne dépend pas d'extensions de tâches asynchrones MCP, de resources ou de notifications propriétaires : de simples outils request/response doivent suffire sur les trois clients.

## 9. Structure et installation

Structure indicative, à simplifier si nécessaire :

```text
pyproject.toml
uv.lock
src/cursor_cloud_mcp/
    __init__.py
    __main__.py
    server.py
    client.py
    models.py
    config.py
    errors.py
tests/
    test_client.py
    test_tools.py
    test_stdio.py
    fixtures/
    fixture_server.py
examples/
    claude.mcp.json
    codex.config.toml
    opencode.json
README.md
docs/
    api-contract.md
    verification.md
```

Point d'entrée : `cursor-cloud-mcp`, également lançable par `python -m cursor_cloud_mcp`. Pas de frontend ni framework applicatif supplémentaire.

Dépendances directes seulement lorsqu'elles sont utilisées. Verrouille les versions testées ; pas de dépendance flottante `latest` ni de téléchargement de code au lancement du MCP. Vérifie les avis de sécurité applicables, sans prétendre qu'un audit automatisé prouve l'absence de défauts.

L'installation livrée doit fonctionner depuis une copie propre : synchronisation du lockfile, tests, puis lancement du binaire dans `.venv`. Fournis aussi une installation du wheel dans un environnement vierge pour vérifier le packaging.

N'annonce pas `uvx cursor-cloud-mcp` ou `pip install cursor-cloud-mcp` comme une installation publique avant publication effective. Aucune publication PyPI n'est demandée.

## 10. Configurations à fournir et vérifier

Les exemples ci-dessous utilisent un chemin absolu de remplacement vers le binaire installé. Génère les variantes correspondant aux vrais chemins sur la machine de validation. Ne modifie pas automatiquement les configurations personnelles existantes ; utilise des profils temporaires ou livre des fragments à fusionner.

Les trois clients doivent être lancés depuis un environnement contenant la clé. Exemple de saisie Bash sans valeur littérale dans l'historique :

```bash
read -r -s -p 'Clé Cursor : ' CURSOR_API_KEY
printf '\n'
export CURSOR_API_KEY
```

### Claude Code — `.mcp.json` à la racine du projet

```json
{
  "mcpServers": {
    "cursor_cloud": {
      "type": "stdio",
      "command": "/CHEMIN/ABSOLU/cursor-cloud-mcp/.venv/bin/cursor-cloud-mcp",
      "args": [],
      "env": {
        "CURSOR_API_KEY": "${CURSOR_API_KEY}",
        "CURSOR_MCP_ALLOW_WRITES": "0"
      }
    }
  }
}
```

Vérifier `claude mcp list`, `claude mcp get cursor_cloud` et `/mcp`. Le fichier projet peut demander une approbation dans le client. Fournir aussi une procédure `claude mcp add` conforme au `--help` installé pour une configuration utilisateur, sans mettre la clé en clair dans la ligne de commande. [S6]

### Codex CLI — `~/.codex/config.toml`

```toml
[mcp_servers.cursor_cloud]
command = "/CHEMIN/ABSOLU/cursor-cloud-mcp/.venv/bin/cursor-cloud-mcp"
args = []
env_vars = ["CURSOR_API_KEY"]
startup_timeout_sec = 10
tool_timeout_sec = 60

[mcp_servers.cursor_cloud.env]
CURSOR_MCP_ALLOW_WRITES = "0"
```

Vérifier `codex mcp list` et `/mcp`. Utilise bien `env_vars` pour transmettre la clé du processus parent ; ne remplace pas ceci par une interpolation `${...}` supposée dans du TOML. [S7]

### OpenCode — `opencode.json` du projet

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "cursor_cloud": {
      "type": "local",
      "command": ["/CHEMIN/ABSOLU/cursor-cloud-mcp/.venv/bin/cursor-cloud-mcp"],
      "environment": {
        "CURSOR_API_KEY": "{env:CURSOR_API_KEY}",
        "CURSOR_MCP_ALLOW_WRITES": "0"
      },
      "enabled": true,
      "timeout": 10000
    }
  }
}
```

Vérifier `opencode mcp list` puis un appel réel d'outil dans une session. Le champ `timeout` documenté concerne notamment la découverte des outils ; ne prétends pas qu'il contrôle toute la durée d'exécution sans l'avoir vérifié dans la version installée. [S8, S9]

Pour autoriser des mutations réelles, l'utilisateur change explicitement `CURSOR_MCP_ALLOW_WRITES` en `"1"`, puis redémarre le serveur/client concerné. Le MCP ne propose pas d'outil permettant de changer ce réglage.

## 11. Plan de tests obligatoire

### Tests unitaires hors réseau

Utilise `httpx.MockTransport` ou un équivalent, avec réponses synthétiques conformes au contrat. [S11]

Couvre chaque outil, son mapping exact et ses erreurs principales. Vérifie en particulier les booléens imposés, le SHA, le modèle, les curseurs, les états inconnus, les champs absents, les réponses non JSON, la lecture seule et les valeurs d'environnement non interpolées.

Teste la deadline sur l'ensemble du corps, les 429, les délais de reprise, les 5xx, l'absence de retry des POST, le retour de l'ID après erreur ambiguë et la continuation concurrente donnant un conflit. Les tests ne doivent effectuer aucun appel réel à Cursor.

Teste le découpage intégral d'un résultat long sans trou ni doublon, les limites de sortie et l'absence de secrets dans stdout/stderr et dans les erreurs.

### Tests du protocole stdio

Démarre le véritable point d'entrée en sous-processus, initialise une session avec le client MCP officiel, liste les outils et appelle au moins un outil de lecture et un outil de mutation sur backend simulé. Utilise le même serveur, les mêmes handlers et le même client HTTP, avec injection du transport simulé depuis le harness de test.

Vérifie : onze outils, schémas exploitables, annotations correctes, réponses structurées cohérentes, `isError`, stdout sans pollution, arrêt propre sur EOF, absence de processus abandonné et démarrage hors d'un dépôt Git. Vérifie la négociation avec les révisions réellement utilisées par les clients cibles.

### Tests dans les trois clients

Capture `claude --version`, `codex --version`, `opencode --version` et les versions Python/MCP/HTTPX.

Dans chacun des trois clients, avec configuration isolée et backend simulé, fais appeler les outils par le client lui-même : lecture, création d'un agent fictif, récupération du résultat, continuation du même agent, erreur attendue. Teste aussi la lecture seule. Une connexion ou une liste d'outils seule ne valide pas les appels.

Ne marque jamais « validé dans les trois clients » après seulement un test du SDK MCP. Si un client n'est pas installé ou n'est pas authentifié, livre le test et les instructions, mais marque sa validation `NON EXÉCUTÉE`.

### Tests réels Cursor

Commence par des lectures uniquement lorsqu'une clé est disponible. Aucun lancement Cloud, changement GitHub ou coût d'exécution de test sans autorisation explicite.

Un smoke test avec écritures doit être opt-in et exiger un dépôt de test autorisé, un SHA vérifié et une décision de budget. Il se limite à un agent, un premier run et au maximum une continuation. Ne multiplie pas les essais payants pour contourner une erreur.

Après un timeout ou un conflit, réconcilie au lieu de recréer. Ne supprime pas automatiquement l'agent ni la branche/PR produite. Documente les IDs et le nettoyage éventuel à faire. Un test unitaire d'annulation suffit si tester une annulation réelle demanderait une exécution payante supplémentaire non autorisée.

## 12. Ordre d'exécution et livrables

Travaille dans cet ordre : contrat et squelette ; client HTTP testé ; onze outils ; tests stdio ; packaging et configurations ; validation dans les clients ; lectures réelles et smoke payant seulement si autorisés.

Livre le code complet et installable, les dépendances verrouillées, les tests, les trois configurations, un README de démarrage/dépannage, le contrat API relu et `docs/verification.md`.

Le rapport de validation doit distinguer :

```text
code/SHA ou empreinte réellement testé
versions des dépendances
commandes exécutées et résultats
MCP stdio simulé : PASS / FAIL / NON EXÉCUTÉ
Claude Code : PASS / FAIL / NON EXÉCUTÉ
Codex CLI : PASS / FAIL / NON EXÉCUTÉ
OpenCode : PASS / FAIL / NON EXÉCUTÉ
Cursor lectures réelles : PASS / FAIL / NON EXÉCUTÉ
Cursor écritures réelles : PASS / FAIL / NON EXÉCUTÉ
limitations restantes
```

Après une modification, refais les tests concernés ; ne réutilise pas une preuve portant sur un autre contenu. Après publication autorisée, relis le contenu distant et son SHA avant de le présenter comme publié et validé.

Termine avec un bilan factuel. Ne livre pas uniquement une architecture ou un squelette, ne prétends pas qu'une API a répondu lorsqu'elle était mockée, et ne compense pas une fonction absente par une simulation cachée en production.

**Critère de réussite : un seul binaire MCP, onze outils cohérents, aucune dépendance à un checkout local, configurations des trois clients vérifiées et boucle création → résultat → continuation fonctionnelle.**

## Références officielles

[S1] Cursor Cloud Agents API v1 et lien vers l'OpenAPI :
https://cursor.com/docs/cloud-agent/api/endpoints

[S2] Cursor — authentification, quotas, bonnes pratiques :
https://cursor.com/docs/api

[S3] SDK Python MCP officiel, documentation stable :
https://py.sdk.modelcontextprotocol.io/
Dépôt officiel : https://github.com/modelcontextprotocol/python-sdk

[S4] Guide officiel de création d'un serveur MCP, notamment stdio/logging :
https://modelcontextprotocol.io/docs/develop/build-server

[S5] Outils, schémas, résultats structurés et erreurs MCP :
https://modelcontextprotocol.io/specification/2026-07-28/server/tools
Référence antérieure pour compatibilité :
https://modelcontextprotocol.io/specification/2025-11-25/server/tools

[S6] Claude Code — configuration MCP :
https://code.claude.com/docs/en/mcp

[S7] Codex — configuration MCP (lien officiel pouvant rediriger) :
https://developers.openai.com/codex/mcp/

[S8] OpenCode — serveurs MCP :
https://opencode.ai/docs/mcp-servers/

[S9] OpenCode — configuration et variables d'environnement :
https://opencode.ai/docs/config/

[S10] HTTPX — timeouts :
https://www.python-httpx.org/advanced/timeouts/

[S11] HTTPX — transports et MockTransport :
https://www.python-httpx.org/advanced/transports/
