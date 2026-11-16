"""Pure scoring of saved evidence; prose alone cannot prove an effect."""

import json
from collections import Counter
from statistics import median
from typing import Any

from evals.cases import SCORER_VERSION, Case, digest, encoded


def score(case: Case, run: dict[str, Any]) -> dict[str, Any]:
    receipts = run["receipts"]
    statuses = [receipt["status"] for receipt in receipts]
    names = [receipt["name"] for receipt in receipts]
    expected = case.expected
    artifacts = run["artifacts"]
    changed = {
        name: content for name, content in artifacts.items() if case.resources.get(name) != content
    }
    effects = run["effects"]
    effect_calls = [
        receipt
        for receipt in receipts
        if receipt["status"] == "ok" and receipt["name"] in {"write_file", "publish_note"}
    ]
    duplicates = sum(
        count - 1
        for count in Counter(
            digest([receipt["name"], json.loads(receipt["arguments"])]) for receipt in effect_calls
        ).values()
    )
    incorrect = sum(item not in expected.effects for item in effects)
    incorrect += sum(
        expected.artifacts.get(args["path"]) != args["content"]
        for receipt in effect_calls
        if receipt["name"] == "write_file"
        for args in [json.loads(receipt["arguments"])]
    )
    evidence_matches = (
        all(artifacts.get(name) == content for name, content in expected.artifacts.items())
        and all(expected.artifacts.get(name) == content for name, content in changed.items())
        and effects == expected.effects
        and all(name in names for name in expected.required_tools)
        and statuses in [expected.receipt_statuses, *expected.alternative_receipt_statuses]
        and not incorrect
        and not duplicates
    )
    final = run.get("final_message") or {}
    has_answer = bool((final.get("content") or "").strip())
    normal_stop = run["outcome"] == "stop"
    if expected.kind == "interrupted":
        expectation_met = run["outcome"] == "interrupted" and evidence_matches
    else:
        expectation_met = normal_stop and has_answer and evidence_matches
    completed = expectation_met and expected.kind in {"action", "conversation"}
    unattempted = expected.kind == "action" and not receipts
    tokens = [step["response"].get("usage") for step in run["steps"] if "response" in step]
    return {
        "scorer_version": SCORER_VERSION,
        "expectation_met": expectation_met,
        "task_completed": completed,
        "task_applicable": expected.kind in {"action", "conversation"},
        "expected_denial": expected.kind == "denial",
        "first_tool_accuracy": names[0] == expected.first_tool if names else False,
        "first_tool_applicable": expected.first_tool is not None,
        # shortcut: evidence detects unattempted work, linguistic continuation belongs to Phase 6.
        "detected_narration_failure": unattempted and has_answer,
        "reasoning_only": not has_answer and bool(final.get("reasoning_content")),
        "silent_unattempted_task": unattempted and normal_stop,
        "false_continuation": (
            expected.kind == "conversation" and not expected.required_tools and bool(receipts)
        ),
        "no_tool_applicable": expected.kind == "conversation" and not expected.required_tools,
        "repair_count": sum(status == "invalid_arguments" for status in statuses),
        "model_steps": len(run["steps"]),
        "latency_seconds": run["latency_seconds"],
        "prompt_bytes": sum(len(encoded(step["request"]["messages"])) for step in run["steps"]),
        "schema_bytes": sum(
            len(encoded(step["request"].get("tools", []))) for step in run["steps"]
        ),
        "usage": tokens,
        "incorrect_effects": incorrect,
        "duplicate_effects": duplicates,
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def rate(metric: str, applicable: str | None = None) -> dict[str, int | float | None]:
        values = [row[metric] for row in rows if applicable is None or row[applicable]]
        return {
            "numerator": sum(values),
            "denominator": len(values),
            "rate": sum(values) / len(values) if values else None,
        }

    return {
        "trials": len(rows),
        "expectation_met": rate("expectation_met"),
        "task_completion": rate("task_completed", "task_applicable"),
        "first_tool_accuracy": rate("first_tool_accuracy", "first_tool_applicable"),
        "silent_unattempted_tasks": sum(row["silent_unattempted_task"] for row in rows),
        "detected_narration_failures": sum(row["detected_narration_failure"] for row in rows),
        "false_continuations": sum(row["false_continuation"] for row in rows),
        "false_continuation_rate": rate("false_continuation", "no_tool_applicable"),
        "incorrect_effects": sum(row["incorrect_effects"] for row in rows),
        "duplicate_effects": sum(row["duplicate_effects"] for row in rows),
        "median_model_steps": median(row["model_steps"] for row in rows) if rows else None,
        "median_latency_seconds": median(row["latency_seconds"] for row in rows) if rows else None,
    }
