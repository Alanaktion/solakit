"""Portable records shared by storage, channels and execution."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


def now() -> datetime:
    return datetime.now(UTC)


class Origin(BaseModel):
    channel: str = "api"
    channel_id: str | None = None
    author_id: str | None = None
    external_id: str | None = None
    job: str | None = None


class Session(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    title: str = ""
    bound_channel: str | None = None
    default_profile: str = "interactive"
    memory_scope: Literal["shared", "isolated", "ephemeral"] = "shared"
    parent_session: str | None = None
    created_at: datetime = Field(default_factory=now)


class Message(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None
    origin: Origin
    run_id: str
    ts: datetime = Field(default_factory=now)

    def model_message(self) -> dict[str, Any]:
        return self.model_dump(
            include={"role", "content", "tool_calls", "tool_call_id"},
            exclude_none=True,
        )


class RunStatus(StrEnum):
    queued = "queued"
    running = "running"
    completed = "completed"
    model_unavailable = "model_unavailable"
    tool_error = "tool_error"
    failed = "failed"
    cancelled = "cancelled"
    interrupted = "interrupted"
    budget_exceeded = "budget_exceeded"
    missed = "missed"


class Run(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    session: str
    profile: str
    input_message: str | None = None
    origin: Origin
    status: RunStatus = RunStatus.queued
    created_at: datetime = Field(default_factory=now)
    finished_at: datetime | None = None
    prompt_sha256: str | None = None
    tokens: dict[str, int] | None = None
    steps: int = 0
    context_skipped_messages: int = 0
    error: str | None = None
