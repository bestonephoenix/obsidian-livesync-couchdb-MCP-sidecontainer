# Obsidian LiveSync CouchDB — MCP Side Container

A **standalone MCP (Model Context Protocol) server** that runs as a separate side container next to [`oleduc/docker-obsidian-livesync-couchdb`](https://github.com/oleduc/docker-obsidian-livesync-couchdb), giving AI agents (Hermes, Claude Desktop, Cursor, …) full read/write/search access to your Obsidian vault — with end-to-end encryption support.

Built from the combined container [`bestonephoenix/docker-obsidian-livesync-couchdb-mcp`](https://github.com/bestonephoenix/docker-obsidian-livesync-couchdb-mcp), with the MCP server decoupled into its own image so both projects can be maintained, versioned, and released independently.

```
┌─────────────────────────────── Docker Compose ───────────────────────────────┐
│                                                                              │
│  ┌─────────────────────────────┐         ┌────────────────────────────────┐  │
│  │  oleduc/docker-obsidian-    │         │  This image (MCP side car)     │  │
│  │  livesync-couchdb           │         │                                │  │
│  │                             │         │  ┌──────────────────────────┐  │  │
│  │  CouchDB  :5984             │◄────────│  │  MCP StreamableHTTP      │  │  │
│  │  (LiveSync configured)      │  HTTP   │  │  :8000/mcp               │  │  │
│  │                             │  (int.) │  │  (13 vault tools)        │  │  │
│  └────────────┬────────────────┘         │  └──────────┬───────────────┘  │  │
│               │                                    │                     │  │
└───────────────┼────────────────────────────────────┼─────────────────────┘
                │                                    │
           Obsidian clients                     AI agents
           (LiveSync plugin)              (Hermes, Claude Desktop, …)
```

- **CouchDB container** — your LiveSync database, managed by the upstream oleduc project. Obsidian clients sync to it directly.
- **MCP side container** — talks to CouchDB over the Docker network, exposes 13 vault tools over the [Model Context Protocol](https://modelcontextprotocol.io/) via **StreamableHTTP** (single `/mcp` endpoint).
- Each container has **one responsibility**, one entrypoint, its own release cycle.

---

## Quick start

```bash
git clone https://github.com/bestonephoenix/obsidian-livesync-couchdb-MCP-sidecontainer.git
cd obsidian-livesync-couchdb-MCP-sidecontainer

cp docker-compose.example.yml docker-compose.yml
cp .env.example .env
# Edit .env — set COUCHDB_PASSWORD at minimum

docker compose up -d
```

This brings up:

| Service | Container | Port | Purpose |
|---|---|---|---|
| `couchdb` | `oleduc/docker-obsidian-livesync-couchdb:latest` | 5984 | LiveSync database (Obsidian clients connect here) |
| `mcp` | `obsidian-livesync-mcp:latest` (built from this repo) | 8000 | MCP endpoint for AI agents |

> **No Docker Hub image yet?** The compose file builds the MCP container from this repo's `Dockerfile` (`build: .`). Once CI publishing is enabled you can switch to a pinned published image (see [Publishing](#publishing)).

---

## Environment variables

### CouchDB service (passed through to oleduc container)

| Variable | Required | Default | Description |
|---|---|---|---|
| `COUCHDB_USER` | No | `admin` | CouchDB admin username |
| `COUCHDB_PASSWORD` | **Yes** | — | CouchDB admin password |
| `COUCHDB_DATABASE` | No | `obsidian` | Database name for the vault |
| `COUCHDB_PORT` | No | `5984` | Host port for CouchDB |
| `COUCHDB_DATA` | No | `./couchdb-data` | Persistent data volume path |

### MCP service

| Variable | Required | Default | Description |
|---|---|---|---|
| `OBSIDIAN_COUCH_URL` | No | `http://couchdb:5984` | CouchDB endpoint (service name on the compose network) |
| `OBSIDIAN_COUCH_USER` | No | `admin` | CouchDB admin username |
| `OBSIDIAN_COUCH_PASS` | No | — | CouchDB admin password |
| `OBSIDIAN_COUCH_DB` | No | `obsidian` | Database name for the vault |
| `MCP_PORT` | No | `8000` | Host port for MCP endpoint |
| `LIVESYNC_PASSPHRASE` | No | — | LiveSync E2E encryption passphrase (env var fallback) |
| `LIVESYNC_PBKDF2_SALT` | No | — | Override salt (normally auto-discovered from CouchDB) |

The CouchDB credentials are needed because the MCP server reads your vault directly from the database — the same credentials the LiveSync plugin uses.

---

## Encryption support

Two ways to provide the LiveSync E2E encryption passphrase:

### Option A: HTTP header (recommended)

```yaml
# ~/.hermes/config.yaml
mcp_servers:
  obsidian:
    url: "http://your-host:8000/mcp"
    timeout: 120
    headers:
      X-Livesync-Passphrase: "your-passphrase"
```

The passphrase lives in your agent's config — never touches Docker, never reaches the agent's context window.

### Option B: Environment variable

```bash
# In .env:
LIVESYNC_PASSPHRASE=your-passphrase
```

Both methods work simultaneously — header takes priority.

### What happens at startup

1. PBKDF2 salt is **auto-discovered** from `_local/obsidian_livesync_sync_parameters` in CouchDB (with retry while CouchDB boots)
2. Passphrase is retrieved per request (header or env var)
3. Encrypted chunks (`e_: true`) are decrypted transparently via PBKDF2 → HKDF → AES-256-GCM

---

## Connecting agents

### Hermes Agent

```yaml
# ~/.hermes/config.yaml
mcp_servers:
  obsidian:
    url: "http://your-host:8000/mcp"
    timeout: 120
    headers:
      X-Livesync-Passphrase: "your-passphrase"
```

### Claude Desktop

```json
{
  "mcpServers": {
    "obsidian": {
      "command": "npx",
      "args": ["-y", "@anthropic/mcp-client-sse"],
      "env": {
        "MCP_SERVER_URL": "http://your-host:8000/mcp"
      }
    }
  }
}
```

### Available MCP tools

13 tools from [`obsidian-self-mcp`](https://github.com/suhasvemuri/obsidian-self-mcp), with additional fixes:

| Tool | Description |
|---|---|
| `list_notes` | List notes with metadata, filter by folder |
| `read_note` | Read full content of a note |
| `write_note` | Create or update a note |
| `search_notes` | Full-text search across vault |
| `append_note` | Append content to an existing note |
| `delete_note` | Delete a note and its chunks |
| `list_folders` | List folders with note counts |
| `read_frontmatter` | Read YAML frontmatter properties |
| `update_frontmatter` | Set/update frontmatter (JSON input) |
| `list_tags` | List all tags with occurrence counts |
| `search_by_tag` | Find notes containing a tag |
| `get_backlinks` | Find notes linking to a given note |
| `get_outbound_links` | List wikilinks from a note |

### Vault hygiene fixes (monkey-patched into `mcp_server.py`)

- **Deleted files excluded** — checks all three deletion indicators (`doc.deleted`, `doc._deleted`, row-level `deleted`)
- **Ghost files excluded** — orphaned parent documents with empty `children` arrays
- **LiveSync internal docs excluded** — `i:`, `ps:`, `ix:` prefixes and CouchDB system docs
- **Filename deduplication** — when a file exists at root and in a folder (moved-file artifacts), the folder version wins; ties broken by highest `mtime`

---

## Building locally

```bash
docker build -t obsidian-livesync-mcp .
```

The image is intentionally small: `python:3.11-slim` base, no supervisord, no Deno, no CouchDB runtime — just the MCP server and its decryption library.

## Publishing

GitHub Actions (`docker-publish.yml`) builds multi-arch images (`linux/amd64`, `linux/arm64`) on push/PR and publishes to Docker Hub on GitHub releases, tagged with semver + `latest`.

## Migrating from the combined container

If you currently run `bestonephoenix/docker-obsidian-livesync-couchdb-mcp` (single container):

1. Keep your CouchDB data volume — it's compatible as-is
2. Switch to the two-service compose file in this repo
3. Update your agent config if the host port for `/mcp` changed
4. Your passphrase (header or env var) works unchanged

## Credits

| Component | Source |
|---|---|
| CouchDB configuration | [oleduc/docker-obsidian-livesync-couchdb](https://github.com/oleduc/docker-obsidian-livesync-couchdb) |
| MCP tools | [suhasvemuri/obsidian-self-mcp](https://github.com/suhasvemuri/obsidian-self-mcp) |
| Encryption | Python reimplementation of [vrtmrz/octagonal-wheels](https://github.com/vrtmrz/octagonal-wheels) HKDF scheme |

## License

MIT — see [LICENSE](LICENSE).
