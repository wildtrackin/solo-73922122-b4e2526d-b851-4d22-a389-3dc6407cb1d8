"""State machine tests: idempotency, conflict, slot locking and recovery."""
from __future__ import annotations

import asyncio

import pytest

from app.service import SlotBusyError
from tests.conftest import make_package


async def _wait_state(db, task_id, expected, tries=100):
    for _ in range(tries):
        task = await db.get_task(task_id)
        if task["state"] == expected:
            return task
        await asyncio.sleep(0.02)
    raise AssertionError(f"task {task_id} never reached {expected}: {task}")


async def _wait_terminal(db, task_id, tries=100):
    for _ in range(tries):
        task = await db.get_task(task_id)
        if task["state"] in ("SUCCESS", "REJECTED"):
            return task
        await asyncio.sleep(0.02)
    raise AssertionError(f"task {task_id} stuck: {task}")


@pytest.mark.asyncio
async def test_same_version_same_digest_returns_original_result(fresh_service, key_pair):
    svc, db, device = fresh_service
    fw, mb, sig, sha = make_package(key_pair[0], version=3)
    t1 = await svc.submit_installation(fw, mb, sig)
    task1 = await _wait_state(db, t1, "SUCCESS")
    assert device.install_calls == 1

    # Resubmit the identical package: the service sees the device is already
    # on (version, digest) and returns the original successful result without
    # re-sending bytes. Both tasks end SUCCESS with the same outcome.
    t2 = await svc.submit_installation(fw, mb, sig)
    task2 = await _wait_terminal(db, t2)
    assert task2["state"] == "SUCCESS"
    assert device.install_calls == 1  # confirmed by status query, no re-send
    assert "already installed" in (task2["device_response"] or "")
    assert task1["sha256"] == task2["sha256"] == sha


@pytest.mark.asyncio
async def test_same_version_different_digest_rejected(fresh_service, key_pair):
    svc, db, device = fresh_service
    fw1, mb1, sig1, _ = make_package(key_pair[0], version=4, size=128)
    t1 = await svc.submit_installation(fw1, mb1, sig1)
    await _wait_state(db, t1, "SUCCESS")

    fw2, mb2, sig2, _ = make_package(key_pair[0], version=4, size=256)
    t2 = await svc.submit_installation(fw2, mb2, sig2)
    task2 = await _wait_terminal(db, t2)
    assert task2["state"] == "REJECTED"
    assert "different digest" in task2["reject_reason"]
    # Device kept the original firmware/digest.
    assert device.sha256 == make_package(key_pair[0], version=4, size=128)[3]


@pytest.mark.asyncio
async def test_downgrade_refused_by_device(fresh_service, key_pair):
    svc, db, device = fresh_service
    fw5, mb5, sig5, _ = make_package(key_pair[0], version=5)
    t = await svc.submit_installation(fw5, mb5, sig5)
    await _wait_state(db, t, "SUCCESS")

    fw2, mb2, sig2, _ = make_package(key_pair[0], version=2)
    t2 = await svc.submit_installation(fw2, mb2, sig2)
    task2 = await _wait_terminal(db, t2)
    assert task2["state"] == "REJECTED"
    assert device.version == 5


@pytest.mark.asyncio
async def test_lost_receipt_goes_verifying_then_backfills_success(fresh_service, key_pair):
    svc, db, device = fresh_service
    device.lose_next = True
    fw, mb, sig, sha = make_package(key_pair[0], version=6)
    t = await svc.submit_installation(fw, mb, sig)

    # No receipt: task must be VERIFYING, never failed, slot stays locked.
    task = await _wait_state(db, t, "VERIFYING")
    assert "pending verification" in (task["device_response"] or "")
    # The device did activate, despite the lost receipt.
    assert device.version == 6 and device.sha256 == sha

    # A second installation is refused while verification is open.
    fw7, mb7, sig7, _ = make_package(key_pair[0], version=7)
    with pytest.raises(SlotBusyError) as exc:
        await svc.submit_installation(fw7, mb7, sig7)
    assert exc.value.task_id == t

    # Reconciliation queries the device and back-fills SUCCESS.
    await svc.reconcile()
    task = await db.get_task(t)
    assert task["state"] == "SUCCESS"

    # Slot is free again.
    t2 = await svc.submit_installation(fw7, mb7, sig7)
    await _wait_state(db, t2, "SUCCESS")


