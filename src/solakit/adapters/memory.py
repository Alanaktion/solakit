"""OpenViking 0.5 boundary and bounded, durable extraction observation."""

import asyncio
import os
import re
import time
from collections.abc import Callable
from datetime import timedelta
from functools import partial
from typing import Any, cast
from urllib.parse import quote

import httpx2
from pydantic import JsonValue

from solakit.config import EffectiveProfile, MemorySettings, restrict_to_admission
from solakit.records import (
    ErrorCode,
    Failure,
    MemoryCommit,
    MemoryNote,
    MemoryRecall,
    MemoryState,
    Message,
    Run,
    RunState,
    SessionMode,
    utc_now,
)
from solakit.redaction import Redactor
from solakit.storage.evidence import canonical, checked_json, digest
from solakit.storage.store import LOCAL_RUN, Store


class MemoryError(ValueError):
    pass


_STATUS_UNAVAILABLE = Failure(
    code=ErrorCode.MEMORY_UNAVAILABLE,
    message="OpenViking task status unavailable",
)


def scope(settings: MemorySettings) -> dict[str, JsonValue]:
    return {"account": settings.account, "user": settings.user, "roots": list(settings.roots)}


def eligible(profile: EffectiveProfile) -> bool:
    return profile.mode == SessionMode.SHARED and profile.memory_mode != "off"


class OpenViking:
    def __init__(self, settings: MemorySettings, redactor: Redactor) -> None:
        self.settings, self.redactor = settings, redactor
        self.transport: httpx2.AsyncBaseTransport | None = None
        self.client: httpx2.AsyncClient | None = None
        self.verified = False

    async def start(self) -> None:
        headers = {
            "X-OpenViking-Account": self.settings.account,
            "X-OpenViking-User": self.settings.user,
        }
        if self.settings.api_key_env:
            key = os.environ.get(self.settings.api_key_env)
            if key:
                self.redactor.register(key)
                headers["X-API-Key"] = key
        self.client = httpx2.AsyncClient(
            base_url=self.settings.base_url.rstrip("/"),
            headers=headers,
            timeout=self.settings.timeout_seconds,
            trust_env=False,
            transport=self.transport,
        )

    async def stop(self) -> None:
        if self.client:
            await self.client.aclose()
            self.client = None
        self.verified = False

    async def request(self, method: str, path: str, body: object = None) -> Any:
        if self.client is None:
            raise MemoryError("Memory client is stopped")
        if self.settings.api_key_env and not os.environ.get(self.settings.api_key_env):
            raise MemoryError("Memory credential is unavailable")
        try:
            async with self.client.stream(method, path, json=body) as response:
                response.raise_for_status()
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 1024 * 1024:
                        raise MemoryError("Memory response exceeded 1 MiB")
                result = checked_json(bytes(data))
                canonical(result)
            if not isinstance(result, dict) or result.get("status") != "ok":
                raise MemoryError("Memory returned an invalid response")
            return result.get("result", result)
        except (httpx2.HTTPError, ValueError) as error:
            raise MemoryError("OpenViking request unavailable or invalid") from error

    async def verify(self) -> dict[str, Any]:
        health: dict[str, Any] = await self.request("GET", "/health")
        version = health.get("version") if isinstance(health, dict) else None
        # Allow stable patches and four-part hotfixes; a new minor needs API review.
        if not isinstance(version, str) or not re.fullmatch(
            r"0\.5\.(?:[5-9]|[1-9][0-9]+)(?:\.[0-9]+)?", version
        ):
            raise MemoryError("OpenViking requires a stable version >=0.5.5,<0.6.0")
        self.verified = True
        return health

    def check(self, profile: EffectiveProfile) -> None:
        if (
            not eligible(profile)
            or not self.settings.enabled
            or profile.memory != self.settings
            or self.settings.roots != (f"viking://user/{self.settings.user}/memories",)
        ):
            raise MemoryError("Memory is disabled or outside the configured scope")

    async def recall(
        self, profile: EffectiveProfile, query: str
    ) -> tuple[dict[str, JsonValue], ...]:
        self.check(profile)
        if not self.verified:
            await self.verify()
        result = await self.request(
            "POST",
            "/api/v1/search/find",
            {
                "query": query[-1000:],
                "target_uri": list(self.settings.roots),
                "limit": 8,
                "read_content": True,
            },
        )
        if not isinstance(result, dict) or not isinstance(result.get("memories"), list):
            raise MemoryError("Invalid recall sources")
        if len(result["memories"]) > 8:
            raise MemoryError("Recall exceeded its result limit")
        sources = []
        for hit in result["memories"]:
            if not isinstance(hit, dict) or not isinstance(hit.get("uri"), str):
                raise MemoryError("Invalid memory source")
            uri = hit["uri"]
            if not any(uri == root or uri.startswith(root + "/") for root in self.settings.roots):
                raise MemoryError("Recall source escaped its scope")
            # Reject encoded traversal and noncanonical source paths, rather than normalizing them.
            if any(part in (".", "..", "") for part in uri[9:].split("/")) or any(
                c in uri for c in "%\\?#\r\n"
            ):
                raise MemoryError("Noncanonical memory source")
            sources.append(
                {key: hit[key] for key in ("uri", "content", "abstract", "score") if key in hit}
            )
        if len(canonical(sources)) > 32768:
            raise MemoryError("Recall content exceeded 32 KiB")
        return tuple(sources)

    async def submit(
        self,
        profile: EffectiveProfile,
        commit: MemoryCommit,
        authority: Callable[[], EffectiveProfile],
    ) -> str:
        self.check(profile)
        if not self.verified:
            await self.verify()
        self.check(authority())
        session = "sola-" + commit.id
        created = await self.request(
            "POST",
            "/api/v1/sessions",
            {
                "session_id": session,
                "auto_commit_policy": None,
                "memory_policy": {
                    "self": {"enabled": True},
                    "peer": {"enabled": False},
                    "working_memory": {"enabled": False},
                    "memory_types": ["profile", "preferences", "entities", "events"],
                },
            },
        )
        user = created.get("user") if isinstance(created, dict) else None
        if (
            not isinstance(user, dict)
            or user.get("account_id") != self.settings.account
            or user.get("user_id") != self.settings.user
            or created.get("session_id") != session
            or created.get("uri") != f"viking://user/{self.settings.user}/sessions/{session}"
        ):
            raise MemoryError("Provider session differs from the selected memory scope")
        self.check(authority())
        await self.request(
            "POST",
            f"/api/v1/sessions/{session}/messages/batch",
            {
                "messages": list(commit.payload),
            },
        )
        self.check(authority())
        result = await self.request(
            "POST",
            f"/api/v1/sessions/{session}/commit",
            {
                "enable_working_memory": False,
            },
        )
        task_id = result.get("task_id") if isinstance(result, dict) else None
        if not isinstance(task_id, str) or not 1 <= len(task_id) <= 256:
            raise MemoryError("Commit acceptance has no task reference")
        return task_id

    async def status(self, profile: EffectiveProfile, task_id: str) -> str:
        self.check(profile)
        if not self.verified:
            await self.verify()
        result = await self.request("GET", "/api/v1/tasks/" + quote(task_id, safe=""))
        status = result.get("status") if isinstance(result, dict) else None
        if status not in ("pending", "running", "cancelling", "completed", "failed", "cancelled"):
            raise MemoryError("Invalid extraction status")
        return str(status)


