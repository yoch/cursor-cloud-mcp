"""Workarounds for the MCP SDK, isolated here and limited to this server's tools."""

import functools
import json
from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver.tools import Tool
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, ConfigDict


def strict_tool(fn: Callable[..., Any], *, name: str, annotations: ToolAnnotations) -> Tool:
    """Build a tool that rejects unknown arguments and returns a compact output.

    MCP 2.3.0 still accepts unknown fields (``ArgModelBase`` without ``extra="forbid"``).
    Rather than modifying that class for the whole process, we derive a model specific
    to the tool and republish its schema: the advertised schema and the validation stay consistent.

    The SDK also writes the text block as indented JSON, null fields included: often more than
    double the useful content, re-read by the model on every call. The text and the structured
    content become the same compact JSON here, without null fields.
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
