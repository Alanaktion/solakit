"""Paired corpus collection through the runtime, with the Phase 1 baseline retained."""

import argparse
import asyncio
import copy
import json
import os
import platform
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

from evals.cases import SCORER_VERSION, Case, digest, encoded, load_cases
from evals.sandbox import Sandbox
from evals.scoring import score, summarize


class Driver(Protocol):
    async def complete(self, request: dict[str, Any]) -> dict[str, Any]: ...


class FakeDriver:
    def __init__(self, responses: list[dict[str, Any]]):
        self.responses = iter(copy.deepcopy(responses))

    async def complete(self, request: dict[str, Any]) -> dict[str, Any]:
        try:
            return next(self.responses)
        except StopIteration as error:
            raise RuntimeError("fake responses exhausted") from error


class OpenAIDriver:
    def __init__(self, client: Any):
        self.client = client

    async def complete(self, request: dict[str, Any]) -> dict[str, Any]:
        response = await self.client.chat.completions.create(**request)
        return cast(dict[str, Any], response.model_dump(mode="json"))


async def run_trial(case: Case, trial: int, driver: Driver, model: str) -> dict[str, Any]:
    messages = copy.deepcopy(case.messages)
    run: dict[str, Any] = {
        "version": 1,
        "case": case.model_dump(),
        "case_hash": digest(case.model_dump()),
        "trial": trial,
        "steps": [],
        "receipts": [],
        "final_message": None,
        "outcome": "step_limit",
        "started_at": datetime.now(UTC).isoformat(),
        "fixture_only": case.fixture_only,
    }
    start = perf_counter()
    with TemporaryDirectory(prefix="sola-eval-") as directory:
        sandbox = Sandbox(Path(directory), case)
        for _ in range(case.max_steps):
            request = {
                "model": model,
                "messages": copy.deepcopy(messages),
                "temperature": 0,
                "max_tokens": 1024,
            }
            if case.tools:
                request["tools"] = case.tools
            step: dict[str, Any] = {
                "request": request,
                "prompt_hash": digest(messages),
                "schema_hash": digest(case.tools),
                "request_hash": digest(request),
            }
            run["steps"].append(step)
            step_start = perf_counter()
            try:
                response = await driver.complete(request)
                step["response"] = response
                if response.get("interrupted"):
                    run["outcome"] = "interrupted"
                    break
                if response.get("model") != model:
                    run["outcome"] = "model_mismatch"
                    break
                choice = response["choices"][0]
                message = choice["message"]
                run["final_message"] = message
                calls = message.get("tool_calls") or []
                finish = choice["finish_reason"]
                if finish not in {"stop", "tool_calls"}:
                    run["outcome"] = f"incomplete:{finish}"
                    break
                if not calls:
                    run["outcome"] = "stop" if finish == "stop" else "malformed_response"
                    break
                if (
                    finish != "tool_calls"
                    or any(
                        not call.get("id") or not isinstance(call.get("function"), dict)
                        for call in calls
                    )
                    or len({call["id"] for call in calls}) != len(calls)
                ):
                    run["outcome"] = "malformed_response"
                    break
                messages.append(
                    {
                        key: message[key]
                        for key in ("role", "content", "tool_calls")
                        if key in message
                    }
                )
                for call in calls:
                    receipt = sandbox.execute(call)
                    run["receipts"].append(receipt)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "content": encoded(receipt).decode(),
                        }
                    )
            except Exception as error:
                # Preserve failures as evidence; never retry a potentially accepted generation.
                step["error"] = {"type": type(error).__name__, "message": str(error)}
                run["outcome"] = "model_error"
                break
            finally:
                step["latency_seconds"] = perf_counter() - step_start
        run["artifacts"] = sandbox.artifacts()
        run["effects"] = sandbox.effects
    run["latency_seconds"] = perf_counter() - start
    run["score"] = score(case, run)
    return run


def report(runs: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "scorer_version": SCORER_VERSION,
        "aggregate": summarize([run["score"] for run in runs]),
        "cases": {
            name: summarize([run["score"] for run in runs if run["case"]["id"] == name])
            for name in sorted({run["case"]["id"] for run in runs})
        },
    }


def save_json(path: Path, value: Any) -> None:
    path.write_bytes(json.dumps(value, indent=2, ensure_ascii=False).encode() + b"\n")


