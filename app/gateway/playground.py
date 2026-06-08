"""Live 'try it yourself' driver -- official GitHub MCP vs smart gateway, on-device.

Powers the ``/demo/try`` endpoints. Given a natural-language task, it runs the
identical prompt through a **local model** (Ollama) twice -- a literal A/B
against two *different servers*:

* **raw** -- the official remote GitHub MCP server, called directly: every tool,
  full descriptions (the literal "flat injection" baseline). Authenticated with
  the gateway's PAT server-side; falls back to the gateway's full list if the
  upstream is unreachable, so the demo never breaks.
* **smart** -- the gateway, handed the *same task* as a free-form ``x-mcp-query``.
  It embeds the task, runs ``$vectorSearch`` over the catalog, and
  returns only the top-k tools -- same tools, same descriptions, just far fewer
  of them. Nothing declared up front.

For each it records the *model-reported* prompt/completion tokens (measured, not
estimated), which tool the model picked, and whether that pick is correct. No
cloud, no API key -- the win is structural, so it shows up even on a small local
model.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from typing import Any

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from app.ollama import chat_stream, list_models
from app.settings import Settings, settings
from app.tokens import count_tokens, tool_to_wire_dict

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)
_PICK_LINE = re.compile(r"PICK:\s*([A-Za-z0-9_./-]+)")

# How many tokens the model may generate. The answer is tiny, but we ask for one
# line of visible reasoning first so the stream is something you can watch.
_NUM_PREDICT = 192

_SYSTEM_PROMPT = (
    "You are a tool-routing agent. You are given a catalog of available tools. "
    "Pick the single best tool to accomplish the user's request.\n\n"
    "Respond in exactly this shape:\n"
    "- First, ONE short sentence: which tool you'll use and why.\n"
    "- Then a final line on its own, exactly: PICK: <tool_name>\n"
    "Use the tool's exact name from the catalog. Write nothing after the PICK "
    "line.\n\nAVAILABLE TOOLS:\n{tools}"
)

# The demo is pointed at a real, recognizable public repo: MongoDB's own
# open-source MCP server. The meta payoff -- a smart MCP gateway routing an agent
# over the real GitHub MCP server to work on MongoDB's MCP server.
DEMO_REPO = "mongodb-js/mongodb-mcp-server"
DEMO_REPO_URL = f"https://github.com/{DEMO_REPO}"
DEMO_REPO_NOTE = "MongoDB's own open-source MCP server"

# Curated tasks. The task text is exactly what gets sent to the gateway as the
# free-form X-MCP-Query -- the demo declares nothing up front, it just describes
# the work. ``expected_tool`` is the tool a correct agent should pick.
DEMO_TASKS: list[dict[str, str]] = [
    {
        "label": "Review a pull request",
        "task": f"A contributor opened a pull request on {DEMO_REPO}. Pull it up so I can review the diff.",
        "expected_tool": "pull_request_read",
    },
    {
        "label": "Triage open issues",
        "task": f"Show me the open issues on {DEMO_REPO} so I can start triaging the backlog.",
        "expected_tool": "list_issues",
    },
    {
        "label": "Debug a failed CI run",
        "task": f"CI on {DEMO_REPO} has been red all morning -- pull up the recent workflow runs.",
        "expected_tool": "actions_list",
    },
    {
        "label": "Check the latest release",
        "task": f"What's the latest published release of {DEMO_REPO}?",
        "expected_tool": "get_latest_release",
    },
    {
        "label": "Find org repositories",
        "task": "Find the other open-source repositories under the mongodb-js organization.",
        "expected_tool": "search_repositories",
    },
]


class PlaygroundError(Exception):
    """A user-facing error (bad config / model failure), safe to surface."""


def _tools_json(tools: list[Any]) -> str:
    return json.dumps(
        {"tools": [tool_to_wire_dict(t) for t in tools]}, ensure_ascii=False
    )


def _names(tools: list[Any]) -> list[str]:
    return [t.name if hasattr(t, "name") else t["name"] for t in tools]


def _pick_tool(answer: str, candidates: list[str]) -> str | None:
    match = _PICK_LINE.search(answer)
    if match:
        picked = match.group(1).strip().strip("`\"'")
        for name in candidates:
            if picked == name:
                return name
    cleaned = answer.strip().strip("`\"' \n")
    for name in candidates:
        if cleaned == name:
            return name
    for name in sorted(candidates, key=len, reverse=True):
        if name in answer:
            return name
    return None


async def _list_tools(url: str, headers: dict[str, str]) -> list[Any]:
    async with Client(transport=StreamableHttpTransport(url=url, headers=headers)) as client:
        return list(await client.list_tools())


async def _raw_tools_from_upstream(cfg: Settings, gateway_url: str) -> list[Any]:
    """List the *literal* upstream catalog -- the official GitHub MCP server."""

    if cfg.github_pat:
        headers = {"Authorization": f"Bearer {cfg.github_pat}"}
        if cfg.github_readonly:
            headers["X-MCP-Readonly"] = "true"
        try:
            return await _list_tools(cfg.github_remote_url, headers)
        except Exception:  # noqa: BLE001 -- fall back to the gateway's full list
            pass
    # No query => the gateway returns its full catalog (full descriptions; the
    # gateway never rewrites them), which is the firehose baseline.
    return await _list_tools(gateway_url, {})


async def _build_strategies(
    url: str, task: str, cfg: Settings
) -> tuple[list[Any], list[Any]]:
    """Return ``(raw_tools, smart_tools)`` as a literal A/B, fetched in parallel.

    The smart side hands the gateway the task itself as ``x-mcp-query`` -- the
    gateway retrieves the relevant tools by meaning.
    """

    raw_tools, smart_tools = await asyncio.gather(
        _raw_tools_from_upstream(cfg, url),
        _list_tools(url, {cfg.query_header: task}),
    )
    return raw_tools, smart_tools


async def _run_one_stream(
    *,
    model: str,
    host: str | None,
    tools: list[Any],
    task: str,
    stage: str,
) -> AsyncIterator[dict[str, Any]]:
    """One local model call, streamed: emit prefill start, token deltas, result."""

    candidates = _names(tools)
    tools_json = _tools_json(tools)
    system = _SYSTEM_PROMPT.format(tools=tools_json)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": task},
    ]
    injected_tokens = count_tokens(tools_json)

    yield {
        "type": "infer",
        "stage": stage,
        "tool_count": len(tools),
        "injected_tokens": injected_tokens,
    }

    parts: list[str] = []
    usage: dict[str, Any] = {}
    async for ev in chat_stream(model=model, messages=messages, host=host, num_predict=_NUM_PREDICT):
        if ev["type"] == "token":
            parts.append(ev["text"])
            yield {"type": "token", "stage": stage, "text": ev["text"]}
        elif ev["type"] == "done":
            usage = ev

    answer = _THINK_BLOCK.sub("", "".join(parts)).strip()
    latency_ms = usage.get("compute_ms") or usage.get("latency_ms")
    result = {
        "tool_count": len(tools),
        "injected_tool_tokens": injected_tokens,
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "picked_tool": _pick_tool(answer, candidates),
        "answer": answer[:200],
        "latency_ms": latency_ms,
        "wall_ms": usage.get("latency_ms"),
    }
    yield {"type": "result", "stage": stage, "result": result}


_STREAM_DONE = object()


async def _merge_streams(
    gens: dict[str, AsyncIterator[dict[str, Any]]],
) -> AsyncIterator[dict[str, Any]]:
    """Drive several event generators concurrently, yielding events as they arrive."""

    queue: asyncio.Queue[Any] = asyncio.Queue()

    async def _pump(gen: AsyncIterator[dict[str, Any]]) -> None:
        try:
            async for ev in gen:
                await queue.put(ev)
        except Exception as exc:  # noqa: BLE001 -- surfaced to the UI as an error event
            await queue.put({"type": "error", "detail": str(exc)})
        finally:
            await queue.put(_STREAM_DONE)

    tasks = [asyncio.create_task(_pump(g)) for g in gens.values()]
    remaining = len(tasks)
    try:
        while remaining:
            ev = await queue.get()
            if ev is _STREAM_DONE:
                remaining -= 1
                continue
            yield ev
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def run_playground_stream(
    *,
    url: str,
    task: str,
    model: str | None = None,
    host: str | None = None,
    expected_tool: str | None = None,
    cfg: Settings = settings,
) -> AsyncIterator[dict[str, Any]]:
    """Stream the full raw-vs-smart run as structured events for the live UI.

    Event types: ``phase``, ``built``, ``infer`` / ``token`` / ``result`` (per
    strategy), ``summary``, ``error``. The two strategies run **concurrently** so
    the UI streams both at once; the reported latency is the model's own
    prefill+generation time, fair even when the passes contend for one runtime.
    """

    if not task.strip():
        yield {"type": "error", "detail": "Task is empty."}
        return
    model = (model or cfg.ollama_model).strip()

    yield {
        "type": "phase",
        "step": "build",
        "msg": "Fetching both catalogs over MCP -- raw from the official GitHub server, smart by handing this gateway the task...",
    }
    try:
        raw_tools, smart_tools = await _build_strategies(url, task, cfg)
    except Exception as exc:  # noqa: BLE001 -- surfaced to the UI as an error event
        yield {"type": "error", "detail": f"Failed to fetch tools over MCP (smart gateway {url}): {exc}"}
        return

    routed = bool(task.strip()) and len(smart_tools) <= cfg.route_top_k and len(smart_tools) < len(raw_tools)
    yield {
        "type": "built",
        "query": task,
        "model": model,
        "routed": routed,
        "route_top_k": cfg.route_top_k,
        "raw": {
            "tool_count": len(raw_tools),
            "list_tokens": count_tokens(_tools_json(raw_tools)),
            "names": _names(raw_tools),
        },
        "smart": {
            "tool_count": len(smart_tools),
            "list_tokens": count_tokens(_tools_json(smart_tools)),
            "names": _names(smart_tools),
        },
    }
    if not routed:
        yield {
            "type": "error",
            "detail": (
                "Vector retrieval is not ready yet, so the gateway returned its "
                "fail-open full catalog. Wait for embeddings/indexes to finish, "
                "then run the comparison again."
            ),
        }
        return

    results: dict[str, dict[str, Any]] = {}
    streams = {
        "raw": _run_one_stream(model=model, host=host, tools=raw_tools, task=task, stage="raw"),
        "smart": _run_one_stream(model=model, host=host, tools=smart_tools, task=task, stage="smart"),
    }
    async for ev in _merge_streams(streams):
        if ev["type"] == "error":
            yield ev
            return
        if ev["type"] == "result":
            results[ev["stage"]] = ev["result"]
        yield ev

    raw = results.get("raw", {})
    smart = results.get("smart", {})
    if expected_tool:
        raw["correct"] = raw.get("picked_tool") == expected_tool
        smart["correct"] = smart.get("picked_tool") == expected_tool

    pt_raw = raw.get("prompt_tokens") or 0
    pt_smart = smart.get("prompt_tokens") or 0
    savings = {
        "prompt_factor": round(pt_raw / pt_smart, 2) if pt_smart else None,
        "prompt_saved": pt_raw - pt_smart,
    }
    yield {
        "type": "summary",
        "summary": {
            "query": task,
            "task": task,
            "model": model,
            "provider": "ollama",
            "expected_tool": expected_tool,
            "raw": raw,
            "smart": smart,
            "savings": savings,
        },
    }


async def run_playground(
    *,
    url: str,
    task: str,
    model: str | None = None,
    host: str | None = None,
    expected_tool: str | None = None,
    cfg: Settings = settings,
) -> dict[str, Any]:
    """Non-streaming wrapper over :func:`run_playground_stream` for ``POST /demo/try``."""

    summary: dict[str, Any] | None = None
    async for ev in run_playground_stream(
        url=url, task=task, model=model, host=host,
        expected_tool=expected_tool, cfg=cfg,
    ):
        if ev["type"] == "error":
            raise PlaygroundError(ev["detail"])
        if ev["type"] == "summary":
            summary = ev["summary"]
    if summary is None:
        raise PlaygroundError("No result produced by the local model.")
    return summary


async def playground_config(cfg: Settings = settings) -> dict[str, Any]:
    """Config for the UI: local-runtime readiness, defaults, and demo tasks."""

    models = await list_models(cfg.ollama_host)
    return {
        "local_ready": models is not None,
        "local_models": models or [],
        "default_model": cfg.ollama_model,
        "default_host": cfg.ollama_host,
        "tasks": DEMO_TASKS,
        "repo": DEMO_REPO,
        "repo_url": DEMO_REPO_URL,
        "repo_note": DEMO_REPO_NOTE,
    }
