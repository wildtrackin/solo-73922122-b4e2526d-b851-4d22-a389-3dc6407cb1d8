"""Ed25519 signature handling over raw manifest bytes."""
from __future__ import annotations

import base64

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


def load_public_key(path: str | Path) -> Ed25519PublicKey:
    with open(path, "rb") as fh:
        data = fh.read()
    key = serialization.load_pem_public_key(data)
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("trusted key is not an Ed25519 public key")
    return key


def load_private_key(path: str | Path) -> Ed25519PrivateKey:
    with open(path, "rb") as fh:
        data = fh.read()
    key = serialization.load_pem_private_key(data, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("signing key is not an Ed25519 private key")
    return key


def sign_manifest(private_key: Ed25519PrivateKey, manifest_bytes: bytes) -> str:
    """Return base64-encoded Ed25519 signature over the exact manifest bytes."""
    return base64.b64encode(private_key.sign(manifest_bytes)).decode("ascii")


def verify_manifest(
    public_key: Ed25519PublicKey, manifest_bytes: bytes, signature_b64: str
) -> bool:
    try:
        signature = base64.b64decode(signature_b64, validate=True)
    except (ValueError, base64.binascii.Error):
        return False
    try:
        public_key.verify(signature, manifest_bytes)
    except InvalidSignature:
        return False
    return True
