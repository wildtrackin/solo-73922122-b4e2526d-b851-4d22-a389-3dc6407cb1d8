"""Shared test fixtures: real Ed25519 keys, in-memory API + device ASGI apps."""
from __future__ import annotations

import hashlib
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.config import Settings
from app.crypto_utils import sign_manifest


@asynccontextmanager
async def _lifespan(app):
    """Drive an ASGI app's lifespan startup/shutdown for in-process tests."""
    async with app.router.lifespan_context(app):
        yield


@pytest.fixture()
def key_pair(tmp_path: Path):
    priv = Ed25519PrivateKey.generate()
    priv_path = tmp_path / "private.pem"
    pub_path = tmp_path / "public.pem"
    priv_path.write_bytes(
        priv.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    pub_path.write_bytes(
        priv.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return priv_path, pub_path


@pytest.fixture()
def settings(tmp_path: Path, key_pair) -> Settings:
    _, pub_path = key_pair
    return Settings(
        database_path=tmp_path / "installer.db",
        trusted_public_key_path=pub_path,
        device_base_url="http://device-test",
        device_model="gateway-gx100",
        max_firmware_bytes=8 * 1024 * 1024,
        device_timeout_seconds=2.0,
        reconcile_interval_seconds=60.0,
    )


def make_package(
    priv_path: Path,
    *,
    version: int,
    model: str = "gateway-gx100",
    size: int = 256,
    digest_override: str | None = None,
    firmware_override: bytes | None = None,
) -> tuple[bytes, bytes, str, str]:
    """Return (firmware, manifest_bytes, signature, sha256)."""
    firmware = firmware_override or (
        f"FW-v{version}\n".encode() + b"x" * size
    )
    sha = digest_override or hashlib.sha256(firmware).hexdigest()
    manifest = {"model": model, "version": version, "sha256": sha}
    manifest_bytes = json.dumps(
        manifest, sort_keys=True, separators=(",", ":")
    ).encode()
    from app.crypto_utils import load_private_key

    sig = sign_manifest(load_private_key(priv_path), manifest_bytes)
    return firmware, manifest_bytes, sig, sha


class RecordingDevice:
    """In-process stand-in for the device simulator, with fault injection."""

    def __init__(self, model: str = "gateway-gx100", factory_version: int = 1):
        self.model = model
        self.version = factory_version
        self.sha256 = "0" * 63 + "1"
        self.install_calls = 0
        self.status_calls = 0
        self.lose_next = False
        self.offline = False
        self.install_delay = 0.0
        self.offline_after_status = False

    async def status(self):
        self.status_calls += 1
        if self.offline or self.offline_after_status:
            from app.device_client import DeviceUnreachable

            raise DeviceUnreachable("simulated offline")
        from app.device_client import DeviceStatus

        return DeviceStatus(
            model=self.model,
            version=self.version,
            sha256=self.sha256,
            installed=True,
        )

    async def install(self, *, model: str, version: int, sha256: str, firmware: bytes):
        import asyncio

        from app.device_client import DeviceUnreachable

        self.install_calls += 1
        if self.install_delay:
            await asyncio.sleep(self.install_delay)
        if self.offline or self.offline_after_status:
            raise DeviceUnreachable("simulated offline")
        assert hashlib.sha256(firmware).hexdigest() == sha256
        if version < self.version:
            return {"status": "rejected", "reason": "downgrade refused"}
        if version == self.version:
            if sha256 == self.sha256:
                return {"status": "already_installed"}
            return {
                "status": "rejected",
                "reason": "same version with different digest is forbidden",
            }
        # Activate first, then maybe lose the receipt.
        self.version = version
        self.sha256 = sha256
        if self.lose_next:
            self.lose_next = False
            raise DeviceUnreachable("receipt connection dropped after activation")
        return {"status": "installed", "model": model, "version": version, "sha256": sha256}


@pytest_asyncio.fixture
async def started_app(settings, key_pair):
    """Start the real FastAPI app with an in-process recording device."""
    from app.main import create_app

    device = RecordingDevice()
    application = create_app(
        settings,
        start_reconcile_loop=False,
        device_factory=lambda: device,
    )

    async with _lifespan(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            yield client, device


@pytest_asyncio.fixture
async def fresh_service(settings, key_pair):
    """A service without an ASGI client, for direct state-machine tests."""
    from app.crypto_utils import load_public_key
    from app.db import Database
    from app.service import InstallService

    db = Database(settings.database_path)
    await db.connect()
    pub = load_public_key(settings.trusted_public_key_path)
    svc = InstallService(settings, db, pub)
    device = RecordingDevice()
    svc.device = device  # type: ignore[assignment]
    yield svc, db, device
    await db.close()
