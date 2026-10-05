# Contrat API utilisé

Consultation du 30 septembre 2026, relue le 1er octobre 2026. L'empreinte du fichier brut est inchangée.

Ce document ne recopie pas l'OpenAPI. Il fixe les champs que ce MCP envoie ou lit. En cas d'écart avec `mission_cursor_cloud_mcp.md`, le contrat officiel relu prime. Les écarts sont listés plus bas.

## Source

- Page endpoints : <https://cursor.com/docs/cloud-agent/api/endpoints>
- Lien OpenAPI suivi depuis cette page : <https://cursor.com/docs-static/cloud-agents-openapi.yaml>
- Réponse HTTP du téléchargement : `200`, `content-type: text/yaml; charset=utf-8`, `date: Wed, 30 Sep 2026 19:13:31 GMT`, `etag: "fb90bd68a38f8414b4d85c52e6bfa7e0"`, 59344 octets
- `info.title` : Cursor Cloud Agents API
- `info.version` : `1.0.0`
- `openapi` : `3.0.3`
- Serveur : `https://api.cursor.com`
- Empreinte SHA-256 du fichier brut : `7fb350f40e928721afaba13d44377880f235f087734bc26ef51b54f7323ad209`

Authentification retenue : `Authorization: Bearer`. L'OpenAPI accepte aussi Basic avec la clé comme nom d'utilisateur et un mot de passe vide. Les deux schémas sont documentés comme équivalents. Aucune redirection n'est suivie.

## Endpoints utilisés

| Méthode et chemin | Succès OpenAPI | Rôle dans ce MCP |
|---|---|---|
| `GET /v1/me` | 200 `ApiKeyInfo` | `cursor_get_account` |
| `GET /v1/models` | 200 `ListModelsResponse` | `cursor_list_models` |
| `GET /v1/repositories` | 200 `ListRepositoriesResponse` | `cursor_list_repositories` |
| `GET /v1/agents` | 200 `ListAgentsResponse` | `cursor_list_agents` |
| `GET /v1/agents/{id}` | 200 `Agent` | `cursor_get_agent` |
| `POST /v1/agents` | 201 `CreateAgentResponse` | `cursor_create_agent` |
| `GET /v1/agents/{id}/runs` | 200 `ListRunsResponse` | `cursor_list_runs` |
| `GET /v1/agents/{id}/runs/{runId}` | 200 `Run` | `cursor_get_run` |
| `POST /v1/agents/{id}/runs` | 201 `CreateRunResponse` | `cursor_create_run` |
| `POST /v1/agents/{id}/runs/{runId}/cancel` | 200 `IdResponse` | `cursor_cancel_run` |
| `GET /v1/agents/{id}/runs/{runId}/stream` | 200 `text/event-stream` | `cursor_read_run_events` |
| `GET /v1/agents/{id}/usage` | 200 `AgentUsageResponse` | `cursor_get_usage` |
| `GET /v1/agents/{id}/artifacts` | 200 `ListArtifactsResponse` | `cursor_list_artifacts` |
| `GET /v1/agents/{id}/artifacts/download` | 200 `DownloadArtifactResponse` | `cursor_read_artifact` |
| `POST /v1/agents/{id}/archive` | 200 `IdResponse` | `cursor_archive_agent` |
| `POST /v1/agents/{id}/unarchive` | 200 `IdResponse` | `cursor_archive_agent` avec `unarchive=true` |
| `DELETE /v1/agents/{id}` | 200 `IdResponse` | `cursor_delete_agent` |

## Champs envoyés

`POST /v1/agents`, uniquement les clés fournies, jamais de `null` :

- `prompt.text` : obligatoire, non vide
- `repos` : zéro à vingt éléments `{ "url", "startingRef" }`. Absent s'il n'y a pas de dépôt. `startingRef` est un nom de branche. Le schéma OpenAPI le type comme `string`, mais un essai réel du 1er octobre 2026 a reçu `400 validation_error` pour un SHA complet de 40 caractères, et `201` pour le nom de branche dont la tête était ce SHA
- `workOnCurrentBranch` : toujours `false`
- `autoCreatePR` : booléen, `false` si l'appelant ne demande pas `true`
- `agentId` : `bc-` suivi d'un UUID, fourni ou généré une fois avant l'envoi. Absent quand `envVars` est envoyé : l'API interdit les deux ensemble
- `name`, `model` (`id` et éventuellement `params[{id,value}]`), `mode` (`agent` ou `plan`) : seulement s'ils sont fournis
- `env` : `{ "type": "cloud" | "pool" | "machine", "name"? }` seulement s'il est fourni
- `envVars` : objet de chaînes, au plus 50, seulement s'il est fourni. Les noms ne commencent pas par `CURSOR_`

