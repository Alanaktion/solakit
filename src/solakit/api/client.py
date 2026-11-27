"""Bounded operator views over the same headless services used by tools and jobs."""

import asyncio
import re
from collections.abc import AsyncGenerator
from pathlib import PurePath
from typing import Any

from pydantic import JsonValue

from solakit.adapters.model import DOCUMENT_EXTENSIONS, ModelError
from solakit.api.auth import AuthService, Login, RequestError
from solakit.config import EffectiveProfile
from solakit.engine.prompts import PromptInput, assemble, inspect_prompt
from solakit.records import (
    DOCUMENT_TEXT_BYTES,
    Artifact,
    CompactionBoundary,
    ErrorCode,
    Message,
    ModelRequest,
    Origin,
    Record,
    Run,
    SessionMode,
    ToolCall,
    ToolReceipt,
    new_id,
)
from solakit.runtime import ApplicationContext
from solakit.storage.evidence import RECORDS, canonical, read_artifact
from solakit.tools.images import MAX_IMAGE_BYTES, normalize_upload

PAGE_BYTES = 512 * 1024
RECORD_BYTES = 64 * 1024
ARTIFACT_BYTES = 256 * 1024
REPLAY_WINDOW = 1000
MAX_STREAMS = 16
HEARTBEAT_SECONDS = 15


def record_view(sequence: int, record: Record) -> dict[str, Any]:
    kind = next(name for name, cls in RECORDS.items() if isinstance(record, cls))
    body = record.model_dump(mode="json")
    size = len(canonical(body))
    if size > RECORD_BYTES:
        body = {
            key: body[key]
            for key in ("id", "run_id", "session_id", "origin", "created_at")
            if key in body
        }
        if isinstance(record, Message):
            body.update(role=record.role, tool_call_id=record.tool_call_id)
            if record.documents:
                body["documents"] = [doc.model_dump(mode="json") for doc in record.documents]
        if isinstance(record, CompactionBoundary):
            body["summary_message_id"] = record.summary_message_id
        if isinstance(record, ToolCall):
            body["name"] = record.name[:256]
        if isinstance(record, ToolReceipt):
            body.update(
                tool_call_id=record.tool_call_id, status=record.status, effect=record.effect
            )
        body["artifacts"] = {
            name: value.model_dump(mode="json")
            for name in type(record).model_fields
            if isinstance(value := getattr(record, name), Artifact)
        }
    return {"sequence": sequence, "kind": kind, "record": body, "truncated": size > RECORD_BYTES}


def page(rows: list[tuple[int, Record]], after: int) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    size = 0
    for sequence, record in rows:
        view = record_view(sequence, record)
        length = len(canonical(view))
        if items and size + length > PAGE_BYTES:
            break
        items.append(view)
        size += length
    return {"items": items, "next": items[-1]["sequence"] if items else after}


def frame(event: str, data: object, cursor: str | None = None) -> bytes:
    prefix = f"id: {cursor}\n" if cursor is not None else ""
    return prefix.encode() + f"event: {event}\ndata: ".encode() + canonical(data) + b"\n\n"


