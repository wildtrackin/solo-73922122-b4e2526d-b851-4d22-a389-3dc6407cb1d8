"""Simulated target gateway device.

The device enforces its own acceptance rules:

* only a higher version is installed (anti-downgrade);
* same version + same digest is idempotent -> the previous result is returned;
* same version + different digest is rejected;
* activation (firmware/version/digest switch) is persisted atomically.

Fault injection for demo / recovery testing:

* POST /admin/lose-next-receipt  -> the next /install that actually activates
  new firmware will commit the switch locally but never send a receipt
  (connection dropped), exactly like an activated-but-receipt-lost event;
* POST /admin/reset              -> restore the factory state.
"""
from __future__ import annotations

import base64
import hashlib
import os
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

DEVICE_MODEL = os.environ.get("DEVICE_MODEL", "gateway-gx100")
DB_PATH = Path(os.environ.get("DEVICE_DB_PATH", "/data/device.db"))
FACTORY_VERSION = int(os.environ.get("DEVICE_FACTORY_VERSION", "1"))
FACTORY_DIGEST = os.environ.get(
    "DEVICE_FACTORY_DIGEST",
    "0000000000000000000000000000000000000000000000000000000000000001",
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = await _connect()
    await conn.close()
    yield


app = FastAPI(title="Gateway Device Simulator", version="1.0.0", lifespan=lifespan)

# In-process fault-injection flag. It only governs receipt delivery; the
# firmware switch itself is always durable first.
_lose_next_receipt = False


class InstallRequest(BaseModel):
    model: str
    version: int = Field(..., gt=0)
    sha256: str = Field(..., min_length=64, max_length=64)
    firmware_b64: str


class LoseReceiptRequest(BaseModel):
    enabled: bool = True


async def _connect() -> aiosqlite.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(DB_PATH)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS device_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            model TEXT NOT NULL,
            version INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            installed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        )
        """
    )
    await conn.execute(
        "INSERT OR IGNORE INTO device_state (id, model, version, sha256) "
        "VALUES (1, ?, ?, ?)",
        (DEVICE_MODEL, FACTORY_VERSION, FACTORY_DIGEST),
    )
    await conn.commit()
    return conn


async def _state(conn: aiosqlite.Connection) -> aiosqlite.Row:
    async with conn.execute(
        "SELECT model, version, sha256, installed_at FROM device_state WHERE id = 1"
    ) as cur:
        return await cur.fetchone()


@app.get("/status")
async def status():
    conn = await _connect()
    try:
        row = await _state(conn)
        return {
            "model": row["model"],
            "version": row["version"],
            "sha256": row["sha256"],
            "installed_at": row["installed_at"],
            "installed": True,
        }
    finally:
        await conn.close()


@app.post("/install")
async def install(req: InstallRequest, x_lose_receipt: bool | None = Header(default=None)):
    global _lose_next_receipt

    if req.model != DEVICE_MODEL:
        raise HTTPException(status_code=400, detail=f"unknown model {req.model!r}")

    try:
        firmware = base64.b64decode(req.firmware_b64, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise HTTPException(status_code=400, detail="invalid firmware encoding") from exc

    digest = hashlib.sha256(firmware).hexdigest()
    if digest != req.sha256.lower():
        raise HTTPException(
            status_code=400,
            detail={
                "status": "rejected",
                "reason": "firmware sha256 mismatch",
            },
        )

    conn = await _connect()
    try:
        row = await _state(conn)
        current_version = row["version"]
        current_digest = row["sha256"]

        if req.version < current_version:
            raise HTTPException(
                status_code=409,
                detail={
                    "status": "rejected",
                    "reason": (
                        f"downgrade refused: current v{current_version}, "
                        f"requested v{req.version}"
                    ),
                },
            )

        if req.version == current_version:
            if req.sha256.lower() == current_digest:
                # Idempotent repeat: return the original result unchanged.
                return {
                    "status": "already_installed",
                    "model": DEVICE_MODEL,
                    "version": current_version,
                    "sha256": current_digest,
                }
            raise HTTPException(
                status_code=409,
                detail={
                    "status": "rejected",
                    "reason": (
                        f"same version v{req.version} with different digest "
                        "is forbidden"
                    ),
                },
            )

        # Activate: firmware, version and digest switch in one durable commit.
        await conn.execute(
            "UPDATE device_state SET model = ?, version = ?, sha256 = ?, "
            "installed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = 1",
            (DEVICE_MODEL, req.version, req.sha256.lower()),
        )
        await conn.commit()
    finally:
        await conn.close()

    should_lose = _lose_next_receipt or bool(x_lose_receipt)
    if should_lose:
        _lose_next_receipt = False
        # Simulate a dropped connection AFTER durable activation. Raising here
        # means the API receives no receipt while the switch already happened.
        raise HTTPException(
            status_code=502,
            detail={
                "status": "receipt_lost",
                "note": "firmware was activated but the receipt connection dropped",
            },
        )

    return {
        "status": "installed",
        "model": DEVICE_MODEL,
        "version": req.version,
        "sha256": req.sha256.lower(),
    }


@app.post("/admin/lose-next-receipt")
async def lose_next_receipt(req: LoseReceiptRequest):
    global _lose_next_receipt
    _lose_next_receipt = req.enabled
    return {"lose_next_receipt": _lose_next_receipt}


@app.post("/admin/reset")
async def reset():
    global _lose_next_receipt
    _lose_next_receipt = False
    conn = await _connect()
    try:
        await conn.execute(
            "UPDATE device_state SET model = ?, version = ?, sha256 = ?, "
            "installed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = 1",
            (DEVICE_MODEL, FACTORY_VERSION, FACTORY_DIGEST),
        )
        await conn.commit()
        row = await _state(conn)
        return {
            "status": "reset",
            "model": row["model"],
            "version": row["version"],
            "sha256": row["sha256"],
        }
    finally:
        await conn.close()
