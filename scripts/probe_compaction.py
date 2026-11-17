"""Exercise repeated Qwen compaction with synthetic context and private disposable evidence."""

import argparse
import asyncio
import json
from pathlib import Path

from scripts.probe_model import load_key
from solakit.compaction import active_boundary, manifest_for
from solakit.config import OperatorConfig, load_config, resolve_profile
from solakit.evidence import canonical, digest, publish
from solakit.records import Message, ModelStep, RunState, new_id, utc_now
from solakit.runtime import compose


async def probe(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    load_key(args.env_file, config.model.api_key_env or "OPENAI_API_KEY")
    output = args.output.resolve()
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    soul = output / "SOUL.md"
    soul.write_text("You are Sola. Your operator in this synthetic test is Rowan.\n")
    soul.chmod(0o600)
    selected = OperatorConfig.model_validate(
        {
            "data_dir": str(output / "data"),
            "soul_file": str(soul),
            "model": config.model.model_dump() | {"context_tokens": 18000},
            "defaults": {
                "workspace_roots": [str(output)],
                "memory_mode": "off",
                "budgets": {"output_tokens": 2048},
            },
        }
    )
    profile = resolve_profile(selected)
    context = compose(profile)
    report: dict[str, object] = {
        "at": utc_now().isoformat(),
        "pin": profile.model_pin,
        "quality_gate": "pending",
        "configured_window": config.model.context_tokens,
        "fixture_window": profile.model_context_tokens,
        "scope": "synthetic local evidence; no semantic memory or tool effects",
        "checks": [],
    }
    checks = []
    try:
        await context.start()
        assert context.store and context.runs and context.model
        report["identity"] = await context.model.verify()
        store = context.store
        session = store.create_session(title="Phase 8 synthetic compaction quality")

        def add_turn(i: int) -> None:
            facts = {
                0: "The project is Pineglass. The access code is amber-4729. "
                "Deadline is December 1.",
                2: "Correction: deadline is 2026-12-05, superseding December 1. "
                "I promised to finish the report; that commitment is still unfinished.",
            }.get(i, "We discussed routine background; no dates or commitments changed.")
            run = store.admit(
                session.id,
                request_id=new_id(),
                profile=profile.name,
                content=facts + "\n" + "Routine background detail. " * 60,
                admission=profile.model_dump(mode="json"),
            )
            run = store.transition_run(run.id, RunState.RUNNING)
            step = ModelStep(
                origin=run.origin,
                session_id=session.id,
                run_id=run.id,
                index=1,
                model=profile.model_pin,
                content="Acknowledged. " + "Background remains unchanged. " * 15,
            )
            store.append(step)
            store.append(
                Message(
                    origin=run.origin,
                    session_id=session.id,
                    run_id=run.id,
                    role="assistant",
                    content=step.content,
                )
            )
            store.transition_run(run.id, RunState.COMPLETED)

        for i in range(6):
            add_turn(i)
        originals = {path: path.read_bytes() for path in store.log.paths()}
        prior_boundary = None
        for attempt in range(2):
            run = context.runs.submit(
                session.id,
                profile=profile,
                request_id=new_id(),
                content="Using our context, return only a JSON object with keys: "
                "assistant, operator, project, access_code, corrected_deadline (YYYY-MM-DD), "
                "unfinished_commitment. Preserve the user's latest correction.",
            )
            result = await context.runs.wait(run.id, timeout=300)
            boundary = active_boundary(store, session.id)
            messages = [
                m
                for _, m in store.messages(session.id, limit=100)
                if m.run_id == run.id
                and m.role == "assistant"
                and (not boundary or m.id != boundary.summary_message_id)
            ]
            answer = messages[-1].content if messages else ""
            try:
                parsed = json.loads(answer)
            except ValueError:
                parsed = {}
            expected = {
                "assistant": "Sola",
                "operator": "Rowan",
                "project": "Pineglass",
                "access_code": "amber-4729",
                "corrected_deadline": "2026-12-05",
            }
            passed = (
                result.state == RunState.COMPLETED
                and boundary is not None
                and boundary.id != prior_boundary
                and all(parsed.get(key) == value for key, value in expected.items())
                and "report" in str(parsed.get("unfinished_commitment", "")).lower()
                and all(path.read_bytes().startswith(raw) for path, raw in originals.items())
            )
            checks.append(
                {
                    "attempt": attempt + 1,
                    "passed": passed,
                    "run_id": run.id,
                    "outcome": result.model_dump(mode="json"),
                    "answer": answer,
                    "boundary": boundary.model_dump(mode="json") if boundary else None,
                    "manifest": manifest_for(store, boundary).model_dump(mode="json")
                    if boundary
                    else None,
                    "original_prefix_hashes": {
                        path.name: digest(raw) for path, raw in originals.items()
                    },
                }
            )
            if not passed:
                break
            prior_boundary = boundary.id if boundary else None
            for i in range(4):
                add_turn(10 + i)
        report["quality_gate"] = (
            "passed" if len(checks) == 2 and all(check["passed"] for check in checks) else "failed"
        )
        report["memory_observer"] = context.memory.status() if context.memory else None
    except Exception as error:
        report["error_type"] = type(error).__name__
    finally:
        await context.stop()
        report["checks"] = checks
        publish(output / "report.json", canonical(report))
    print(
        json.dumps({"report": str(output / "report.json"), "quality_gate": report["quality_gate"]})
    )
    if report["quality_gate"] != "passed":
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.example.toml"))
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(probe(parser.parse_args()))


if __name__ == "__main__":
    main()
