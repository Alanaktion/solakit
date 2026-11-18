"""Offline legacy snapshot, resumable import, verification and rollback reconciliation."""

import argparse
import asyncio
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import tempfile
from collections import Counter
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from pydantic import ValidationError

from solakit.backup import _copy
from solakit.evidence import (
    Entry,
    EvidenceLog,
    StorageError,
    canonical,
    checked_json,
    digest,
    ensure_directory,
    publish,
    sync_directory,
)
from solakit.jobs import CONTROL_ID, next_due
from solakit.records import (
    ErrorCode,
    Failure,
    Job,
    Message,
    ModelStep,
    Origin,
    Run,
    RunState,
    SchedulerControl,
    Session,
    SessionMode,
)
from solakit.runtime import DataDirectoryLock, OwnershipError
from solakit.search import rebuild
from solakit.store import Store, StoredRecord

VERSION = 1
MARKER = "phase13.json"
TRANSCRIPT = re.compile(r"^(.+)\.jsonl(?:\.([1-9][0-9]*))?$")


def metadata(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("expected a regular file")
    with path.open("rb") as stream:
        fingerprint = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"length": path.stat().st_size, "sha256": fingerprint}


def disjoint(destination: Path, sources: list[Path]) -> None:
    target = destination.resolve()
    if destination.is_symlink() or any(
        target.is_relative_to(p.resolve()) or p.resolve().is_relative_to(target) for p in sources
    ):
        raise ValueError("destination must be separate from every source tree")


def tree(root: Path) -> list[Path]:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("expected a directory without symlinks")
    paths = sorted(root.rglob("*"))
    if any(p.is_symlink() or not (p.is_file() or p.is_dir()) for p in paths):
        raise ValueError("snapshot trees may contain only regular files and directories")
    return [p for p in paths if p.is_file()]


def object_json(path: Path) -> dict[str, Any]:
    value = checked_json(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path.name}")
    return value