class MemoryService:
    def __init__(
        self,
        store: Store,
        adapter: OpenViking,
        current: Callable[[EffectiveProfile], EffectiveProfile],
    ) -> None:
        self.store, self.adapter, self.current = store, adapter, current
        self.worker: asyncio.Task[None] | None = None
        self.changed = asyncio.Event()
        self.failure: BaseException | None = None
        self.after = 0

    async def start(self) -> None:
        await self.adapter.start()
        self.failure = None
        self.after = 0
        self.worker = asyncio.create_task(self._observe(), name="sola-memory")

    async def stop(self) -> None:
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
            self.worker = None
        await self.adapter.stop()

    def profile(self, run_id: str) -> EffectiveProfile:
        admitted = self.store.admitted_profile(run_id)
        return restrict_to_admission(self.current(admitted), admitted)

    async def recall(self, run: Run, profile: EffectiveProfile, query: str) -> MemoryRecall:
        started = time.monotonic()
        values: dict[str, Any] = {
            "origin": run.origin,
            "session_id": run.session_id,
            "run_id": run.id,
            "query": query[-1000:] if eligible(profile) else "",
            "scope": scope(profile.memory),
            "status": "disabled",
            "sources": (),
        }
        if eligible(profile) and profile.memory.enabled:
            try:
                values["sources"] = await self.adapter.recall(profile, query[-1000:])
                values["status"] = "ok"
            except MemoryError:
                values["status"] = "degraded"
        record = MemoryRecall(**values, elapsed_seconds=time.monotonic() - started)
        self.store.append(record)
        return record

    def stage(self, run_id: str, tool_call_id: str, content: str) -> MemoryNote:
        profile = self.profile(run_id)
        self.adapter.check(profile)
        run = self.store.run(run_id)
        note = MemoryNote(
            origin=run.origin,
            session_id=run.session_id,
            run_id=run.id,
            tool_call_id=tool_call_id,
            content=content,
        )
        self.store.append(note)
        return note

    def enqueue(self, run: Run) -> None:
        # Historical imports have no v2 admission and must never trigger extraction.
        if not self.store.db.execute(
            "SELECT 1 FROM admissions WHERE run_id=?", (run.id,)
        ).fetchone():
            return
        if self.store.db.execute(
            "SELECT 1 FROM entities WHERE kind='memory' AND run_id=?",
            (run.id,),
        ).fetchone():
            return
        profile = self.profile(run.id)
        if run.state != RunState.COMPLETED or not eligible(profile) or not profile.memory.enabled:
            return
        rows = self.store.db.execute(
            "SELECT kind,body FROM entities WHERE run_id=? AND kind IN ('message','memory_note') "
            "ORDER BY sequence",
            (run.id,),
        ).fetchall()
        messages = [Message.model_validate_json(r["body"]) for r in rows if r["kind"] == "message"]
        notes = [
            MemoryNote.model_validate_json(r["body"]) for r in rows if r["kind"] == "memory_note"
        ]
        summary_ids = {
            row[0]
            for row in self.store.db.execute(
                "SELECT json_extract(body,'$.summary_message_id') FROM entities "
                "WHERE kind='compaction' AND session_id=?",
                (run.session_id,),
            )
        }
        selected = [
            m for m in messages if m.role in ("user", "assistant") and m.id not in summary_ids
        ]
        payload = cast(
            tuple[dict[str, JsonValue], ...],
            tuple(
                {"role": m.role, "content": m.content, "source_message_ids": [m.id]}
                for m in selected
            )
            + tuple(
                {"role": "user", "content": n.content, "source_message_ids": [n.id]} for n in notes
            ),
        )
        commit = MemoryCommit(
            origin=run.origin,
            session_id=run.session_id,
            run_id=run.id,
            payload=payload,
            payload_sha256=digest(canonical(payload)),
            source_ids=tuple(m.id for m in selected),
            note_ids=tuple(n.id for n in notes),
            scope=scope(profile.memory),
        )
        count = self.store.db.execute(
            f"SELECT count(*) FROM entities WHERE kind='memory' AND {LOCAL_RUN} "
            "AND json_extract(body,'$.state') IN ('pending','running','accepted','ambiguous')",
        ).fetchone()[0]
        self.store.append(commit)
        if (
            count >= profile.memory.backlog_cap
            or len(payload) > 100
            or len(canonical(payload)) > 65536
        ):
            self.store.transition_memory(
                commit.id,
                MemoryState.FAILED,
                reason=Failure(
                    code=ErrorCode.BUDGET_EXCEEDED,
                    message="Memory backlog or batch cap exceeded; inspect locally",
                ),
            )
        self.changed.set()

    async def tick(self) -> None:
        # Recover the crash gap between terminal reply and enqueuing memory intent.
        for row in self.store.db.execute(
            "SELECT r.id,o.ordinal FROM entities AS r JOIN outbox AS o ON o.id=r.entry_id "
            "WHERE r.kind='run' AND json_extract(r.body,'$.state')='completed' "
            "AND EXISTS (SELECT 1 FROM admissions WHERE run_id=r.id) "
            "AND o.ordinal>? ORDER BY o.ordinal LIMIT 128",
            (self.after,),
        ).fetchall():
            try:
                self.enqueue(self.store.run(row[0]))
            except ValueError:
                pass  # Current revocation/configuration prevents submission.
            self.after = row[1]
        for row in self.store.db.execute(
            f"SELECT id FROM entities WHERE kind='memory' AND {LOCAL_RUN} "
            "AND json_extract(body,'$.state') IN ('pending','accepted') "
            "AND julianday(json_extract(body,'$.next_check'))<=julianday(?) "
            "ORDER BY created_at LIMIT 128",
            (utc_now().isoformat(),),
        ).fetchall():
            commit = self.store._load("memory", row[0], MemoryCommit)
            if commit.next_check > utc_now():
                continue
            try:
                profile = self.profile(commit.run_id)
                self.adapter.check(profile)
            except ValueError:
                self.store.transition_memory(
                    commit.id,
                    MemoryState.FAILED,
                    reason=Failure(
                        code=ErrorCode.TOOL_DENIED,
                        message="Memory authority was revoked or changed",
                    ),
                )
                continue
            if commit.state == MemoryState.PENDING:
                try:
                    await self.adapter.verify()
                except MemoryError:
                    attempts = commit.attempts + 1
                    self.store.transition_memory(
                        commit.id,
                        MemoryState.FAILED if attempts >= 12 else MemoryState.PENDING,
                        reason=Failure(
                            code=ErrorCode.MEMORY_UNAVAILABLE,
                            message="Memory unavailable before submission",
                        ),
                        attempts=attempts,
                        next_check=utc_now() + timedelta(seconds=min(300, 2 ** min(attempts, 9))),
                    )
                    continue
                # Persist before ANY remote mutation. No idempotency guarantee in 0.5.5.
                self.store.transition_memory(commit.id, MemoryState.RUNNING)
                try:
                    task_id = await self.adapter.submit(
                        profile, commit, partial(self.profile, commit.run_id)
                    )
                except ValueError:
                    self.store.transition_memory(
                        commit.id,
                        MemoryState.AMBIGUOUS,
                        reason=Failure(
                            code=ErrorCode.MEMORY_UNAVAILABLE,
                            message="Submission outcome unknown; inspect provider session",
                        ),
                    )
                    continue
                commit = self.store.transition_memory(
                    commit.id,
                    MemoryState.ACCEPTED,
                    provider_commit_id=task_id,
                    attempts=0,
                )
            assert commit.provider_commit_id
            try:
                status = await self.adapter.status(profile, commit.provider_commit_id)
            except MemoryError:
                status = "unavailable"
            attempts = commit.attempts + 1
            # A reachable long-running task and a broken status endpoint have separate streaks.
            if (status == "unavailable") != (commit.reason == _STATUS_UNAVAILABLE):
                attempts = 1
            state = MemoryState.ACCEPTED
            reason = None
            if status == "completed":
                state = MemoryState.COMPLETED
            elif status in ("failed", "cancelled"):
                state = MemoryState.FAILED
                reason = Failure(
                    code=ErrorCode.MEMORY_UNAVAILABLE,
                    message=f"OpenViking reported extraction {status}; inspect provider task",
                )
            elif status == "unavailable":
                reason = _STATUS_UNAVAILABLE
                if attempts >= 12:
                    state = MemoryState.FAILED
                    reason = Failure(
                        code=ErrorCode.MEMORY_UNAVAILABLE,
                        message="OpenViking task status unavailable after 12 consecutive checks",
                    )
            self.store.transition_memory(
                commit.id,
                state,
                reason=reason,
                attempts=attempts,
                next_check=utc_now() + timedelta(seconds=min(300, 2 ** min(attempts, 9))),
            )

    async def _observe(self) -> None:
        try:
            while True:
                self.changed.clear()
                await self.tick()
                try:
                    await asyncio.wait_for(self.changed.wait(), timeout=1)
                except TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.failure = error

    def status(self) -> dict[str, JsonValue]:
        counts = {
            row[0]: row[1]
            for row in self.store.db.execute(
                "SELECT json_extract(body,'$.state'), count(*) FROM entities "
                f"WHERE kind='memory' AND {LOCAL_RUN} GROUP BY 1",
            )
        }
        return {"observer": "failed" if self.failure else "running", "commits": counts}

    async def reconcile(self, identity: str, task_id: str) -> MemoryCommit:
        work = self.store._load("memory", identity, MemoryCommit)
        if work.state not in (MemoryState.AMBIGUOUS, MemoryState.FAILED):
            raise ValueError("only ambiguous or failed work needs manual reconciliation")
        profile = self.profile(work.run_id)
        self.adapter.check(profile)
        await self.adapter.verify()
        result = await self.adapter.request("GET", "/api/v1/tasks/" + quote(task_id, safe=""))
        if (
            not isinstance(result, dict)
            or result.get("task_type") != "session_commit"
            or result.get("resource_id") != "sola-" + work.id
            or result.get("status")
            not in ("pending", "running", "cancelling", "completed", "failed", "cancelled")
        ):
            raise ValueError("provider task does not identify this commit's session")
        return self.store.transition_memory(
            identity,
            MemoryState.ACCEPTED,
            provider_commit_id=task_id,
            attempts=0,
            next_check=utc_now(),
        )
