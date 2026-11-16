import asyncio
import copy
import json
from pathlib import Path
from typing import Any

import pytest

from evals.cases import Case, load_cases
from evals.compare import compare
from evals.replay import replay
from evals.runner import FakeDriver, report, run_trial, save_json
from evals.sandbox import Sandbox
from evals.scoring import score

CASES = load_cases(Path("evals/cases"))
EXPECTED_FAILURES = {"announcement-only", "reasoning-only", "incorrect-success", "duplicate-effect"}


def trial(case: Case) -> dict[str, Any]:
    return asyncio.run(run_trial(case, 0, FakeDriver(case.fake_responses), "fake-qwen"))


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.id)
def test_fake_corpus(case: Case) -> None:
    run = trial(case)
    assert run["score"]["expectation_met"] == (case.id not in EXPECTED_FAILURES)
    assert run["score"]["task_completed"] == (
        case.id not in EXPECTED_FAILURES and case.expected.kind in {"action", "conversation"}
    )
    assert score(case, run) == run["score"]
    assert all(receipt["call_id"] for receipt in run["receipts"])


def test_scorer_uses_artifacts_and_receipts() -> None:
    cases = {case.id: case for case in CASES}
    wrong = trial(cases["incorrect-success"])
    duplicate = trial(cases["duplicate-effect"])
    assert wrong["score"]["incorrect_effects"] == 1
    assert not wrong["score"]["task_completed"]
    assert duplicate["score"]["duplicate_effects"] == 1
    assert not duplicate["score"]["task_completed"]
    denied = trial(cases["denied-call"])
    assert denied["score"]["expectation_met"]
    assert not denied["score"]["task_completed"]


def test_duplicate_file_write_is_an_effect() -> None:
    case = next(case for case in CASES if case.id == "incorrect-success")
    responses = copy.deepcopy(case.fake_responses)
    call = responses[0]["choices"][0]["message"]["tool_calls"][0]
    repeated = copy.deepcopy(call)
    repeated["id"] = "duplicate-write"
    responses[0]["choices"][0]["message"]["tool_calls"].append(repeated)
    run = asyncio.run(run_trial(case, 0, FakeDriver(responses), "fake-qwen"))
    assert run["score"]["duplicate_effects"] == 1
    assert not run["score"]["task_completed"]


def test_correcting_a_bad_write_preserves_incorrect_effect() -> None:
    case = next(case for case in CASES if case.id == "incorrect-success")
    responses = copy.deepcopy(case.fake_responses)
    call = responses[0]["choices"][0]["message"]["tool_calls"][0]
    repaired = copy.deepcopy(call)
    repaired["id"] = "corrected-write"
    repaired["function"]["arguments"] = json.dumps({"path": "result.txt", "content": "done\n"})
    responses[0]["choices"][0]["message"]["tool_calls"].append(repaired)
    run = asyncio.run(run_trial(case, 0, FakeDriver(responses), "fake-qwen"))
    assert run["artifacts"] == {"result.txt": "done\n"}
    assert run["score"]["incorrect_effects"] == 1
    assert not run["score"]["task_completed"]


def test_replay_and_comparison(tmp_path: Path) -> None:
    runs = [trial(case) for case in CASES]
    results = tmp_path / "results.jsonl"
    results.write_text("".join(json.dumps(run) + "\n" for run in runs))
    save_json(tmp_path / "report.json", report(runs))
    save_json(
        tmp_path / "metadata.json",
        {
            "model": "fake-qwen",
            "server_version": "unverified",
            "parameters": {},
            "corpus_hash": "fixture",
            "scorer_version": "1",
            "driver": "fake",
            "thresholds": json.loads(Path("evals/thresholds.json").read_bytes()),
        },
    )
    assert replay(tmp_path) == runs
    comparison = compare(tmp_path, tmp_path)
    assert comparison["paired_trials"] == len(CASES)
    assert comparison["lost_paired_successes"] == []
    assert comparison["deferred_gate"] != "passed"
    metadata = json.loads((tmp_path / "metadata.json").read_bytes())
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    for name in ("results.jsonl", "report.json"):
        (candidate / name).write_bytes((tmp_path / name).read_bytes())
    save_json(candidate / "metadata.json", metadata | {"server_version": "different"})
    assert compare(tmp_path, candidate)["unmatched_conditions"] == ["server_version"]
    runs[0]["steps"][0]["request"]["model"] = "tampered"
    results.write_text("".join(json.dumps(run) + "\n" for run in runs))
    with pytest.raises(ValueError, match="request hash mismatch"):
        replay(tmp_path)


