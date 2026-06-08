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

# ---------------------------------------------------------------------------
# THE SAFETY FLOOR -- annotation-only, the philosophy lives here.
#
# The floor leans *entirely* on the upstream's own MCP tool annotations
# (``readOnlyHint`` / ``destructiveHint``). The gateway makes no guess of its
# own: a tool is destructive iff the upstream *says* so. There is deliberately
# no name-based heuristic -- inferring "delete" from a tool name is unreliable
# (it sails right past a ``label_write`` tool whose ``delete`` lives in a method
# parameter, while offering a false sense of safety), and it is the wrong layer
# for the fix. Annotating tools for risk is the MCP *server author's* job; a
# downstream gateway papering over missing annotations with string-matching only
# hides the real gap.
#
# Toggle the whole floor with ``MCPX_BLOCK_DESTRUCTIVE``; ask the upstream for a
# read-only catalog with ``MCPX_GITHUB_READONLY``. See the "Safety floor" docs
# in README.md.
# ---------------------------------------------------------------------------


def is_destructive(tool: Any) -> bool:
    """True if a tool destroys state, used only by the safety floor.

    Decided *entirely* by the upstream's own MCP annotations, which are
    authoritative:

    1. ``readOnlyHint`` -> never destructive (a read-only tool can't destroy
       anything). This is what stops false positives like a "list admins" tool.
    2. ``destructiveHint`` -> destructive, full stop.
    3. Otherwise (no annotation object, or the upstream left the hints unset) ->
       treated as non-destructive. The gateway does not guess from the tool
       name; supplying accurate annotations is the upstream server's job.
    """

    annotations = getattr(tool, "annotations", None)
    if annotations is None:
        return False
    if getattr(annotations, "readOnlyHint", None):
        return False
    return bool(getattr(annotations, "destructiveHint", None))


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