@pytest.mark.asyncio
async def test_reconcile_resends_original_package_when_not_installed(
    fresh_service, key_pair
):
    svc, db, device = fresh_service
    device.lose_next = True
    fw, mb, sig, sha = make_package(key_pair[0], version=8)
    t = await svc.submit_installation(fw, mb, sig)
    await _wait_state(db, t, "VERIFYING")

    # Simulate the activation NOT having happened (device still at v1).
    device.version = 1
    device.sha256 = "0" * 63 + "1"
    calls_before = device.install_calls

    await svc.reconcile()
    task = await _wait_terminal(db, t)
    assert task["state"] == "SUCCESS"
    # The original stored package was re-sent.
    assert device.install_calls == calls_before + 1
    assert device.version == 8 and device.sha256 == sha


@pytest.mark.asyncio
async def test_device_unreachable_keeps_verifying_not_failed(fresh_service, key_pair):
    svc, db, device = fresh_service
    # First status query succeeds (so dispatch proceeds); the install call
    # then finds the device offline and never gets a receipt.
    device.offline_after_status = True
    fw, mb, sig, _ = make_package(key_pair[0], version=9)
    t = await svc.submit_installation(fw, mb, sig)

    task = await _wait_state(db, t, "VERIFYING")
    assert task["state"] == "VERIFYING"

    # Repeated reconciliation while offline must never fail or free the slot.
    device.offline = True
    await svc.reconcile()
    await svc.reconcile()
    task = await db.get_task(t)
    assert task["state"] == "VERIFYING"
    with pytest.raises(SlotBusyError):
        fw10, mb10, sig10, _ = make_package(key_pair[0], version=10)
        await svc.submit_installation(fw10, mb10, sig10)

    # Device returns, reconciliation completes the original task.
    device.offline = False
    device.offline_after_status = False
    await svc.reconcile()
    task = await _wait_terminal(db, t)
    assert task["state"] == "SUCCESS"
    assert device.version == 9


@pytest.mark.asyncio
async def test_restart_recovery_preserves_open_task(fresh_service, key_pair, settings):
    """Simulate API process exit: a new service opens the same database."""
    svc, db, device = fresh_service
    device.lose_next = True
    fw, mb, sig, sha = make_package(key_pair[0], version=11)
    t = await svc.submit_installation(fw, mb, sig)
    await _wait_state(db, t, "VERIFYING")
    await db.close()

    # A brand-new process: new Database + service over the same SQLite file.
    from app.crypto_utils import load_public_key
    from app.db import Database
    from app.service import InstallService

    db2 = Database(settings.database_path)
    await db2.connect()
    svc2 = InstallService(
        settings, db2, load_public_key(settings.trusted_public_key_path)
    )
    svc2.device = device  # type: ignore[assignment]

    open_task = await db2.get_open_task()
    assert open_task is not None and open_task["id"] == t
    # Stored package is intact for an exact resend.
    assert open_task["firmware"] == fw

    await svc2.recover_on_startup()
    task = await db2.get_task(t)
    assert task["state"] == "SUCCESS"  # device had activated v11
    assert device.version == 11 and device.sha256 == sha
    await db2.close()


@pytest.mark.asyncio
async def test_restart_with_uninstalled_device_resends(fresh_service, key_pair, settings):
    svc, db, device = fresh_service
    device.lose_next = True
    fw, mb, sig, sha = make_package(key_pair[0], version=12)
    t = await svc.submit_installation(fw, mb, sig)
    await _wait_state(db, t, "VERIFYING")
    # Device never actually activated.
    device.version = 1
    device.sha256 = "0" * 63 + "1"
    await db.close()

    from app.crypto_utils import load_public_key
    from app.db import Database
    from app.service import InstallService

    db2 = Database(settings.database_path)
    await db2.connect()
    svc2 = InstallService(
        settings, db2, load_public_key(settings.trusted_public_key_path)
    )
    svc2.device = device  # type: ignore[assignment]
    await svc2.recover_on_startup()

    for _ in range(100):
        task = await db2.get_task(t)
        if task["state"] in ("SUCCESS", "REJECTED"):
            break
        await asyncio.sleep(0.02)
    assert task["state"] == "SUCCESS"
    assert device.version == 12 and device.sha256 == sha
    await db2.close()