`model.id` est un id du catalogue ; un alias n'est accepté que s'il ne désigne qu'un modèle, et il est envoyé sous la forme de cet id. `model.params` est vérifié contre `GET /v1/models` avant l'envoi : chaque valeur, puis la combinaison, qui doit tenir dans au moins une variante publiée (le catalogue réel du 5 octobre 2026 omet 5 combinaisons sur 20 pour `gpt-5.5`). `reasoning_level` est placé dans le premier paramètre présent parmi `effort`, `reasoning_effort` et `reasoning`.

`POST /v1/agents/{id}/runs` : `prompt.text`, et `mode` seulement s'il est fourni.

`GET /v1/agents` et `GET /v1/agents/{id}/runs` : `limit` (1 à 100) et `cursor` seulement s'ils sont fournis. `GET /v1/agents` ajoute `includeArchived` et `prUrl` seulement s'ils sont fournis. L'API refuse tout autre filtre (`400`, « Unrecognized key(s) », vérifié le 5 octobre 2026 pour `name`, `q`, `search`, `status`, `sort`) : la recherche par nom de `cursor_list_agents` parcourt donc au plus cinq pages de cent et filtre localement.

`GET /v1/agents/{id}/runs/{runId}/stream` : en-tête `Last-Event-ID` seulement s'il est fourni. La lecture s'arrête sur `done`, `result` ou `error`, à l'échéance locale, ou à 1 Mo. `error` est une erreur du flux, rendue comme `stream_error` : seuls `result` et `done` marquent `finished`. Le décodage UTF-8 est incrémental, pour qu'un caractère coupé entre deux blocs réseau reste intact. `heartbeat` et `interaction_update` ne sont pas renvoyés à l'appelant. Les fragments `assistant` (et `thinking`) consécutifs, envoyés mot par mot par l'API, sont fusionnés en un événement de 4000 caractères au plus, qui porte l'identifiant du dernier fragment.

`GET /v1/agents/{id}/artifacts/download` : `path`, relatif, préfixe `artifacts/`, sans `..`.

`GET /v1/agents/{id}/usage` : `runId` seulement s'il est fourni.

## Champs lus

- Compte : `apiKeyName`, `createdAt`, et s'ils sont présents `userId`, `userEmail`, `userFirstName`, `userLastName`. Pas de secret de clé.
- Modèles : `items[]` avec `id`, `displayName`, et s'ils sont présents `description`, `aliases`, `parameters`, `variants`. Les variantes sont le produit des paramètres, parfois incomplet ; elles portent aussi un paramètre non publié (`cyber`), ignoré. Elles servent au contrôle des combinaisons et à `defaults`, et ne sont rendues qu'avec `model_id`.
- Dépôts : `items[].url`. Pas de curseur dans ce schéma.
- Agents, page : `items[]` (`id`, `status`, `env`, `url`, `createdAt`, `updatedAt`, `name` et `latestRunId` optionnels) et `nextCursor` s'il est présent. Son absence signifie fin de liste, pas une valeur `null`.
- Agent : les champs de la page, plus `repos`, `workOnCurrentBranch`, `autoCreatePR` lorsqu'ils sont présents. Une absence reste une absence.
- Runs, page : `items[]` du schéma `Run` et `nextCursor` selon la même règle.
- Run : `id`, `agentId`, `status`, `createdAt`, `updatedAt`, et s'ils sont présents `durationMs`, `result`, `error` (forme libre ; `code: message` si c'est un objet), `git.branches[]` (`repoUrl`, `branch`, `prUrl`). `git` est l'état courant de l'agent, pas un instantané immuable du run. `repoUrl` est renvoyé sans schéma. Aucun `final_sha` n'est inventé.
- Annulation, archivage, désarchivage, suppression : `id`. Il est comparé à l'identifiant demandé quand il est présent.
- Usage : `totalUsage` et `runs[].usage` avec `inputTokens`, `outputTokens`, `cacheWriteTokens`, `cacheReadTokens`, `totalTokens`. `usageUuid` s'il est présent. `cost` et `runs[].cost` (`rawCostCents`, `chargedCents`) s'ils sont présents : absents de l'OpenAPI du 30 septembre, mais renvoyés par l'API réelle le 5 octobre 2026 et typés par le SDK Cursor. Rendus en centimes, arrondis à 4 décimales, jamais estimés.
- Artefacts : `items[]` avec `path`, `sizeBytes`, `updatedAt`. Le téléchargement renvoie `url` et `expiresAt`. L'URL présignée n'est suivie que si elle est HTTPS et si l'hôte se termine par `.amazonaws.com`, sans redirection et sans en-tête `Authorization`.
- Flux : événements `status`, `assistant`, `tool_call`, `result`, `error`, `done`, et `thinking` seulement sur demande. L'en-tête `X-Cursor-Stream-Retention-Seconds` est conservé s'il est présent.

