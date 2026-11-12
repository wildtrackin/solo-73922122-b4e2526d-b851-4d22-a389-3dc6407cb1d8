"""Verify a signed sample locally (trust anchor check without the API)."""
from __future__ import annotations

import argparse
from pathlib import Path

from app.crypto_utils import load_public_key, verify_manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keys-dir", default="keys")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--signature", required=True)
    args = parser.parse_args()

    public_key = load_public_key(Path(args.keys_dir) / "public.pem")
    manifest_bytes = Path(args.manifest).read_bytes()
    signature = Path(args.signature).read_text().strip()
    ok = verify_manifest(public_key, manifest_bytes, signature)
    print("signature VALID" if ok else "signature INVALID")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
