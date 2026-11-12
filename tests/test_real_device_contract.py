"""Regression tests over the real device HTTP contract.

These wire the real installation API to the real device simulator ASGI app
through an in-process httpx transport, so FastAPI's wrapped error body
(``{"detail": {"status": "rejected", ...}}``) is exercised end to end.
"""
from __future__ import annotations

import importlib
import os

import httpx
import pytest
import pytest_asyncio

from app.device_client import DeviceClient
from app.main import create_app
from tests.conftest import _lifespan, make_package


@pytest_asyncio.fixture
async def wired_app(tmp_path, monkeypatch, key_pair):
    # Fresh device simulator with its own SQLite file.
    monkeypatch.setenv("DEVICE_DB_PATH", str(tmp_path / "device.db"))
    monkeypatch.setenv("DEVICE_FACTORY_VERSION", "1")
    import device_sim.app as device_app

    importlib.reload(device_app)

    device_transport = httpx.ASGITransport(app=device_app.app)

    # API settings point at a dummy host; the transport ignores the host.
    from app.config import Settings

    api_settings = Settings(
        database_path=tmp_path / "api.db",
        trusted_public_key_path=key_pair[1],
        device_base_url="http://device-test",
        device_model="gateway-gx100",
        max_firmware_bytes=8 * 1024 * 1024,
        device_timeout_seconds=5.0,
        reconcile_interval_seconds=3600.0,
    )

    async with _lifespan(device_app.app):
        application = create_app(
            api_settings,
            start_reconcile_loop=False,
            device_factory=lambda: DeviceClient(
                "http://device-test", 5.0, transport=device_transport
            ),
        )
        async with _lifespan(application):
            api_transport = httpx.ASGITransport(app=application)
            async with httpx.AsyncClient(
                transport=api_transport, base_url="http://api"
            ) as client:
                yield client


async def _post(client, pkg) -> int:
    firmware, manifest_bytes, sig, _ = pkg
    r = await client.post(
        "/installations",
        files={
            "firmware": ("f.bin", firmware, "application/octet-stream"),
            "manifest": ("m.json", manifest_bytes, "application/json"),
        },
        data={"signature": sig},
    )
    assert r.status_code == 202, r.text
    return r.json()["task_id"]


async def _wait(client, task_id):
    for _ in range(60):
        view = (await client.get(f"/installations/{task_id}")).json()
        if view["state"] in ("SUCCESS", "REJECTED"):
            return view
        import asyncio

        await asyncio.sleep(0.02)
    raise AssertionError("task never reached terminal state")


@pytest.mark.asyncio
async def test_device_wrapped_rejection_recorded_as_rejected(wired_app, key_pair):
    client = wired_app

    # Install v2 normally.
    t1 = await _post(client, make_package(key_pair[0], version=2, size=128))
    view = await _wait(client, t1)
    assert view["state"] == "SUCCESS"

    # Same version, different digest: the device returns a wrapped 409.
    t2 = await _post(client, make_package(key_pair[0], version=2, size=256))
    view = await _wait(client, t2)
    assert view["state"] == "REJECTED"
    assert "different digest" in (view["reject_reason"] or "")
    # A wrapped rejection must never be mistaken for an unknown receipt.
    assert view["state"] != "VERIFYING"


@pytest.mark.asyncio
async def test_device_client_unwraps_fastapi_error_body(tmp_path, monkeypatch):
    """The device's 4xx body is {"detail": {"status": "rejected", ...}}.

    DeviceClient must normalize it to a flat receipt dict, otherwise the
    service misclassifies a real rejection as an unparseable/unknown receipt.
    """
    monkeypatch.setenv("DEVICE_DB_PATH", str(tmp_path / "device.db"))
    monkeypatch.setenv("DEVICE_FACTORY_VERSION", "1")
    import device_sim.app as device_app

    importlib.reload(device_app)

    import hashlib

    transport = httpx.ASGITransport(app=device_app.app)
    async with _lifespan(device_app.app):
        client = DeviceClient("http://device-test", 5.0, transport=transport)

        # First put the device on v2 with a known digest.
        fw_a = b"firmware-A"
        await client.install(
            model="gateway-gx100",
            version=2,
            sha256=hashlib.sha256(fw_a).hexdigest(),
            firmware=fw_a,
        )
        # Same version, different digest -> device 409 with wrapped body.
        fw_b = b"firmware-B-different"
        receipt = await client.install(
            model="gateway-gx100",
            version=2,
            sha256=hashlib.sha256(fw_b).hexdigest(),
            firmware=fw_b,
        )
        assert receipt["status"] == "rejected"
        assert "different digest" in receipt["reason"]


@pytest.mark.asyncio
async def test_downgrade_over_real_device_is_rejected(wired_app, key_pair):
    client = wired_app

    t_hi = await _post(client, make_package(key_pair[0], version=10, size=64))
    assert (await _wait(client, t_hi))["state"] == "SUCCESS"

    t_lo = await _post(client, make_package(key_pair[0], version=3, size=64))
    view = await _wait(client, t_lo)
    assert view["state"] == "REJECTED"
    # Rejected either by the API pre-dispatch guard or by the device itself;
    # both correctly prevent the downgrade.
    reason = (view["reject_reason"] or "").lower()
    assert "downgrade" in reason or "already at version 10" in reason
