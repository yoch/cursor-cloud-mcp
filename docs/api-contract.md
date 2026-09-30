# Contrat API utilisé

Consultation du 30 septembre 2026.

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
| `GET /v1/agents/{id}/usage` | 200 `AgentUsageResponse` | `cursor_get_usage` |

## Champs envoyés

`POST /v1/agents`, uniquement les clés fournies, jamais de `null` :

- `prompt.text` : obligatoire, non vide
- `repos` : un seul élément `{ "url", "startingRef" }`
- `workOnCurrentBranch` : toujours `false`
- `autoCreatePR` : booléen, `false` si l'appelant ne demande pas `true`
- `agentId` : `bc-` suivi d'un UUID, fourni ou généré une fois avant l'envoi
- `name`, `model` (`id` et éventuellement `params[{id,value}]`), `mode` (`agent` ou `plan`) : seulement s'ils sont fournis

`POST /v1/agents/{id}/runs` : `prompt.text`, et `mode` seulement s'il est fourni.

`GET /v1/agents` et `GET /v1/agents/{id}/runs` : `limit` (1 à 100) et `cursor` seulement s'ils sont fournis.

`GET /v1/agents/{id}/usage` : `runId` seulement s'il est fourni.

## Champs lus

- Compte : `apiKeyName`, `createdAt`, et s'ils sont présents `userId`, `userEmail`, `userFirstName`, `userLastName`. Pas de secret de clé.
- Modèles : `items[]` avec `id`, `displayName`, et s'ils sont présents `description`, `aliases`, `parameters`, `variants`.
- Dépôts : `items[].url`. Pas de curseur dans ce schéma.
- Agents, page : `items[]` (`id`, `status`, `env`, `url`, `createdAt`, `updatedAt`, `name` et `latestRunId` optionnels) et `nextCursor` s'il est présent. Son absence signifie fin de liste, pas une valeur `null`.
- Agent : les champs de la page, plus `repos`, `workOnCurrentBranch`, `autoCreatePR` lorsqu'ils sont présents. Une absence reste une absence.
- Runs, page : `items[]` du schéma `Run` et `nextCursor` selon la même règle.
- Run : `id`, `agentId`, `status`, `createdAt`, `updatedAt`, et s'ils sont présents `durationMs`, `result`, `git.branches[]` (`repoUrl`, `branch`, `prUrl`). `git` est l'état courant de l'agent, pas un instantané immuable du run. `repoUrl` est renvoyé sans schéma. Aucun `final_sha` n'est inventé.
- Annulation : `id`.
- Usage : `totalUsage` et `runs[].usage` avec `inputTokens`, `outputTokens`, `cacheWriteTokens`, `cacheReadTokens`, `totalTokens`. `usageUuid` s'il est présent. Aucun coût : le schéma n'en contient pas.

États de run connus : `CREATING`, `RUNNING`, `FINISHED`, `ERROR`, `CANCELLED`, `EXPIRED`. Les quatre derniers sont terminaux. Tout autre état est conservé et n'est pas un succès.

États d'agent : la page endpoints décrit `ACTIVE`, `IDLE` et `ARCHIVED`. L'OpenAPI enumère seulement `ACTIVE` et `ARCHIVED`. `IDLE` est donc accepté et affiché. Un état inconnu reste visible.

`agentId` créé : `^bc-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$`.

## Erreurs distantes

Corps documenté : `{ "error": { "code", "message", "helpUrl"?, "provider"? } }`.

Codes cités par le schéma : `unauthorized`, `api_key_not_found`, `plan_required`, `role_forbidden`, `feature_unavailable`, `integration_not_connected`, `validation_error`, `missing_body`, `invalid_model`, `invalid_branch_name`, `repository_required`, `repository_access`, `pr_resolution_failed`, `artifact_not_found`, `service_account_required`, `agent_not_found`, `run_not_found`, `agent_busy`, `agent_archived`, `agent_id_conflict`, `run_not_cancellable`, `rate_limit_exceeded`, `usage_limit_exceeded`, `stream_expired`, `stream_unavailable`, `invalid_last_event_id`, `client_cancelled`, `not_implemented`, `upstream_error`, `internal_error`.

Statuts utilisés pour classer : 400 validation, 401 authentification, 403 permissions (`feature_unavailable` inclus), 404 ressource absente, 409 conflit, 429 quota. Le 429 peut porter `Retry-After`. L'OpenAPI mentionne aussi `X-RateLimit-Limit`, `X-RateLimit-Remaining` et `X-RateLimit-Reset`. Aucun identifiant de requête n'est spécifié : les en-têtes `x-request-id`, `request-id` et `x-cursor-request-id` sont conservés seulement s'ils sont présents.

`GET /v1/agents/{id}/usage` peut répondre `403 feature_unavailable` (accès anticipé).

`GET /v1/repositories` est limité à 1 requête par utilisateur et par minute, et 30 par heure. La page indique que la réponse peut prendre des dizaines de secondes.

## Ajustements assumés

- Les créations sont spécifiées en `201`. Un `200` avec le même JSON est accepté, car le succès se juge sur le schéma, pas sur un statut voisin.
- Deadline totale : 40 secondes par appel, 90 secondes pour `GET /v1/repositories`, parce que le contrat officiel prévient que cet appel peut durer des dizaines de secondes. Une deadline de 40 secondes rendrait le succès normal indistinguable d'un timeout.
- `IDLE` n'est pas dans l'enum OpenAPI de l'agent, mais la page endpoints le définit. Il n'est pas rejeté.
- Le flux SSE, les artifacts, l'archivage, la suppression, les workers et `prUrl` existent dans l'API et restent hors de ce MCP.
- `startingRef` est ignoré par Cursor lorsque `prUrl` est fourni. Ce MCP n'envoie pas `prUrl`.
