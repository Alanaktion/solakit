"""Explicit operator operations: extraction inspection and scratch live verification."""

import asyncio
from uuid import uuid4

from solakit.core.io import durable
from solakit.core.loop import Execution
from solakit.core.memory import MemoryUnavailable
from solakit.core.schemas import Run, RunStatus, Session
from solakit.core.store import StorageCorruption


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
    if (
        record["memory_status"] == run.memory_status
        and record.get("memory_commit") == result
    ):
        return run
    return await durable(
        execution.store.update_run,
        run_id,
        {"memory_status": run.memory_status, "memory_commit": result},
    )


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
    passed = (
        second.status == RunStatus.completed
        and expected in (evidence["prompt"] or "")
        and expected in (evidence.get("recall") or "")
    )
    return {
        **result,
        "second_run": second.id,
        "passed": passed,
        "reason": "Expected fact present in next persisted prompt"
        if passed
        else "Expected fact absent from next persisted prompt",
    }


async def refresh_pending(execution: Execution):
    """Bounded read-only polling; never retries message appends or commits."""
    for record in await durable(execution.store.pending_memory):
        try:
            await refresh_run(execution, record["id"])
        except StorageCorruption:
            continue  # Storage health reports the damaged evidence; never rewrite it.
        except MemoryUnavailable:
            if record["memory_status"] != "status_unavailable":
                await durable(
                    execution.store.update_run,
                    record["id"],
                    {"memory_status": "status_unavailable"},
                )
            break  # One unavailable endpoint should not create a retry storm.


async def watch_memory(execution: Execution):
    while True:
        await refresh_pending(execution)
        await asyncio.sleep(5)