class ClientService:
    def __init__(self, context: ApplicationContext, profiles: tuple[str, ...] = ()) -> None:
        assert context.store and context.runs and context.tools and context.model
        self.context = context
        self.store, self.runs, self.tools, self.model = (
            context.store,
            context.runs,
            context.tools,
            context.model,
        )
        self.profiles = tuple(dict.fromkeys((context.profile.name, *profiles)))
        self.streams = 0

    def profile(self, name: str | None, mode: SessionMode) -> EffectiveProfile:
        name = self.context.profile.name if name is None else name
        if name not in self.profiles:
            raise RequestError(422, "invalid_profile", "Unknown runtime profile")
        return self.tools.current(
            self.context.profile.model_copy(
                update={"name": name, "mode": mode, "origin": Origin.INTERACTIVE}
            )
        )

    def schemas(self, profile: EffectiveProfile) -> tuple[dict[str, JsonValue], ...]:
        registry = self.tools.registry
        return registry.visible(registry.eligible(profile))

    async def submit(
        self,
        session_id: str,
        name: str | None,
        content: str,
        request_id: str,
        wait: float,
        image: tuple[str, str] | None = None,
        upload_id: str | None = None,
        document_ids: tuple[str, ...] = (),
    ) -> Run:
        name = self.context.profile.name if name is None else name
        if not content.strip() or len(content.encode()) > 32768:
            raise RequestError(422, "invalid_input", "Content must contain 1–32768 UTF-8 bytes")
        topic = self.store.session(session_id)
        if topic.parent_session_id is not None or topic.origin == Origin.DELEGATED:
            raise RequestError(409, "conflict", "Delegated topics require delegation")
        existing = self.store.request(request_id)
        if existing is not None:
            if existing.profile != name or existing.origin != Origin.INTERACTIVE:
                raise RequestError(409, "conflict", "Idempotency key belongs to another submission")
            profile = self.store.admitted_profile(existing.id)
        else:
            profile = self.profile(name, topic.mode)
        reference = None
        if image is not None or upload_id is not None:
            if not (profile.model_image_inputs or profile.vision):
                raise RequestError(
                    422, "unsupported_image", "Image inputs are not enabled for this model"
                )
        if image is not None:
            source_run, identity = image
            if self.store.run(source_run).session_id != session_id:
                raise RequestError(403, "image_scope", "Image must belong to this topic")
            _, reference = self.artifact(source_run, identity, 0, 1)
            if reference.media_type != "image/png" or reference.length > MAX_IMAGE_BYTES:
                raise RequestError(422, "invalid_image", "Attach a bounded PNG image")
        if len(set(document_ids)) != len(document_ids):
            raise RequestError(422, "invalid_document", "Select each document only once")
        documents = (
            tuple(self.read_document(session_id, identity) for identity in document_ids)
            if existing is None
            else ()
        )
        if sum(len(raw) for _, raw in documents) > DOCUMENT_TEXT_BYTES:
            raise RequestError(422, "invalid_document", "Document text exceeds 24 KiB per message")
        run = self.runs.submit(
            session_id,
            profile=profile,
            content=content,
            request_id=request_id,
            image=reference,
            image_source=image,
            upload_id=upload_id,
            upload=self.read_upload(session_id, upload_id)
            if upload_id is not None and existing is None
            else None,
            document_ids=document_ids,
            documents=documents,
        )
        if wait:
            try:
                return await self.runs.wait(run.id, timeout=wait)
            except TimeoutError:
                return self.store.run(run.id)
        return run

    def upload_image(self, session_id: str, raw: bytes, media_type: str) -> str:
        topic = self.store.session(session_id)
        if topic.archived:
            raise RequestError(409, "conflict", "Archived topics cannot accept images")
        profile = self.profile(None, topic.mode)
        if not (profile.model_image_inputs or profile.vision):
            raise RequestError(422, "unsupported_image", "Image inputs are disabled")
        try:
            return self.runs.uploads.stage(session_id, normalize_upload(raw, media_type))
        except ValueError as error:
            raise RequestError(422, "invalid_image", str(error)) from None
        except OverflowError as error:
            raise RequestError(429, "upload_storage_full", str(error)) from None

    def read_upload(self, session_id: str, upload_id: str) -> bytes:
        self.store.session(session_id)
        try:
            return self.runs.uploads.read(session_id, upload_id)
        except KeyError:
            raise RequestError(
                404, "image_upload_missing", "Image upload expired or unavailable"
            ) from None

    async def upload_document(self, session_id: str, raw: bytes, filename: str) -> tuple[str, int]:
        topic = self.store.session(session_id)
        if topic.archived or topic.parent_session_id is not None:
            raise RequestError(409, "conflict", "This topic cannot accept documents")
        if not filename.strip() or not raw:
            raise RequestError(422, "invalid_document", "Choose a nonempty file with a name")
        if PurePath(filename).suffix.lower() in DOCUMENT_EXTENSIONS:
            try:
                content = await self.model.convert_document(filename, raw)
            except ModelError as error:
                code = error.code
                status = (
                    415
                    if code == ErrorCode.CONFIGURATION
                    else 503
                    if code == ErrorCode.MODEL_UNAVAILABLE
                    else 422
                )
                raise RequestError(status, "invalid_document", str(error)) from None
            if self.store.session(session_id).archived:
                raise RequestError(409, "conflict", "Archived topics cannot accept documents")
        else:
            try:
                content = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                raise RequestError(
                    422, "invalid_document", "Choose a UTF-8 text file or a supported document"
                ) from None
        if not content.strip() or re.search(r"[\x00-\x08\x0b\x0e-\x1f\x7f-\x9f]", content):
            raise RequestError(422, "invalid_document", "The document has no readable text")
        data = content.encode()
        if len(data) > DOCUMENT_TEXT_BYTES:
            raise RequestError(422, "invalid_document", "Document text exceeds 24 KiB")
        try:
            identity = self.runs.document_uploads.stage(
                session_id, filename.encode() + b"\n" + data
            )
        except OverflowError as error:
            raise RequestError(429, "upload_storage_full", str(error)) from None
        return identity, len(data)

    def read_document(self, session_id: str, identity: str) -> tuple[str, bytes]:
        try:
            raw = self.runs.document_uploads.read(session_id, identity)
        except KeyError:
            raise RequestError(
                404, "document_upload_missing", "Document upload expired or unavailable"
            ) from None
        name, _, text = raw.partition(b"\n")
        return name.decode(), text

    def job_page(
        self, after: tuple[str, str] | None, limit: int, job_id: str | None = None
    ) -> dict[str, Any]:
        from solakit.records import Job, Occurrence

        kind = "occurrence" if job_id else "job"
        if job_id:
            self.store.job(job_id)
        rows = self.store.db.execute(
            "SELECT id FROM entities WHERE kind=? AND (created_at,id)>(?,?) "
            "AND (? IS NULL OR json_extract(body,'$.job_id')=?) "
            "AND (kind!='job' OR NOT json_extract(body,'$.deleted')) "
            "ORDER BY created_at,id LIMIT ?",
            (kind, *(after or ("", "")), job_id, job_id, limit),
        ).fetchall()
        items: list[dict[str, Any]] = []
        size = 0
        cursor = f"{after[0]},{after[1]}" if after else None
        for row in rows:
            record = self.store._load(kind, row[0], Occurrence if job_id else Job)
            body = record.model_dump(mode="json")
            if (
                isinstance(record, Occurrence)
                and record.run_id
                and record.definition.get("follow_up")
            ):
                delivery = self.store.db.execute(
                    "SELECT json_extract(body,'$.state') FROM entities "
                    "WHERE kind='delivery' AND run_id=?",
                    (record.run_id,),
                ).fetchone()
                body["delivery_state"] = delivery[0] if delivery else None
            length = len(canonical(body))
            if items and size + length > PAGE_BYTES:
                break
            items.append(body)
            size += length
            cursor = f"{record.created_at.isoformat()},{record.id}"
        return {"items": items, "next": cursor}

    def snapshot(self, session_id: str) -> dict[str, Any]:
        session, sequence = self.store.snapshot(session_id)
        messages = self.store.db.execute(
            "SELECT id,sequence FROM entities WHERE kind='message' AND session_id=? "
            "AND json_extract(body,'$.origin') NOT IN ('scheduled','proactive') "
            "OR kind='message' AND session_id=? AND EXISTS (SELECT 1 FROM entities AS d "
            "WHERE d.kind='delivery' AND json_extract(d.body,'$.message_id')=entities.id "
            "AND json_extract(d.body,'$.state')='delivered') ORDER BY sequence DESC LIMIT 50",
            (session_id, session_id),
        ).fetchall()
        runs = self.store.db.execute(
            "SELECT id FROM entities WHERE kind='run' AND session_id=? "
            "ORDER BY sequence DESC LIMIT 50",
            (session_id,),
        ).fetchall()
        return {
            "session": session.model_dump(mode="json") | {"title": session.title[:256]},
            "cursor": f"{session_id}:{sequence}",
            "messages": [{"id": row[0], "sequence": row[1]} for row in reversed(messages)],
            "runs": [
                {"id": run.id, "state": run.state, "request_id": run.request_id}
                for row in reversed(runs)
                for run in (self.store.run(row[0]),)
            ],
            "replace_previews": True,
        }

    def event_cursor(self, session_id: str, cursor: str | None) -> int | None:
        self.store.session(session_id)
        if cursor is None:
            return None
        scope, separator, number = cursor.partition(":")
        if (
            scope != session_id
            or not separator
            or not number.isascii()
            or not number.isdecimal()
            or len(number) > 18
        ):
            raise RequestError(422, "invalid_cursor", "Cursor must belong to this session")
        return int(number)

    async def events(
        self, session_id: str, after: int | None, auth: AuthService, login: Login
    ) -> AsyncGenerator[bytes]:
        snapshot = self.snapshot(session_id)
        high = int(snapshot["cursor"].split(":")[1])
        reset = after is not None and (after > high or high - after > REPLAY_WINDOW)
        cursor = high if after is None or reset else after
        yield frame("reset" if reset else "snapshot", snapshot, f"{session_id}:{cursor}")
        preview_version = 0
        heartbeat = asyncio.get_running_loop().time()
        while True:
            try:
                auth.validate(login)
            except RequestError:
                yield frame("auth.expired", {})
                return
            _, high = self.store.snapshot(session_id)
            events = self.store.events(session_id, after=cursor, limit=100)
            gap = any(event.sequence != cursor + i + 1 for i, event in enumerate(events))
            if high - cursor > REPLAY_WINDOW or gap or (high > cursor and not events):
                snapshot = self.snapshot(session_id)
                cursor = int(snapshot["cursor"].split(":")[1])
                yield frame("reset", snapshot, f"{session_id}:{cursor}")
                preview_version = self.model.previews.version
                continue
            for event in events:
                cursor = event.sequence
                yield frame(event.type, event.model_dump(mode="json"), f"{session_id}:{cursor}")
            for preview in tuple(self.model.previews.latest.values()):
                if preview["session_id"] == session_id and preview["version"] > preview_version:
                    yield frame("preview", preview)
            preview_version = self.model.previews.version
            now = asyncio.get_running_loop().time()
            if now - heartbeat >= HEARTBEAT_SECONDS:
                yield b": heartbeat\n\n"
                heartbeat = now
            await asyncio.sleep(0 if len(events) == 100 else 0.1)

    def artifact(
        self, run_id: str, identity: str, offset: int, limit: int, *, preview: bool = False
    ) -> tuple[bytes, Artifact]:
        from solakit.storage.store import valid_id

        valid_id(identity)
        if not 1 <= limit <= ARTIFACT_BYTES or offset < 0:
            raise RequestError(422, "invalid_range", "Invalid artifact offset or byte limit")
        # A guessed artifact ID is insufficient: it must be linked by this run's evidence.
        self.store.run(run_id)
        row = self.store.db.execute(
            "SELECT kind,id FROM entities WHERE run_id=? AND ("
            "json_extract(body,'$.body.path')=? OR json_extract(body,'$.manifest.path')=? OR "
            "json_extract(body,'$.response.path')=? OR json_extract(body,'$.artifact.path')=? OR "
            "json_extract(body,'$.image.path')=? OR EXISTS ("
            "SELECT 1 FROM json_each(body,'$.documents') WHERE json_extract(value,'$.path')=?)) "
            "LIMIT 1",
            (run_id, *([f"artifacts/{identity}"] * 6)),
        ).fetchone()
        if row is None:
            raise KeyError(identity)
        record = self.store._load(row[0], row[1], RECORDS[row[0]])
        artifacts = [
            value
            for name in type(record).model_fields
            if isinstance(value := getattr(record, name), Artifact)
        ]
        if isinstance(record, Message):
            artifacts.extend(record.documents)
        artifact = next(value for value in artifacts if value.path == f"artifacts/{identity}")
        if preview:
            if artifact.media_type != "image/png" or artifact.length > MAX_IMAGE_BYTES:
                raise RequestError(422, "invalid_image", "Preview requires a bounded PNG image")
            offset, limit = 0, artifact.length
        if offset > artifact.length:
            raise RequestError(416, "invalid_range", "Offset exceeds artifact length")
        return read_artifact(self.store.root, artifact, offset, limit), artifact

    def prompt(self, name: str | None, content: str, hashes: bool) -> dict[str, Any]:
        if not content.strip() or len(content.encode()) > 32768:
            raise RequestError(422, "invalid_input", "Content must contain 1–32768 UTF-8 bytes")
        from solakit.records import utc_now

        profile = self.profile(name, SessionMode.SHARED)
        prepared = assemble(
            profile,
            PromptInput(
                at=utc_now(),
                input=Message(
                    origin=Origin.INTERACTIVE,
                    session_id=new_id(),
                    run_id=new_id(),
                    role="user",
                    content=content,
                ),
                tools=self.schemas(profile),
                enable_thinking=False,
            ),
        )
        result = inspect_prompt(prepared, hashes=hashes)
        if len(canonical(result)) > PAGE_BYTES:
            raise RequestError(
                413, "inspection_too_large", "Use hashes or recorded artifact retrieval"
            )
        return result

    def prompts(self, run_id: str, after: int, limit: int) -> dict[str, Any]:
        self.store.run(run_id)
        rows = self.store.db.execute(
            "SELECT id,sequence FROM entities WHERE kind='request' AND run_id=? AND sequence>? "
            "ORDER BY sequence LIMIT ?",
            (run_id, after, limit),
        ).fetchall()
        return page(
            [(row[1], self.store._load("request", row[0], ModelRequest)) for row in rows], after
        )
