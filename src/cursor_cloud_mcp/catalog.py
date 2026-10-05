"""Résolution du modèle et du niveau de réflexion à partir du catalogue Cursor."""

from cursor_cloud_mcp.errors import ErrorCode, failure
from cursor_cloud_mcp.models import (
    ModelParam,
    RemoteModel,
    RemoteModelList,
    RemoteModelParameter,
)

_REASONING_IDS = ("effort", "reasoning_effort", "reasoning")


def reasoning_parameter(model: RemoteModel) -> RemoteModelParameter | None:
    """Premier paramètre de réflexion exposé, dans l'ordre effort, reasoning_effort, reasoning."""
    by_id = {parameter.id: parameter for parameter in model.parameters or []}
    for name in _REASONING_IDS:
        found = by_id.get(name)
        if found is not None:
            return found
    return None


def find_model(catalog: RemoteModelList, model_id: str) -> RemoteModel:
    """Un id du catalogue, ou un alias qui ne désigne qu'un seul modèle."""
    exact = next((item for item in catalog.items if item.id == model_id), None)
    if exact is not None:
        return exact
    matches = [item for item in catalog.items if model_id in (item.aliases or [])]
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise failure(
            ErrorCode.VALIDATION,
            f"L'alias {model_id} désigne plusieurs modèles : {', '.join(item.id for item in matches)}. "
            "Choisir un id.",
        )
    raise failure(
        ErrorCode.VALIDATION,
        f"{model_id} n'est ni un id ni un alias du catalogue Cursor. Appeler cursor_list_models.",
    )


def default_selection(model: RemoteModel) -> dict[str, str] | None:
    """Valeurs de la variante par défaut, limitées aux paramètres publiés."""
    published = {parameter.id for parameter in model.parameters or []}
    for variant in model.variants or []:
        if variant.isDefault:
            values = {item.id: item.value for item in variant.params if item.id in published}
            return values or None
    return None


def restricted(model: RemoteModel) -> bool:
    """Vrai si le catalogue n'offre pas toutes les combinaisons de valeurs publiées."""
    if not model.variants or not model.parameters:
        return False
    return len(_published_combinations(model)) < _product(model)


def resolve_model_selection(
    catalog: RemoteModelList,
    *,
    model_id: str,
    model_params: list[ModelParam] | None,
    reasoning_level: str | None,
) -> dict[str, object]:
    """Construit ``model`` pour POST /v1/agents. Refuse un id, une valeur ou une combinaison hors catalogue."""
    model = find_model(catalog, model_id)
    ordered: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in model_params or []:
        _require_param(model, item.id, item.value)
        if item.id in seen:
            raise failure(ErrorCode.VALIDATION, f"Le paramètre {item.id} est répété.")
        seen.add(item.id)
        ordered.append((item.id, item.value))
    if reasoning_level is not None:
        parameter = reasoning_parameter(model)
        if parameter is None:
            raise failure(
                ErrorCode.VALIDATION,
                f"{model.id} n'expose pas de niveau de réflexion (effort, reasoning_effort ou reasoning).",
            )
        allowed = _values(parameter)
        if reasoning_level not in allowed:
            raise failure(
                ErrorCode.VALIDATION,
                f"{parameter.id} pour {model.id} accepte : {', '.join(allowed)}. "
                "Aucune traduction n'est faite : xhigh et extra-high restent distincts.",
            )
        if parameter.id in seen:
            current = next(value for key, value in ordered if key == parameter.id)
            if current != reasoning_level:
                raise failure(ErrorCode.VALIDATION, "reasoning_level contredit model_params.")
        else:
            ordered.append((parameter.id, reasoning_level))
            seen.add(parameter.id)
    _require_combination(model, ordered)
    body: dict[str, object] = {"id": model.id}
    if ordered:
        body["params"] = [{"id": key, "value": value} for key, value in ordered]
    return body


def _require_combination(model: RemoteModel, chosen: list[tuple[str, str]]) -> None:
    """Une sélection partielle doit tenir dans au moins une variante publiée."""
    if not chosen or not model.variants:
        return
    wanted = set(chosen)
    if any(wanted <= combination for combination in _published_combinations(model)):
        return
    text = ", ".join(f"{key}={value}" for key, value in chosen)
    raise failure(
        ErrorCode.VALIDATION,
        f"Combinaison refusée par le catalogue de {model.id} : {text}. "
        f"cursor_list_models avec model_id={model.id} liste les variantes valides.",
    )


def _published_combinations(model: RemoteModel) -> list[set[tuple[str, str]]]:
    published = {parameter.id for parameter in model.parameters or []}
    found: list[set[tuple[str, str]]] = []
    for variant in model.variants or []:
        combination = {(item.id, item.value) for item in variant.params if item.id in published}
        if combination not in found:
            found.append(combination)
    return found


def _product(model: RemoteModel) -> int:
    total = 1
    for parameter in model.parameters or []:
        total *= len(parameter.values)
    return total


def _values(parameter: RemoteModelParameter) -> list[str]:
    return [item.value for item in parameter.values]


def _require_param(model: RemoteModel, param_id: str, value: str) -> None:
    found = next((parameter for parameter in model.parameters or [] if parameter.id == param_id), None)
    if found is None:
        names = ", ".join(parameter.id for parameter in model.parameters or []) or "aucun"
        raise failure(
            ErrorCode.VALIDATION,
            f"Paramètre {param_id} inconnu pour {model.id}. Paramètres : {names}.",
        )
    allowed = _values(found)
    if value not in allowed:
        raise failure(
            ErrorCode.VALIDATION,
            f"{param_id} pour {model.id} accepte : {', '.join(allowed)}.",
        )
