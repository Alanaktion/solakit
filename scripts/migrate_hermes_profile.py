#!/usr/bin/env python3
"""Plan or import one Hermes profile's chat history into a Solakit profile.

Companion to migrate_session_history.py: that script imports every legacy
profile as "interactive"; this one imports a single Hermes profile into a named
(typically private) Solakit profile, so its sessions and runs belong to it.
Only chat history moves. SOUL.md, memories/ and skills are reported, not copied.

The default is a read-only dry run. Pass --apply after reviewing the JSON report.
Hermes is never modified. Stop Solakit and Hermes first; reruns are idempotent
(existing sessions are verified, any mismatch aborts).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from migrate_session_history import (  # noqa: E402
    ALLOWED_ROLES,
    LegacySession,
    Report,
    import_sessions,
    normalize_tool_calls,
    sqlite_ro,
    utc,
)

from solakit.core.config import load_config  # noqa: E402
from solakit.core.schemas import Message, Origin  # noqa: E402

NOT_MIGRATED = ("SOUL.md", "memories", "skills", "cron")


def profile_dir(root: Path, name: str) -> Path:
    return root if name == "default" else root / "profiles" / name


def check_target(config_path: Path | None, profile: str, report: Report) -> None:
    if config_path is None:
        report.warnings.append("no --config: Solakit profile was not validated")
        return
    profiles = load_config(config_path).profiles
    if profile not in profiles:
        report.errors.append(f"Solakit profile {profile!r} is not in {config_path}")
    elif not profiles[profile].sessions_private:
        report.warnings.append(
            f"profile {profile!r} has sessions_private = false; imported history "
            "is readable by other profiles' tools"
        )


def load_profile(
    root: Path, hermes_profile: str, target: str, report: Report
) -> list[LegacySession]:
    directory = profile_dir(root, hermes_profile)
    path = directory / "state.db"
    if not path.is_file():
        report.errors.append(f"no Hermes database at {path}")
        return []
    for name in NOT_MIGRATED:
        if (directory / name).exists():
            report.warnings.append(f"not migrated: {directory / name}")
    try:
        db = sqlite_ro(path)
        sessions = db.execute(
            "SELECT * FROM sessions ORDER BY started_at, id"
        ).fetchall()
    except sqlite3.Error as exc:
        report.errors.append(f"cannot read {path}: {exc}")
        return []
    result: list[LegacySession] = []
    # A distinct key namespace keeps ids apart from migrate_session_history.py.
    prefix = f"hermes-profile:{hermes_profile}"
    for row in sessions:
        legacy_id = str(row["id"])
        rows = db.execute(
            "SELECT * FROM messages WHERE session_id=? ORDER BY id", (legacy_id,)
        ).fetchall()
        origin = Origin(
            channel=f"legacy-hermes-{row['source']}",
            external_id=f"{prefix}:{legacy_id}",
        )
        run_id = f"migration-run-{hermes_profile}-{legacy_id}"[:128]
        messages = []
        for item in rows:
            if str(item["role"]) not in ALLOWED_ROLES:
                report.counts["messages_skipped_role"] += 1
                continue
            messages.append(
                Message(
                    role=str(item["role"]),
                    content=item["content"],
                    tool_calls=normalize_tool_calls(item["tool_calls"]),
                    tool_call_id=item["tool_call_id"],
                    origin=origin,
                    run_id=run_id,
                    ts=utc(item["timestamp"]),
                )
            )
        if not messages:
            report.counts["sessions_empty"] += 1
            continue
        parent = row["parent_session_id"]
        result.append(
            LegacySession(
                source_key=f"{prefix}:{legacy_id}",
                source="hermes",
                title=str(row["title"] or row["display_name"] or f"Hermes {legacy_id}"),
                created_at=utc(row["started_at"]),
                profile=target,
                messages=messages,
                parent_key=f"{prefix}:{parent}" if parent else None,
            )
        )
        report.counts["messages"] += len(messages)
    db.close()
    report.counts["sessions"] = len(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes", type=Path, default=Path("~/.hermes"))
    parser.add_argument(
        "--hermes-profile", required=True, help='profile name, or "default"'
    )
    parser.add_argument("--profile", required=True, help="target Solakit profile")
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--config", type=Path, help="validate the target profile")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    destination = args.destination.expanduser().resolve()
    report = Report()
    check_target(args.config and args.config.expanduser(), args.profile, report)
    sessions = load_profile(
        args.hermes.expanduser().resolve(), args.hermes_profile, args.profile, report
    )
    if args.apply and not report.errors:
        import_sessions(sessions, destination, report)
    rendered = (
        json.dumps(
            report.payload(apply=args.apply, destination=destination),
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )
    sys.stdout.write(rendered)
    if args.report:
        args.report.expanduser().write_text(rendered, encoding="utf-8")
    return 1 if report.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
