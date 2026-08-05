FROM python:3.11-slim

# ── Metadata ──────────────────────────────────────────────────────────
LABEL org.opencontainers.image.title="Obsidian LiveSync CouchDB — MCP Side Container"
LABEL org.opencontainers.image.description="Standalone MCP server providing AI agent vault access to an Obsidian LiveSync CouchDB database — pairs with oleduc/docker-obsidian-livesync-couchdb in a joint docker-compose setup"
LABEL org.opencontainers.image.url="https://github.com/bestonephoenix/obsidian-livesync-couchdb-MCP-sidecontainer"
LABEL org.opencontainers.image.source="https://github.com/bestonephoenix/obsidian-livesync-couchdb-MCP-sidecontainer"
LABEL org.opencontainers.image.licenses="MIT"
LABEL org.opencontainers.image.authors="bestonephoenix"

WORKDIR /app

# ── git — required to pip-install obsidian-self-mcp from GitHub ─────
# python:3.11-slim has no git; pip install git+https:// fails without it.
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# ── MCP server (obsidian-self-mcp by @suhasvemuri) ────────────────────
# Not on PyPI — install directly from GitHub, pinned to a commit SHA
# (ea77d3b = 2026-02-28) for reproducible builds.
# https://github.com/suhasvemuri/obsidian-self-mcp
# NOTE: mcp<2.0.0 pin is REQUIRED — MCP SDK 2.0 moved FastMCP out of
# mcp.server.fastmcp, which breaks obsidian-self-mcp's imports.
RUN pip install --no-cache-dir \
    "git+https://github.com/suhasvemuri/obsidian-self-mcp.git@ea77d3b5746b55a19c90d4d6e24e3c694fdfc45e" \
    "mcp[cli]>=1.0.0,<2.0.0" \
    cryptography \
    uvicorn

# ── Our files ──────────────────────────────────────────────────────────
COPY livesync_decrypt.py /app/livesync_decrypt.py
COPY mcp_server.py /app/mcp_server.py

EXPOSE 8000

# Environment defaults — override in docker-compose or .env
ENV MCP_HOST=0.0.0.0 \
    MCP_PORT=8000 \
    OBSIDIAN_COUCH_URL=http://couchdb:5984

# Single process — no supervisord needed
CMD ["python3", "/app/mcp_server.py"]
