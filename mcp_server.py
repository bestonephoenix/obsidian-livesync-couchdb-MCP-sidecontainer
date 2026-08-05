#!/usr/bin/env python3
"""
MCP StreamableHTTP server for Obsidian vault access via CouchDB LiveSync.

Runs as a STANDALONE side container next to a CouchDB LiveSync database
(oleduc/docker-obsidian-livesync-couchdb). Connects over the Docker network.

Passphrase via X-Livesync-Passphrase HTTP header (or LIVESYNC_PASSPHRASE env var fallback).
PBKDF2 salt auto-discovered from CouchDB at startup with retry.
No passphrase ever touches the agent.

Agents connect at: http://<host>:8000/mcp (StreamableHTTP)
"""

import asyncio
import contextvars
import logging
import os
import sys
from typing import Optional

# ── mcp SDK compatibility: FastMCP → MCPServer (mcp SDK 2.0) ───────
# obsidian-self-mcp imports `from mcp.server.fastmcp import FastMCP`.
# mcp SDK 2.0 renamed FastMCP → MCPServer and removed mcp.server.fastmcp.
# Until upstream fixes the import, alias MCPServer as FastMCP — the
# decorator API is unchanged, so all 13 tool registrations keep working.
# On mcp SDK 1.x the real module exists and nothing is shimmed.
try:
    import mcp.server.fastmcp  # noqa: F401  (present on mcp SDK 1.x)
except ModuleNotFoundError:
    import types
    from mcp.server import MCPServer

    _fastmcp_shim = types.ModuleType("mcp.server.fastmcp")
    _fastmcp_shim.FastMCP = MCPServer
    sys.modules["mcp.server.fastmcp"] = _fastmcp_shim

from obsidian_self_mcp.server import mcp, _get_client
from obsidian_self_mcp.client import ObsidianVaultClient

# ── Per-request passphrase (ContextVar, set by middleware) ─────────

passphrase_ctx = contextvars.ContextVar("livesync_passphrase", default="")

# ── Global salt (discovered once at startup) ──────────────────────

_pbkdf2_salt: Optional[str] = None


def _get_passphrase() -> str:
    """Return passphrase from request header, falling back to env var."""
    return passphrase_ctx.get() or os.environ.get("LIVESYNC_PASSPHRASE", "")


# ── Middleware: extract passphrase from HTTP header ────────────────
# IMPORTANT: Raw ASGI middleware, NOT BaseHTTPMiddleware.
# BaseHTTPMiddleware wraps call_next() which reads the full response body,
# killing the long-lived SSE GET stream used by StreamableHTTP session
# establishment (symptom: 500 "Session terminated"). Raw ASGI passes
# scope/receive/send through untouched.

def _header_middleware(app):
    """Extract X-Livesync-Passphrase header into ContextVar per request."""

    async def wrapper(scope, receive, send):
        if scope["type"] != "http":
            await app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        passphrase = headers.get(b"x-livesync-passphrase", b"").decode() or ""
        token = passphrase_ctx.set(passphrase)
        try:
            await app(scope, receive, send)
        finally:
            passphrase_ctx.reset(token)

    return wrapper


# ── Auto-discover PBKDF2 salt from CouchDB ────────────────────────

async def _discover_pbkdf2_salt() -> Optional[str]:
    """Find PBKDF2 salt from CouchDB sync params document (base64-encoded)."""
    import base64

    vault_client = _get_client()
    http = await vault_client._get_client()
    resp = await http.get("_local/obsidian_livesync_sync_parameters")
    if resp.status_code != 200:
        logging.warning("Sync params doc not found (status %s)", resp.status_code)
        return None
    doc = resp.json()
    salt_b64 = doc.get("pbkdf2salt", "")
    if not salt_b64:
        logging.warning("pbkdf2salt field not found in sync params")
        return None
    salt_bytes = base64.b64decode(salt_b64)
    return salt_bytes.hex()


# ── Patch _fetch_chunks for decryption ─────────────────────────────


async def _patched_fetch_chunks(self, chunk_ids):
    httpx_client = await self._get_client()
    resp = await httpx_client.post(
        "/_all_docs",
        json={"keys": chunk_ids},
        params={"include_docs": "true"},
    )
    resp.raise_for_status()
    result = {}
    passphrase = _get_passphrase()
    for row in resp.json().get("rows", []):
        doc = row.get("doc")
        if doc and "data" in doc:
            data = doc["data"]
            if doc.get("e_"):
                if passphrase and _pbkdf2_salt:
                    from livesync_decrypt import decrypt_chunk
                    try:
                        data = decrypt_chunk(data, passphrase, _pbkdf2_salt)
                    except Exception as e:
                        data = f"[DECRYPT FAILED: {e}]"
                        logging.warning("Decrypt failed for chunk %s: %s", row["id"], e)
                else:
                    data = (
                        "[ENCRYPTED — set X-Livesync-Passphrase header"
                        " or LIVESYNC_PASSPHRASE env var]"
                    )
            result[row["id"]] = data
    return result


ObsidianVaultClient._fetch_chunks = _patched_fetch_chunks


# ── Patch _get_all_file_docs for deduplication ─────────────────────

# LiveSync internal metadata prefixes (from livesync-commonlib constants)
_LIVESYNC_INTERNAL_PREFIXES = ("i:", "ps:", "ix:", "_design/", "_local/")


