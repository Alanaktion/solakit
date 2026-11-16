import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from solakit import records
from solakit.records import (
    Delivery,
    DeliveryState,
    ErrorCode,
    Failure,
    InvalidTransition,
    MemoryCommit,
    MemoryState,
    Origin,
    Run,
    RunState,
)

NOW = datetime(2026, 11, 16, 12, tzinfo=UTC)
IDENTITY = "1" * 32


def queued() -> Run:
    return Run(
        id="2" * 32,
        created_at=NOW,
        origin=Origin.INTERACTIVE,
        session_id=IDENTITY,
        request_id="3" * 32,
        profile="default",
    )


@pytest.mark.parametrize("state", list(RunState))
@pytest.mark.parametrize("target", list(RunState))
def test_run_transition_matrix(state: RunState, target: RunState) -> None:
    run = queued()
    if state == RunState.RUNNING:
        run = run.transition(state, at=NOW)
    elif state == RunState.COMPLETED:
        run = run.transition(RunState.RUNNING, at=NOW).transition(state, at=NOW)
    elif state != RunState.QUEUED:
        run = run.transition(state, at=NOW)
    if target in records.RUN_TRANSITIONS.get(state, ()):
        changed = run.transition(target, at=NOW)
        assert changed.state == target and run.state == state
    else:
        with pytest.raises(InvalidTransition):
            run.transition(target, at=NOW)


def test_run_outcome_and_validation() -> None:
    reason = Failure(code=ErrorCode.TOOL_DENIED, message="fixture denial")
    failed = queued().transition(RunState.FAILED, at=NOW, reason=reason)
    assert failed.reason == reason and failed.started_at is None
    with pytest.raises(InvalidTransition):
        failed.transition(RunState.FAILED, at=NOW)
    with pytest.raises(ValidationError):
        queued().transition(RunState.RUNNING, at=NOW - timedelta(seconds=1))
    with pytest.raises(ValidationError):
        Run.model_validate(queued().model_dump() | {"origin": Origin.PROACTIVE})
    with pytest.raises(ValidationError):
        Run.model_validate(queued().model_dump() | {"request_id": queued().id})
    proactive = Run.model_validate(
        queued().model_dump()
        | {"origin": Origin.PROACTIVE, "job_id": "4" * 32, "occurrence_id": "5" * 32}
    )
    assert proactive.transition(RunState.RUNNING, at=NOW).origin == Origin.PROACTIVE


@pytest.mark.parametrize(
    "bad_id", ["../escape", "/tmp/x", "..", "a" * 33, "G" * 32, "a" * 32 + "\n"]
)
def test_path_ids(bad_id: str) -> None:
    with pytest.raises(ValidationError):
        Run.model_validate(queued().model_dump() | {"session_id": bad_id})


def test_utc_version_and_strict_fields() -> None:
    for changes in (
        {"created_at": NOW.replace(tzinfo=None)},
        {"created_at": datetime.fromisoformat("2026-11-16T12:00:00+02:00")},
        {"schema_version": 2},
        {"schema_version": True},
        {"schema_version": 1.0},
        {"unexpected": "value"},
    ):
        with pytest.raises(ValidationError):
            Run.model_validate(queued().model_dump() | changes)


def test_independent_lifecycles() -> None:
    completed = queued().transition(RunState.RUNNING, at=NOW).transition(RunState.COMPLETED, at=NOW)
    memory = MemoryCommit(origin=Origin.INTERACTIVE, session_id=IDENTITY, run_id="2" * 32)
    delivery = Delivery(
        origin=Origin.INTERACTIVE, session_id=IDENTITY, run_id="2" * 32, message_id="6" * 32
    )
    assert completed.state == RunState.COMPLETED
    assert memory.state == MemoryState.PENDING and delivery.state == DeliveryState.PENDING
    memory = memory.transition(MemoryState.RUNNING).transition(MemoryState.COMPLETED)
    delivery = delivery.transition(DeliveryState.SENDING).transition(
        DeliveryState.UNCERTAIN,
        reason=Failure(code=ErrorCode.EFFECT_UNCERTAIN, message="connection lost"),
    )
    with pytest.raises(InvalidTransition):
        memory.transition(MemoryState.RUNNING)
    with pytest.raises(InvalidTransition):
        delivery.transition(DeliveryState.SENDING)


def test_failed_tool_preserves_link() -> None:
    receipt = records.ToolReceipt(
        origin=Origin.INTERACTIVE,
        session_id=IDENTITY,
        run_id="2" * 32,
        tool_call_id="7" * 32,
        status="failed",
        failure=Failure(code=ErrorCode.TOOL_FAILED, message="fixture failure"),
    )
    assert records.ToolReceipt.model_validate_json(receipt.model_dump_json()) == receipt
    with pytest.raises(ValidationError):
        records.ToolReceipt.model_validate(receipt.model_dump() | {"tool_call_id": None})
    with pytest.raises(ValidationError):
        records.ToolReceipt.model_validate(receipt.model_dump() | {"effect": "uncertain"})


def test_nonfinite_values_are_not_json_evidence() -> None:
    with pytest.raises(ValidationError):
        records.ToolReceipt(
            origin=Origin.INTERACTIVE,
            session_id=IDENTITY,
            run_id="2" * 32,
            tool_call_id="7" * 32,
            status="succeeded",
            result={"invalid": float("nan")},
        )


def test_record_and_event_fixtures() -> None:
    root = Path(__file__).parents[1] / "docs/contracts/examples"
    for name, raw in json.loads((root / "records.json").read_text()).items():
        model = getattr(records, name)
        parsed = model.model_validate_json(json.dumps(raw))
        assert parsed.model_dump(mode="json") == raw
        assert model.model_json_schema()["properties"]["schema_version"]["const"] == 1
    events = [
        records.DurableEvent.model_validate_json(line)
        for line in (root / "events.jsonl").read_text().splitlines()
    ]
    assert [event.sequence for event in events] == [1, 2, 3]
    assert events[-1].type == "run.terminal"
    assert len({event.event_id for event in events}) == 3


def test_phase_three_model_steps_remain_readable() -> None:
    root = Path(__file__).parents[1] / "docs/contracts/examples"
    raw = json.loads((root / "records.json").read_text())["ModelStep"]
    for key in ("request_id", "response", "usage"):
        raw.pop(key)
    step = records.ModelStep.model_validate_json(json.dumps(raw))
    assert step.request_id is None and step.response is None and step.usage is None
