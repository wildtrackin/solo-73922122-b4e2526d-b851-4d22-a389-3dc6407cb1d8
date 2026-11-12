"""Local signing sample: build firmware, manifest and Ed25519 signature.

Usage:
    python -m signing.sample_sign \
        --keys-dir keys --out build/signing-sample \
        --model gateway-gx100 --version 7

Produces three files:
    firmware.bin   deterministic demo firmware payload
    manifest.json  {"model", "version", "sha256"} (canonical, compact JSON)
    signature.b64  base64 Ed25519 signature over the exact manifest bytes

These map directly onto the /installations multipart fields
(firmware, manifest, signature).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path

from app.crypto_utils import load_private_key, sign_manifest


def build_firmware(version: int, size: int) -> bytes:
    # Deterministic payload: a header plus a repeated body, so the digest is
    # reproducible and the file stays small for demos.
    header = f"GATEWAY-FIRMWARE v{version}\n".encode("ascii")
    if size < len(header):
        return header[:size]
    return header + b"payload-block\n" * ((size - len(header)) // 13 + 1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keys-dir", default="keys")
    parser.add_argument("--out", default="build/signing-sample")
    parser.add_argument("--model", default="gateway-gx100")
    parser.add_argument("--version", type=int, default=2)
    parser.add_argument("--size", type=int, default=4096)
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    firmware = build_firmware(args.version, args.size)
    digest = hashlib.sha256(firmware).hexdigest()
    manifest = {"model": args.model, "version": args.version, "sha256": digest}
    # Compact, sorted JSON so the signed bytes are canonical.
    manifest_bytes = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )

    private_key = load_private_key(Path(args.keys_dir) / "private.pem")
    signature = sign_manifest(private_key, manifest_bytes)

    (out_dir / "firmware.bin").write_bytes(firmware)
    (out_dir / "manifest.json").write_bytes(manifest_bytes)
    (out_dir / "signature.b64").write_text(signature + "\n")

    print(f"firmware   : {out_dir / 'firmware.bin'} ({len(firmware)} bytes)")
    print(f"manifest   : {manifest_bytes.decode()}")
    print(f"sha256     : {digest}")
    print(f"signature  : {signature[:48]}...")
    print(
        "\nupload with:\n"
        f"  curl -X POST http://localhost:8000/installations \\\n"
        f"    -F firmware=@{out_dir / 'firmware.bin'} \\\n"
        f"    -F manifest=@{out_dir / 'manifest.json'};type=application/json \\\n"
        f"    -F signature={signature}"
    )


if __name__ == "__main__":
    main()
