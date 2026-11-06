#!/usr/bin/env python3
"""Plan or import Hermes/OpenClaw history into a Solakit evidence tree.

The default is a read-only dry run. Pass --apply only after reviewing the JSON
report. Legacy inputs are never modified. Run this while Solakit and Hermes are
stopped so the SQLite snapshots and destination evidence cannot change under it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import sys
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from solakit.core.schemas import Message, Origin, Run, RunStatus, Session
from solakit.core.store import Store, atomic_write

UUID_RE = re.compile(
    r"(?P<id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)
ALLOWED_ROLES = {"system", "user", "assistant", "tool"}


@dataclass
class LegacySession:
    source_key: str
    source: str
    title: str
    created_at: datetime
    profile: str
    messages: list[Message]
    parent_key: str | None = None


@dataclass
class Report:
    counts: Counter = field(default_factory=Counter)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def payload(self, *, apply: bool, destination: Path) -> dict[str, Any]:
        return {
            "mode": "apply" if apply else "dry-run",
            "destination": str(destination),
            "counts": dict(sorted(self.counts.items())),
            "warnings": self.warnings,
            "errors": self.errors,
        }


def utc(value: float | int | str | None, fallback: datetime | None = None) -> datetime:
    if isinstance(value, (float, int)):
        # OpenClaw embeds both seconds and milliseconds.
        return datetime.fromtimestamp(
            value / (1000 if value > 10_000_000_000 else 1), UTC
        )
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
        except ValueError:
            pass
    return fallback or datetime.fromtimestamp(0, UTC)


def stable_id(prefix: str, value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "-", value).strip("-")
    candidate = f"{prefix}-{cleaned}"
    if len(candidate) <= 128:
        return candidate
    digest = hashlib.sha256(value.encode()).hexdigest()
    return f"{prefix}-{digest}"


def json_value(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def hermes_databases(root: Path) -> list[tuple[str, Path]]:
    found = [("default", root / "state.db")]
    profiles = root / "profiles"
    if profiles.is_dir():
        found.extend(
            (path.parent.name, path) for path in sorted(profiles.glob("*/state.db"))
        )
    return [(profile, path) for profile, path in found if path.is_file()]


def sqlite_ro(path: Path) -> sqlite3.Connection:
    # mode=ro prevents an accidental database creation or journal write.
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def normalize_tool_calls(value: Any) -> list[dict[str, Any]] | None:
    calls = json_value(value, None)
    if not isinstance(calls, list):
        return None
    return [call for call in calls if isinstance(call, dict)] or None


def load_hermes(root: Path, report: Report) -> tuple[list[LegacySession], set[str]]:
    result: list[LegacySession] = []
    covered_openclaw: set[str] = set()
    for profile, path in hermes_databases(root):
        try:
            db = sqlite_ro(path)
            sessions = db.execute(
                "SELECT * FROM sessions ORDER BY started_at, id"
            ).fetchall()
        except sqlite3.Error as exc:
            report.errors.append(f"cannot read Hermes database {path}: {exc}")
            continue
        report.counts["hermes_databases"] += 1
        for row in sessions:
            legacy_id = str(row["id"])
            if row["source"] == "openclaw":
                match = UUID_RE.search(legacy_id)
                if match:
                    covered_openclaw.add(match.group("id").lower())
            rows = db.execute(
                "SELECT * FROM messages WHERE session_id=? ORDER BY id", (legacy_id,)
            ).fetchall()
            origin = Origin(
                channel=f"legacy-hermes-{row['source']}",
                external_id=f"hermes:{profile}:{legacy_id}",
            )
            run_id = stable_id("migration-run", f"hermes:{profile}:{legacy_id}")
            messages: list[Message] = []
            for item in rows:
                role = str(item["role"])
                if role not in ALLOWED_ROLES:
                    report.counts["hermes_messages_skipped_role"] += 1
                    continue
                messages.append(
                    Message(
                        role=role,
                        content=item["content"],
                        tool_calls=normalize_tool_calls(item["tool_calls"]),
                        tool_call_id=item["tool_call_id"],
                        origin=origin,
                        run_id=run_id,
                        ts=utc(item["timestamp"]),
                    )
                )
            if not messages:
                report.counts["hermes_sessions_empty"] += 1
                continue
            title = row["title"] or row["display_name"] or f"Hermes {legacy_id}"
            result.append(
                LegacySession(
                    source_key=f"hermes:{profile}:{legacy_id}",
                    source="hermes",
                    title=str(title),
                    created_at=utc(row["started_at"]),
                    # Legacy profile names are not Solakit profile grants. Imported
                    # chats must remain usable with Solakit's standard profile.
                    profile="interactive",
                    messages=messages,
                    parent_key=(
                        f"hermes:{profile}:{row['parent_session_id']}"
                        if row["parent_session_id"]
                        else None
                    ),
                )
            )
            report.counts["hermes_messages"] += len(messages)
        db.close()
    report.counts["hermes_sessions"] = len(result)
    report.counts["openclaw_sessions_covered_by_hermes"] = len(covered_openclaw)
    return result, covered_openclaw


def open_text_lines(path: Path, report: Report) -> Iterable[str]:
    if path.suffix != ".zst":
        with path.open(encoding="utf-8") as stream:
            yield from stream
        return
    executable = shutil.which("zstd")
    if not executable:
        report.errors.append(f"zstd is required to read {path}")
        return
    process = subprocess.Popen(
        [executable, "-dc", "--", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    assert process.stdout is not None
    yield from process.stdout
    _, error = process.communicate()
    if process.returncode:
        report.errors.append(f"cannot decompress {path}: {error.strip()}")


def content_text(parts: Any) -> str | None:
    if isinstance(parts, str):
        return parts
    if not isinstance(parts, list):
        return None
    texts = [
        part.get("text", "")
        for part in parts
        if isinstance(part, dict) and part.get("type") == "text"
    ]
    return "".join(texts) or None


def openclaw_message(
    record: dict[str, Any], session_id: str, run_id: str
) -> Message | None:
    body = record.get("message")
    if not isinstance(body, dict):
        return None
    role = body.get("role")
    if role == "toolResult":
        role = "tool"
    if role not in ALLOWED_ROLES:
        return None
    calls = []
    parts = body.get("content")
    if isinstance(parts, list):
        for part in parts:
            if not isinstance(part, dict) or part.get("type") != "toolCall":
                continue
            calls.append(
                {
                    "id": str(part.get("id", "")),
                    "type": "function",
                    "function": {
                        "name": str(part.get("name", "")),
                        "arguments": json.dumps(
                            part.get("arguments", {}),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    },
                }
            )
    return Message(
        role=role,
        content=content_text(parts),
        tool_calls=calls or None,
        tool_call_id=(str(body["toolCallId"]) if body.get("toolCallId") else None),
        origin=Origin(channel="legacy-openclaw", external_id=f"openclaw:{session_id}"),
        run_id=run_id,
        ts=utc(body.get("timestamp") or record.get("timestamp")),
    )


def openclaw_candidates(root: Path) -> dict[str, Path]:
    candidates: dict[str, Path] = {}
    for path in root.iterdir() if root.is_dir() else ():
        match = UUID_RE.search(path.name)
        if not match or "jsonl" not in path.name or "trajectory" in path.name:
            continue
        session_id = match.group("id").lower()
        current = candidates.get(session_id)
        # Prefer the largest copy: deleted suffixes frequently have several snapshots.
        if current is None or path.stat().st_size > current.stat().st_size:
            candidates[session_id] = path
    return candidates


def load_openclaw(root: Path, covered: set[str], report: Report) -> list[LegacySession]:
    result = []
    candidates = openclaw_candidates(root)
    report.counts["openclaw_unique_files"] = len(candidates)
    for session_id, path in sorted(candidates.items()):
        if session_id in covered:
            report.counts["openclaw_files_skipped_covered"] += 1
            continue
        run_id = stable_id("migration-run", f"openclaw:{session_id}")
        messages: list[Message] = []
        created = None
        try:
            for line_no, line in enumerate(open_text_lines(path, report), 1):
                try:
                    record = json.loads(line)
                except (TypeError, ValueError):
                    report.warnings.append(f"invalid JSON skipped: {path}:{line_no}")
                    continue
                if record.get("type") == "session":
                    created = utc(record.get("timestamp"))
                if record.get("type") == "message":
                    message = openclaw_message(record, session_id, run_id)
                    if message:
                        messages.append(message)
        except (OSError, UnicodeError) as exc:
            report.errors.append(f"cannot read {path}: {exc}")
            continue
        if not messages:
            report.counts["openclaw_sessions_empty"] += 1
            continue
        result.append(
            LegacySession(
                source_key=f"openclaw:{session_id}",
                source="openclaw",
                title=f"OpenClaw {session_id}",
                created_at=created or messages[0].ts,
                profile="interactive",
                messages=messages,
            )
        )
        report.counts["openclaw_messages"] += len(messages)
    report.counts["openclaw_sessions"] = len(result)
    return result


def import_sessions(
    sessions: list[LegacySession], destination: Path, report: Report
) -> None:
    store = Store(destination, exclusive=True)
    try:
        existing = {record["id"] for record in store.list_records("sessions")}
        key_to_id = {
            item.source_key: stable_id("legacy", item.source_key) for item in sessions
        }
        for item in sessions:
            session_id = key_to_id[item.source_key]
            if session_id in existing:
                actual = store.transcript(session_id)
                expected = [
                    message.model_dump(mode="json") for message in item.messages
                ]
                if actual != expected:
                    raise RuntimeError(
                        f"destination collision or partial migration: {session_id}"
                    )
                report.counts["destination_sessions_verified_existing"] += 1
                continue
            parent = key_to_id.get(item.parent_key) if item.parent_key else None
            session = Session(
                id=session_id,
                title=item.title,
                default_profile=item.profile,
                memory_scope="shared",
                parent_session=parent,
                created_at=item.created_at,
            )
            # Store.append_message intentionally updates search evidence after every
            # live append. Bulk migration writes one atomic, validated transcript
            # before publishing its metadata, then rebuilds the index once at the
            # end. An interrupted pre-metadata write is therefore only an orphan
            # that a rerun safely replaces.
            records = [message.model_dump(mode="json") for message in item.messages]
            transcript = b"".join(
                json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode(
                    "utf-8"
                )
                + b"\n"
                for record in records
            )
            atomic_write(destination / "sessions" / f"{session.id}.jsonl", transcript)
            store.create_session(session)
            first_user = next(
                (
                    message.content
                    for message in item.messages
                    if message.role == "user"
                ),
                None,
            )
            run = Run(
                id=item.messages[0].run_id,
                session=session.id,
                profile=item.profile,
                input_message=first_user,
                origin=item.messages[0].origin,
                status=RunStatus.completed,
                created_at=item.created_at,
                finished_at=item.messages[-1].ts,
            )
            store.save_run(run)
            existing.add(session_id)
            report.counts["destination_sessions_written"] += 1
            report.counts["destination_messages_written"] += len(item.messages)
        store.rebuild()
    finally:
        store.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes", type=Path, default=Path("~/.hermes").expanduser())
    parser.add_argument(
        "--openclaw",
        type=Path,
        default=Path("~/workspace.old/openclaw-sessions").expanduser(),
    )
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="write to the destination")
    parser.add_argument("--report", type=Path, help="also write the JSON report here")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    destination = args.destination.expanduser().resolve()
    report = Report()
    hermes, covered = load_hermes(args.hermes.expanduser().resolve(), report)
    openclaw = load_openclaw(args.openclaw.expanduser().resolve(), covered, report)
    sessions = sorted([*hermes, *openclaw], key=lambda item: item.created_at)
    report.counts["sessions_planned"] = len(sessions)
    report.counts["messages_planned"] = sum(len(item.messages) for item in sessions)
    if args.apply and not report.errors:
        import_sessions(sessions, destination, report)
    payload = report.payload(apply=args.apply, destination=destination)
    rendered = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    sys.stdout.write(rendered)
    if args.report:
        args.report.expanduser().write_text(rendered, encoding="utf-8")
    return 1 if report.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
