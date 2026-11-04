"""Explicit operator operations: extraction inspection and scratch live verification."""

from uuid import uuid4

from solakit.core.io import durable
from solakit.core.loop import Execution
from solakit.core.schemas import Run, RunStatus, Session


async def refresh_run(execution: Execution, run_id: str, *, wait_s: float = 0) -> Run:
    record = await durable(execution.store.run, run_id)
    run = Run.model_validate(record)
    if run.status in {RunStatus.running, RunStatus.queued}:
        raise ValueError("Wait for run finalization before refreshing extraction")
    if execution.memory is None:
        raise ValueError("Memory is disabled")
    session = await durable(execution.store.session, run.session)
    result = await execution.memory.scoped(session).refresh(
        execution.store, run_id, wait_s=wait_s
    )
    run.memory_status = result["status"]
    run.memory_commit = result
    await durable(execution.store.save_run, run)
    return run


async def probe(execution: Execution, fact: str, query: str, expected: str) -> dict:
    """Only an explicit operator request writes this scratch fact to live memory."""
    if execution.memory is None:
        raise ValueError("Enable/configure the existing OpenViking instance first")
    session = await durable(
        execution.store.create_session,
        Session(id=f"probe-{uuid4().hex}", title="Memory verification"),
    )
    first = await execution.submit(session.id, fact)
    result = {"session": session.id, "first_run": first.id, "passed": False}
    if first.status != RunStatus.completed or first.memory_status != "accepted":
        return {**result, "reason": "Initial run did not acknowledge a memory commit"}
    refreshed = await refresh_run(
        execution, first.id, wait_s=execution.config.memory.timeout_s
    )
    if refreshed.memory_status != "completed":
        return {**result, "reason": f"Extraction is {refreshed.memory_status}"}
    second = await execution.submit(session.id, query)
    evidence = await durable(execution.store.run, second.id)
    passed = second.status == RunStatus.completed and expected in (
        evidence["prompt"] or ""
    )
    return {
        **result,
        "second_run": second.id,
        "passed": passed,
        "reason": "Expected fact present in next persisted prompt"
        if passed
        else "Expected fact absent from next persisted prompt",
    }
