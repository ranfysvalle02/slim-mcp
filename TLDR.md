# TL;DR — Smart MCP Gateway on MongoDB

**The token tax people blame on MCP is a server/host *implementation* problem, not a *protocol* problem.** Nothing in MCP says a server must dump its entire tool catalog into the model's context on every turn — that's a choice. A *smart gateway* (embed the catalog once, then retrieve only the tools a task needs by meaning) makes a different choice, and MongoDB is the natural home for it: the catalog, the search engine, and the telemetry are all just documents in one place.

> Full story: [`blog.md`](./blog.md). How to run it: [`README.md`](./README.md).

---

## In one paragraph

Naive MCP servers hand the model a 500-page binder — every tool name, description, and parameter schema — before it does anything useful, every turn. We point the agent straight at the **real, official GitHub MCP server** (93 tools): **~21,830 tokens/turn**. The same task — you just say *"review this pull request"* — through a gateway that **retrieves the relevant tools by meaning** costs **~2,480 tokens — ~8.8x fewer**, while staying a completely standard MCP server. The crucial part: those 8 tools are **byte-for-byte identical** to the firehose's — same names, same descriptions, same schemas. The *only* thing the gateway changes is **how many** tools cross the wire, so the win is 100% attributable to retrieval. MongoDB is what makes that pattern work — embed once, retrieve by meaning with `$vectorSearch`, govern in one place.

## The numbers (measured live against the official GitHub MCP server)

| Approach | Tokens/turn | Tools exposed | Descriptions | Needs a shell? |
| --- | ---: | ---: | --- | --- |
| Raw MCP (the binder) | ~21,830 | 93 | full | no |
| Smart gateway (`x-mcp-query: "review this pull request"`) | ~2,480 | 8 | full (identical) | no |
| Smart gateway (no query, full catalog) | ~20,316 | 89 | full | no |
| `gh` CLI (shell) | ~50 | 1 | — | yes |

The live dashboard measures the **literal** official server (all 93 tools, including the destructive ones the gateway's safety floor hides), counted with `cl100k_base` (tiktoken). The smart row carries the *same 8 tools' full descriptions* — nothing trimmed — so its ~8.8x edge is pure retrieval. (The no-query row only drops the 4 destructive tools the safety floor hides; the gateway's real win is *retrieval*, not the fallback path.) On the live on-device A/B the model's own prompt-token count falls by the same order of magnitude on the PR task, and both passes pick `pull_request_read` correctly. Industry corroboration: the real GitHub MCP server is ~55k tokens for 93 tools vs. ~638 for `gh --help` (86:1, Willison); a 7-server setup tops 67k tokens before reading any code (Layered).

## The decision boundary (read this *before* adding any MCP server)

It is not a leaderboard. One question settles it: **does the agent have a shell?**

- **Shell + CLI-shaped task → use the CLI.** `gh` is ~50 tokens, no discovery tax, no extra hop. Bolting on MCP is strictly worse. Do nothing.
- **No shell** (web chat, sandboxed/hosted agent, no `gh` auth, non-CLI API) **→ MCP is forced.** Now the only real contest is **raw MCP vs. smart gateway**, and the gateway wins by ~8.8x on the PR task every turn. The CLI was never the gateway's rival; raw MCP is, and only here.

## Why MongoDB

A tool gateway needs a catalog, semantic search, and analytics. That's normally 3–4 systems (Postgres + a vector DB + a warehouse) held together by sync jobs that drift. MongoDB does all of it against **one copy of the data** — and in this repo it's *running*, not roadmap:

- **Catalog** — the tool *is* a document: name, description, and its local embedding side by side.
- **Atlas Vector Search** — route by *meaning* (`"the CI workflow runs are failing"` → the GitHub Actions tools, with zero shared keywords). One `$vectorSearch` over the embedded catalog, live on the MCP hot path via the `X-MCP-Query` header. (Want hybrid? MongoDB does keyword `$search` on the same documents — we kept the demo to one clean mechanism; see the blog appendix.)
- **Aggregation pipeline** — live token telemetry and audit analytics, no ETL.

Embeddings are generated **locally** (Ollama `nomic-embed-text`, 768-dim — no keys), and persistence uses **PyMongo's native async API** (`AsyncMongoClient`), not the deprecated Motor driver.

## The critics agree — and they're pointing at implementations

- **Jeremiah Lowin** (creator of FastMCP): *"Stop converting your REST APIs. Start curating them."* — retrieving only the task's tools, by meaning, is exactly that curation.
- **Brandon Dennis** ("MCP Servers Are the Wrong Abstraction"): the identical shell vs. no-shell boundary; *"the problem isn't MCP existing"* — it's wrapping tools the agent can already call.
- **Denis Urayev** ("MCP Might Quietly Fade Away"): MCP must *"evolve towards exploration, not just declaration"* — a gateway that retrieves tools by meaning **is** that evolution.
- **Layered Systems**: the industry's emerging fix is *gateways that collapse catalogs to "search + execute"* and host-side *semantic tool search* — i.e. this gateway.

## Also true (beyond the token bill)

- **One idea, on purpose:** this demo makes a single airtight claim — *fewer tools, identical text* — so it does exactly one thing: semantic retrieval on the *input* side. Token bloat has a second face (fat tool *results* on every `tools/call`), and there are further input-side levers (description pruning) too. Those stack cleanly on top but would muddy the one clean claim, so they're kept *out of the headline* — see the blog's "The other half of the bill", Appendix E, and the **further hardening** levers in Appendix F.
- **Accuracy:** fewer tools in context means fewer wrong-tool picks and hallucinated args. Cheaper *and* more correct.
- **No hardcoded policy:** there's no routing table to maintain — you don't pre-declare anything, you describe the work in plain words and the embeddings do the rest.
- **Security is *more* visible, not less:** every call is a document in an `invocation_audit` ledger, and the safety floor hides destructive tools in both directions — a caller can't invoke `delete_file` even if it knows the name.

## Run it

```bash
echo 'GITHUB_PAT=ghp_your_token' > .env   # the gateway proxies the real GitHub MCP server
docker compose up                          # build + start + seed everything, on :8000

# route by meaning (semantic + keyword over the catalog) — seeds automatically on
# startup; to (re)build by hand:
ollama pull nomic-embed-text               # local embedder, one-time
PYTHONPATH=. python scripts/seed_catalog.py
```

Open `http://localhost:8000/` for the live token-savings dashboard.
