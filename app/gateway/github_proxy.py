"""Proxy to the real, official GitHub MCP server, plus destructive-tool detection.

The gateway proxies the *remote* GitHub MCP server and serves its catalog as-is
(the "firehose"). The only judgement it makes about a tool without being asked is
whether it's *destructive* -- that's the safety floor (see
:class:`app.gateway.transforms.SafetyFloorTransform`). Everything else about which
tools a caller sees is decided by semantic retrieval, not by hand.
"""

from __future__ import annotations

from typing import Any

from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server import create_proxy

from app.settings import Settings, settings

# Belt-and-suspenders fallback on top of the upstream's own ``destructiveHint``.
_DESTRUCTIVE_NEEDLES = ("delete", "admin")


def is_destructive(tool: Any) -> bool:
    """True if a tool mutates/destroys state -- the upstream's destructive hint,
    or a delete/admin name as a fallback. Used only by the safety floor."""

    annotations = getattr(tool, "annotations", None)
    if annotations is not None and getattr(annotations, "destructiveHint", False):
        return True
    name = (getattr(tool, "name", "") or "").lower()
    return any(needle in name for needle in _DESTRUCTIVE_NEEDLES)


def build_github_proxy(cfg: Settings = settings, *, name: str = "Lean-MCP-Gateway") -> FastMCP:
    """Build a FastMCP proxy to the official remote GitHub MCP server.

    Authenticated with the gateway's own PAT (server-side; the browser never
    sees it). Refuses to start without a PAT -- this is a real integration.
    """

    if not cfg.github_pat:
        raise RuntimeError(
            "GITHUB_PAT is required: the gateway proxies the real GitHub MCP "
            "server. Put one in .env (https://github.com/settings/personal-access-tokens/new)."
        )
    headers = {"Authorization": f"Bearer {cfg.github_pat}"}
    if cfg.github_readonly:
        headers["X-MCP-Readonly"] = "true"
    transport = StreamableHttpTransport(url=cfg.github_remote_url, headers=headers)
    return create_proxy(Client(transport=transport), name=name)
