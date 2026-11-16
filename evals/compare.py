"""Compare paired results, labeling unmatched measurement conditions."""

import argparse
import json
from pathlib import Path
from statistics import median
from typing import Any

from evals.replay import replay
from evals.scoring import summarize


def compare(baseline: Path, candidate: Path) -> dict[str, Any]:
    before = {(run["case"]["id"], run["trial"]): run for run in replay(baseline)}
    after = {(run["case"]["id"], run["trial"]): run for run in replay(candidate)}
    old = json.loads((baseline / "metadata.json").read_bytes())
    new = json.loads((candidate / "metadata.json").read_bytes())
    unmatched = [
        key
        for key in (
            "model",
            "server_version",
            "parameters",
            "corpus_hash",
            "scorer_version",
            "thresholds",
            "driver",
        )
        if old[key] != new[key]
    ]
    if before.keys() != after.keys():
        unmatched.append("trial_membership")
    pairs = sorted(before.keys() & after.keys())
    lost = [
        key
        for key in pairs
        if before[key]["score"]["task_completed"] and not after[key]["score"]["task_completed"]
    ]
    extra = [
        after[key]["score"]["model_steps"] - before[key]["score"]["model_steps"] for key in pairs
    ]
    heavy = [key for key in pairs if len(before[key]["case"]["tools"]) > 1]
    old_bytes = sum(before[key]["score"]["schema_bytes"] for key in heavy)
    new_bytes = sum(after[key]["score"]["schema_bytes"] for key in heavy)
    reduction = 1 - new_bytes / old_bytes if old_bytes else None
    thresholds = old["thresholds"]["deferred_disclosure"]
    reliability = old["thresholds"]["reliability"]
    aggregate = summarize([run["score"] for run in after.values()])
    safety_passed = all(
        aggregate[metric] <= reliability[metric + "_max"]
        for metric in (
            "silent_unattempted_tasks",
            "false_continuations",
            "incorrect_effects",
            "duplicate_effects",
        )
    )
    rates_passed = all(
        aggregate[metric]["rate"] is not None
        and aggregate[metric]["rate"] >= reliability[threshold]
        for metric, threshold in (
            ("task_completion", "task_completion_rate_min"),
            ("first_tool_accuracy", "first_tool_accuracy_min"),
        )
    )
    live_verified = (
        old["driver"] == new["driver"] == "openai" and old["server_version"] != "unverified"
    )
    return {
        "paired_trials": len(pairs),
        "unmatched_conditions": unmatched,
        "lost_paired_successes": lost,
        "median_extra_model_steps": median(extra) if extra else None,
        "tool_heavy_pairs": len(heavy),
        "schema_byte_reduction": reduction,
        "candidate_reliability_gate": "passed" if safety_passed and rates_passed else "not_met",
        "deferred_gate": "passed"
        if (
            live_verified
            and safety_passed
            and rates_passed
            and not unmatched
            and pairs
            and len(lost) <= thresholds["lost_paired_successes_max"]
            and median(extra) <= thresholds["median_extra_model_steps_max"]
            and reduction is not None
            and reduction >= thresholds["schema_byte_reduction_min"]
        )
        else "pending_or_not_met",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    args = parser.parse_args()
    print(json.dumps(compare(args.baseline, args.candidate), indent=2))


if __name__ == "__main__":
    main()