async def collect(args: argparse.Namespace) -> None:
    cases = load_cases(args.cases)
    # Refuse to mix evidence from separate runs or overwrite an earlier baseline.
    args.output.mkdir(parents=True, exist_ok=False)
    args.output.chmod(0o700)
    metadata = {
        "version": 1,
        "driver": args.driver,
        "harness": args.harness,
        "runtime_policy": "json-receipts-revoke-at-intent-v1"
        if args.harness == "runtime"
        else None,
        "disclosure": args.disclosure,
        "model": args.model,
        "server_version": args.server_version or "unverified",
        "base_url": args.base_url,
        "python": platform.python_version(),
        "packages": {name: version(name) for name in ("openai", "fastapi")},
        "corpus_hash": digest([case.model_dump() for case in cases]),
        "parameters": {"temperature": 0, "max_tokens": 1024}
        | ({"stream": True, "enable_thinking": False} if args.harness == "runtime" else {}),
        "scorer_version": SCORER_VERSION,
        "thresholds": json.loads(Path("evals/thresholds.json").read_bytes()),
        "live_gate": "pending" if args.driver == "fake" else "requires_evidence_review",
    }
    client = None
    if args.driver == "openai" and args.harness == "baseline":
        from openai import AsyncOpenAI

        client = AsyncOpenAI(
            base_url=args.base_url,
            api_key=os.getenv("SOLA_EVAL_API_KEY") or os.getenv("OPENAI_API_KEY", "local"),
            max_retries=0,
            timeout=60,
        )
    runs = []
    save_json(args.output / "metadata.json", metadata)
    try:
        if client is not None:
            models = await client.models.list()
            save_json(args.output / "models.json", models.model_dump(mode="json"))
            if args.model not in {model.id for model in models.data}:
                raise ValueError(f"pinned model {args.model!r} is absent from /models")
        with (args.output / "results.jsonl").open("x", encoding="utf-8") as stream:
            for case in cases:
                for trial in range(case.trials):
                    if args.harness == "runtime":
                        from evals.runtime import run_runtime_trial

                        run = await run_runtime_trial(
                            case,
                            trial,
                            args.model,
                            live=args.driver == "openai",
                            base_url=args.base_url or "http://127.0.0.1:8000/v1",
                            deferred=args.disclosure == "deferred",
                            output=args.output.resolve() / "evidence" / f"{case.id}-{trial}",
                        )
                    else:
                        driver = OpenAIDriver(client) if client else FakeDriver(case.fake_responses)
                        if case.fixture_only:
                            driver = FakeDriver(case.fake_responses)
                        run = await run_trial(case, trial, driver, args.model)
                    stream.write(encoded(run).decode() + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                    runs.append(run)
                    print(
                        f"{case.id} [{trial + 1}/{case.trials}]: {run['score']['expectation_met']}",
                        flush=True,
                    )
    except Exception as error:
        metadata["live_gate"] = "pending_probe_failed"
        save_json(args.output / "metadata.json", metadata)
        save_json(args.output / "probe-error.json", {"type": type(error).__name__})
        raise
    finally:
        if client is not None:
            await client.close()
        save_json(args.output / "report.json", report(runs))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--driver", choices=("fake", "openai"), default="fake")
    parser.add_argument("--harness", choices=("runtime", "baseline"), default="runtime")
    parser.add_argument("--disclosure", choices=("eager", "deferred"), default="eager")
    parser.add_argument("--cases", type=Path, default=Path("evals/cases"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model")
    parser.add_argument("--base-url", default=os.getenv("OPENAI_BASE_URL"))
    parser.add_argument("--server-version")
    args = parser.parse_args()
    if args.harness == "baseline" and args.disclosure != "eager":
        parser.error("deferred disclosure is a runtime-only experiment")
    args.model = args.model or (
        os.getenv("OPENAI_MODEL") if args.driver == "openai" else "fake-qwen"
    )
    if args.driver == "openai" and (
        not args.base_url or not args.model or args.model == "fake-qwen"
    ):
        parser.error("live runs require --base-url and an explicit --model pin")
    if args.base_url:
        url = urlsplit(args.base_url)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or (url.username or url.password or url.query or url.fragment)
        ):
            parser.error("base URL must be HTTP(S) without credentials, query, or fragment")
    asyncio.run(collect(args))


if __name__ == "__main__":
    main()