États de run connus : `CREATING`, `RUNNING`, `FINISHED`, `ERROR`, `CANCELLED`, `EXPIRED`. Les quatre derniers sont terminaux. Tout autre état est conservé et n'est pas un succès.

États d'agent : la page endpoints décrit `ACTIVE`, `IDLE` et `ARCHIVED`. L'OpenAPI enumère seulement `ACTIVE` et `ARCHIVED`. `IDLE` est donc accepté et affiché. Un état inconnu reste visible.

`agentId` créé : `^bc-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$`.

## Erreurs distantes

Corps documenté : `{ "error": { "code", "message", "helpUrl"?, "provider"? } }`. `helpUrl` (HTTPS seulement) et `provider` sont rendus dans `help_url` et `provider` quand ils sont présents, par exemple pour `integration_not_connected`. `invalid_last_event_id` porte une consigne de reprise sans curseur.

Codes cités par le schéma : `unauthorized`, `api_key_not_found`, `plan_required`, `role_forbidden`, `feature_unavailable`, `integration_not_connected`, `validation_error`, `missing_body`, `invalid_model`, `invalid_branch_name`, `repository_required`, `repository_access`, `pr_resolution_failed`, `artifact_not_found`, `service_account_required`, `agent_not_found`, `run_not_found`, `agent_busy`, `agent_archived`, `agent_id_conflict`, `run_not_cancellable`, `rate_limit_exceeded`, `usage_limit_exceeded`, `stream_expired`, `stream_unavailable`, `invalid_last_event_id`, `client_cancelled`, `not_implemented`, `upstream_error`, `internal_error`.

Statuts utilisés pour classer : 400 validation, 401 authentification, 403 permissions (`feature_unavailable` inclus), 404 ressource absente, 409 conflit, 410 flux expiré, 429 quota. Le 429 peut porter `Retry-After`. Sans cet en-tête, une seule relance GET attend une seconde, dans la deadline. L'OpenAPI mentionne aussi `X-RateLimit-Limit`, `X-RateLimit-Remaining` et `X-RateLimit-Reset`. Aucun identifiant de requête n'est spécifié : les en-têtes `x-request-id`, `request-id` et `x-cursor-request-id` sont conservés seulement s'ils sont présents.

`GET /v1/agents/{id}/usage` peut répondre `403 feature_unavailable` (accès anticipé).

`GET /v1/repositories` est limité à 1 requête par utilisateur et par minute, et 30 par heure. La page indique que la réponse peut prendre des dizaines de secondes.

## Ajustements assumés

