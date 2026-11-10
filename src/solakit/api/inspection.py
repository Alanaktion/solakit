"""Operator API routes."""

import asyncio
from datetime import datetime

from fastapi import APIRouter, HTTPException
from starlette.responses import Response

from solakit.api.dependencies import RuntimeDep
from solakit.api.schemas import (
    MemoryProbeRequest,
)
from solakit.core.media import Media, MediaError
from solakit.core.memory import MemoryUnavailable
from solakit.core.memory_ops import probe
from solakit.core.prompt import inspection, read_turn
from solakit.core.schemas import now
from solakit.core.search import SearchUnavailable

router = APIRouter()


@router.get("/media/{digest}")
async def image_media(runtime: RuntimeDep, digest: str):
    try:
        payload = await asyncio.to_thread(Media(runtime.store.root).read_hash, digest)
    except MediaError as exc:
        raise HTTPException(404, "Image unavailable") from exc
    return Response(
        payload,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/channels")
async def channels(runtime: RuntimeDep) -> list[dict]:
    return runtime.channels.status()


@router.get("/prompt")
async def prompt(
    runtime: RuntimeDep,
    profile: str = "interactive",
    session: str = "inspection",
    recall: str | None = None,
    at: datetime | None = None,
    origin: str = "api",
    run_id: str | None = None,
    job: str | None = None,
    step: int | None = None,
) -> dict:
    if run_id is not None:
        try:
            record = await asyncio.to_thread(runtime.store.run, run_id)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(404, "Run not found") from exc
        if record.get("prompt") is None or record.get("tool_schemas") is None:
            raise HTTPException(
                409, "Prompt or tool schema snapshot was not recorded for this run"
            )
        schemas = record["tool_schemas"]
        view = record.get("tool_view")
        if step is not None:
            snapshots = (view or {}).get("schema_steps", [])
            snapshot = next((s for s in snapshots if s["step"] == step), None)
            if snapshot is None:
                raise HTTPException(404, "Schema step not recorded")
            schemas = [record["tool_catalog"][name] for name in snapshot["visible"]]
        return {
            **inspection(
                record["prompt"],
                schemas,
                record.get("prompt_fragments"),
                record.get("turn_context"),
            ),
            "tool_view": view,
            "schema_step": step,
            "tool_catalog_sha256": record.get("tool_catalog_sha256"),
            "run_id": run_id,
            "profile": record["profile"],
            "session": record["session"],
            "effective_tools": record["effective_tools"],
            "tool_schema_sha256": record["tool_schema_sha256"],
        }
    try:
        if job is not None:
            try:
                definition = runtime.jobs.job(job)
            except KeyError as exc:
                raise HTTPException(404, "Job not found") from exc
            profile = definition.profile
            view = runtime.jobs.tool_view(definition)
        else:
            view = runtime.registry.view(profile)
        result, turn = await asyncio.to_thread(
            read_turn,
            runtime.config,
            profile=profile,
            session=session,
            origin=origin,
            at=at or now(),
            recall=recall,
            tool_index=view.index_text,
        )
        return {
            **inspection(result, view.schemas(), turn=turn),
            "tool_view": view.metadata(),
            "effective_tools": list(view.effective),
        }
    except (OSError, ValueError) as exc:
        raise HTTPException(422, "Prompt files or profile are invalid") from exc


@router.get("/storage/health")
def storage_health(runtime: RuntimeDep) -> dict:
    return runtime.store.storage_health()


@router.get("/search")
def search(
    runtime: RuntimeDep,
    query: str,
    session_id: str | None = None,
    role: str | None = None,
    kind: str | None = None,
    after: str | None = None,
    before: str | None = None,
    limit: int = 50,
) -> list[dict]:
    try:
        results = runtime.store.search(
            query,
            session_id=session_id,
            role=role,
            kind=kind,
            after=after,
            before=before,
            limit=limit,
        )
        return runtime.redactor.redact(results)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    except SearchUnavailable as exc:
        raise HTTPException(503, str(exc)) from None


@router.get("/tools")
async def tools(runtime: RuntimeDep) -> dict:
    catalog = runtime.registry.catalog()
    catalog["jobs"] = {
        job.id: runtime.jobs.get(job.id)["tool_view"]
        for job in runtime.jobs.jobs.values()
    }
    return runtime.redactor.redact(catalog)


@router.get("/stats/model")
async def stats_model(runtime: RuntimeDep, window: int = 100) -> dict:
    from solakit.core import doctor
    from solakit.core.stats import model_stats

    if not 1 <= window <= 1000:
        raise HTTPException(422, "window must be between 1 and 1000")
    stats = await asyncio.to_thread(model_stats, runtime.store, window)
    stats["doctor"] = doctor.last(runtime.config)
    return runtime.redactor.redact(stats)


@router.get("/playbooks")
async def list_playbooks(runtime: RuntimeDep) -> list[dict]:
    from solakit.core import playbooks

    cfg = runtime.config
    jobs = [j for j in runtime.jobs.jobs.values()]
    result = []
    for name in playbooks.available(cfg):
        try:
            item = playbooks.load(cfg, name)
            entry = {
                "name": name,
                "summary": item.summary,
                "tools": list(item.tools),
                "error": None,
            }
        except playbooks.PlaybookError as exc:
            entry = {"name": name, "summary": "", "tools": [], "error": str(exc)}
        entry["profiles"] = sorted(
            p for p, c in cfg.profiles.items() if name in c.playbooks
        )
        entry["jobs"] = sorted(j.id for j in jobs if j.playbook == name)
        result.append(entry)
    return result


@router.get("/profiles")
async def profiles(runtime: RuntimeDep) -> list[str]:
    return sorted(runtime.config.profiles)


@router.post("/memory/probe")
async def memory_probe(runtime: RuntimeDep, request: MemoryProbeRequest) -> dict:
    try:
        return await probe(
            runtime.execution, request.fact, request.query, request.expected
        )
    except (ValueError, MemoryUnavailable) as exc:
        raise HTTPException(409, str(exc)) from exc