def timestamp(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("legacy timestamps must have an explicit UTC offset")
    return result.astimezone(UTC)


def identity(source: str, kind: str, old: str) -> str:
    return uuid5(NAMESPACE_URL, canonical(["sola-phase13-v1", source, kind, old]).decode()).hex


@contextmanager
def legacy_lock(root: Path) -> Iterator[None]:
    # Open the existing lock read-only; never create or rewrite legacy files.
    fd = os.open(root / ".runtime.lock", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError(
                "legacy runtime is active; stop it cleanly before snapshotting"
            ) from None
        yield
    finally:
        os.close(fd)


def repository_manifest(repo: Path) -> dict[str, Any]:
    def git(*args: str) -> bytes:
        return subprocess.check_output(["git", "-C", str(repo), *args])

    names = git("ls-files", "--cached", "--others", "--exclude-standard", "-z").split(b"\0")
    files: dict[str, Any] = {}
    for name in names:
        if name:
            path = repo / os.fsdecode(name)
            if path.is_symlink():
                files[os.fsdecode(name)] = {"symlink": os.readlink(path)}
            else:
                files[os.fsdecode(name)] = metadata(path) if path.exists() else {"deleted": True}
    return {
        "root": str(repo.resolve()),
        "commit": git("rev-parse", "HEAD").decode().strip(),
        "status": git("status", "--porcelain=v1", "-z").decode(),
        "files": files,
    }


def snapshot(
    source: Path,
    destination: Path,
    *,
    legacy_repo: Path,
    includes: dict[str, Path] | None = None,
) -> dict[str, Any]:
    if source.is_symlink():
        raise ValueError("legacy root cannot be a symlink")
    source, destination = source.resolve(), destination.absolute()
    includes = includes or {}
    disjoint(destination, [source, legacy_repo, *includes.values()])
    for label, path in includes.items():
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", label) or path.is_symlink():
            raise ValueError("include labels must be simple names; symlinks are unsupported")
    with legacy_lock(source):
        sources = {"legacy/" + str(p.relative_to(source)): p for p in tree(source)}
        for label, path in includes.items():
            paths = tree(path) if path.is_dir() else [path]
            for p in paths:
                relative = p.relative_to(path) if path.is_dir() else Path(p.name)
                sources[f"extras/{label}/{relative}"] = p
        before = {name: metadata(p) for name, p in sources.items()}
        repo = repository_manifest(legacy_repo)
        destination.mkdir(mode=0o700, parents=False, exist_ok=False)
        files = {}
        for name, path in sources.items():
            if name in ("legacy/.runtime.lock", "legacy/app.db-wal", "legacy/app.db-shm"):
                continue
            target = destination / name
            if name == "legacy/app.db":
                ensure_directory(target.parent)
                target.touch(mode=0o600, exist_ok=False)
                # Recover WAL on private copies; even SQLite shared-memory writes stay off source.
                with tempfile.TemporaryDirectory(prefix=".sqlite-", dir=destination) as temporary:
                    local = Path(temporary) / "app.db"
                    if _copy(path, local) != before[name]:
                        raise ValueError("source database changed during snapshot")
                    wal = path.with_name("app.db-wal")
                    if (
                        wal.exists()
                        and _copy(wal, local.with_name("app.db-wal")) != before["legacy/app.db-wal"]
                    ):
                        raise ValueError("source WAL changed during snapshot")
                    with (
                        closing(sqlite3.connect(local)) as src,
                        closing(sqlite3.connect(target)) as dst,
                    ):
                        src.backup(dst)
                        dst.execute("PRAGMA journal_mode=DELETE")
                        if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                            raise ValueError("legacy SQLite integrity check failed")
                with target.open("rb") as stream:
                    os.fsync(stream.fileno())
                files[name] = metadata(target)
            else:
                files[name] = _copy(path, target)
                if files[name] != before[name]:
                    raise ValueError("source changed during snapshot")
        if before != {name: metadata(p) for name, p in sources.items()} or set(
            p.relative_to(source).as_posix() for p in tree(source)
        ) != {name.removeprefix("legacy/") for name in sources if name.startswith("legacy/")}:
            raise ValueError("legacy source changed during snapshot")
        if repository_manifest(legacy_repo) != repo:
            raise ValueError("legacy checkout changed during snapshot")
        for label, path in includes.items():
            if path.is_dir() and {str(p.relative_to(path)) for p in tree(path)} != {
                name.removeprefix(f"extras/{label}/")
                for name in sources
                if name.startswith(f"extras/{label}/")
            }:
                raise ValueError("included source changed during snapshot")
        manifest = {
            "schema_version": VERSION,
            "kind": "sola-legacy-snapshot",
            "created_at": datetime.now(UTC).isoformat(),
            "source_root": str(source),
            "repository": repo,
            "includes": {k: str(v.resolve()) for k, v in includes.items()},
            "source_files": before,
            "files": files,
            "login_policy": "invalidate; retain raw backup only",
            "external_services": "not copied; separate memory/resource backups required",
        }
        for path in sorted(destination.rglob("*"), reverse=True):
            if path.is_dir():
                sync_directory(path)
        publish(destination / "manifest.json", canonical(manifest))
        sync_directory(destination.parent)
    return {
        "status": "snapshotted",
        "files": len(files),
        "bytes": sum(m["length"] for m in files.values()),
    }


def validate_snapshot(root: Path) -> dict[str, Any]:
    files = tree(root)
    manifest = object_json(root / "manifest.json")
    if (
        type(manifest.get("schema_version")) is not int
        or manifest.get("schema_version") != VERSION
        or manifest.get("kind") != "sola-legacy-snapshot"
    ):
        raise ValueError("unsupported legacy snapshot")
    expected = manifest["files"]
    if {str(p.relative_to(root)) for p in files} != set(expected) | {"manifest.json"}:
        raise ValueError("snapshot file inventory mismatch")
    for name, info in expected.items():
        path = Path(name)
        if path.is_absolute() or ".." in path.parts or path.as_posix() != name:
            raise ValueError("unsafe snapshot path")
        if metadata(root / name) != info:
            raise ValueError("snapshot checksum mismatch")
    return manifest


def json_lines(path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    with path.open("rb") as stream:
        for number, raw in enumerate(stream, 1):
            try:
                value = checked_json(raw)
            except ValueError:
                # A torn final record stays in the raw snapshot and is reported separately.
                if not raw.endswith(b"\n"):
                    return
                raise ValueError(f"invalid complete JSONL record: {path.name}:{number}") from None
            if not isinstance(value, dict):
                raise ValueError(f"expected a JSONL object: {path.name}:{number}")
            yield number, value


def catalog(root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    legacy = root / "legacy"
    sessions = {}
    runs = {}
    transcripts = []
    run_order: dict[str, int] = {}
    anomalies: list[dict[str, Any]] = []
    for path in sorted((legacy / "sessions").glob("*.meta.json")):
        raw = object_json(path)
        old = path.name.removesuffix(".meta.json")
        if raw["id"] != old:
            raise ValueError("session filename/identifier mismatch")
        sessions[old] = raw
    for path in sorted((legacy / "runs").glob("*.json")):
        if "." in path.stem:
            continue
        raw = object_json(path)
        if path.stem != raw.get("id"):
            raise ValueError("run filename/identifier mismatch")
        runs[raw["id"]] = raw
        if raw.get("finished_at") and timestamp(raw["finished_at"]) < timestamp(raw["created_at"]):
            anomalies.append(
                {
                    "kind": "run_timestamp_inversion",
                    "run": raw["id"],
                    "mapping": "clamp normalized end to creation; preserve original timestamps raw",
                }
            )
        journal = path.with_suffix(".events.jsonl")
        if journal.exists():
            latest = next(reversed(list(json_lines(journal))), None)
            if latest is None or latest[1] != raw:
                anomalies.append(
                    {
                        "kind": "run_journal_conflict",
                        "run": raw["id"],
                        "snapshot_status": raw["status"],
                        "journal_status": latest[1].get("status") if latest else None,
                        "fields": sorted(
                            k
                            for k in raw.keys() | latest[1].keys()
                            if raw.get(k) != latest[1].get(k)
                        )
                        if latest
                        else [],
                    }
                )
                if latest is None or latest[1].get("status") != raw["status"]:
                    runs[raw["id"]] = {**raw, "status": "interrupted"}
    for path in (legacy / "sessions").glob("*.jsonl*"):
        match = TRANSCRIPT.fullmatch(path.name)
        if match is None:
            raise ValueError("unrecognized transcript segment")
        old, segment = match.groups()
        transcripts.append((old, int(segment) if segment else 2**63, path))
    transcripts.sort()
    for old, _, path in transcripts:
        if old not in sessions:
            raise ValueError("transcript has no session metadata")
        for _, raw in json_lines(path):
            run_id = raw["run_id"]
            run_order.setdefault(run_id, len(run_order))
            if run_id not in runs:
                runs[run_id] = {
                    "id": run_id,
                    "session": old,
                    "profile": "legacy-unavailable",
                    "created_at": raw["ts"],
                    "status": "interrupted",
                    "missing_snapshot": True,
                }
                anomalies.append({"kind": "missing_run_snapshot", "run": run_id})
            if runs[run_id]["session"] != old:
                raise ValueError("run appears in multiple sessions")
    for raw in runs.values():
        if raw["session"] not in sessions:
            raise ValueError("run has no session metadata")
    for name in manifest["files"]:
        path = root / name
        if ".jsonl" in path.name and path.stat().st_size:
            last = path.read_bytes().splitlines(keepends=True)[-1]
            if not last.endswith(b"\n"):
                try:
                    checked_json(last)
                except ValueError:
                    anomalies.append({"kind": "torn_tail", "file": name, "bytes": len(last)})
    return {
        "sessions": sessions,
        "runs": runs,
        "transcripts": transcripts,
        "run_order": run_order,
        "anomalies": anomalies,
    }


def batches(
    root: Path, manifest: dict[str, Any], cat: dict[str, Any]
) -> Iterator[tuple[str, list[StoredRecord | Entry]]]:
    source = manifest["source_root"]
    at = timestamp(manifest["created_at"])

    def mapped(kind: str, old: str) -> str:
        return identity(source, kind, old)

    for old, raw in cat["sessions"].items():
        yield (
            "session:" + old,
            [
                Session(
                    id=mapped("session", old),
                    origin=Origin.SYSTEM,
                    created_at=timestamp(raw["created_at"]),
                    title=raw.get("title", ""),
                    mode=SessionMode(raw.get("memory_scope", "shared")),
                    archived=bool(raw.get("archived_at")),
                    pinned=raw.get("pinned")
                    if raw.get("pinned") is not None
                    else bool(raw.get("bound_channel")),
                )
            ],
        )
    for old, raw in sorted(
        cat["runs"].items(),
        key=lambda item: (
            item[1]["session"],
            cat["run_order"].get(item[0], len(cat["run_order"])),
            timestamp(item[1]["created_at"]),
            item[0],
        ),
    ):
        status = raw["status"]
        state = {
            "completed": RunState.COMPLETED,
            "cancelled": RunState.CANCELLED,
            "interrupted": RunState.INTERRUPTED,
            "queued": RunState.INTERRUPTED,
            "running": RunState.INTERRUPTED,
        }.get(status, RunState.FAILED)
        start = timestamp(raw["created_at"])
        end = timestamp(raw["finished_at"]) if raw.get("finished_at") else max(at, start)
        end = max(start, end)
        yield (
            "run:" + old,
            [
                Run(
                    id=mapped("run", old),
                    request_id=mapped("request", old),
                    session_id=mapped("session", raw["session"]),
                    origin=Origin.SYSTEM,
                    profile=raw["profile"],
                    created_at=start,
                    started_at=start,
                    ended_at=end,
                    state=state,
                    reason=None
                    if state == RunState.COMPLETED
                    else Failure(
                        code=ErrorCode.PROCESS_INTERRUPTED
                        if state == RunState.INTERRUPTED
                        else ErrorCode.TOOL_FAILED,
                        message="Historical legacy outcome: " + status,
                    ),
                )
            ],
        )
    model_indices: Counter[str] = Counter()
    for old, _, path in cat["transcripts"]:
        relative = str(path.relative_to(root))
        records: list[StoredRecord | Entry] = []
        for line, raw in json_lines(path):
            role, content = raw["role"], raw.get("content")
            # Tool groups, images and compaction remain exact, scoped legacy evidence.
            if (
                role in ("user", "assistant")
                and raw.get("kind", "message") == "message"
                and isinstance(content, str)
            ):
                sid, rid = mapped("session", old), mapped("run", raw["run_id"])
                mid = mapped("message", f"{relative}:{line}")
                created = timestamp(raw["ts"])
                records.append(
                    Message(
                        id=mid,
                        origin=Origin.SYSTEM,
                        created_at=created,
                        session_id=sid,
                        run_id=rid,
                        role=role,
                        content=content,
                    )
                )
                if role == "assistant":
                    model_indices[rid] += 1
                    records.append(
                        ModelStep(
                            id=mapped("model", f"{relative}:{line}"),
                            origin=Origin.SYSTEM,
                            created_at=created,
                            session_id=sid,
                            run_id=rid,
                            index=model_indices[rid],
                            model="legacy-unrecorded",
                            content=content,
                        )
                    )
            if len(records) >= 200:
                yield f"dialogue:{relative}:{line}", records
                records = []
        if records:
            yield f"dialogue:{relative}:end", records
    dbpath = root / "legacy/app.db"
    with closing(sqlite3.connect(dbpath.as_uri() + "?mode=ro", uri=True)) as db:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("legacy SQLite integrity check failed")
        for old, payload, deleted in db.execute(
            "SELECT id,payload,deleted FROM job_definitions ORDER BY id"
        ):
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", old):
                raise ValueError("unsafe legacy job identifier")
            raw = checked_json(payload.encode())
            if not isinstance(raw, dict):
                raise ValueError("expected a job payload object")
            if raw["id"] != old:
                raise ValueError("job row/payload identifier mismatch")
            state_path = root / "legacy/jobs" / (old + ".jsonl")
            states = list(json_lines(state_path)) if state_path.exists() else []
            cursor = states[-1][1] if states else {}
            bound = raw.get("bind_session")
            if (
                bound not in cat["sessions"]
                or cat["sessions"][bound].get("memory_scope") == "ephemeral"
            ):
                bound = None
            job = Job(
                id=mapped("job", old),
                created_at=at,
                origin=Origin.SCHEDULED,
                profile=raw["profile"],
                task=raw["task"],
                schedule="once:" + raw["run_at"] if raw.get("run_at") else "cron:" + raw["cron"],
                timezone=raw.get("timezone", "America/Denver"),
                session_id=mapped("session", bound) if bound else None,
                mode=SessionMode(cat["sessions"][bound].get("memory_scope", "shared"))
                if bound
                else SessionMode.EPHEMERAL,
                enabled=False,
                paused=True,
                deleted=bool(deleted),
                next_due=None,
                consumed=bool(
                    cursor.get("consumed")
                    or cursor.get("active")
                    or (raw.get("run_at") and not states)
                ),
                tools=(),
                workspace_roots=(),
                delivery="none",
            )
            next_due(job, at)  # Validate timezone/schedule even while disabled.
            yield (
                "job:" + old,
                [
                    job,
                    Entry(
                        id=mapped("job-row", old),
                        kind="legacy",
                        record={"id": old, "payload": raw, "deleted": deleted, "cursor": cursor},
                        details={
                            "migration": VERSION,
                            "source": "legacy/app.db:job_definitions",
                            "old_id": old,
                        },
                    ),
                ],
            )
    yield (
        "scheduler",
        [
            SchedulerControl(
                id=CONTROL_ID,
                created_at=at,
                origin=Origin.SYSTEM,
                paused=True,
            )
        ],
    )
    for name, info in sorted(manifest["files"].items()):
        path = Path(name)
        if (
            len(path.parts) < 3
            or path.parts[0] != "legacy"
            or path.parts[1]
            not in (
                "sessions",
                "runs",
                "jobs",
                "pack-operations",
                "channel-posts",
                "recovery",
            )
        ):
            continue
        records = []
        values: Iterator[tuple[int, dict[str, Any]]]
        if ".jsonl" in path.name:
            values = json_lines(root / name)
        elif path.suffix == ".json":
            value = checked_json((root / name).read_bytes())
            values = iter([(1, value if isinstance(value, dict) else {"value": value})])
        else:
            continue
        for line, raw in values:
            details: dict[str, Any] = {"migration": VERSION, "source": name, "line": line, **info}
            if path.parts[1] == "sessions":
                old = path.name.split(".")[0]
                if old in cat["sessions"]:
                    details["session_id"] = mapped("session", old)
            old_run = raw.get("run_id")
            if path.parts[1] == "runs":
                old_run = path.name.split(".")[0]
            if isinstance(old_run, str) and old_run in cat["runs"]:
                details["run_id"] = mapped("run", old_run)
                details["session_id"] = mapped("session", cat["runs"][old_run]["session"])
            records.append(
                Entry(
                    id=mapped("raw", f"{name}:{line}"),
                    kind="legacy",
                    record=raw,
                    details=details,
                )
            )
            if len(records) >= 200:
                yield f"raw:{name}:{line}", records
                records = []
        if records:
            yield f"raw:{name}:end", records


def audit(root: Path, manifest: dict[str, Any], cat: dict[str, Any]) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    pair_anomalies = []
    calls: Counter[tuple[str, str, str]] = Counter()
    results: Counter[tuple[str, str, str]] = Counter()
    for old, _, path in cat["transcripts"]:
        for _, raw in json_lines(path):
            for call in raw.get("tool_calls") or []:
                calls[old, raw["run_id"], call["id"]] += 1
            if raw.get("tool_call_id"):
                results[old, raw["run_id"], raw["tool_call_id"]] += 1
    for pair in sorted(calls.keys() | results.keys()):
        if calls[pair] != 1 or results[pair] != 1:
            pair_anomalies.append(
                {
                    "session": pair[0],
                    "run": pair[1],
                    "call_id": pair[2],
                    "calls": calls[pair],
                    "results": results[pair],
                }
            )
    jobs = {}
    job_reviews = []
    for key, records in batches(root, manifest, cat):
        for record in records:
            counts[record.kind if isinstance(record, Entry) else type(record).__name__] += 1
            if isinstance(record, Job):
                jobs[key.removeprefix("job:")] = record.id
            elif isinstance(record, Entry) and key.startswith("job:"):
                job_raw = record.record["payload"]
                assert isinstance(job_raw, dict)
                job_reviews.append(
                    {
                        "id": key.removeprefix("job:"),
                        "bound_session": job_raw.get("bind_session"),
                        "sub_session": job_raw.get("sub_session", False),
                        "cursor": record.record["cursor"],
                        "review": "review profile/tools, binding, task templates and delivery",
                    }
                )
    references = []
    for old, raw in cat["runs"].items():
        for field, suffix in (
            ("prompt_sha256", ".prompt.txt"),
            ("turn_context_sha256", ".turn.txt"),
            ("tool_catalog_sha256", ".tool-catalog.json"),
            ("tool_schema_sha256", ".tool-schemas.json"),
        ):
            if raw.get(field):
                name = f"legacy/runs/{old}{suffix}"
                info = manifest["files"].get(name)
                status = (
                    "unavailable"
                    if info is None
                    else ("matched" if info["sha256"] == raw[field] else "mismatch")
                )
                references.append({"run": old, "file": name, "status": status})
    return {
        "schema_version": VERSION,
        "counts": dict(counts),
        "anomalies": cat["anomalies"],
        "tool_pairing": pair_anomalies,
        "artifact_references": references,
        "job_review": job_reviews,
        "id_map": {
            kind: {old: identity(manifest["source_root"], kind, old) for old in cat[key]}
            for kind, key in (("session", "sessions"), ("run", "runs"))
        },
        "mapping": {
            "run_origin": "system; original origin/lineage retained in raw evidence",
            "run_start": "legacy created_at (legacy has no separate start timestamp)",
            "inverted_run_times": (
                "normalized end clamped to creation; original timestamps retained"
            ),
            "nonterminal_runs": "interrupted; never queued for execution",
            "jobs": "disabled/paused; no tools/roots/delivery; uncertain once cursors consumed",
            "dialogue": "text user/assistant messages and assistant model steps",
            "other_history": "scoped legacy JSONL retains tools/images/compaction/unknown fields",
            "login_state": "raw private backup only; no v2 logins imported",
        },
        "job_id_map": jobs,
        "cutover_gate": "pending: anomalies, job/config/resources review and live/operator gates",
    }


def immutable_json(path: Path, value: object) -> None:
    data = canonical(value)
    if path.exists():
        if path.is_symlink() or path.read_bytes() != data:
            raise ValueError("migration checkpoint/report conflicts")
    else:
        publish(path, data)


async def import_snapshot(source: Path, destination: Path) -> dict[str, Any]:
    manifest = validate_snapshot(source)
    disjoint(
        destination,
        [source, Path(manifest["source_root"]), *[Path(p) for p in manifest["includes"].values()]],
    )
    if (destination / "imports/phase13-checkpoint.json").exists():
        return {**verify(source, destination), "status": "imported", "new_records": 0}
    cat = catalog(source, manifest)
    report = audit(source, manifest, cat)  # Preflight all parsers/mappings before writing v2 state.
    fingerprint = metadata(source / "manifest.json")["sha256"]
    marker = {"schema_version": VERSION, "snapshot_sha256": fingerprint}
    if destination.exists() and (not (destination / "imports" / MARKER).is_file()):
        raise ValueError("import requires a new directory or its own interrupted import")
    ensure_directory(destination)
    owner = DataDirectoryLock(destination)
    owner.acquire()
    store = Store(destination)
    try:
        ensure_directory(destination / "imports")
        immutable_json(destination / "imports" / MARKER, marker)
        # Keep original filenames and all bytes, including secrets and non-JSON artifacts.
        retained = destination / "imports/phase13-source"
        ensure_directory(retained)
        tree(
            destination
        )  # Reject aliases in a resumed destination before copying or opening SQLite.
        staging = destination / "imports/.phase13-copy"
        for path in tree(source):
            target = retained / path.relative_to(source)
            expected_file = metadata(path)
            if target.exists():
                actual = metadata(target)
            else:
                staging.unlink(missing_ok=True)
                actual = _copy(path, staging)
                ensure_directory(target.parent)
                os.link(staging, target)
                sync_directory(target.parent)
                staging.unlink()
            if actual != expected_file:
                raise ValueError("retained source conflicts with snapshot")
        await store.start()
        imported = 0
        for key, records in batches(source, manifest, cat):
            batch_key = "phase13:" + key
            expected = digest(canonical([r.model_dump(mode="json") for r in records]))
            saved = store.db.execute(
                "SELECT fingerprint FROM imports WHERE source=?", (batch_key,)
            ).fetchone()
            if saved:
                if saved[0] != expected:
                    raise ValueError("import checkpoint differs from source")
                continue
            store._flush()
            try:
                with store.db:
                    for record in records:
                        if isinstance(record, Session):
                            store.db.execute("INSERT INTO sessions VALUES (?,0)", (record.id,))
                        entry = (
                            record
                            if isinstance(record, Entry)
                            else store._entry(record, {"migration": VERSION, "batch": key})
                        )
                        store.db.execute(
                            "INSERT INTO outbox(id,body) VALUES (?,?)", (entry.id, entry.encoded())
                        )
                    store.db.execute("INSERT INTO imports VALUES (?,?)", (batch_key, expected))
                store.fault("state_commit")
                store._flush()
            except BaseException:
                store.failed = True
                raise
            imported += len(records)
        store._reconcile()
        rebuild(destination)
        validate_snapshot(source)
        for directory in sorted(retained.rglob("*"), reverse=True):
            if directory.is_dir():
                sync_directory(directory)
        immutable_json(destination / "imports/phase13-report.json", report)
        cursors = {row[0]: row[1] for row in store.db.execute("SELECT id,sequence FROM sessions")}
        checkpoint = {
            **marker,
            "evidence_high_water": dict(store.log.sizes),
            "session_high_water": cursors,
            "evidence_files": {p.name: metadata(p) for p in store.log.paths()},
        }
        immutable_json(destination / "imports/phase13-checkpoint.json", checkpoint)
        return {
            "status": "imported",
            "new_records": imported,
            "counts": report["counts"],
            "anomalies": len(report["anomalies"]),
            "tool_pairing_anomalies": len(report["tool_pairing"]),
            "scheduling": "paused",
            "cutover_gate": "pending",
        }
    finally:
        await store.stop()
        owner.release()


def verify(source: Path, destination: Path) -> dict[str, Any]:
    manifest = validate_snapshot(source)
    cat = catalog(source, manifest)
    owner = DataDirectoryLock(destination)
    if not (destination / "state.db").is_file():
        raise ValueError("candidate state.db missing")
    owner.acquire()
    try:
        if object_json(destination / "imports" / MARKER) != {
            "schema_version": VERSION,
            "snapshot_sha256": metadata(source / "manifest.json")["sha256"],
        }:
            raise ValueError("candidate belongs to another snapshot")
        retained = destination / "imports/phase13-source"
        if {str(p.relative_to(retained)) for p in tree(retained)} != {
            str(p.relative_to(source)) for p in tree(source)
        }:
            raise ValueError("retained inventory mismatch")
        for path in tree(source):
            if metadata(path) != metadata(retained / path.relative_to(source)):
                raise ValueError("retained source hash mismatch")
        with closing(
            sqlite3.connect((destination / "state.db").as_uri() + "?mode=ro", uri=True)
        ) as db:
            db.row_factory = sqlite3.Row
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("candidate state integrity check failed")
            store = Store(destination)
            store._connection = db
            store.log.references = {ref.entry_id: ref for ref, _ in store.log.scan()}
            store._reconcile()
            if db.execute("SELECT 1 FROM outbox WHERE delivered=0").fetchone():
                raise ValueError("candidate evidence is pending; finish offline recovery first")
            count = 0
            actual_entries = iter(db.execute("SELECT body FROM outbox ORDER BY ordinal"))
            for key, records in batches(source, manifest, cat):
                saved = db.execute(
                    "SELECT fingerprint FROM imports WHERE source=?", ("phase13:" + key,)
                ).fetchone()
                expected = digest(canonical([r.model_dump(mode="json") for r in records]))
                if saved is None or saved[0] != expected:
                    raise ValueError("missing or invalid batch checkpoint")
                for record in records:
                    row = next(actual_entries, None)
                    if row is None:
                        raise ValueError("candidate evidence is missing")
                    entry = Entry.model_validate_json(row[0])
                    if isinstance(record, Entry) and entry != record:
                        raise ValueError("legacy envelope/order differs from snapshot")
                    if not isinstance(record, Entry) and (
                        entry.record != record.model_dump(mode="json")
                        or entry.details != {"migration": VERSION, "batch": key}
                    ):
                        raise ValueError("mapped record/order differs from snapshot")
                    count += 1
            if next(actual_entries, None) is not None:
                raise ValueError("candidate has new evidence; use reconciliation before rollback")
            checkpoint = object_json(destination / "imports/phase13-checkpoint.json")
            if checkpoint["evidence_high_water"] != {
                p.name: p.stat().st_size for p in store.log.paths()
            }:
                raise ValueError("candidate has new evidence; use reconciliation before rollback")
            if checkpoint["evidence_files"] != {p.name: metadata(p) for p in store.log.paths()}:
                raise ValueError("candidate checkpoint hash mismatch")
            if checkpoint["session_high_water"] != {
                row[0]: row[1] for row in db.execute("SELECT id,sequence FROM sessions")
            }:
                raise ValueError("candidate session high-water mismatch")
            report = audit(source, manifest, cat)
            if object_json(destination / "imports/phase13-report.json") != report:
                raise ValueError("migration report mismatch")
        return {
            "status": "verified",
            "records": count,
            "counts": report["counts"],
            "anomalies": len(report["anomalies"]),
            "tool_pairing_anomalies": len(report["tool_pairing"]),
            "cutover_gate": "pending",
        }
    finally:
        owner.release()


def reconcile(destination: Path, output: Path) -> dict[str, Any]:
    """Inventory every post-checkpoint entry; never reverse-import or replay effects."""
    owner = DataDirectoryLock(destination)
    if not (destination / "state.db").is_file():
        raise ValueError("candidate state.db missing")
    owner.acquire()
    try:
        retained = destination / "imports/phase13-source"
        manifest = validate_snapshot(retained)
        disjoint(
            output,
            [
                destination,
                Path(manifest["source_root"]),
                *[Path(p) for p in manifest["includes"].values()],
            ],
        )
        checkpoint = object_json(destination / "imports/phase13-checkpoint.json")
        if checkpoint["snapshot_sha256"] != metadata(retained / "manifest.json")["sha256"]:
            raise ValueError("checkpoint belongs to another snapshot")
        baseline = checkpoint["evidence_high_water"]
        if set(baseline) != set(checkpoint["evidence_files"]) or any(
            not re.fullmatch(r"[0-9]{8}\.jsonl", name) for name in baseline
        ):
            raise ValueError("invalid checkpoint evidence inventory")
        for name, info in checkpoint["evidence_files"].items():
            path = destination / "evidence" / name
            with path.open("rb") as stream:
                prefix = stream.read(info["length"])
            if len(prefix) != info["length"] or digest(prefix) != info["sha256"]:
                raise ValueError("candidate checkpoint evidence changed")
        entries = []
        counts: Counter[str] = Counter()
        log = EvidenceLog(destination)
        seen: dict[str, int] = {}
        for ref, entry in log.scan():
            seen[ref.segment] = ref.offset + ref.length
            offset = baseline.get(ref.segment, 0)
            if ref.offset < offset < ref.offset + ref.length:
                raise ValueError("checkpoint splits an evidence record")
            if ref.offset >= offset:
                counts[entry.kind] += 1
                entries.append(
                    {
                        "kind": entry.kind,
                        "id": entry.record.get("id", entry.id),
                        "reference": ref.model_dump(mode="json"),
                        "review": "transfer/reconcile; never automatically replay",
                    }
                )
        if any(seen.get(name, 0) < size for name, size in baseline.items()):
            raise ValueError("candidate evidence is missing checkpoint bytes")
        with closing(
            sqlite3.connect((destination / "state.db").as_uri() + "?mode=ro", uri=True)
        ) as db:
            db.row_factory = sqlite3.Row
            store = Store(destination)
            store._connection = db
            store.log.references = {ref.entry_id: ref for ref, _ in log.scan()}
            store._reconcile()
            if db.execute("SELECT 1 FROM outbox WHERE delivered=0").fetchone():
                raise ValueError("candidate evidence is pending; finish offline recovery first")
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("candidate state integrity check failed")
            if db.execute(
                "SELECT 1 FROM entities WHERE kind='run' "
                "AND json_extract(body,'$.state') IN ('queued','running')"
            ).fetchone():
                raise ValueError("quiesce candidate runs before reconciliation")
            control = db.execute(
                "SELECT body FROM entities WHERE kind='scheduler' AND id=?", (CONTROL_ID,)
            ).fetchone()
            if not control or not json.loads(control[0])["paused"]:
                raise ValueError("pause candidate scheduling before reconciliation")
            auth = {
                "sessions": db.execute("SELECT count(*) FROM login_sessions").fetchone()[0],
                "throttle_rows": db.execute("SELECT count(*) FROM login_throttle").fetchone()[0],
            }
        artifacts = {
            str(p.relative_to(destination)): metadata(p) for p in tree(destination / "artifacts")
        }
        changed = bool(entries or artifacts or any(auth.values()))
        report = {
            "schema_version": VERSION,
            "checkpoint": checkpoint,
            "counts": dict(counts),
            "entries": entries,
            "artifacts": artifacts,
            "auth_state": auth,
            "rollback": "reconciliation_required" if changed else "before_new_writes",
            "reverse_import": "unsupported",
            "required_review": [
                "conversations and artifacts",
                "occurrences/consumption/job edits/tombstones",
                "memory commits/extraction",
                "external receipts and uncertain effects",
            ],
        }
        publish(output, canonical(report))
        return {"status": "reported", "new_entries": len(entries), "rollback": report["rollback"]}
    finally:
        owner.release()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("snapshot")
    command.add_argument("--source", required=True, type=Path)
    command.add_argument("--legacy-repo", required=True, type=Path)
    command.add_argument("--include", action="append", default=[], metavar="LABEL=PATH")
    command.add_argument("destination", type=Path)
    for name in ("import", "verify"):
        command = commands.add_parser(name)
        command.add_argument("snapshot", type=Path)
        command.add_argument("destination", type=Path)
    command = commands.add_parser("inventory")
    command.add_argument("snapshot", type=Path)
    command.add_argument("output", type=Path)
    command = commands.add_parser("reconcile")
    command.add_argument("destination", type=Path)
    command.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "snapshot":
            includes = {}
            for value in args.include:
                label, path = value.split("=", 1)
                if label in includes:
                    raise ValueError("duplicate include label")
                includes[label] = Path(path).expanduser().absolute()
            result = snapshot(
                args.source, args.destination, legacy_repo=args.legacy_repo, includes=includes
            )
        elif args.command == "import":
            result = asyncio.run(
                import_snapshot(args.snapshot.resolve(), args.destination.absolute())
            )
        elif args.command == "verify":
            result = verify(args.snapshot.resolve(), args.destination.absolute())
        elif args.command == "inventory":
            source = args.snapshot.resolve()
            manifest = validate_snapshot(source)
            disjoint(
                args.output,
                [
                    source,
                    Path(manifest["source_root"]),
                    *[Path(p) for p in manifest["includes"].values()],
                ],
            )
            report = audit(source, manifest, catalog(source, manifest))
            publish(args.output, canonical(report))
            result = {
                "status": "inventoried",
                "counts": report["counts"],
                "anomalies": len(report["anomalies"]),
                "cutover_gate": "pending",
            }
        else:
            result = reconcile(args.destination.absolute(), args.output.absolute())
    except ValidationError:
        print(json.dumps({"status": "failed", "error": "legacy record schema validation failed"}))
        raise SystemExit(2) from None
    except KeyError, TypeError, UnicodeError:
        print(json.dumps({"status": "failed", "error": "malformed legacy data or manifest"}))
        raise SystemExit(2) from None
    except (
        ValueError,
        OSError,
        sqlite3.Error,
        StorageError,
        OwnershipError,
        subprocess.CalledProcessError,
    ) as error:
        # Paths can be private; never print record bodies or credential values.
        print(json.dumps({"status": "failed", "error": str(error)}))
        raise SystemExit(2) from None
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
