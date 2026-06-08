"""Proxy to the real, official GitHub MCP server, plus destructive-tool detection.

The gateway proxies the *remote* GitHub MCP server and serves its catalog as-is
(the "firehose"). The only judgement it makes about a tool without being asked is
whether it's *destructive* -- that's the safety floor (see
:class:`app.gateway.transforms.SafetyFloorTransform`). Everything else about which
tools a caller sees is decided by semantic retrieval, not by hand.
"""

from __future__ import annotations

import re
from typing import Any

from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server import create_proxy

from app.settings import Settings, settings

# ---------------------------------------------------------------------------
# THE BLOCKLIST -- edit here.
#
# The authoritative signal is the upstream's own MCP tool annotations
# (``readOnlyHint`` / ``destructiveHint``), which :func:`is_destructive` trusts
# first. These needles are only a *backstop* for tools the upstream leaves
# UNannotated -- so we keep the set tiny and unambiguous: catastrophic, clearly
# irreversible verbs. They are matched as whole words in the tool name (see
# :func:`_name_looks_destructive`), never as loose substrings -- so a read-only
# tool like ``list_org_admins`` is never mistaken for a destructive one.
#
# To widen the backstop, add another unambiguous destructive verb (lowercase,
# e.g. ``"truncate"``). Toggle the whole floor with ``MCPX_BLOCK_DESTRUCTIVE``;
# ask the upstream for a read-only catalog with ``MCPX_GITHUB_READONLY``. See the
# "Safety floor" docs in README.md.
# ---------------------------------------------------------------------------
DESTRUCTIVE_NEEDLES = ("delete", "destroy", "purge", "erase", "wipe")

# Split a tool name into lowercase word tokens (snake_case, kebab-case, etc.) so
# the backstop matches whole words -- ``delete`` in ``delete_file`` -- and never
# a fragment buried in an unrelated word.
_WORD_SPLIT = re.compile(r"[^a-z0-9]+")


def _name_looks_destructive(name: str) -> bool:
    """Backstop heuristic: does a tool name contain a destructive verb as a *word*?"""

    tokens = _WORD_SPLIT.split(name.lower())
    return any(token in DESTRUCTIVE_NEEDLES for token in tokens)


def is_destructive(tool: Any) -> bool:
    """True if a tool destroys state, used only by the safety floor.

    Leans on the upstream's own MCP annotations, which are authoritative:

    1. ``readOnlyHint`` -> never destructive (a read-only tool can't destroy
       anything). This is what stops false positives like a "list admins" tool.
    2. ``destructiveHint`` -> destructive, full stop.
    3. Otherwise (typically a tool the upstream left unannotated), fall back to a
       conservative whole-word match on unambiguous destructive verbs in the name.
    """

    annotations = getattr(tool, "annotations", None)
    if annotations is not None:
        if getattr(annotations, "readOnlyHint", None):
            return False
        if getattr(annotations, "destructiveHint", None):
            return True
    return _name_looks_destructive(getattr(tool, "name", "") or "")


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
