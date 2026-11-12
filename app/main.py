"""FastAPI installation API (application factory).

Endpoints:
  POST /installations            upload firmware + manifest + Ed25519 signature
  GET  /installations/{id}       task status (always available, even while busy)
  GET  /installations            recent tasks
  POST /installations/reconcile  manually trigger device verification/resend
  GET  /health                   liveness + current slot owner
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, status
from fastapi.responses import JSONResponse

from app.config import Settings, settings as default_settings
from app.crypto_utils import load_public_key
from app.db import Database
from app.models import HealthView, TaskCreated, TaskView
from app.service import InstallService, SlotBusyError, ValidationError

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("installer.api")


def create_app(
    app_settings: Settings | None = None,
    *,
    start_reconcile_loop: bool = True,
    device_factory=None,
) -> FastAPI:
    """Build the API application.

    ``device_factory`` is a test seam returning a device client instance; when
    omitted the real HTTP :class:`DeviceClient` is created by the service.
    """
    app_settings = app_settings or default_settings
    db = Database(app_settings.database_path)
    service: InstallService | None = None
    reconcile_task: asyncio.Task | None = None

    async def reconcile_loop() -> None:
        assert service is not None
        while True:
            try:
                await service.reconcile()
            except Exception:  # pragma: no cover - defensive background loop
                logger.exception("background reconciliation failed")
            await asyncio.sleep(app_settings.reconcile_interval_seconds)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal service, reconcile_task
        await db.connect()
        public_key = load_public_key(app_settings.trusted_public_key_path)
        service = InstallService(app_settings, db, public_key)
        if device_factory is not None:  # test seam
            service.device = device_factory()
        # Preserve any task left open by a previous process and reconcile it.
        await service.recover_on_startup()
        if start_reconcile_loop:
            reconcile_task = asyncio.create_task(reconcile_loop())
        logger.info(
            "installation API started (device=%s, model=%s, max_firmware=%d bytes)",
            app_settings.device_base_url,
            app_settings.device_model,
            app_settings.max_firmware_bytes,
        )
        try:
            yield
        finally:
            if reconcile_task is not None:
                reconcile_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await reconcile_task
            await db.close()

    app = FastAPI(
        title="Firmware Installation API",
        version="1.0.0",
        description=(
            "Authenticated firmware installation with Ed25519 manifest "
            "signatures, anti-downgrade enforcement and crash-safe "
            "pending-verification recovery."
        ),
        lifespan=lifespan,
    )

    def get_service() -> InstallService:
        if service is None:  # pragma: no cover - lifespan always runs
            raise HTTPException(status_code=503, detail="service starting")
        return service

    @app.post(
        "/installations",
        response_model=TaskCreated,
        status_code=status.HTTP_202_ACCEPTED,
        responses={
            400: {"description": "manifest / signature / digest validation failed"},
            409: {"description": "another installation is still in progress"},
            413: {"description": "firmware larger than 8 MiB"},
        },
        tags=["installations"],
    )
    async def create_installation(
        firmware: UploadFile = File(..., description="firmware binary, at most 8 MiB"),
        manifest: UploadFile = File(
            ..., description="JSON manifest (model, version, sha256) raw bytes"
        ),
        signature: str = Form(
            ...,
            description="base64 Ed25519 signature over the raw manifest bytes",
        ),
    ):
        svc = get_service()
        firmware_bytes = await firmware.read()
        manifest_bytes = await manifest.read()

        if len(firmware_bytes) > app_settings.max_firmware_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=(
                    f"firmware exceeds limit of {app_settings.max_firmware_bytes} "
                    f"bytes (got {len(firmware_bytes)})"
                ),
            )

        try:
            task_id = await svc.submit_installation(
                firmware_bytes, manifest_bytes, signature.strip()
            )
        except SlotBusyError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "installation slot busy",
                    "open_task_id": exc.task_id,
                    "message": (
                        "an installation is pending verification; query its "
                        "status before attempting another installation"
                    ),
                },
            ) from exc
        except ValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc

        return TaskCreated(
            task_id=task_id,
            state="PENDING",
            detail="accepted; installation dispatched asynchronously",
        )

    @app.get(
        "/installations/{task_id}",
        response_model=TaskView,
        tags=["installations"],
        responses={404: {"description": "unknown task id"}},
    )
    async def get_installation(task_id: int):
        task = await db.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="installation task not found")
        return TaskView(**{k: task[k] for k in TaskView.model_fields})

    @app.get("/installations", response_model=list[TaskView], tags=["installations"])
    async def list_installations(limit: int = 50):
        tasks = await db.list_tasks(min(max(limit, 1), 200))
        return [TaskView(**{k: t[k] for k in TaskView.model_fields}) for t in tasks]

    @app.post(
        "/installations/reconcile",
        tags=["installations"],
        description=(
            "Immediately verify the open task against the device: back-fill "
            "SUCCESS if already installed, otherwise resend the original "
            "stored package."
        ),
    )
    async def trigger_reconcile():
        svc = get_service()
        open_task = await db.get_open_task()
        if open_task is None:
            return JSONResponse(
                {"detail": "no open installation task; nothing to reconcile"}
            )
        await svc.reconcile()
        refreshed = await db.get_task(open_task["id"])
        return TaskView(**{k: refreshed[k] for k in TaskView.model_fields})

    @app.get("/health", response_model=HealthView, tags=["monitoring"])
    async def health():
        open_task = await db.get_open_task()
        return HealthView(
            status="ok",
            open_task_id=open_task["id"] if open_task else None,
        )

    return app


# Default ASGI application used by uvicorn.
app = create_app()
