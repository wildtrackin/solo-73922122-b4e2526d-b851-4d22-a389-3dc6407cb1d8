"""Generate the fixed Ed25519 trust anchor (demo key pair).

Idempotent: exits cleanly if the key pair already exists. In the Compose
stack this runs once; both containers mount /keys. The private key exists for
local signing samples only — production deployments would keep it offline.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

KEYS_DIR = Path(os.environ.get("KEYS_DIR", "/keys"))


def main() -> int:
    KEYS_DIR.mkdir(parents=True, exist_ok=True)
    priv_path = KEYS_DIR / "private.pem"
    pub_path = KEYS_DIR / "public.pem"

    if priv_path.exists() and pub_path.exists():
        print(f"keys already exist in {KEYS_DIR}; leaving untouched")
        return 0

    private_key = Ed25519PrivateKey.generate()
    priv_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    pub_path.write_bytes(
        private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    os.chmod(priv_path, 0o600)
    print(f"generated Ed25519 key pair in {KEYS_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
