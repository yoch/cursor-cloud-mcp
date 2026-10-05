"""Contournements du SDK MCP, isolés ici et limités aux outils de ce serveur."""

from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver.tools import Tool
from mcp.types import ToolAnnotations
from pydantic import ConfigDict


def strict_tool(fn: Callable[..., Any], *, name: str, annotations: ToolAnnotations) -> Tool:
    """Construit un outil qui refuse les arguments inconnus.

    MCP 2.3.0 accepte encore les champs inconnus (``ArgModelBase`` sans ``extra="forbid"``).
    Plutôt que de modifier cette classe pour tout le processus, on dérive un modèle propre
    à l'outil et on republie son schéma : schéma annoncé et validation restent cohérents.
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
    return tool
