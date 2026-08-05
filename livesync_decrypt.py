"""
LiveSync chunk decryption for obsidian-self-mcp.

Implements the HKDF-based AES-256-GCM decryption used by Obsidian LiveSync's
chunk encryption (octagonal-wheels package, hkdf.ts).

Format (%= prefix):
  %= + base64( IV(12 bytes) | HKDF_salt(32 bytes) | AES-GCM-encrypted data )

Key derivation:
  1. PBKDF2(passphrase, pbkdf2_salt, 310000 iter, SHA-256) -> master_key (32 bytes)
  2. HKDF(master_key, hkdf_salt, SHA-256) -> chunk_key (32 bytes)
  3. AES-256-GCM(chunk_key, IV) -> decrypt
"""

from __future__ import annotations

import base64
import logging

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

logger = logging.getLogger(__name__)

IV_LENGTH = 12
HKDF_SALT_LENGTH = 32
PBKDF2_ITERATIONS = 310_000
HKDF_ENCRYPTED_PREFIX = "%="
HKDF_SALTED_PREFIX = "%$"


def decrypt_chunk(encrypted_base64: str, passphrase: str, pbkdf2_salt_hex: str) -> str:
    """Decrypt a LiveSync HKDF-encrypted chunk.

    Args:
        encrypted_base64: The chunk `data` field from CouchDB (with %= prefix).
        passphrase: The LiveSync encryption passphrase.
        pbkdf2_salt_hex: The PBKDF2 salt as a 64-character hex string (32 bytes).

    Returns:
        Decrypted plaintext string.
    """
    # Determine format
    data = encrypted_base64
    if data.startswith(HKDF_ENCRYPTED_PREFIX):
        data = data[2:]
        pbkdf2_salt = bytes.fromhex(pbkdf2_salt_hex)
        return _decrypt_hkdf(data, passphrase, pbkdf2_salt)
    elif data.startswith(HKDF_SALTED_PREFIX):
        # Embedded salt format — not yet implemented
        raise NotImplementedError("Embedded PBKDF2 salt format (%$) not yet supported")
    else:
        # Plaintext (no encryption prefix)
        return data


def _decrypt_hkdf(base64_data: str, passphrase: str, pbkdf2_salt: bytes) -> str:
    """Decrypt a %=-prefixed HKDF-encrypted chunk."""
    raw = base64.b64decode(base64_data)

    if len(raw) < IV_LENGTH + HKDF_SALT_LENGTH:
        raise ValueError(f"Data too short: {len(raw)} bytes")

    iv = raw[:IV_LENGTH]
    hkdf_salt = raw[IV_LENGTH : IV_LENGTH + HKDF_SALT_LENGTH]
    encrypted = raw[IV_LENGTH + HKDF_SALT_LENGTH :]

    passphrase_bytes = passphrase.encode("utf-8")

    # Step 1: PBKDF2 -> master key (32 bytes / 256 bits)
    kdf = PBKDF2HMAC(
        algorithm=SHA256(),
        length=32,
        salt=pbkdf2_salt,
        iterations=PBKDF2_ITERATIONS,
    )
    master_key = kdf.derive(passphrase_bytes)

    # Step 2: HKDF -> chunk key
    hkdf = HKDF(algorithm=SHA256(), length=32, salt=hkdf_salt, info=b"")
    chunk_key = hkdf.derive(master_key)

    # Step 3: AES-256-GCM decrypt
    aesgcm = AESGCM(chunk_key)
    decrypted = aesgcm.decrypt(iv, encrypted, None)

    return decrypted.decode("utf-8")