async def _patched_get_all_file_docs(self):
    """Fetch all file docs (skip chunks, design docs, index docs).

    Patched to:
    1. Exclude LiveSync internal metadata prefixes (i:, ps:, ix:)
    2. Exclude CouchDB system docs (_design/, _local/)
    3. Exclude deleted documents (all three deletion indicators)
    4. Exclude ghost files (parent docs with empty children arrays)
    5. Deduplicate by base filename — folder version wins, mtime as tiebreaker
    """
    httpx_client = await self._get_client()

    all_rows = []

    # Range 1: docs before "h:" (excludes all chunk IDs)
    resp = await httpx_client.get(
        "/_all_docs",
        params={
            "include_docs": "true",
            "endkey": '"h:"',
            "inclusive_end": "false",
        },
    )
    resp.raise_for_status()
    all_rows.extend(resp.json().get("rows", []))

    # Range 2: docs after "h:~" (after all possible chunk IDs)
    resp = await httpx_client.get(
        "/_all_docs",
        params={
            "include_docs": "true",
            "startkey": '"h:~"',
        },
    )
    resp.raise_for_status()
    all_rows.extend(resp.json().get("rows", []))

    # Process with deduplication by base filename
    seen: dict[str, dict] = {}
    skipped_deleted = 0
    skipped_internal = 0
    skipped_ghost = 0
    skipped_duplicate = 0

    for row in all_rows:
        doc = row.get("doc")
        if not doc:
            continue

        # Skip deleted documents (LiveSync `deleted` field, CouchDB `_deleted`
        # tombstone, and CouchDB row-level deletion marker)
        if doc.get("deleted") or doc.get("_deleted") or row.get("value", {}).get("deleted"):
            skipped_deleted += 1
            continue

        # Skip non-file docs (must have type and children)
        if doc.get("type") not in ("plain", "newnote") or "children" not in doc:
            continue

        # Skip ghost files — parent docs with empty children arrays (zero chunks)
        if not doc.get("children"):
            skipped_ghost += 1
            continue

        doc_id = doc.get("_id", "")

        # Skip LiveSync internal metadata and CouchDB system docs
        if any(doc_id.startswith(p) for p in _LIVESYNC_INTERNAL_PREFIXES):
            skipped_internal += 1
            continue

        # Deduplicate by base filename (last path segment)
        path = doc.get("path", doc_id)
        filename = path.rsplit("/", 1)[-1]
        existing = seen.get(filename)
        if existing is None:
            seen[filename] = doc
            continue

        # Same filename already seen — decide which version to keep
        existing_path = existing.get("path", existing.get("_id", ""))
        existing_in_folder = "/" in existing_path
        new_in_folder = "/" in path

        # 1. Folder version beats root version (handles moved files)
        if new_in_folder and not existing_in_folder:
            skipped_duplicate += 1
            seen[filename] = doc
        # 2. Same category (both folder or both root) — highest mtime wins
        elif new_in_folder == existing_in_folder and doc.get("mtime", 0) > existing.get("mtime", 0):
            skipped_duplicate += 1
            seen[filename] = doc
        # 3. Otherwise keep the existing version
        else:
            skipped_duplicate += 1

    if skipped_deleted or skipped_internal or skipped_ghost or skipped_duplicate:
        logging.info(
            "_get_all_file_docs: filtered %d deleted, %d internal, %d ghost, "
            "%d duplicate filename(s) — %d unique file docs remain",
            skipped_deleted,
            skipped_internal,
            skipped_ghost,
            skipped_duplicate,
            len(seen),
        )

    return list(seen.values())


ObsidianVaultClient._get_all_file_docs = _patched_get_all_file_docs


# ── Startup (async — handles retry for CouchDB readiness) ─────────

async def _startup():
    """Discover PBKDF2 salt, build app with passphrase middleware."""
    global _pbkdf2_salt

    # Try explicit salt first, then auto-discover from CouchDB
    salt = os.environ.get("LIVESYNC_PBKDF2_SALT", "")
    if not salt:
        print("Discovering PBKDF2 salt from CouchDB...", file=sys.stderr, flush=True)
        for attempt in range(1, 8):
            await asyncio.sleep(3)
            try:
                salt = await _discover_pbkdf2_salt()
                if salt:
                    break
            except Exception as e:
                print(f"  Attempt {attempt}/7: {e}", file=sys.stderr, flush=True)

    if salt:
        _pbkdf2_salt = salt
        print("Salt discovered. Vault ready.", file=sys.stderr, flush=True)
    else:
        print(
            "WARNING: Could not discover PBKDF2 salt. "
            "Set LIVESYNC_PBKDF2_SALT if encryption is used.",
            file=sys.stderr,
            flush=True,
        )

    # DNS rebinding: mcp SDK 2.0 auto-enables protection for localhost hosts,
    # which BLOCKS container-name access (Host: mcp:8000 → HTTP 421). Pass
    # transport_security explicitly to disable it — safe inside Docker's
    # network namespace (same rationale as the old `settings` line on 1.x,
    # which no longer exists in 2.0).
    from mcp.server.transport_security import TransportSecuritySettings

    app = mcp.streamable_http_app(
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
        ),
    )
    app = _header_middleware(app)
    return app


if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_PORT", "8000"))

    app = asyncio.run(_startup())

    print(
        f"Obsidian MCP server starting on {host}:{port}/mcp"
        " (StreamableHTTP + header auth)",
        file=sys.stderr,
    )
    uvicorn.run(app, host=host, port=port, proxy_headers=False, log_level="info")
