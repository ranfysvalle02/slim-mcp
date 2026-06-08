"""Server-level transform: the safety floor.

``SafetyFloorTransform`` is *static* with respect to the caller (it shapes the
whole catalog) and runs before the per-request
:class:`app.gateway.middleware.SemanticFilterMiddleware`. It drops *destructive*
tools in both directions (list *and* lookup-by-name), so a semantic match can
never surface (nor a client invoke) something like ``delete_file``. This is the
only hand-written rule in the gateway; everything else is learned from the tools'
own descriptions.

Note: the gateway never rewrites a tool's description. The tools the model sees
are byte-for-byte identical to what the upstream server published -- the *only*
thing the gateway changes is *how many* of them it hands over.
"""

from __future__ import annotations

from collections.abc import Sequence

from fastmcp.server.transforms import GetToolNext, Transform, VersionSpec
from fastmcp.tools import Tool

from app.gateway.github_proxy import is_destructive


class SafetyFloorTransform(Transform):
    """Permanently hide destructive tools (the only static rule in the gateway).

    A *floor*, not a filter: it sits above the semantic middleware and is
    caller-independent. Enforced on both ``list_tools`` and ``get_tool`` so a
    client can't invoke a destructive tool it already knows by name, and so a
    high-scoring semantic match for, say, "clean up the repo" can never hand the
    model ``delete_repository``.
    """

    def __init__(self, enabled: bool = True):
        self.enabled = enabled

    def __repr__(self) -> str:
        return f"SafetyFloorTransform(enabled={self.enabled})"

    async def list_tools(self, tools: Sequence[Tool]) -> Sequence[Tool]:
        if not self.enabled:
            return tools
        return [t for t in tools if not is_destructive(t)]

    async def get_tool(
        self, name: str, call_next: GetToolNext, *, version: VersionSpec | None = None
    ) -> Tool | None:
        tool = await call_next(name, version=version)
        if tool and self.enabled and is_destructive(tool):
            return None  # invisible to lookup-by-name too
        return tool
