"""Operator API routes."""

import asyncio

from fastapi import APIRouter, HTTPException
from starlette.responses import JSONResponse

from solakit.api.dependencies import RuntimeDep
from solakit.core.config import Job
from solakit.jobs.schemas import JobUpdate

router = APIRouter()


@router.get("/jobs")
async def jobs(runtime: RuntimeDep) -> list[dict]:
    return runtime.jobs.status(definitions=True)


@router.get("/job-options")
async def job_options(runtime: RuntimeDep) -> dict:
    cfg = runtime.config
    return {
        "timezone": cfg.timezone,
        "profiles": sorted(p for p in cfg.profiles if p != "maintenance"),
        "channels": [c.id for c in cfg.channels],
        "tools": runtime.registry.catalog()["tools"],
    }


def job_error(exc, runtime):
    from solakit.jobs.schemas import JobToolsError

    if isinstance(exc, JobToolsError):
        return HTTPException(422, runtime.redactor.redact(str(exc)))
    if isinstance(exc, KeyError):
        return HTTPException(404, "Job not found")
    if isinstance(exc, (FileExistsError, RuntimeError)):
        return HTTPException(409, str(exc))
    return HTTPException(422, "Invalid job definition or references")


@router.post("/jobs", status_code=201)
async def job_create(runtime: RuntimeDep, job: Job) -> dict:
    try:
        return await runtime.jobs.create(job)
    except (ValueError, KeyError, FileExistsError, RuntimeError) as exc:
        raise job_error(exc, runtime) from exc


@router.get("/jobs/{job_id}")
async def job_get(runtime: RuntimeDep, job_id: str) -> dict:
    try:
        return runtime.jobs.get(job_id)
    except KeyError as exc:
        raise job_error(exc, runtime) from exc


@router.patch("/jobs/{job_id}")
async def job_update(runtime: RuntimeDep, job_id: str, changes: JobUpdate) -> dict:
    try:
        return await runtime.jobs.update(job_id, changes.model_dump(exclude_unset=True))
    except (ValueError, KeyError, RuntimeError) as exc:
        raise job_error(exc, runtime) from exc


@router.delete("/jobs/{job_id}")
async def job_delete(runtime: RuntimeDep, job_id: str) -> dict:
    try:
        return await runtime.jobs.delete(job_id)
    except (ValueError, KeyError, RuntimeError) as exc:
        raise job_error(exc, runtime) from exc


@router.post("/jobs/{job_id}/run")
async def job_run(
    runtime: RuntimeDep, job_id: str, wait: bool = True, admission: bool = False
):
    if job_id not in runtime.jobs.states:
        raise HTTPException(404, "Job not found")
    try:
        if admission:
            return JSONResponse(await runtime.jobs.trigger_run(job_id), status_code=202)
        if not wait:
            return JSONResponse(await runtime.jobs.trigger(job_id), status_code=202)
        run = await runtime.jobs.run_now(job_id)
        return await asyncio.to_thread(runtime.store.run, run.id)
    except KeyError as exc:
        raise HTTPException(404, "Job not found") from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/jobs/{job_id}/{operation}")
async def job_control(runtime: RuntimeDep, job_id: str, operation: str) -> dict:
    if job_id not in runtime.jobs.states or operation not in {"pause", "resume"}:
        raise HTTPException(404, "Job operation not found")
    try:
        await runtime.jobs.pause(job_id, operation == "pause")
    except KeyError as exc:
        raise HTTPException(404, "Job not found") from exc
    except ValueError as exc:
        raise HTTPException(409, "Job unavailable or scheduler shutting down") from exc
    return next(j for j in runtime.jobs.status() if j["id"] == job_id)
