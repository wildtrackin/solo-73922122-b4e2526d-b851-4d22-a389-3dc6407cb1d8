"""End-to-end demo client, designed to run inside the Compose network.

Run after the stack is up:

    docker compose run --rm demo

It walks through every required behavior:
  1. valid signed installation            -> SUCCESS
  2. identical package (same ver+digest)  -> original successful result
  3. same version, different digest       -> REJECTED
  4. activation with a lost receipt       -> VERIFYING, then reconciled
  5. new upload while verification open   -> HTTP 409 (status still works)
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time

import httpx

from app.crypto_utils import load_private_key, sign_manifest

API = os.environ.get("API_URL", "http://api:8000")
DEVICE = os.environ.get("DEVICE_URL", "http://device:8001")
KEYS_DIR = os.environ.get("KEYS_DIR", "/keys")
MODEL = os.environ.get("FW_DEVICE_MODEL", "gateway-gx100")


def log(msg: str) -> None:
    print(f"\n\033[1;36m== {msg} ==\033[0m", flush=True)


def package(version: int, *, size: int = 4096) -> dict:
    firmware = f"GATEWAY-FIRMWARE v{version}\n".encode() + b"payload-block\n" * (
        size // 13
    )
    digest = hashlib.sha256(firmware).hexdigest()
    manifest = {"model": MODEL, "version": version, "sha256": digest}
    manifest_bytes = json.dumps(
        manifest, sort_keys=True, separators=(",", ":")
    ).encode()
    signature = sign_manifest(load_private_key(f"{KEYS_DIR}/private.pem"), manifest_bytes)
    return {
        "firmware": firmware,
        "manifest": manifest_bytes,
        "signature": signature,
        "digest": digest,
    }


def submit(client: httpx.Client, pkg: dict) -> httpx.Response:
    return client.post(
        f"{API}/installations",
        files={
            "firmware": ("firmware.bin", pkg["firmware"], "application/octet-stream"),
            "manifest": ("manifest.json", pkg["manifest"], "application/json"),
        },
        data={"signature": pkg["signature"]},
    )


def wait_terminal(client: httpx.Client, task_id: int) -> dict:
    for _ in range(40):
        view = client.get(f"{API}/installations/{task_id}").json()
        print(f"  task {task_id}: {view['state']}")
        if view["state"] in ("SUCCESS", "REJECTED"):
            return view
        time.sleep(0.5)
    raise RuntimeError(f"task {task_id} never reached a terminal state")


def main() -> int:
    with httpx.Client(timeout=15.0) as client:
        log("device before demo")
        print(json.dumps(client.get(f"{DEVICE}/status").json(), indent=2))

        log("1) signed v2 installation")
        r = submit(client, package(2))
        assert r.status_code == 202, r.text
        t2 = r.json()["task_id"]
        wait_terminal(client, t2)

        log("2) identical package -> original result, no firmware re-sent")
        r = submit(client, package(2))
        wait_terminal(client, r.json()["task_id"])

        log("3) same version v2, different digest -> REJECTED")
        r = submit(client, package(2, size=8192))
        view = wait_terminal(client, r.json()["task_id"])
        assert view["state"] == "REJECTED"
        print("  reason:", view["reject_reason"])

        log("4) arm receipt loss, install v3 -> VERIFYING -> reconcile SUCCESS")
        client.post(f"{DEVICE}/admin/lose-next-receipt", json={"enabled": True})
        r = submit(client, package(3))
        t3 = r.json()["task_id"]
        for _ in range(20):
            view = client.get(f"{API}/installations/{t3}").json()
            if view["state"] == "VERIFYING":
                break
            time.sleep(0.25)
        print("  observed:", view["state"], "-", view["device_response"])
        print("  device actually installed:", client.get(f"{DEVICE}/status").json()["version"])
        # Background loop reconciles; nudge it immediately for the demo.
        print(json.dumps(client.post(f"{API}/installations/reconcile").json(), indent=2))
        wait_terminal(client, t3)

        log("5) open verification blocks new uploads with 409")
        client.post(f"{DEVICE}/admin/lose-next-receipt", json={"enabled": True})
        r = submit(client, package(4))
        t4 = r.json()["task_id"]
        for _ in range(20):
            if client.get(f"{API}/installations/{t4}").json()["state"] == "VERIFYING":
                break
            time.sleep(0.25)
        conflict = submit(client, package(5))
        print("  HTTP", conflict.status_code, conflict.json()["detail"]["error"])
        assert conflict.status_code == 409
        print("  status query still works:", client.get(f"{API}/health").json())
        client.post(f"{API}/installations/reconcile")
        wait_terminal(client, t4)

        log("final device state + task list")
        print(json.dumps(client.get(f"{DEVICE}/status").json(), indent=2))
        for t in client.get(f"{API}/installations").json():
            print(f"  task {t['id']}: {t['state']:9s} v{t['version']} {t['reject_reason'] or ''}")

    print(
        "\nDemo complete. Interactive docs: "
        f"{API.replace('api:8000', 'localhost:8000')}/docs"
    )
    print("Restart recovery test: docker compose restart api")
    return 0


if __name__ == "__main__":
    sys.exit(main())
