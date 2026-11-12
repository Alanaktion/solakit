"""Rebuildable SQLite indexes, session activity and search (Store mixin)."""

import json
import logging
import os
import re
import sqlite3
import time
from datetime import UTC, datetime

from pydantic import ValidationError

from solakit.core.contracts import StoreServices
from solakit.core.schemas import (
    Message,
    Run,
    RunStatus,
    now,
)
from solakit.core.search import (
    FTS_INSERT,
    FTS_SCHEMA,
    SearchUnavailable,
    search_index,
    sqlite_retry,
    sqlite_transient,
    timestamp,
)
from solakit.core.store_base import (  # noqa: F401,E501
    INDEX_VERSION,
    StorageCorruption,
    atomic_write,
    safe_id,
    sync_directory,
)
from solakit.core.store_offsets import OFFSET_SCHEMA


class IndexMixin(StoreServices):
    def _index(self, kind: str, record_id: str, payload: dict) -> None:
        with self._database() as db:
            db.execute(
                f"INSERT OR REPLACE INTO {kind} VALUES (?, ?)",
                (record_id, json.dumps(payload)),
            )

    def _inventory(self):
        """Stat evidence once per directory; never open unchanged historical files."""
        units: dict[tuple[str, str], list] = {}
        recovery = []
        patterns = {
            "sessions": (
                r"([A-Za-z0-9][A-Za-z0-9_-]{0,127})\."
                r"(?:meta\.json|jsonl(?:\.[0-9]+)?|compaction\.json|archive\.json)$"
            ),
            "runs": r"([A-Za-z0-9][A-Za-z0-9_-]{0,127})\.(?:events|deliveries)\.jsonl$",
            "jobs": r"([A-Za-z0-9][A-Za-z0-9_-]{0,127})\.jsonl$",
        }
        for kind, pattern in patterns.items():
            matcher = re.compile(pattern)
            with os.scandir(self.root / kind) as entries:
                for entry in entries:
                    match = matcher.fullmatch(entry.name)
                    if not match or not entry.is_file():
                        continue
                    stat = entry.stat()
                    units.setdefault((kind, match[1]), []).append(
                        [
                            entry.name,
                            stat.st_size,
                            stat.st_mtime_ns,
                            stat.st_ctime_ns,
                            stat.st_ino,
                            stat.st_dev,
                        ]
                    )
        with os.scandir(self.root / "recovery") as entries:
            for entry in entries:
                if entry.name.endswith(".json") and entry.is_file():
                    stat = entry.stat()
                    recovery.append(
                        [
                            entry.name,
                            stat.st_size,
                            stat.st_mtime_ns,
                            stat.st_ctime_ns,
                            stat.st_ino,
                            stat.st_dev,
                        ]
                    )
        return (
            {key: json.dumps(sorted(files)) for key, files in units.items()},
            json.dumps(sorted(recovery)),
        )

    def rebuild(self, *, incremental=False) -> None:  # noqa: C901
        """Refresh changed source units; explicit rebuild always rechecks everything.

        Source stats are a disposable cache, committed with the derived rows.
        Runtime writes intentionally leave old signatures: a crash or an index
        failure therefore forces changed evidence to be re-read on restart.
        """
        started = time.perf_counter()
        with self._database() as db:
            sources, recovery = self._inventory()
            required = {
                "sessions",
                "runs",
                "jobs",
                "session_activity",
                "source_activity",
                "messages_fts",
                "index_sources",
                "index_meta",
            }
            tables = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            cached, metadata = {}, {}
            if required <= tables:
                metadata = dict(db.execute("SELECT key, value FROM index_meta"))
                if (
                    incremental
                    and metadata.get("version") == INDEX_VERSION
                    and metadata.get("recovery") == recovery
                ):
                    cached = {
                        (kind, identity): signature
                        for kind, identity, signature in db.execute(
                            "SELECT kind, id, signature FROM index_sources"
                        )
                    }
                else:
                    incremental = False
            else:
                incremental = False
            if incremental:
                try:
                    self._issues = {
                        (i["file"], i["offset"]): i
                        for i in json.loads(metadata.get("issues", "[]"))
                    }
                    for signature in cached.values():
                        json.loads(signature)
                except (ValueError, TypeError, KeyError):
                    incremental = False
                # An unhealthy derived index must be repaired, never cached as good.
                if any(i["file"] == "app.db" for i in self._issues.values()):
                    incremental = False
            if not incremental:
                self._issues.clear()
                self._fts_dirty_sessions.clear()
                cached = {}
            changed = {
                key
                for key in sources.keys() | cached.keys()
                if not incremental or sources.get(key) != cached.get(key)
            }
            db.executescript(OFFSET_SCHEMA)
            db.execute("BEGIN")
            for kind in ("sessions", "runs", "jobs"):
                db.execute(
                    f"CREATE TABLE IF NOT EXISTS {kind} "
                    "(id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
                )
                if not incremental:
                    db.execute(f"DELETE FROM {kind}")
            for field in ("session", "status", "memory_status", "created_at"):
                db.execute(
                    f"CREATE INDEX IF NOT EXISTS runs_{field} "
                    f"ON runs(json_extract(payload, '$.{field}'))"
                )
            db.execute(
                "CREATE INDEX IF NOT EXISTS runs_pending_memory ON runs("
                "json_extract(payload, '$.memory_status')='status_unavailable') "
                "WHERE json_extract(payload, '$.memory_status') IN "
                "('accepted','pending','running','cancelling','status_unavailable') "
                "AND json_extract(payload, '$.status') NOT IN ('queued','running')"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS session_activity "
                "(id TEXT PRIMARY KEY, last_activity TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS source_activity "
                "(kind TEXT, id TEXT, session_id TEXT, at TEXT, PRIMARY KEY(kind,id))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS index_sources "
                "(kind TEXT, id TEXT, signature TEXT, PRIMARY KEY(kind,id))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS index_meta "
                "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            if not incremental:
                db.execute("DELETE FROM message_offsets")
                db.execute("DELETE FROM message_files")
                db.execute("DELETE FROM source_activity")
                db.execute("DELETE FROM index_sources")
            fts = True
            try:
                if not incremental:
                    db.execute("DROP TABLE IF EXISTS messages_fts")
                    db.execute(FTS_SCHEMA)
            except sqlite3.Error:
                self._issue(self.root / "app.db", "fts_index_unavailable")
                fts = False
            for kind, identity in sorted(changed):
                old_files = json.loads(cached.get((kind, identity), "[]"))
                new_files = json.loads(sources.get((kind, identity), "[]"))
                history_changed = (
                    not incremental
                    or [f for f in old_files if not f[0].endswith(".meta.json")]
                    != [f for f in new_files if not f[0].endswith(".meta.json")]
                    or not db.execute(
                        f"SELECT 1 FROM {kind} WHERE id=?", (identity,)
                    ).fetchone()
                    or not any(f[0].endswith(".meta.json") for f in new_files)
                )
                db.execute(f"DELETE FROM {kind} WHERE id=?", (identity,))
                if kind != "sessions" or history_changed:
                    db.execute(
                        "DELETE FROM source_activity WHERE kind=? AND id=?",
                        (kind, identity),
                    )
                if kind == "sessions" and fts and incremental and history_changed:
                    db.execute(
                        "DELETE FROM messages_fts WHERE session_id=?", (identity,)
                    )
                # Recheck previous issues for this unit, including deleted files.
                self._issues = {
                    key: issue
                    for key, issue in self._issues.items()
                    if not (
                        issue["file"].startswith(f"{kind}/{identity}.")
                        and (
                            kind != "sessions"
                            or history_changed
                            or issue["file"].endswith(".meta.json")
                        )
                    )
                }
                db.execute(
                    "DELETE FROM index_sources WHERE kind=? AND id=?", (kind, identity)
                )
                signature = sources.get((kind, identity))
                if signature is None:
                    continue
                db.execute(
                    "INSERT INTO index_sources VALUES (?, ?, ?)",
                    (kind, identity, signature),
                )
                activity = None
                try:
                    if kind == "sessions":
                        if not self._path(kind, identity, ".meta.json").exists():
                            continue
                        session = self.session(identity)
                        db.execute(
                            "INSERT INTO sessions VALUES (?, ?)",
                            (identity, session.model_dump_json()),
                        )
                        if not history_changed:
                            # Title/lifecycle edits never reread old transcripts.
                            continue
                        archives = [
                            item[0]
                            for item in json.loads(signature)
                            if re.fullmatch(
                                re.escape(identity) + r"\.jsonl\.[0-9]+", item[0]
                            )
                        ]
                        archives.sort(key=lambda name: int(name.rsplit(".", 1)[1]))
                        self._refresh_offsets(db, identity)
                        rows = self._message_search_rows(identity, archives=archives)
                        dates = [row[7] for row in rows if row[7] is not None]
                        if dates:
                            activity = (identity, max(dates))
                        if fts:
                            try:
                                db.executemany(FTS_INSERT, rows)
                            except sqlite3.Error:
                                self._issue(
                                    self.root / "app.db", "fts_index_unavailable"
                                )
                                fts = False
                    elif kind == "runs":
                        path = self._path(kind, identity, ".events.jsonl")
                        records = self._read(path)
                        if not records:
                            continue
                        try:
                            run = Run.model_validate(records[-1])
                        except ValidationError:
                            self._issue(path, "invalid_run")
                            continue
                        db.execute(
                            "INSERT INTO runs VALUES (?, ?)",
                            (run.id, run.model_dump_json()),
                        )
                        if records[-1].get("created_at"):
                            activity = (
                                run.session,
                                self._activity_timestamp(run.created_at),
                            )
                    else:
                        records = self._read(self._path(kind, identity, ".jsonl"))
                        if records:
                            db.execute(
                                "INSERT INTO jobs VALUES (?, ?)",
                                (identity, json.dumps(records[-1])),
                            )
                    if activity:
                        db.execute(
                            "INSERT INTO source_activity VALUES (?, ?, ?, ?)",
                            (kind, identity, *activity),
                        )
                except StorageCorruption:
                    # Invalid metadata also invalidates retained history addresses.
                    if kind == "sessions" and not history_changed:
                        db.execute(
                            "DELETE FROM source_activity WHERE kind=? AND id=?",
                            (kind, identity),
                        )
                        if fts:
                            db.execute(
                                "DELETE FROM messages_fts WHERE session_id=?",
                                (identity,),
                            )
                    # Keep other sessions usable; retain diagnostics across restarts.
            db.execute("DELETE FROM session_activity")
            db.execute(
                "INSERT INTO session_activity SELECT session_id, max(at) "
                "FROM source_activity GROUP BY session_id"
            )
            db.executemany(
                "INSERT OR REPLACE INTO index_meta VALUES (?, ?)",
                [
                    ("version", INDEX_VERSION),
                    ("recovery", recovery),
                    ("issues", json.dumps(list(self._issues.values()))),
                ],
            )

        logging.getLogger(__name__).info(
            "Storage index: %s, %d source units refreshed, %d reused, %.3fs",
            "incremental" if incremental else "full rebuild",
            len(changed),
            len(sources.keys() - changed),
            time.perf_counter() - started,
        )

    def recover(self) -> None:
        # Completed historical runs without delivery evidence need no recovery.
        with self._database() as db:
            candidates = db.execute(
                "SELECT payload FROM runs WHERE "
                "json_extract(payload, '$.status') IN ('queued', 'running') OR "
                "json_extract(payload, '$.delivery_status')='sending'"
            ).fetchall()
            seen = {json.loads(row[0])["id"] for row in candidates}
            for path in (self.root / "runs").glob("*.deliveries.jsonl"):
                identity = path.name.removesuffix(".deliveries.jsonl")
                if identity not in seen:
                    candidates.extend(
                        db.execute(
                            "SELECT payload FROM runs WHERE id=? AND "
                            "json_extract(payload, '$.delivery_status')='none'",
                            (identity,),
                        ).fetchall()
                    )
        for (payload,) in candidates:
            record = json.loads(payload)
            run = Run.model_validate(record)
            if run.status in (RunStatus.queued, RunStatus.running):
                run.status = RunStatus.interrupted
                run.finished_at = now()
                run.error = "Process stopped before run finalization"
                self.save_run(run)
            if run.delivery_status in {"none", "sending"}:
                deliveries = self.delivery_events(run.id)
                if deliveries:
                    acknowledged = next(
                        (
                            event
                            for event in reversed(deliveries)
                            if event["stage"] == "delivered"
                        ),
                        None,
                    )
                    run.delivery_status = (
                        "delivered" if acknowledged else "failed_or_uncertain"
                    )
                    if acknowledged:
                        run.delivery = {
                            k: v for k, v in acknowledged.items() if k != "stage"
                        }
                    self.save_run(run)

    @staticmethod
    def _activity_timestamp(value):
        return timestamp(value.replace(tzinfo=UTC) if value.tzinfo is None else value)

    @staticmethod
    def _activity(db, session_id, at):
        db.execute(
            "INSERT INTO session_activity VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET "
            "last_activity=max(last_activity, excluded.last_activity)",
            (session_id, at),
        )

    def _touch_session(self, session_id, at):
        try:
            with self._database() as db:
                self._activity(db, session_id, self._activity_timestamp(at))
        except sqlite3.Error:
            self._issue(self.root / "app.db", "session_activity_index_unavailable")

    @staticmethod
    def _pinned(payload, default_ids):
        explicit = payload.get("pinned")
        return payload.get("id") in default_ids if explicit is None else explicit

    def list_sessions(
        self,
        *,
        status="active",
        q="",
        before=None,
        profile=None,
        exclude_source=None,
        roots_only=False,
        parent_session=None,
        limit=50,
        offset=0,
        ids=None,
        pinned_ids=(),
        pinned=None,
        pin_first=False,
    ):
        """pinned_ids are sessions pinned by default (channel-bound); an explicit
        per-session choice in metadata overrides that default."""
        if status not in {"active", "archived", "all"}:
            raise ValueError("Invalid session status")
        if (
            type(limit) is not int
            or not 1 <= limit <= 500
            or type(offset) is not int
            or offset < 0
        ):
            raise ValueError("Use limit 1–500 and a nonnegative offset")
        if len(q) > 200:
            raise ValueError("Session query exceeds 200 characters")
        at = "coalesce(a.last_activity, json_extract(s.payload, '$.created_at'))"
        source = "sessions s LEFT JOIN session_activity a ON a.id=s.id"
        clauses, args = [], []
        if roots_only:
            clauses.append("json_extract(s.payload, '$.parent_session') IS NULL")
        if parent_session is not None:
            clauses.append("json_extract(s.payload, '$.parent_session') = ?")
            args.append(parent_session)
        if ids is not None:
            if not ids or len(ids) > 500:
                raise ValueError("Select 1–500 session IDs")
            clauses.append("s.id IN (" + ",".join("?" for _ in ids) + ")")
            args.extend(ids)
        if status != "all":
            clauses.append(
                "json_extract(s.payload, '$.archived_at') IS "
                + ("NOT NULL" if status == "archived" else "NULL")
            )
        if exclude_source is not None:
            clauses.append("coalesce(json_extract(s.payload, '$.source'), 'api') != ?")
            args.append(exclude_source)
        if q:
            clauses.append(
                "(instr(lower(s.id), lower(?))>0 OR "
                "instr(lower(coalesce(json_extract(s.payload, '$.title'), '')), "
                "lower(?))>0)"
            )
            args.extend([q, q])
        if before:
            clauses.append(f"julianday({at}) < julianday(?)")
            args.append(timestamp(before))
        if profile is not None:
            clauses.append("json_extract(s.payload, '$.default_profile')=?")
            args.append(profile)
        pin_sql = (
            "coalesce(json_extract(s.payload, '$.pinned'), s.id IN ("
            + ",".join("?" for _ in pinned_ids)
            + "))"
            if pinned_ids
            else "coalesce(json_extract(s.payload, '$.pinned'), 0)"
        )
        if pinned is not None:
            clauses.append(f"{pin_sql} = ?")
            args.extend([*pinned_ids, 1 if pinned else 0])
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        pin_order = f"{pin_sql} DESC, " if pin_first else ""
        pin_args = list(pinned_ids) if pin_first and pinned_ids else []
        with self._database() as db:
            total = db.execute(
                f"SELECT count(*) FROM {source}" + where, args
            ).fetchone()[0]
            rows = db.execute(
                f"SELECT s.payload, {at} FROM {source}"
                + where
                + f" ORDER BY {pin_order}julianday({at}) DESC, s.id LIMIT ? OFFSET ?",
                [*args, *pin_args, limit, offset],
            ).fetchall()
            counts = dict(
                db.execute(
                    "SELECT CASE WHEN json_extract(payload, '$.archived_at') IS NULL "
                    "THEN 'active' ELSE 'archived' END, count(*) "
                    "FROM sessions GROUP BY 1"
                ).fetchall()
            )
        with self._database() as db:
            profiles = [
                row[0]
                for row in db.execute(
                    "SELECT DISTINCT json_extract(payload, '$.default_profile') "
                    "FROM sessions ORDER BY 1"
                )
                if row[0]
            ]
        return {
            "profiles": profiles,
            "sessions": [
                {
                    "source": "api",
                    **json.loads(payload),
                    "pinned": self._pinned(json.loads(payload), pinned_ids),
                    "last_activity_at": self._activity_timestamp(
                        datetime.fromisoformat(at)
                    ),
                }
                for payload, at in rows
            ],
            "total": total,
            "counts": {
                "active": counts.get("active", 0),
                "archived": counts.get("archived", 0),
            },
            "limit": limit,
            "offset": offset,
        }

    def list_runs(
        self,
        session_id: str | None = None,
        include_archived: bool = False,
        *,
        limit: int = 100,
        offset: int = 0,
        status: str | None = None,
        descending: bool = False,
    ) -> list[dict]:
        if not 1 <= limit <= 1000 or offset < 0:
            raise ValueError("limit must be 1–1000 and offset must be nonnegative")
        clauses, args = [], []
        if session_id is not None:
            clauses.append("json_extract(r.payload, '$.session')=?")
            args.append(session_id)
        if status is not None:
            clauses.append("json_extract(r.payload, '$.status')=?")
            args.append(status)
        if not include_archived:
            clauses.append("json_extract(s.payload, '$.archived_at') IS NULL")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._database() as db:
            rows = db.execute(
                "SELECT r.payload FROM runs r LEFT JOIN sessions s ON "
                "s.id=json_extract(r.payload, '$.session')"
                + where
                + (
                    " ORDER BY json_extract(r.payload, '$.created_at') DESC, r.id DESC"
                    if descending
                    else " ORDER BY r.rowid"
                )
                + " LIMIT ? OFFSET ?",
                [*args, limit, offset],
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    @staticmethod
    def _message_search_row(session_id, archive, line_no, record):
        message = Message.model_validate(record)
        content = message.content or ""
        if message.role == "assistant":
            names = []
            for call in message.tool_calls or []:
                function = call.get("function")
                if isinstance(function, dict) and isinstance(function.get("name"), str):
                    names.append(function["name"])
            content += "\n" + "\n".join(names) if names else ""
        stamp = message.ts if "ts" in record else None
        return (
            content,
            message.role,
            message.kind,
            message.origin.channel,
            session_id,
            archive,
            line_no,
            timestamp(stamp.replace(tzinfo=stamp.tzinfo or UTC)) if stamp else None,
            message.run_id,
        )

    def _message_search_rows(self, session_id, *, archives=None):
        self.transcript(session_id)  # Validate active evidence and rotation state.
        rows = []
        names = self.archives(session_id) if archives is None else archives
        for name in [*names, f"{session_id}.jsonl"]:
            path = self.root / "sessions" / name
            for line_no, record in self._read_lines(path):
                try:
                    rows.append(
                        self._message_search_row(
                            session_id,
                            "active" if name == f"{session_id}.jsonl" else name,
                            line_no,
                            record,
                        )
                    )
                except ValidationError as exc:
                    raise self._corruption(path, "invalid_message") from exc

        return rows

    def _insert_search_message(self, row) -> None:
        def insert():
            with self._database() as db:
                db.execute(FTS_INSERT, row)

        sqlite_retry(insert)

    def _search_write_failed(self, session_id: str, exc: BaseException) -> None:
        issue = self._issues.get(("app.db", None))
        if sqlite_transient(exc) and (issue is None or self._fts_dirty_sessions):
            self._fts_dirty_sessions.add(session_id)
        else:
            # A structural error must remain visible until explicit repair.
            self._fts_dirty_sessions.clear()
        self._issue(self.root / "app.db", "fts_index_unavailable")

    def _repair_search_writes(self) -> None:
        for session_id in list(self._fts_dirty_sessions):
            self.reindex_messages(session_id)

    def reindex_messages(self, session_id: str) -> None:
        """Refresh addresses after durable rotation; index failures lose no evidence."""
        with self.lock:
            try:
                rows = self._message_search_rows(session_id)
                dates = [row[7] for row in rows if row[7] is not None]
                if dates:
                    self._touch_session(session_id, datetime.fromisoformat(max(dates)))

                def replace_rows():
                    with self._database() as db:
                        db.execute(
                            "DELETE FROM messages_fts WHERE session_id = ?",
                            (session_id,),
                        )
                        db.executemany(FTS_INSERT, rows)
                        self._refresh_offsets(db, session_id)

                sqlite_retry(replace_rows)
                was_dirty = session_id in self._fts_dirty_sessions
                self._fts_dirty_sessions.discard(session_id)
                if was_dirty and not self._fts_dirty_sessions:
                    self._issues.pop(("app.db", None), None)
            except (sqlite3.Error, OSError, StorageCorruption) as exc:
                self._search_write_failed(session_id, exc)

    def search(
        self,
        query: str,
        *,
        session_id: str | None = None,
        role: str | None = None,
        kind: str | None = None,
        after: str | datetime | None = None,
        before: str | datetime | None = None,
        limit: int = 50,
        owner_allow: list[str] | None = None,
        owner_deny: list[str] | None = None,
    ) -> list[dict]:
        with self.lock:
            self._repair_search_writes()
            if any(i["kind"] == "fts_index_unavailable" for i in self._issues.values()):
                raise SearchUnavailable(
                    "Search index unavailable; rebuild app.db from evidence"
                )
            try:
                return search_index(
                    self.root,
                    query,
                    session_id=session_id,
                    role=role,
                    kind=kind,
                    after=after,
                    before=before,
                    limit=limit,
                    redactor=self.redactor,
                    owner_allow=owner_allow,
                    owner_deny=owner_deny,
                )
            except SearchUnavailable as exc:
                if not sqlite_transient(exc.__cause__):
                    self._fts_dirty_sessions.clear()
                    self._issue(self.root / "app.db", "fts_index_unavailable")
                raise

    def list_records(self, kind: str) -> list[dict]:
        if kind not in {"sessions", "runs", "jobs"}:
            raise ValueError("unknown index")
        with self._database() as db:
            rows = db.execute(f"SELECT payload FROM {kind} ORDER BY rowid").fetchall()
        return [json.loads(row[0]) for row in rows]
