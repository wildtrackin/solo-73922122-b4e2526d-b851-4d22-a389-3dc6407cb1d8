"""Validation-layer tests: nothing invalid may reach the device."""
from __future__ import annotations

import base64
import hashlib
import json

import httpx
import pytest

from app.service import ValidationError
from tests.conftest import make_package


async def _post(client: httpx.AsyncClient, firmware, manifest_bytes, signature):
    return await client.post(
        "/installations",
        files={
            "firmware": ("firmware.bin", firmware, "application/octet-stream"),
            "manifest": ("manifest.json", manifest_bytes, "application/json"),
        },
        data={"signature": signature},
    )


@pytest.mark.asyncio
async def test_valid_installation_succeeds(started_app, key_pair):
    client, device = started_app
    firmware, manifest_bytes, sig, _sha = make_package(key_pair[0], version=2)
    resp = await _post(client, firmware, manifest_bytes, sig)
    assert resp.status_code == 202, resp.text
    task_id = resp.json()["task_id"]

    # Background dispatch runs; wait for the terminal state.
    for _ in range(50):
        view = (await client.get(f"/installations/{task_id}")).json()
        if view["state"] in ("SUCCESS", "REJECTED"):
            break
        await _spin()
    assert view["state"] == "SUCCESS"
    assert device.version == 2
    assert device.install_calls == 1


@pytest.mark.asyncio
async def test_bad_signature_rejected(started_app, key_pair):
    client, device = started_app
    firmware, manifest_bytes, _sig, _sha = make_package(key_pair[0], version=2)
    resp = await _post(client, firmware, manifest_bytes, base64.b64encode(b"x" * 64).decode())
    assert resp.status_code == 400
    assert "signature" in resp.json()["detail"]
    assert device.install_calls == 0


@pytest.mark.asyncio
async def test_tampered_manifest_rejected(started_app, key_pair):
    client, device = started_app
    firmware, manifest_bytes, sig, _sha = make_package(key_pair[0], version=2)
    tampered = manifest_bytes.replace(b'"version":2', b'"version":3')
    resp = await _post(client, firmware, tampered, sig)
    assert resp.status_code == 400
    assert device.install_calls == 0


@pytest.mark.asyncio
async def test_wrong_model_rejected(started_app, key_pair):
    client, device = started_app
    firmware, manifest_bytes, sig, _sha = make_package(
        key_pair[0], version=2, model="other-model-x"
    )
    resp = await _post(client, firmware, manifest_bytes, sig)
    assert resp.status_code == 400
    assert "model" in resp.json()["detail"]
    assert device.install_calls == 0


@pytest.mark.asyncio
async def test_digest_mismatch_rejected(started_app, key_pair):
    client, device = started_app
    firmware, manifest_bytes, sig, _sha = make_package(
        key_pair[0], version=2, digest_override="a" * 64
    )
    resp = await _post(client, firmware, manifest_bytes, sig)
    assert resp.status_code == 400
    assert "SHA-256" in resp.json()["detail"]
    assert device.install_calls == 0


@pytest.mark.asyncio
async def test_non_positive_version_rejected(started_app, key_pair):
    client, _device = started_app
    for bad_version in (0, -1):
        firmware, manifest_bytes, sig, _ = make_package(
            key_pair[0], version=1,
            firmware_override=b"fw",
            digest_override=hashlib.sha256(b"fw").hexdigest(),
        )
        manifest = {"model": "gateway-gx100", "version": bad_version,
                    "sha256": hashlib.sha256(b"fw").hexdigest()}
        manifest_bytes = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        from app.crypto_utils import load_private_key, sign_manifest
        sig = sign_manifest(load_private_key(key_pair[0]), manifest_bytes)
        resp = await _post(client, b"fw", manifest_bytes, sig)
        assert resp.status_code == 400
        assert "positive integer" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_boolean_version_rejected(started_app, key_pair):
    client, _device = started_app
    manifest = {"model": "gateway-gx100", "version": True,
                "sha256": hashlib.sha256(b"fw").hexdigest()}
    manifest_bytes = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    from app.crypto_utils import load_private_key, sign_manifest
    sig = sign_manifest(load_private_key(key_pair[0]), manifest_bytes)
    resp = await _post(client, b"fw", manifest_bytes, sig)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_oversized_firmware_rejected(started_app, key_pair, settings):
    client, device = started_app
    big = b"z" * (settings.max_firmware_bytes + 1)
    firmware, manifest_bytes, sig, _ = make_package(
        key_pair[0], version=2, firmware_override=big,
        digest_override=hashlib.sha256(big).hexdigest(),
    )
    resp = await _post(client, big, manifest_bytes, sig)
    assert resp.status_code == 413
    assert device.install_calls == 0


@pytest.mark.asyncio
async def test_invalid_json_manifest_rejected(started_app, key_pair):
    client, device = started_app
    from app.crypto_utils import load_private_key, sign_manifest
    raw = b"{not-json"
    sig = sign_manifest(load_private_key(key_pair[0]), raw)
    resp = await _post(client, b"fw", raw, sig)
    assert resp.status_code == 400
    assert device.install_calls == 0


async def _spin():
    import asyncio
    await asyncio.sleep(0.05)
