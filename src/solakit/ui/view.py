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


def turns(
    session: dict,
    runs: list[dict],
    evidence: dict[str, dict],
    *,
    include_empty_runs: bool = True,
) -> list[dict]:
    metadata = {run["id"]: run for run in runs if run["session"] == session["id"]}
    grouped, boundaries = {}, []
    cutoff = timestamp(session.get("context_started_at"))
    for message in session["messages"]:
        if message.get("kind") == "compaction":
            cutoff = max(cutoff, timestamp(message["ts"]))
            boundaries.append(
                {
                    "boundary": True,
                    "segment": message.get("timeline_segment"),
                    "content": message.get("content") or "",
                    "date": message["ts"],
                    "archive_number": (message.get("archive") or "").rsplit(".", 1)[-1],
                    "at": timestamp(message["ts"]),
                    "node_id": "boundary-" + message["ts"],
                }
            )
            continue
        grouped.setdefault(
            (message["run_id"], message.get("timeline_segment")), []
        ).append(message)
    for identity, run in metadata.items():
        if (
            include_empty_runs
            and run["status"] != "completed"
            and (
                run["status"] in {"queued", "running"}
                or max(
                    timestamp(run["created_at"]),
                    timestamp(
                        (run.get("compaction") or {}).get("boundary", {}).get("ts")
                    ),
                )
                >= cutoff
            )
        ):
            grouped.setdefault((identity, None), [])
    result = boundaries
    # Mid-run compaction can split one run across multiple transcript segments.
    # Its immutable tool log still covers the whole run in execution order.
    tool_offsets = {}
    for (identity, segment), messages in grouped.items():
        run = metadata.get(
            identity,
            {
                "id": identity,
                "status": "unknown",
                "tokens": None,
                "profile": session["default_profile"],
            },
        )
        recorded = evidence.get(identity, {}).get("tools", [])
        offset = tool_offsets.get(
            identity, evidence.get(identity, {}).get("tool_offset", 0)
        )
        logs = iter(recorded[offset:])
        tool_offsets[identity] = offset + sum(
            len(m.get("tool_calls") or []) for m in messages
        )
        items = []
        completed_calls = {
            message.get("tool_call_id")
            for message in messages
            if message["role"] == "tool"
        }
        for message in messages:
            if message["role"] != "tool" and (
                message.get("content") is not None or message.get("images")
            ):
                items.append(
                    {
                        "role": message["role"],
                        "content": message.get("content") or "",
                        "images": message.get("images", []),
                        "anchor": message.get("anchor"),
                        "node_id": "message-" + str(len(items)),
                    }
                )
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
                            "node_id": f"tool-{len(items)}-" + call["id"],
                        }
                    )
                else:
                    items.append(
                        {
                            "role": "tool",
                            "name": call["function"]["name"],
                            "state": (
                                "running"
                                if run["status"] == "running"
                                and call["id"] not in completed_calls
                                else "not recorded"
                            ),
                            "args": "Not recorded",
                            "content": "No recorded result is available.",
                            "node_id": f"tool-{len(items)}-" + call["id"],
                        }
                    )
        status = run["status"]
        result.append(
            {
                "boundary": False,
                "timeline_id": identity + ("-archive-" + segment if segment else ""),
                "segment": segment,
                "run": run,
                "items": items,
                "status_label": LABELS.get(status, "Run record unavailable"),
                "pending": not segment and status in {"queued", "running"},
                "waiting_for_tool": any(
                    item.get("state") == "running" for item in items
                ),
                "failed": status not in {"queued", "running", "completed"},
                "at": (
                    timestamp(messages[0]["ts"])
                    if messages
                    else max(cutoff, timestamp(run.get("created_at")))
                ),
            }
        )
    return sorted(result, key=lambda turn: turn["at"])
