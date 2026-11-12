"""Operator API routes."""

import asyncio
from typing import Literal

from fastapi import APIRouter, HTTPException
from starlette.responses import JSONResponse, Response

from solakit.api.dependencies import RuntimeDep
from solakit.api.schemas import (
    CreateSessionRequest,
    PinRequest,
    RunRequest,
    UpdateSessionRequest,
)
from solakit.core.compaction import ContextLimit
from solakit.core.schemas import Origin, SessionArchived
from solakit.core.session_lifecycle import (
    ArchiveBatch,
    LifecycleRequest,
)
from solakit.core.store import safe_id

router = APIRouter()


@router.get("/sessions/{session_id}/notes")
async def session_notes(runtime: RuntimeDep, session_id: str) -> dict:
    try:
        safe_id(session_id)
        notes = await asyncio.to_thread(runtime.store.session_notes, session_id)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(404, "Session not found") from exc
    return runtime.redactor.redact(notes)


@router.get("/sessions")
def sessions(
    runtime: RuntimeDep,
    status: Literal["active", "archived", "all"] = "active",
    q: str = "",
    before: str | None = None,
    profile: str | None = None,
    exclude_source: str | None = None,
    roots_only: bool = False,
    parent_session: str | None = None,
    limit: int = 50,
    offset: int = 0,
    page: bool = False,
    pin_channels: bool = False,
    pinned: bool | None = None,
):
    """pin_channels=true lists pinned sessions first; pinned=true|false filters.

    Channel-bound sessions are pinned unless the operator unpinned them."""
    channel_names: dict[str, list[str]] = {}
    for channel in runtime.config.channels:
        channel_names.setdefault(channel.session, []).append(channel.id)
    try:
        result = runtime.store.list_sessions(
            status=status,
            q=q,
            before=before,
            profile=profile,
            exclude_source=exclude_source,
            roots_only=roots_only,
            parent_session=parent_session,
            pinned_ids=tuple(channel_names),
            pinned=pinned,
            pin_first=pin_channels,
            limit=limit,
            offset=offset,
        )
        for session in result["sessions"]:
            session["channel_names"] = channel_names.get(session["id"], [])
        return result if page else result["sessions"]
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/sessions/{session_id}/pin")
def pin_session(runtime: RuntimeDep, session_id: str, request: PinRequest) -> dict:
    """Pin or unpin a session; null returns it to its default (channel-bound pinned)."""
    channels = [c.id for c in runtime.config.channels if c.session == session_id]
    try:
        safe_id(session_id)
        updated = runtime.store.set_session_pinned(session_id, request.pinned)
    except FileNotFoundError as exc:
        raise HTTPException(404, "Session not found") from exc
    except ValueError as exc:
        raise HTTPException(404, "Session not found") from exc
    result = updated.model_dump(mode="json")
    result["pinned"] = bool(channels) if updated.pinned is None else updated.pinned
    result["channel_names"] = channels
    return result


@router.post("/sessions/archive-batch")
async def archive_sessions(runtime: RuntimeDep, request: ArchiveBatch):
    try:
        return await runtime.session_lifecycle.batch(request)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/sessions/restore-batch")
async def restore_sessions(runtime: RuntimeDep, request: ArchiveBatch):
    try:
        return await runtime.session_lifecycle.batch(request, archived=False)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/sessions/{session_id}/lifecycle")
async def session_lifecycle(
    runtime: RuntimeDep, session_id: str, request: LifecycleRequest
):
    try:
        return await runtime.session_lifecycle.change(session_id, request.archived)
    except FileNotFoundError as exc:
        raise HTTPException(404, "Session not found") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/sessions", status_code=201)
def create_session(
    runtime: RuntimeDep, session: CreateSessionRequest, response: Response
) -> dict:
    try:
        safe_id(session.id)
        store = runtime.store
        with store.lock:
            try:
                existing = store.session(session.id)
            except FileNotFoundError:
                if session.archived_at is not None:
                    raise ValueError(
                        "Use the session lifecycle endpoint to archive a session"
                    ) from None
                if session.default_profile not in runtime.config.profiles:
                    raise ValueError("Unknown profile") from None
                if session.memory_scope == "isolated":
                    raise ValueError("Isolated memory is not implemented") from None
                if session.bound_channel is not None:
                    raise ValueError(
                        "Channel bindings are controlled by configuration"
                    ) from None
                return store.create_session(session).model_dump(mode="json")
            exclude = (
                set() if "created_at" in session.model_fields_set else {"created_at"}
            )
            if existing.model_dump(exclude=exclude) != session.model_dump(
                exclude=exclude
            ):
                raise HTTPException(
                    409, "Session ID already exists with different fields"
                )
            response.status_code = 200
            return existing.model_dump(mode="json")
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.patch("/sessions/{session_id}")
def update_session(
    runtime: RuntimeDep, session_id: str, request: UpdateSessionRequest
) -> dict:
    try:
        return runtime.store.rename_session(session_id, request.title).model_dump(
            mode="json"
        )
    except FileNotFoundError as exc:
        raise HTTPException(404, "Session not found") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/sessions/{session_id}")
def session(runtime: RuntimeDep, session_id: str) -> dict:
    try:
        loaded = runtime.store.session(session_id)
        metadata = loaded.model_dump(mode="json")
        channels = [c.id for c in runtime.config.channels if c.session == session_id]
        return {
            **metadata,
            "pinned": bool(channels) if loaded.pinned is None else loaded.pinned,
            "channel_names": channels,
            "messages": runtime.store.transcript(session_id),
            "archives": runtime.store.archives(session_id),
            "context_started_at": runtime.store.context_started_at(session_id),
        }
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(404, "Session not found") from exc


@router.post("/sessions/{session_id}/compact")
async def compact_session(runtime: RuntimeDep, session_id: str) -> dict:
    try:
        safe_id(session_id)
    except ValueError as exc:
        raise HTTPException(404, "Session not found") from exc
    try:
        return await runtime.execution.compact_session(session_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, "Session not found") from exc
    except (ValueError, ContextLimit) as exc:
        raise HTTPException(409, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(
            503, "Compaction storage failure; inspect and restart for recovery"
        ) from exc


@router.post("/sessions/{session_id}/archive")
async def archive_session(runtime: RuntimeDep, session_id: str) -> dict:
    try:
        safe_id(session_id)
    except ValueError as exc:
        raise HTTPException(404, "Session not found") from exc
    try:
        return await runtime.execution.archive_session(session_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, "Session not found") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(
            503, "Archival could not finish; check storage and restart for recovery"
        ) from exc


@router.post("/sessions/{session_id}/messages")
async def message(
    runtime: RuntimeDep,
    session_id: str,
    request: RunRequest,
    wait: bool = True,
    stream: bool = False,
):
    try:
        pending = await runtime.execution.enqueue(
            session_id,
            request.message,
            profile=request.profile,
            origin=Origin(channel="api", channel_id=session_id),
            stream=stream,
        )
        if not wait:
            return JSONResponse(pending.run.model_dump(mode="json"), status_code=202)
        run = await asyncio.shield(pending.future)
        return await asyncio.to_thread(runtime.store.run, run.id)
    except SessionArchived as exc:
        raise HTTPException(409, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, "Session not found") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/sessions/{session_id}/archives/{number}")
def archive(runtime: RuntimeDep, session_id: str, number: int) -> list[dict]:
    try:
        return runtime.store.archive(session_id, number)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(404, "Archive not found") from exc
