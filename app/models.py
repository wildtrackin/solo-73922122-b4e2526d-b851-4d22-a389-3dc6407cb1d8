"""Pydantic response models for the installation API."""
from __future__ import annotations

from datetime import datetime  # noqa: F401 (kept for openapi schema clarity)
from typing import Literal

from pydantic import BaseModel, Field

TaskState = Literal["PENDING", "DISPATCHED", "VERIFYING", "SUCCESS", "REJECTED"]


class TaskView(BaseModel):
    id: int
    state: TaskState
    model: str
    version: int = Field(..., description="positive integer firmware version")
    sha256: str
    reject_reason: str | None = None
    device_response: str | None = None
    created_at: str
    updated_at: str


class TaskCreated(BaseModel):
    task_id: int
    state: TaskState
    detail: str


class RejectedView(BaseModel):
    task_id: int
    state: Literal["REJECTED"]
    reject_reason: str


class HealthView(BaseModel):
    status: str
    open_task_id: int | None
