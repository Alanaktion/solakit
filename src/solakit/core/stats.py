"""Run-quality aggregates for the Model health page (read-only, rebuildable).

Computed on demand from the run index and per-run tool logs; nothing here is
authoritative state."""

import json
import re
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import datetime

ARG_ISSUE = re.compile(r"([\w.<>-]+): (\w+) —")


def model_stats(store, window: int = 100) -> dict:
    window = max(1, min(window, 1000))
    with store.lock, closing(sqlite3.connect(store.root / "app.db")) as db, db:
        rows = db.execute(
            "SELECT payload FROM runs ORDER BY rowid DESC LIMIT ?", (window,)
        ).fetchall()
    runs = [json.loads(row[0]) for row in rows]
    status = Counter(run.get("status") for run in runs)
    tool_use, tool_fail, arg_paths = Counter(), Counter(), Counter()
    per_profile: dict[str, Counter] = {}
    calls = repairs = normalized = unknown = 0
    offending = []
    seconds_per_step = []
    for run in runs:
        steps = run.get("steps") or 0
        try:
            if run.get("finished_at") and steps:
                elapsed = datetime.fromisoformat(run["finished_at"]) - (
                    datetime.fromisoformat(run["created_at"])
                )
                seconds_per_step.append(elapsed.total_seconds() / steps)
        except (TypeError, ValueError):
            pass
        try:
            logs = store._read(store._path("runs", run["id"], ".tools.jsonl"))
        except (OSError, ValueError):
            logs = []
        failed = False
        for log in logs:
            name = log.get("name")
            result = log.get("result") or {}
            calls += 1
            tool_use[name] += 1
            per_profile.setdefault(run.get("profile"), Counter())[name] += 1
            normalized += len(log.get("normalizations") or [])
            if result.get("error"):
                tool_fail[name] += 1
                failed = True
                content = result.get("content") or ""
                if result.get("repairable"):
                    repairs += 1
                if content.startswith("Unknown tool"):
                    unknown += 1
                if content.startswith("Invalid tool arguments:"):
                    for path, code in ARG_ISSUE.findall(content):
                        arg_paths[f"{name} {path} ({code})"] += 1
        if failed or run.get("status") not in ("completed", "running", "queued"):
            offending.append(
                {
                    "id": run["id"],
                    "status": run.get("status"),
                    "profile": run.get("profile"),
                    "error": (run.get("error") or "")[:160],
                }
            )
    total = len(runs) or 1
    tokens = [r["tokens"] for r in runs if isinstance(r.get("tokens"), dict)]
    errors = [r.get("error") or "" for r in runs]
    return {
        "window": len(runs),
        "status": dict(status.most_common()),
        "rates": {
            "repair_rate": round(repairs / calls, 4) if calls else 0.0,
            "unknown_tool_rate": round(unknown / calls, 4) if calls else 0.0,
            "normalizations": normalized,
            "repeated_calls": sum(r.get("repeated_calls") or 0 for r in runs),
            "length_stop_rate": round(
                sum("max_tokens" in e for e in errors) / total, 4
            ),
            "empty_completion_rate": round(
                sum("Empty completion" in e for e in errors) / total, 4
            ),
            "reasoning_retries": sum(len(r.get("retries") or []) for r in runs),
            "wrapups": sum(bool(r.get("completed_via_wrapup")) for r in runs),
        },
        "averages": {
            "prompt_tokens": round(
                sum(t.get("prompt_tokens", 0) for t in tokens) / len(tokens)
            )
            if tokens
            else None,
            "completion_tokens": round(
                sum(t.get("completion_tokens", 0) for t in tokens) / len(tokens)
            )
            if tokens
            else None,
            "steps": round(sum(r.get("steps") or 0 for r in runs) / total, 2),
            "seconds_per_step": round(sum(seconds_per_step) / len(seconds_per_step), 2)
            if seconds_per_step
            else None,
        },
        "tool_calls": calls,
        "top_tools": tool_use.most_common(15),
        "top_failures": [
            (name, count, round(count / tool_use[name], 3))
            for name, count in tool_fail.most_common(15)
        ],
        "failing_arguments": arg_paths.most_common(15),
        "profiles": {
            profile: counter.most_common(8)
            for profile, counter in sorted(per_profile.items(), key=str)
        },
        "offending_runs": offending[:20],
    }
