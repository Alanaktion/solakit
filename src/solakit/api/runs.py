"""Operator API routes."""

import asyncio

from fastapi import APIRouter, HTTPException, Query

from solakit.api.dependencies import RuntimeDep
from solakit.core.memory import MemoryUnavailable
from solakit.core.memory_ops import refresh_run

router = APIRouter()


@router.get("/runs")
def runs(
    runtime: RuntimeDep,
    session_id: str | None = None,
    include_archived: bool = False,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    status: str | None = None,
    descending: bool = False,
) -> list[dict]:
    return runtime.store.list_runs(
        session_id,
        include_archived,
        limit=limit,
        offset=offset,
        status=status,
        descending=descending,
    )


@router.get("/runs/{run_id}")
async def run_record(
    runtime: RuntimeDep,
    run_id: str,
    live: bool = False,
    view: bool = False,
    messages: bool = True,
) -> dict:
    try:
        record = await asyncio.to_thread(
            runtime.store.run, run_id, view=view, include_messages=messages
        )
        if live:
            pending = runtime.execution.pending.get(run_id)
            preview = pending.preview if pending else None
            record["preview"] = (
                {
                    **preview,
                    "content": runtime.redactor.preview(preview["content"]),
                }
                if preview
                else None
            )
        return record
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(404, "Run not found") from exc


@router.post("/runs/{run_id}/memory/refresh")
async def refresh_memory(runtime: RuntimeDep, run_id: str) -> dict:
    try:
        run = await refresh_run(runtime.execution, run_id)
        return run.model_dump(mode="json")
    except FileNotFoundError as exc:
        raise HTTPException(404, "Run not found") from exc
    except (ValueError, MemoryUnavailable) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/runs/{run_id}/cancel")
async def cancel(runtime: RuntimeDep, run_id: str) -> dict:
    try:
        await asyncio.to_thread(runtime.store.run, run_id)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(404, "Run not found") from exc
    await runtime.execution.cancel(run_id)
    return await asyncio.to_thread(runtime.store.run, run_id)
