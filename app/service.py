"""Installation orchestration: validation, dispatch and crash recovery.

Safety rules implemented here:

* Only one installation at a time. Any task in PENDING / DISPATCHED /
  VERIFYING owns the slot; new uploads get HTTP 409. Status queries stay
  available.
* Nothing is dispatched until the manifest, signature, model, version and
  firmware digest all pass local verification against the fixed trust anchor.
* A missing / timed-out device receipt means the outcome is unknown. The task
  becomes VERIFYING (pending verification) — never failed, and the slot stays
  locked.
* On API startup (and periodically in the background) the device is queried:
  if the target version+digest is already installed, SUCCESS is back-filled;
  otherwise the original stored package is re-sent unchanged.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging

from app.config import Settings
from app.crypto_utils import verify_manifest
from app.db import Database
from app.device_client import DeviceClient, DeviceUnreachable

logger = logging.getLogger("installer")

_OPEN_TASK_STATES = ("PENDING", "DISPATCHED", "VERIFYING")


class ValidationError(Exception):
    """Local validation failure — the upload must never reach the device."""


class SlotBusyError(Exception):
    """An open installation task already owns the single-installation slot."""

    def __init__(self, task_id: int):
        self.task_id = task_id
        super().__init__(f"installation slot busy with task {task_id}")


class InstallService:
    def __init__(self, settings: Settings, db: Database, public_key):
        self.settings = settings
        self.db = db
        self.public_key = public_key
        self.device = DeviceClient(
            settings.device_base_url, settings.device_timeout_seconds
        )
        # Serializes slot checks, dispatch and reconciliation in this process.
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ upload
    async def submit_installation(
        self, firmware: bytes, manifest_bytes: bytes, signature_b64: str
    ) -> int:
        if len(firmware) > self.settings.max_firmware_bytes:
            raise ValidationError(
                f"firmware exceeds {self.settings.max_firmware_bytes} bytes "
                f"(got {len(firmware)})"
            )

        manifest = self._validate_manifest(firmware, manifest_bytes, signature_b64)

        # Slot check + task insert are atomic relative to other submissions
        # and to reconciliation. Dispatch runs later, without nesting the lock.
        async with self._lock:
            open_task = await self.db.get_open_task()
            if open_task is not None:
                raise SlotBusyError(open_task["id"])

            task_id = await self.db.create_task(
                state="PENDING",
                model=manifest["model"],
                version=manifest["version"],
                sha256=manifest["sha256"],
                firmware=firmware,
                manifest_bytes=manifest_bytes,
                signature=signature_b64,
            )
            logger.info("task %s created (v%s)", task_id, manifest["version"])

        # The slot is already persisted, so concurrent uploads are rejected
        # even before this background dispatch acquires the lock.
        asyncio.create_task(self._dispatch(task_id))
        return task_id

    def _validate_manifest(
        self, firmware: bytes, manifest_bytes: bytes, signature_b64: str
    ) -> dict:
        # 1. Ed25519 signature over the exact raw manifest bytes.
        if not signature_b64:
            raise ValidationError("missing signature")
        if not verify_manifest(self.public_key, manifest_bytes, signature_b64):
            raise ValidationError("manifest signature verification failed")

        # 2. Manifest must be a JSON object.
        try:
            manifest = json.loads(manifest_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError(f"manifest is not valid JSON: {exc}") from exc
        if not isinstance(manifest, dict):
            raise ValidationError("manifest must be a JSON object")

        # 3. Required fields with strict shape rules.
        model = manifest.get("model")
        version = manifest.get("version")
        sha256 = manifest.get("sha256")
        if not isinstance(model, str) or not model:
            raise ValidationError("manifest.model must be a non-empty string")
        # bool is a subclass of int — reject it explicitly.
        if not isinstance(version, int) or isinstance(version, bool) or version <= 0:
            raise ValidationError("manifest.version must be a positive integer")
        if not isinstance(sha256, str) or len(sha256) != 64:
            raise ValidationError("manifest.sha256 must be a 64-char hex string")
        try:
            int(sha256, 16)
        except ValueError as exc:
            raise ValidationError("manifest.sha256 is not hexadecimal") from exc

        # 4. Fixed target model.
        if model != self.settings.device_model:
            raise ValidationError(
                f"model mismatch: manifest targets {model!r}, "
                f"device is fixed to {self.settings.device_model!r}"
            )

        # 5. Firmware digest must match the manifest claim.
        actual_digest = hashlib.sha256(firmware).hexdigest()
        if actual_digest != sha256.lower():
            raise ValidationError(
                "firmware SHA-256 does not match manifest "
                f"(firmware={actual_digest}, manifest={sha256.lower()})"
            )
        return {"model": model, "version": version, "sha256": sha256.lower()}

    # ---------------------------------------------------------------- dispatch
    async def _dispatch(self, task_id: int) -> None:
        """Lock-owning entry point used by the per-upload background task."""
        async with self._lock:
            await self._dispatch_locked(task_id)

    async def _dispatch_locked(self, task_id: int) -> None:
        """Dispatch logic. Caller MUST hold ``self._lock``."""
        task = await self.db.get_task(task_id)
        if task is None or task["state"] not in _OPEN_TASK_STATES:
            return

        # Downgrade / conflict guard against the real device before sending.
        try:
            status = await self.device.status()
        except DeviceUnreachable:
            status = None  # a failed query must not block dispatch attempts
        if status is not None and status.version >= task["version"]:
            if (
                status.version == task["version"]
                and status.sha256 == task["sha256"]
            ):
                await self.db.update_state(
                    task_id, "SUCCESS", "already installed (verified by query)"
                )
                logger.info("task %s confirmed installed before dispatch", task_id)
                return
            await self.db.mark_rejected(
                task_id,
                f"device already at version {status.version}; refusing to "
                f"install v{task['version']} with a different digest",
            )
            logger.warning("task %s rejected by downgrade/conflict guard", task_id)
            return

        await self.db.update_state(task_id, "DISPATCHED")
        try:
            receipt = await self.device.install(
                model=task["model"],
                version=task["version"],
                sha256=task["sha256"],
                firmware=task["firmware"],
            )
        except DeviceUnreachable as exc:
            # Outcome unknown: hold the slot pending verification. Never fail.
            await self.db.update_state(
                task_id, "VERIFYING", f"no receipt ({exc}); pending verification"
            )
            logger.warning("task %s no receipt -> VERIFYING", task_id)
            return

        await self._apply_receipt(task_id, receipt)

    async def _apply_receipt(self, task_id: int, receipt: dict) -> None:
        status = receipt.get("status")
        if status in ("installed", "already_installed"):
            await self.db.update_state(
                task_id, "SUCCESS", f"device receipt: {status}"
            )
            logger.info("task %s SUCCESS (%s)", task_id, status)
            return
        if status == "rejected":
            reason = receipt.get("reason", "device rejected installation")
            await self.db.mark_rejected(task_id, f"device rejected: {reason}")
            logger.warning("task %s REJECTED by device: %s", task_id, reason)
            return
        # Unknown receipt shape — do not trust it.
        await self.db.update_state(
            task_id, "VERIFYING", f"unparseable receipt: {receipt!r}"
        )

    # --------------------------------------------------------------- recovery
    async def recover_on_startup(self) -> None:
        """Preserve open tasks across process restarts and reconcile them."""
        open_task = await self.db.get_open_task()
        if open_task is None:
            logger.info("startup recovery: no open installation task")
            return
        logger.warning(
            "startup recovery: task %s found in state %s — querying device",
            open_task["id"],
            open_task["state"],
        )
        await self.reconcile()

    async def reconcile(self) -> None:
        """Verify the open task against the device; resend the original package.

        * device already on (version, sha256) -> back-fill SUCCESS
        * device not at target -> re-POST the original stored package
        * device unreachable -> remain VERIFYING (never fail, never free slot)
        """
        async with self._lock:
            task = await self.db.get_open_task()
            if task is None:
                return
            task_id = task["id"]
            try:
                status = await self.device.status()
            except DeviceUnreachable as exc:
                await self.db.update_state(
                    task_id,
                    "VERIFYING",
                    f"device unreachable during verification ({exc})",
                )
                logger.warning("reconcile: task %s still unverifiable", task_id)
                return

            if (
                status.version == task["version"]
                and status.sha256 == task["sha256"]
            ):
                await self.db.update_state(
                    task_id, "SUCCESS", "verified against device after recovery"
                )
                logger.info("reconcile: task %s back-filled SUCCESS", task_id)
                return

            # Device is not at the target: resend the original stored package.
            await self.db.update_state(
                task_id, "DISPATCHED", "resending original package"
            )
            try:
                receipt = await self.device.install(
                    model=task["model"],
                    version=task["version"],
                    sha256=task["sha256"],
                    firmware=task["firmware"],
                )
            except DeviceUnreachable as exc:
                await self.db.update_state(
                    task_id, "VERIFYING", f"resend without receipt ({exc})"
                )
                logger.warning("reconcile: task %s resend unverified", task_id)
                return
            await self._apply_receipt(task_id, receipt)