- Les créations sont spécifiées en `201`. Un `200` avec le même JSON est accepté, car le succès se juge sur le schéma, pas sur un statut voisin.
- Budget par outil : une seule échéance absolue par appel MCP, transmise en temps restant à chaque requête, relecture et pause (45 secondes par défaut, 95 pour la création, la continuation et la liste des dépôts, 45 pour l'annulation, `max_wait_seconds` pour le flux, `wait_seconds` (au moins 45) pour l'attente d'un run). Une mutation n'est pas envoyée s'il reste moins de 5 secondes, ou moins de la moitié de son propre délai.
- Deadline par requête, à l'intérieur de ce budget : 40 secondes par appel, 90 secondes pour `POST /v1/agents` et `POST /v1/agents/{id}/runs` (une création réelle a dépassé 40 secondes avant de réussir), 90 secondes pour `GET /v1/repositories`, parce que le contrat officiel prévient que cet appel peut durer des dizaines de secondes. `cursor_get_run` avec `wait_seconds` enchaîne des lectures dans une échéance d'au plus 60 secondes. `cursor_read_run_events` borne son attente à 50 secondes.
- `IDLE` n'est pas dans l'enum OpenAPI de l'agent, mais la page endpoints le définit. Il n'est pas rejeté.
- Le flux SSE est lu une fois, sans reconnexion interne. `410 stream_expired` devient `STREAM_EXPIRED`.
- Les artefacts, l'archivage, le désarchivage et la suppression sont exposés. `prUrl` sert de filtre de lecture (`cursor_list_agents`), pas à la création. Les images, `mcpServers`, `customSubagents` et `POST /v1/sub-tokens` restent hors de ce MCP.
- `startingRef` est ignoré par Cursor lorsque `prUrl` est fourni. Ce MCP n'envoie pas `prUrl`.
- Un SHA complet n'est pas envoyé dans `startingRef`. L'appelant pousse le commit sur une branche et passe le nom de cette branche dans `starting_ref` (l'ancien alias `starting_sha` est retiré le 5 octobre 2026). Ce contrôle de format ne prouve pas que la branche existe. C'est un contournement daté de l'observation du 1er octobre 2026 : la documentation REST et le bridge SDK v1.0.36 annoncent qu'une référence peut inclure un SHA. Il ne sera retiré qu'après un test réel explicitement autorisé.
- Une continuation exige `workOnCurrentBranch` explicitement `false` et un statut d'agent connu. Le schéma autorise l'absence de ce champ ; ce MCP la traite comme un refus, pas comme une autorisation. Une lecture conserve au contraire un état inconnu tel quel.
- Une coupure après l'envoi d'une mutation — en-têtes reçus ou non, pendant la lecture, le décodage ou la fermeture du corps — donne `MUTATION_OUTCOME_UNKNOWN` avec les identifiants, le statut HTTP et le request id connus. Aucune mutation n'est rejouée. Une erreur de fermeture après un corps complet (selon `Content-Length`) n'écrase pas le résultat.
- Aucun champ du schéma ne choisit la taille CPU, RAM ou GPU d'une VM Cursor. `env.type` `pool` ou `machine` vise un worker auto-hébergé.
- `CreateRunRequest` n'a pas de champ `model` dans l'OpenAPI consulté. Le modèle d'une session est celui de la création pour ce MCP.

## Matrice de support

Relue le 5 octobre 2026 contre le code du SDK Python officiel `cursor-sdk` 1.0.36 et de son bridge Node. Pour un agent cloud, le bridge appelle le même REST v1 (`https://api.cursor.com`, client `CloudApiClient`) que ce MCP : la colonne « SDK → REST » indique ce que le SDK envoie réellement à ce REST, même quand l'OpenAPI du 30 septembre ne le documente pas. « Bridge seul » signifie sans équivalent REST.

| Capacité | Ce MCP | OpenAPI du 30 septembre | SDK → REST | Vérifié en réel |
|---|---|---|---|---|
| Création, continuation, annulation, archivage, suppression | oui | oui | oui | oui (1er et 5 octobre 2026) |
| `startingRef` en nom de branche | oui | oui | oui | oui |
| `startingRef` en SHA complet | refusé localement | annoncé | annoncé | refusé (400) le 1er octobre 2026 |
| Filtre `prUrl` de `GET /v1/agents` | oui (`pr_url`) | oui | oui | oui (5 octobre 2026) |
| Coût brut / facturé (`cost`) | oui (`cursor_get_usage`) | non | oui | oui (5 octobre 2026) |
| Erreur d'un run (`Run.error`) | oui, si présente | non | oui | jamais observée : absente des runs `ERROR` relus le 5 octobre 2026 |
| `helpUrl`, `provider` des erreurs | oui | oui | oui | non |
| Images dans le prompt | non | oui | oui | non |
| Serveurs MCP distants (`mcpServers`) | non | oui | oui | non |
| Sous-agents personnalisés | non | oui | oui | non |
| Clé d'idempotence (`Idempotency-Key`, création et envoi) | non (`agentId` fixé avant l'envoi à la place) | non | oui (uuid4 par création) | non |
| Modèle par envoi (`model` sur `POST .../runs`) | non | non | oui | non |
| Variables d'environnement limitées à un run | non | non | oui | non |
| Métadonnées d'agent (`metadata`) | non | non | oui | non |
| Conversation d'un run | non (flux SSE et `cursor_get_run`) | non | oui, reconstruite côté client depuis `interaction_update` | non |
| Observation avec reprise par ordinal (`observe`) | non | non | bridge seul | non |

Prochaine étape possible : un test réel, payant et explicitement autorisé, de `Idempotency-Key` et de `model` sur la continuation. Si l'API les accepte, l'idempotence rendrait une création relançable après `MUTATION_OUTCOME_UNKNOWN`, et lèverait l'incompatibilité entre `agentId` et `envVars`.
