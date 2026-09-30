"""Résolution du modèle et du niveau de réflexion à partir du catalogue Cursor."""

from cursor_cloud_mcp.errors import ErrorCode, failure
from cursor_cloud_mcp.models import ModelParam, ReasoningParamView, RemoteModel, RemoteModelList, RemoteModelParameter

_REASONING_IDS = ("effort", "reasoning_effort", "reasoning")


def reasoning_parameter(model: RemoteModel) -> RemoteModelParameter | None:
    """Premier paramètre de réflexion exposé, dans l'ordre effort, reasoning_effort, reasoning."""
    by_id = {parameter.id: parameter for parameter in model.parameters or []}
    for name in _REASONING_IDS:
        found = by_id.get(name)
        if found is not None:
            return found
    return None


def reasoning_view(model: RemoteModel) -> ReasoningParamView | None:
    parameter = reasoning_parameter(model)
    if parameter is None:
        return None
    return ReasoningParamView(id=parameter.id, values=[item.value for item in parameter.values])


def resolve_model_selection(
    catalog: RemoteModelList,
    *,
    model_id: str,
    model_params: list[ModelParam] | None,
    reasoning_level: str | None,
    thinking: bool | None,
) -> dict[str, object]:
    """Construit ``model`` pour POST /v1/agents. Refuse un id ou une valeur hors catalogue."""
    model = next((item for item in catalog.items if item.id == model_id), None)
    if model is None:
        raise failure(
            ErrorCode.VALIDATION,
            "model_id est absent du catalogue Cursor. Appeler cursor_list_models et utiliser un id renvoyé. "
            "Les alias ne sont pas acceptés.",
        )
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
    if thinking is not None:
        value = "true" if thinking else "false"
        _require_param(model, "thinking", value)
        if "thinking" in seen:
            current = next(item_value for key, item_value in ordered if key == "thinking")
            if current != value:
                raise failure(ErrorCode.VALIDATION, "thinking contredit model_params.")
        else:
            ordered.append(("thinking", value))
    body: dict[str, object] = {"id": model.id}
    if ordered:
        body["params"] = [{"id": key, "value": value} for key, value in ordered]
    return body


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
