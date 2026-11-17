"""Versioned runtime values, independent of transport and provider SDKs."""

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import uuid4

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    model_validator,
)


def new_id() -> str:
    return uuid4().hex


def utc_now() -> datetime:
    return datetime.now(UTC)


def _utc(value: datetime) -> datetime:
    if value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be aware UTC")
    return value


RecordID = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
Timestamp = Annotated[datetime, AfterValidator(_utc)]
ProviderID = Annotated[str, Field(min_length=1, max_length=256)]


def _version(value: object) -> object:
    if type(value) is not int:
        raise ValueError("schema version must be an integer")
    return value


SchemaVersion = Annotated[Literal[1], BeforeValidator(_version)]


class Value(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)


class Origin(StrEnum):
    INTERACTIVE = "interactive"
    SCHEDULED = "scheduled"
    PROACTIVE = "proactive"
    SYSTEM = "system"


class SessionMode(StrEnum):
    SHARED = "shared"
    ISOLATED = "isolated"
    EPHEMERAL = "ephemeral"


class ErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    CONFIGURATION = "configuration"
    BUDGET_EXCEEDED = "budget_exceeded"
    CONTEXT_LIMIT = "context_limit"
    MODEL_UNAVAILABLE = "model_unavailable"
    MODEL_MISMATCH = "model_mismatch"
    INVALID_MODEL_RESPONSE = "invalid_model_response"
    TOOL_DENIED = "tool_denied"
    TOOL_FAILED = "tool_failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    PROCESS_INTERRUPTED = "process_interrupted"
    EFFECT_UNCERTAIN = "effect_uncertain"
    MEMORY_UNAVAILABLE = "memory_unavailable"
    STORAGE_FAILED = "storage_failed"
    CORRUPTION = "corruption"


class Failure(Value):
    code: ErrorCode
    message: str


class Record(Value):
    schema_version: SchemaVersion = 1
    id: RecordID = Field(default_factory=new_id)
    created_at: Timestamp = Field(default_factory=utc_now)
    origin: Origin


class Session(Record):
    mode: SessionMode = SessionMode.SHARED
    title: str = ""
    archived: bool = False
    pinned: bool = False


class Message(Record):
    session_id: RecordID
    run_id: RecordID
    role: Literal["user", "assistant", "tool"]
    content: str
    tool_call_id: RecordID | None = None

    @model_validator(mode="after")
    def tool_link(self) -> Self:
        if (self.role == "tool") != (self.tool_call_id is not None):
            raise ValueError("tool messages require a tool_call_id; other roles cannot carry one")
        return self


class RunState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


RUN_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.QUEUED: frozenset(
        (RunState.RUNNING, RunState.CANCELLED, RunState.INTERRUPTED, RunState.FAILED)
    ),
    RunState.RUNNING: frozenset(
        (RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED, RunState.INTERRUPTED)
    ),
}


class InvalidTransition(ValueError):
    pass


class Run(Record):
    session_id: RecordID
    request_id: RecordID
    profile: str
    state: RunState = RunState.QUEUED
    reason: Failure | None = None
    started_at: Timestamp | None = None
    ended_at: Timestamp | None = None
    job_id: RecordID | None = None
    occurrence_id: RecordID | None = None
    retry_of: RecordID | None = None

    @model_validator(mode="after")
    def lifecycle(self) -> Self:
        scheduled = self.origin in (Origin.SCHEDULED, Origin.PROACTIVE)
        if scheduled != (self.job_id is not None and self.occurrence_id is not None):
            raise ValueError("scheduled/proactive runs require both job and occurrence lineage")
        if not scheduled and (self.job_id is not None or self.occurrence_id is not None):
            raise ValueError("only scheduled/proactive runs carry schedule lineage")
        terminal = self.state not in RUN_TRANSITIONS
        if terminal != (self.ended_at is not None):
            raise ValueError("only terminal runs require ended_at")
        if self.state == RunState.QUEUED and self.started_at is not None:
            raise ValueError("queued runs cannot have started_at")
        if self.state in (RunState.RUNNING, RunState.COMPLETED) and self.started_at is None:
            raise ValueError("running/completed runs require started_at")
        if self.started_at is not None and self.started_at < self.created_at:
            raise ValueError("run cannot start before admission")
        if self.ended_at is not None and self.ended_at < (self.started_at or self.created_at):
            raise ValueError("run cannot end before admission/start")
        if not terminal and self.reason is not None:
            raise ValueError("outcome reason belongs to a terminal run")
        if self.retry_of == self.id or self.request_id == self.id:
            raise ValueError("run, request, and retry identities must be distinct")
        return self

    def transition(self, state: RunState, *, at: datetime, reason: Failure | None = None) -> Self:
        if state not in RUN_TRANSITIONS.get(self.state, ()):
            raise InvalidTransition(f"run cannot transition from {self.state} to {state}")
        changes: dict[str, object] = {"state": state, "reason": reason}
        if state == RunState.RUNNING:
            changes["started_at"] = at
        else:
            changes["ended_at"] = at
        return type(self).model_validate(self.model_dump() | changes)