@pytest.mark.parametrize("path", ["../escape.txt", "/tmp/escape.txt", "."])
def test_sandbox_paths(tmp_path: Path, path: str) -> None:
    sandbox = Sandbox(tmp_path, CASES[0])
    with pytest.raises(ValueError, match="outside disposable workspace"):
        sandbox.write(path, "forbidden")


def test_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "escape").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="outside disposable workspace"):
        Sandbox(root, CASES[0]).write("escape/secret.txt", "forbidden")


def test_wrong_model_and_token_limit_never_dispatch() -> None:
    case = next(case for case in CASES if case.id == "multistep-files")
    for field, value, outcome in [
        ("model", "wrong", "model_mismatch"),
        ("finish_reason", "length", "incomplete:length"),
    ]:
        responses = copy.deepcopy(case.fake_responses)
        if field == "model":
            responses[0][field] = value
        else:
            responses[0]["choices"][0][field] = value
        run = asyncio.run(run_trial(case, 0, FakeDriver(responses), "fake-qwen"))
        assert run["outcome"] == outcome
        assert not run["receipts"]
        assert not run["score"]["task_completed"]


def test_steps_bounded_and_errors_preserved() -> None:
    case = next(case for case in CASES if case.id == "zero-arg-runtime")
    bounded = case.model_copy(update={"max_steps": 1})
    run = trial(bounded)
    assert run["outcome"] == "step_limit"
    assert not run["score"]["task_completed"]
    run = asyncio.run(run_trial(case, 0, FakeDriver([]), "fake-qwen"))
    assert run["outcome"] == "model_error"
    assert run["steps"][0]["error"]["type"] == "RuntimeError"


def test_no_tool_or_incomplete_call_has_no_effect() -> None:
    case = next(case for case in CASES if case.id == "duplicate-effect")
    responses = copy.deepcopy(case.fake_responses)
    del responses[0]["choices"][0]["message"]["tool_calls"][0]["id"]
    run = asyncio.run(run_trial(case, 0, FakeDriver(responses), "fake-qwen"))
    assert run["outcome"] == "malformed_response"
    assert not run["effects"]


def test_case_version_and_unknown_fields_rejected() -> None:
    from pydantic import ValidationError

    for change in ({"version": 2}, {"unrecognized": True}, {"trials": 1}):
        with pytest.raises(ValidationError):
            Case.model_validate(CASES[0].model_dump() | change)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.id)
def test_runtime_corpus_uses_production_cycle(case: Case) -> None:
    from evals.runtime import run_runtime_trial

    run = asyncio.run(run_runtime_trial(case, 0, "fake-qwen"))
    assert run["score"]["expectation_met"] == (case.id not in EXPECTED_FAILURES)
    assert not run["score"]["silent_unattempted_task"]
    assert not run["score"]["false_continuation"]
    if case.id == "announcement-only":
        assert run["runtime_state"] == "failed"
        assert run["recovery"][0]["kind"] == "narration"
    if case.id == "malformed-arguments":
        assert run["receipts"][0]["executed"] is False
        assert run["recovery"][0]["kind"] == "argument_repair"
    if case.id == "denied-call":
        assert run["steps"][0]["request"]["tools"][0]["function"]["name"] == "write_file"
        assert run["receipts"][0]["receipt"]["effect"] == "none"
        assert run["runtime_state"] == "failed"
    assert score(case, run) == run["score"]
