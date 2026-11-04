"""Presentation-only grouping of API data. No storage or execution access."""

import json
from datetime import datetime

LABELS = {
    "queued": "Queued",
    "running": "Thinking",
    "completed": "Complete",
    "model_unavailable": "Model unavailable",
    "memory_unavailable": "Memory unavailable",
    "tool_error": "Tool failed",
    "budget_exceeded": "Run limit reached",
    "context_limit": "Context limit reached",
    "interrupted": "Interrupted",
    "cancelled": "Cancelled",
    "failed": "Run failed",
    "missed": "Missed",
}


def timestamp(value):
    try:
        return datetime.fromisoformat(value).timestamp()
    except (ValueError, TypeError):
        return 0


def argument_text(value):
    return (
        value
        if isinstance(value, str)
        else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    )


def turns(session: dict, runs: list[dict], evidence: dict[str, dict]) -> list[dict]:
    metadata = {run["id"]: run for run in runs if run["session"] == session["id"]}
    grouped, boundaries = {}, []
    cutoff = 0
    for message in session["messages"]:
        if message.get("kind") == "compaction":
            cutoff = max(cutoff, timestamp(message["ts"]))
            boundaries.append(
                {
                    "boundary": True,
                    "content": message.get("content") or "",
                    "at": timestamp(message["ts"]),
                }
            )
            continue
        grouped.setdefault(message["run_id"], []).append(message)
    for identity, run in metadata.items():
        if run["status"] != "completed" and timestamp(run["created_at"]) >= cutoff:
            grouped.setdefault(identity, [])
    result = boundaries
    for identity, messages in grouped.items():
        run = metadata.get(
            identity,
            {
                "id": identity,
                "status": "unknown",
                "tokens": None,
                "profile": session["default_profile"],
            },
        )
        logs = iter(evidence.get(identity, {}).get("tools", []))
        items = []
        for message in messages:
            if message["role"] != "tool" and message.get("content") is not None:
                items.append({"role": message["role"], "content": message["content"]})
            for call in message.get("tool_calls") or []:
                log = next(logs, None)
                # Arguments/results come only from redacted run tool logs. Never
                # fall back to verbatim tool-call arguments in the transcript.
                if log:
                    outcome = log.get("result", {})
                    items.append(
                        {
                            "role": "tool",
                            "name": log["name"],
                            "error": outcome.get("error", False),
                            "state": "error" if outcome.get("error") else "ok",
                            "args": argument_text(log.get("args")),
                            "content": outcome.get("content", ""),
                        }
                    )
                else:
                    items.append(
                        {
                            "role": "tool",
                            "name": call["function"]["name"],
                            "state": "not recorded",
                            "args": "Not recorded",
                            "content": "No recorded result is available.",
                        }
                    )
        status = run["status"]
        result.append(
            {
                "boundary": False,
                "run": run,
                "items": items,
                "status_label": LABELS.get(status, "Run record unavailable"),
                "pending": status in {"queued", "running"},
                "failed": status not in {"queued", "running", "completed"},
                "at": timestamp(
                    run.get("created_at") or (messages[0]["ts"] if messages else None)
                ),
            }
        )
    return sorted(result, key=lambda turn: turn["at"])
