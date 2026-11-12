"""Device simulator HTTP contract tests (run via the real ASGI app)."""
from __future__ import annotations

import base64
import hashlib

import httpx
import pytest
import pytest_asyncio

from tests.conftest import _lifespan


def _install_payload(version: int, firmware: bytes) -> dict:
    return {
        "model": "gateway-gx100",
        "version": version,
        "sha256": hashlib.sha256(firmware).hexdigest(),
        "firmware_b64": base64.b64encode(firmware).decode(),
    }


@pytest_asyncio.fixture
async def device_client(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVICE_DB_PATH", str(tmp_path / "device.db"))
    monkeypatch.setenv("DEVICE_FACTORY_VERSION", "1")
    # Import after env vars are set so module-level constants pick them up.
    import importlib

    import device_sim.app as device_app

    importlib.reload(device_app)

    async with _lifespan(device_app.app):
        transport = httpx.ASGITransport(app=device_app.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://device"
        ) as client:
            yield client, device_app


@pytest.mark.asyncio
async def test_device_idempotent_and_conflict_and_downgrade(device_client):
    client, _ = device_client

    fw2 = b"firmware-two"
    r = await client.post("/install", json=_install_payload(2, fw2))
    assert r.status_code == 200 and r.json()["status"] == "installed"

    # Same version + same digest -> original result.
    r = await client.post("/install", json=_install_payload(2, fw2))
    assert r.json()["status"] == "already_installed"

    # Same version + different digest -> rejected.
    r = await client.post(
        "/install", json=_install_payload(2, b"firmware-two-different")
    )
    assert r.status_code == 409
    assert r.json()["detail"]["status"] == "rejected"

    # Lower version -> downgrade rejected.
    r = await client.post("/install", json=_install_payload(1, b"old"))
    assert r.status_code == 409 and "downgrade" in r.json()["detail"]["reason"]

    # Higher version works and status reflects the atomic switch.
    r = await client.post("/install", json=_install_payload(3, b"firmware-three"))
    assert r.json()["status"] == "installed"
    status = (await client.get("/status")).json()
    assert status["version"] == 3
    assert status["sha256"] == hashlib.sha256(b"firmware-three").hexdigest()


@pytest.mark.asyncio
async def test_device_loses_receipt_after_activation(device_client):
    client, _ = device_client

    await client.post("/admin/lose-next-receipt", json={"enabled": True})
    r = await client.post("/install", json=_install_payload(5, b"firmware-five"))
    # Receipt lost: error response, but activation is durable.
    assert r.status_code == 502
    status = (await client.get("/status")).json()
    assert status["version"] == 5
    assert status["sha256"] == hashlib.sha256(b"firmware-five").hexdigest()

    # Fault was one-shot; next request behaves normally.
    r = await client.post("/install", json=_install_payload(6, b"firmware-six"))
    assert r.status_code == 200 and r.json()["status"] == "installed"


@pytest.mark.asyncio
async def test_device_rejects_digest_mismatch(device_client):
    client, _ = device_client
    payload = _install_payload(2, b"real-fw")
    payload["sha256"] = "f" * 64
    r = await client.post("/install", json=payload)
    assert r.status_code == 400
    assert r.json()["detail"]["status"] == "rejected"
