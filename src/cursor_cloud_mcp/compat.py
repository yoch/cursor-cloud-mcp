"""Contournements du SDK MCP, isolés ici et limités aux outils de ce serveur."""

import functools
import json
from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver.tools import Tool
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, ConfigDict


def strict_tool(fn: Callable[..., Any], *, name: str, annotations: ToolAnnotations) -> Tool:
    """Construit un outil qui refuse les arguments inconnus et rend une sortie compacte.

    MCP 2.3.0 accepte encore les champs inconnus (``ArgModelBase`` sans ``extra="forbid"``).
    Plutôt que de modifier cette classe pour tout le processus, on dérive un modèle propre
    à l'outil et on republie son schéma : schéma annoncé et validation restent cohérents.

    Le SDK écrit aussi le bloc texte en JSON indenté, champs nuls compris : souvent plus du
    double du contenu utile, relu par le modèle à chaque appel. Le texte et le contenu
    structuré deviennent ici le même JSON compact, sans champ nul.
    """
    tool = Tool.from_function(fn, name=name, annotations=annotations)
    base = tool.fn_metadata.arg_model
    strict = type(
        base.__name__,
        (base,),
        {
            "__module__": base.__module__,
            "model_config": ConfigDict(**{**base.model_config, "extra": "forbid"}),
        },
    )
    tool.fn_metadata.arg_model = strict
    tool.parameters = strict.model_json_schema(by_alias=True)
    tool.fn = _compact(fn)
    return tool


def compact_payload(view: BaseModel) -> dict[str, Any]:
    return view.model_dump(mode="json", by_alias=True, exclude_none=True)


def _compact(fn: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        result = await fn(*args, **kwargs)
        if not isinstance(result, BaseModel):
            return result
        payload = compact_payload(result)
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return CallToolResult(content=[TextContent(type="text", text=text)], structured_content=payload)

    return wrapper