class Artifact(Value):
    path: Annotated[str, Field(pattern=r"^artifacts/[0-9a-f]{32}$")]
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    length: Annotated[int, Field(ge=0)]


class ModelRequest(Record):
    session_id: RecordID
    run_id: RecordID
    index: Annotated[int, Field(ge=1)]
    model: Annotated[str, Field(min_length=1)]
    body: Artifact
    manifest: Artifact


class ToolProposal(Value):
    provider_tool_call_id: ProviderID
    name: str = Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_.-]+$")
    arguments: str


class ModelStep(Record):
    session_id: RecordID
    run_id: RecordID
    index: Annotated[int, Field(ge=1)]
    provider_call_id: ProviderID | None = None
    model: Annotated[str, Field(min_length=1)]
    finish_reason: str | None = None
    content: str = ""
    reasoning: str = ""
    failure: Failure | None = None
    request_id: RecordID | None = None
    response: Artifact | None = None
    usage: dict[str, JsonValue] | None = None
    tool_calls: tuple[ToolProposal, ...] = ()
    purpose: Literal["conversation", "compaction"] = "conversation"


class RecoveryNote(Record):
    session_id: RecordID
    run_id: RecordID
    model_step_id: RecordID
    kind: Literal["argument_repair", "schema_disclosure", "narration"]
    content: str


class ToolCall(Record):
    session_id: RecordID
    run_id: RecordID
    model_step_id: RecordID
    provider_tool_call_id: ProviderID
    name: Annotated[str, Field(min_length=1)]
    arguments: dict[str, JsonValue]


class ToolReceipt(Record):
    session_id: RecordID
    run_id: RecordID
    tool_call_id: RecordID
    status: Literal["succeeded", "failed", "denied", "cancelled", "uncertain"]
    effect: Literal["none", "confirmed", "uncertain"] = "none"
    result: JsonValue = None
    artifact: Artifact | None = None
    truncated: bool = False
    result_bytes: Annotated[int, Field(ge=0)] = 0
    result_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None
    failure: Failure | None = None

    @model_validator(mode="after")
    def outcome(self) -> Self:
        if (self.status == "succeeded") != (self.failure is None):
            raise ValueError("non-success receipts require failure details")
        if (self.status == "uncertain") != (self.effect == "uncertain"):
            raise ValueError("uncertain effects require uncertain receipts")
        if self.status == "denied" and self.effect != "none":
            raise ValueError("denied tools cannot have effects")
        return self


class Job(Record):
    profile: str
    task: str
    schedule: Annotated[str, Field(min_length=1)]
    timezone: str = "America/Denver"
    session_id: RecordID | None = None
    mode: SessionMode = SessionMode.SHARED
    enabled: bool = False
    version: Annotated[int, Field(ge=1)] = 1
    generation: Annotated[int, Field(ge=1)] = 1
    paused: bool = False
    deleted: bool = False
    next_due: Timestamp | None = None
    consumed: bool = False
    tools: tuple[str, ...] | None = None
    workspace_roots: tuple[str, ...] | None = None
    budgets: dict[str, int] = Field(default_factory=dict)
    delivery: Literal["session", "none"] = "session"

    @model_validator(mode="after")
    def target(self) -> Self:
        if (self.mode == SessionMode.EPHEMERAL) != (self.session_id is None):
            raise ValueError("jobs require a bound topic or an ephemeral target")
        return self


class Occurrence(Record):
    job_id: RecordID
    due_at: Timestamp
    state: Literal["pending", "claimed", "completed", "failed", "skipped", "uncertain"] = "pending"
    run_id: RecordID | None = None
    reason: Failure | None = None
    generation: Annotated[int, Field(ge=1)] = 1
    trigger: Literal["scheduled", "manual"] = "scheduled"
    definition: dict[str, JsonValue] = Field(default_factory=dict)
    local_slot: str = ""
    coalesced_from: Timestamp | None = None
    decision: Literal["pending", "no_action", "work", "message", "failed", "skipped"] = "pending"
    explanation: str = ""


