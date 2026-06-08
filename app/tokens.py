"""tiktoken-based token measurement -- the single source of truth for "what does
this ``tools/list`` payload cost the model?".

Both the live gateway (telemetry) and the demo routes measure tokens the
same way, against the same encoding (``cl100k_base`` by default, pinned in the
Dockerfile so it resolves offline), so the numbers on screen are exactly what an
MCP client pays on the wire.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

import tiktoken

from app.settings import settings


@lru_cache(maxsize=4)
def _encoding(name: str) -> tiktoken.Encoding:
    return tiktoken.get_encoding(name)


def count_tokens(text: str, *, encoding: str | None = None) -> int:
    """Count tokens in a string using the configured tiktoken encoding."""

    if not text:
        return 0
    enc = _encoding(encoding or settings.tokenizer_encoding)
    return len(enc.encode(text))


def tool_to_wire_dict(tool: Any) -> dict[str, Any]:
    """Project a tool to the MCP wire shape a client actually receives.

    Accepts a FastMCP ``Tool`` (converted via ``to_mcp_tool``) or an
    already-projected dict. Keeps the fields injected into the model's context:
    ``name``, ``description`` and ``inputSchema`` (the parameter schema). This is
    what makes the token count honest -- it measures the real payload, not a
    hand-rolled approximation.
    """

    if isinstance(tool, dict):
        name = tool.get("name", "")
        description = tool.get("description") or ""
        schema = tool.get("inputSchema") or tool.get("parameters") or {}
        return {"name": name, "description": description, "inputSchema": schema}

    # FastMCP Tool -> MCP wire Tool.
    if hasattr(tool, "to_mcp_tool"):
        mcp_tool = tool.to_mcp_tool()
        return {
            "name": mcp_tool.name,
            "description": mcp_tool.description or "",
            "inputSchema": mcp_tool.inputSchema or {},
        }

    # Duck typing for an already-wire mcp.types.Tool (e.g. from a client
    # ``list_tools()``): it carries ``inputSchema`` (not ``parameters``). Reading
    # the wrong field here would silently drop every tool's schema and grossly
    # undercount the real injected payload.
    schema = getattr(tool, "inputSchema", None)
    if schema is None:
        schema = getattr(tool, "parameters", None)
    return {
        "name": getattr(tool, "name", ""),
        "description": getattr(tool, "description", None) or "",
        "inputSchema": schema or {},
    }


def measure_tools(tools: list[Any]) -> tuple[int, int]:
    """Return ``(token_count, payload_bytes)`` for a ``tools/list`` payload.

    Serializes the wire-shaped tool list to JSON exactly once, then measures both
    its token cost and its byte size -- the two numbers the dashboard shows.
    """

    wire = {"tools": [tool_to_wire_dict(t) for t in tools]}
    payload = json.dumps(wire, ensure_ascii=False)
    return count_tokens(payload), len(payload.encode("utf-8"))
