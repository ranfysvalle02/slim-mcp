"""Minimal client for a fully local Ollama runtime -- no cloud SDKs, no keys.

The entire "try it yourself" demo (and route-by-meaning embeddings) runs
on-device through this thin wrapper over Ollama's native HTTP API:

* ``chat`` / ``chat_stream`` post to ``/api/chat`` and read Ollama's own
  ``prompt_eval_count`` / ``eval_count``, so the token numbers are the *model's*,
  not an estimate.
* ``embed`` posts to ``/api/embed`` for local 768-dim tool embeddings.
* ``list_models`` hits ``/api/tags`` so the UI can confirm a model is loaded.

``think=False`` keeps reasoning models (Qwen3, etc.) from emitting hidden
``<think>`` blocks. Nothing here talks to a remote provider; if Ollama isn't
running, calls fail closed with a clear, on-device error.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.settings import settings

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)


class OllamaError(Exception):
    """A user-facing error talking to the local Ollama runtime."""


def _root(host: str | None) -> str:
    """Normalize a host to the native API root.

    Accepts a bare host (``http://localhost:11434``) or an OpenAI-style base
    that ends in ``/v1`` and trims it, since we speak the native API.
    """

    h = (host or settings.ollama_host).rstrip("/")
    if h.endswith("/v1"):
        h = h[: -len("/v1")]
    return h


async def list_models(host: str | None = None, *, timeout: float = 0.8) -> list[str] | None:
    """Return advertised model names, or ``None`` if the runtime is unreachable."""

    base = _root(host)
    try:
        async with httpx.AsyncClient(timeout=timeout) as http:
            resp = await http.get(base + "/api/tags")
            resp.raise_for_status()
            data = resp.json()
        return sorted(m["name"] for m in data.get("models", []) if m.get("name"))
    except Exception:  # noqa: BLE001 -- unreachable is a normal, expected state
        return None


async def embed(
    inputs: str | list[str],
    *,
    model: str | None = None,
    host: str | None = None,
    timeout: float = 120.0,
) -> list[list[float]] | None:
    """Embed text locally via Ollama's ``/api/embed`` -- one vector per input.

    Accepts a single string or a batch; always returns a list of vectors aligned
    to the inputs, or ``None`` if the runtime/model is unreachable. Returning
    ``None`` rather than raising lets the routing layer fall back to serving the
    full catalog instead of failing the request.
    """

    base = _root(host)
    payload = {"model": model or settings.embed_model, "input": inputs}
    try:
        async with httpx.AsyncClient(timeout=timeout) as http:
            resp = await http.post(base + "/api/embed", json=payload)
            resp.raise_for_status()
            data = resp.json()
    except Exception:  # noqa: BLE001 -- unreachable/unpulled is a normal fallback
        return None
    vectors = data.get("embeddings")
    if not isinstance(vectors, list) or not vectors:
        return None
    return vectors


async def chat(
    *,
    model: str,
    messages: list[dict[str, str]],
    host: str | None = None,
    num_predict: int = 32,
    timeout: float = 600.0,
) -> dict[str, Any]:
    """Run one local chat completion and return content + the model's token counts."""

    base = _root(host)
    url = base + "/api/chat"
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
        "think": False,  # reasoning models: skip <think>, answer directly
        "options": {"temperature": 0, "num_predict": num_predict},
    }

    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout) as http:
            resp = await http.post(url, json=payload)
            # Some models reject `think`; retry once without it before giving up.
            if resp.status_code >= 400 and "think" in resp.text.lower():
                payload.pop("think", None)
                resp = await http.post(url, json=payload)
    except httpx.HTTPError as exc:
        raise OllamaError(
            f"Could not reach a local model at {base}. Is Ollama running "
            f"(`ollama serve`) and is the model pulled? ({exc})"
        ) from exc
    latency_ms = (time.perf_counter() - t0) * 1000.0

    if resp.status_code >= 400:
        raise OllamaError(f"Local model error ({resp.status_code}): {_error_detail(resp)}")

    data = resp.json()
    msg = data.get("message") or {}
    content = _THINK_BLOCK.sub("", msg.get("content") or "").strip()
    prompt_tokens = data.get("prompt_eval_count")
    completion_tokens = data.get("eval_count")
    total = None
    if prompt_tokens is not None or completion_tokens is not None:
        total = (prompt_tokens or 0) + (completion_tokens or 0)
    return {
        "content": content,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total,
        "latency_ms": round(latency_ms, 1),
    }


async def chat_stream(
    *,
    model: str,
    messages: list[dict[str, str]],
    host: str | None = None,
    num_predict: int = 32,
    timeout: float = 600.0,
) -> AsyncIterator[dict[str, Any]]:
    """Stream a local chat completion token-by-token.

    Yields ``{"type": "token", "text": ...}`` for each content delta, then a
    final ``{"type": "done", ...}`` carrying the model's own
    ``prompt_eval_count`` / ``eval_count`` and wall latency. This is what lets the
    UI show the *prefill* wait (the model reading a 20k-token catalog) honestly,
    instead of a spinner.
    """

    base = _root(host)
    url = base + "/api/chat"
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": True,
        "think": False,  # reasoning models: skip <think>, answer directly
        "options": {"temperature": 0, "num_predict": num_predict},
    }

    t0 = time.perf_counter()
    # One retry: some models reject the `think` flag with a 400.
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=timeout) as http:
                async with http.stream("POST", url, json=payload) as resp:
                    if resp.status_code >= 400:
                        body = (await resp.aread()).decode("utf-8", "replace")
                        if attempt == 0 and "think" in body.lower():
                            payload.pop("think", None)
                            continue
                        raise OllamaError(
                            f"Local model error ({resp.status_code}): {body[:300]}"
                        )
                    async for line in resp.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        chunk = (obj.get("message") or {}).get("content") or ""
                        if chunk:
                            yield {"type": "token", "text": chunk}
                        if obj.get("done"):
                            pt = obj.get("prompt_eval_count")
                            ct = obj.get("eval_count")
                            total = (
                                (pt or 0) + (ct or 0)
                                if (pt is not None or ct is not None)
                                else None
                            )
                            # The model's *own* compute time (prefill + generation),
                            # excluding queue wait and model load. This keeps the
                            # latency A/B fair even when both passes run at once and
                            # contend for the GPU.
                            eval_ns = (obj.get("prompt_eval_duration") or 0) + (
                                obj.get("eval_duration") or 0
                            )
                            yield {
                                "type": "done",
                                "prompt_tokens": pt,
                                "completion_tokens": ct,
                                "total_tokens": total,
                                "latency_ms": round((time.perf_counter() - t0) * 1000.0, 1),
                                "compute_ms": round(eval_ns / 1e6, 1) if eval_ns else None,
                            }
            return
        except httpx.HTTPError as exc:
            raise OllamaError(
                f"Could not reach a local model at {base}. Is Ollama running "
                f"(`ollama serve`) and is the model pulled? ({exc})"
            ) from exc


def _error_detail(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("error") or resp.text)[:300]
    except Exception:  # noqa: BLE001
        return resp.text[:300]
