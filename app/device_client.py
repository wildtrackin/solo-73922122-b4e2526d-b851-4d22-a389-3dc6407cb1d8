"""HTTP client for the target device simulator."""
from __future__ import annotations

from dataclasses import dataclass

import httpx


class DeviceUnreachable(Exception):
    """Raised when the device does not return a usable receipt."""


@dataclass
class DeviceStatus:
    model: str
    version: int
    sha256: str
    installed: bool


class DeviceClient:
    def __init__(self, base_url: str, timeout: float, transport=None):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        # ``transport`` is a test seam allowing in-process ASGI calls.
        self._transport = transport

    async def install(
        self, *, model: str, version: int, sha256: str, firmware: bytes
    ) -> dict:
        """POST an installation request.

        Any transport error / timeout means the outcome is unknown: the caller
        must treat it as "pending verification", never as failure.
        """
        payload = {
            "model": model,
            "version": version,
            "sha256": sha256,
            "firmware_b64": _b64(firmware),
        }
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                resp = await client.post(f"{self._base_url}/install", json=payload)
        except httpx.HTTPError as exc:
            raise DeviceUnreachable(str(exc)) from exc
        if resp.status_code >= 500:
            raise DeviceUnreachable(f"device HTTP {resp.status_code}")
        try:
            body = resp.json()
        except ValueError as exc:
            raise DeviceUnreachable("device returned non-JSON response") from exc
        # FastAPI wraps HTTPException(detail={...}) bodies as {"detail": {...}}.
        # Normalize both shapes so callers always see a flat status/receipt dict.
        if (
            isinstance(body, dict)
            and isinstance(body.get("detail"), dict)
            and "status" in body["detail"]
        ):
            body = body["detail"]
        return body

    async def status(self) -> DeviceStatus:
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                resp = await client.get(f"{self._base_url}/status")
                resp.raise_for_status()
                data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise DeviceUnreachable(str(exc)) from exc
        return DeviceStatus(
            model=data["model"],
            version=int(data["version"]),
            sha256=data["sha256"],
            installed=bool(data.get("installed", True)),
        )


def _b64(data: bytes) -> str:
    import base64

    return base64.b64encode(data).decode("ascii")