class SchedulerControl(Record):
    paused: bool = False


class DeliveryState(StrEnum):
    PENDING = "pending"
    SENDING = "sending"
    DELIVERED = "delivered"
    FAILED = "failed"
    UNCERTAIN = "uncertain"
    CANCELLED = "cancelled"


class MemoryState(StrEnum):
    ACCEPTED = "accepted"
    AMBIGUOUS = "ambiguous"
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class Delivery(Record):
    session_id: RecordID
    run_id: RecordID
    message_id: RecordID
    state: DeliveryState = DeliveryState.PENDING
    reason: Failure | None = None
    content: str | None = None
    not_before: Timestamp | None = None
    expires_at: Timestamp | None = None

    def transition(self, state: DeliveryState, *, reason: Failure | None = None) -> Self:
        allowed = {
            DeliveryState.PENDING: (DeliveryState.SENDING, DeliveryState.CANCELLED),
            DeliveryState.SENDING: (
                DeliveryState.DELIVERED,
                DeliveryState.FAILED,
                DeliveryState.UNCERTAIN,
            ),
        }
        if state not in allowed.get(self.state, ()):
            raise InvalidTransition(f"delivery cannot transition from {self.state} to {state}")
        return type(self).model_validate(self.model_dump() | {"state": state, "reason": reason})


class MemoryCommit(Record):
    session_id: RecordID
    run_id: RecordID
    state: MemoryState = MemoryState.PENDING
    reason: Failure | None = None
    provider_commit_id: ProviderID | None = None
    payload: tuple[dict[str, JsonValue], ...] = ()
    payload_sha256: str = ""
    source_ids: tuple[RecordID, ...] = ()
    note_ids: tuple[RecordID, ...] = ()
    scope: dict[str, JsonValue] = Field(default_factory=dict)
    attempts: Annotated[int, Field(ge=0)] = 0
    next_check: Timestamp = Field(default_factory=utc_now)

    def transition(self, state: MemoryState, *, reason: Failure | None = None) -> Self:
        allowed = {
            MemoryState.PENDING: (
                MemoryState.PENDING,
                MemoryState.RUNNING,
                MemoryState.SKIPPED,
                MemoryState.FAILED,
            ),
            MemoryState.RUNNING: (
                MemoryState.ACCEPTED,
                MemoryState.AMBIGUOUS,
                MemoryState.COMPLETED,
                MemoryState.FAILED,
            ),
            MemoryState.AMBIGUOUS: (MemoryState.ACCEPTED, MemoryState.FAILED),
            MemoryState.FAILED: (MemoryState.ACCEPTED,),
            MemoryState.ACCEPTED: (
                MemoryState.ACCEPTED,
                MemoryState.COMPLETED,
                MemoryState.FAILED,
            ),
        }
        if state not in allowed.get(self.state, ()):
            raise InvalidTransition(f"memory cannot transition from {self.state} to {state}")
        return type(self).model_validate(self.model_dump() | {"state": state, "reason": reason})


class MemoryNote(Record):
    session_id: RecordID
    run_id: RecordID
    tool_call_id: RecordID
    content: Annotated[str, Field(min_length=1, max_length=2000)]


class MemoryRecall(Record):
    session_id: RecordID
    run_id: RecordID
    query: str
    scope: dict[str, JsonValue] = Field(default_factory=dict)
    status: Literal["ok", "degraded", "disabled"]
    sources: tuple[dict[str, JsonValue], ...] = ()
    elapsed_seconds: float = 0


class CompactionBoundary(Record):
    session_id: RecordID
    run_id: RecordID
    through_message_id: RecordID
    summary_message_id: RecordID
    source_message_ids: Annotated[tuple[RecordID, ...], Field(min_length=1)]
    state: Literal["prepared", "committed"] = "committed"
    manifest: Artifact | None = None


class DurableEvent(Value):
    schema_version: SchemaVersion = 1
    event_id: RecordID = Field(default_factory=new_id)
    run_id: RecordID
    session_id: RecordID
    sequence: Annotated[int, Field(ge=1)]
    timestamp: Timestamp = Field(default_factory=utc_now)
    type: Literal[
        "run.admitted",
        "run.started",
        "run.terminal",
        "message.appended",
        "model.recorded",
        "model.requested",
        "compaction.recorded",
        "tool.intent",
        "tool.receipt",
        "memory.changed",
        "delivery.changed",
        "recovery.noted",
    ]
    payload: dict[str, JsonValue]


class TokenPreview(Value):
    """Ephemeral, lossy, and deliberately outside the durable sequence."""

    run_id: RecordID
    text: str
