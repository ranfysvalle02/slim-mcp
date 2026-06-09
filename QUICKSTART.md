# Quickstart

The fastest way to see the Smart MCP Gateway running and prove it works.

## Prerequisite: a GitHub token

The gateway proxies the **real, official GitHub MCP server**, so it needs a
GitHub Personal Access Token. Put one in `.env` first (a fine-grained, read-only
token is plenty for the demo —
[create one here](https://github.com/settings/personal-access-tokens/new)):

```bash
echo 'GITHUB_PAT=ghp_your_token_here' > .env
```

Without it the gateway will refuse to start (by design — this is a real
integration, not a mock).

## Fastest path (Docker, one command)

Requires Docker + Docker Compose. This builds the gateway, starts MongoDB Atlas
Local, and — in the background — embeds the route-by-meaning catalog so the live
demo is ready the moment you open the page:

```bash
docker compose up        # build + start + seed, then open http://localhost:8000/
```

That's it — the gateway is now at **http://localhost:8000**. (Seeding is
best-effort: if Mongo or the local embedder is missing, the stack still comes up
and the gateway simply serves the full catalog — never worse than a plain proxy.)

Open **http://localhost:8000/** in a browser for the live dashboard: the full
GitHub MCP firehose (~93 tools, ~21,830 tokens) vs. the handful a single task
actually needs (~8 tools, ~2,480 tokens) — **byte-for-byte identical tools, just
fewer of them**. Switch tasks and watch the token bill shrink ~8.8×; that's the
whole pitch in one screen.

In a second terminal, confirm it's healthy:

```bash
curl -s localhost:8000/health
# {"status":"healthy","environment":"development"}
```

Stop everything (also wipes Mongo volumes for a clean start next time):

```bash
docker compose down -v
```

### Optional: re-seed the route-by-meaning catalog by hand

The catalog seeds automatically on startup. To (re)build it manually — for
example after pulling a fresh embedder — you need a local embedding model:

```bash
ollama pull nomic-embed-text
MCPX_MONGO_URI="mongodb://localhost:27019/?directConnection=true" \
    PYTHONPATH=. python scripts/seed_catalog.py
```

## No Docker? Run it locally

Requires Python ≥ 3.11.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Optional: start only Atlas Local (for telemetry + route-by-meaning)
docker compose up -d mongo

# Run the gateway on :8000 (point it at Atlas Local if you started it)
MCPX_MONGO_URI="mongodb://localhost:27019/?directConnection=true" \
    PYTHONPATH=. uvicorn app.main:combined_app --host 0.0.0.0 --port 8000 --reload
```

Persistence is optional: skip the `mongo` step and the gateway still runs fully
standalone — every Mongo writer degrades to a no-op and semantic retrieval falls
back to serving the full catalog. The live on-device demo needs no database.

## Point an MCP client at it

The MCP Streamable HTTP endpoint is:

```
http://localhost:8000/mcp/
```

The gateway's **entire interface is one header**: `x-mcp-query`. Send a free-form
task and `tools/list` comes back as the handful of tools that match by *meaning*
(via MongoDB `$vectorSearch`), descriptions intact:

```
x-mcp-query: get the failing job logs
```

With **no header** you get the full (safety-floored) catalog — the gateway is
never worse than an honest proxy. The only thing it ever changes is *how many*
tools it returns, never their text.

## What next?

- [`README.md`](./README.md) — full architecture, endpoints, and config.
- [`blog.md`](./blog.md) — the motivation behind the lean-gateway design.
- [`architecture.md`](./architecture.md) — how the request pipeline is wired.
