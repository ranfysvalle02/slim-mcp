"""Runtime configuration for the smart MCP gateway.

The gateway does **one** clever thing: instead of injecting a server's entire
tool catalog into the model every turn, it embeds that catalog once and, when a
caller says what they're doing (a free-form ``X-MCP-Query``), retrieves only the
handful of tools that the task actually needs -- by *meaning*, via MongoDB
``$vectorSearch``.

There is no hardcoded routing policy. The only static rule is a thin safety
floor that hides destructive tools by default (``block_destructive``); selection
itself is entirely learned from the tools' own descriptions.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MCPX_",
        env_nested_delimiter="__",
        # Load the project .env so plain secrets like GITHUB_PAT (no MCPX_ prefix)
        # are picked up; ignore any keys that don't map to a field.
        env_file=".env",
        extra="ignore",
    )

    env: str = Field(default="development")

    # --- Request interface --------------------------------------------------
    # The whole gateway interface is one header: a free-form description of the
    # task. The gateway embeds it and returns the most relevant tools (their
    # descriptions passed through untouched). With no query, it behaves like an
    # honest proxy and returns the full catalog.
    query_header: str = Field(default="x-mcp-query")

    # --- Safety floor -------------------------------------------------------
    # The only static rule: hide destructive tools by default, in both
    # directions (list and call). The judgement leans on the upstream's own MCP
    # annotations (``readOnlyHint`` / ``destructiveHint``), with a conservative
    # whole-word name match as a backstop for unannotated tools. Not an
    # authorization policy -- just a guardrail so a semantic match can never
    # surface `delete_file`.
    block_destructive: bool = Field(default=True)

    list_cache_ttl_seconds: float = Field(default=5.0)
    list_cache_max_entries: int = Field(default=64)

    # --- Persistence / telemetry -------------------------------------------
    # When unset, all Mongo writers degrade to no-ops so the gateway runs
    # standalone. Note: with Mongo off there is no embedded catalog, so the
    # gateway falls back to serving the full (proxied) catalog.
    mongo_uri: str | None = Field(default=None)
    mongo_database: str = Field(default="mcpx")

    # --- Semantic retrieval (MongoDB Vector Search) ------------------------
    # The core mechanism. Each tool is one document in ``tool_catalog`` with a
    # local embedding of its name + description. A free-form X-MCP-Query is
    # embedded the same way and matched via $vectorSearch, and the top-k tools
    # are returned. Disabled cleanly when off / unseeded -> falls back to the
    # full catalog, so it is never worse than a plain proxy.
    semantic_retrieval_enabled: bool = Field(default=True)
    # Tools returned per query (the post-retrieval tools/list size).
    route_top_k: int = Field(default=8)
    # $vectorSearch candidate pool scanned before the top-k is returned.
    route_num_candidates: int = Field(default=100)

    # --- Tokenization -------------------------------------------------------
    tokenizer_encoding: str = Field(default="cl100k_base")

    # --- Local model (Ollama, fully on-device) -----------------------------
    # The live demo runs entirely on a local Ollama runtime via its native API.
    # No cloud, no keys, nothing leaves the machine.
    ollama_host: str = Field(default="http://localhost:11434")
    ollama_model: str = Field(default="qwen3:14b")
    # Local embedding model for semantic retrieval. nomic-embed-text is small,
    # fast, on-device; its 768-dim output must match the Vector index.
    embed_model: str = Field(default="nomic-embed-text")
    embed_dimensions: int = Field(default=768)

    # --- Upstream GitHub MCP server (proxied) ------------------------------
    # The gateway proxies the official *remote* GitHub MCP server. The PAT is
    # read from the bare GITHUB_PAT env var (no MCPX_ prefix) to match GitHub's
    # convention and the project .env.
    github_pat: str | None = Field(default=None, validation_alias="GITHUB_PAT")
    # `/x/all` exposes every toolset so the firehose has real tools across
    # issues / pulls / repos / actions / releases / orgs / search.
    github_remote_url: str = Field(default="https://api.githubcopilot.com/mcp/x/all")
    # When true, ask the upstream for a read-only catalog. Default false:
    # destructive tools stay in the catalog so the safety floor has something to
    # hide (and so the firehose baseline is the honest full catalog).
    github_readonly: bool = Field(default=False)


settings = Settings()
