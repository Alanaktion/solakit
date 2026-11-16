"""Replay saved raw evidence through the frozen scorer without contacting services."""

import argparse
import json
from pathlib import Path
from typing import Any

from evals.cases import SCORER_VERSION, Case, digest
from evals.runner import report
from evals.scoring import score


def replay(directory: Path) -> list[dict[str, Any]]:
    runs = [json.loads(line) for line in (directory / "results.jsonl").read_text().splitlines()]
    seen = set()
    for run in runs:
        if run["version"] != 1 or run["score"]["scorer_version"] != SCORER_VERSION:
            raise ValueError("unsupported evidence/scorer version")
        case = Case.model_validate(run["case"])
        key = (case.id, run["trial"])
        if key in seen:
            raise ValueError(f"duplicate trial: {key}")
        seen.add(key)
        if digest(case.model_dump()) != run["case_hash"]:
            raise ValueError("case hash mismatch")
        for step in run["steps"]:
            if digest(step["request"]) != step["request_hash"]:
                raise ValueError("request hash mismatch")
            if digest(step["request"]["messages"]) != step["prompt_hash"]:
                raise ValueError("prompt hash mismatch")
            if digest(step["request"].get("tools", [])) != step["schema_hash"]:
                raise ValueError("schema hash mismatch")
        if score(case, run) != run["score"]:
            raise ValueError(f"score mismatch: {key}")
    if report(runs) != json.loads((directory / "report.json").read_bytes()):
        raise ValueError("report mismatch")
    return runs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    runs = replay(args.directory)
    print(f"Identical scores and report for {len(runs)} saved trials.")


if __name__ == "__main__":
    main()
