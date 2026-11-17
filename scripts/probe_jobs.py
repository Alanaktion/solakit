"""Live scheduled Qwen → bound topic → OpenViking extraction, in disposable data."""

import argparse
import asyncio
import json
import subprocess
from datetime import timedelta
from pathlib import Path

from scripts.probe_model import load_key
from solakit.config import MemorySettings, OperatorConfig, load_config, resolve_profile
from solakit.evidence import canonical, publish
from solakit.records import (
    Delivery,
    Job,
    MemoryCommit,
    MemoryState,
    Message,
    Origin,
    RunState,
    new_id,
    utc_now,
)
from solakit.run_loop import records
from solakit.runtime import compose


async def probe(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    load_key(args.env_file, config.model.api_key_env or "OPENAI_API_KEY")
    output = args.output.resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    selected = OperatorConfig.model_validate(
        {
            "data_dir": str(output / "data"),
            "model": config.model.model_dump(),
            "memory": MemorySettings(
                enabled=True,
                account="sola-test",
                user="default",
                roots=("viking://user/default/memories",),
            ).model_dump(),
            "defaults": {"workspace_roots": [str(output)], "memory_mode": "required"},
        }
    )
    app = compose(resolve_profile(selected))
    report: dict[str, object] = {
        "at": utc_now().isoformat(),
        "gate": "pending",
        "account": "sola-test",
        "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "scope": "Disposable local data and approved sola-test account; no production data",
    }
    try:
        await app.start()
        assert app.store and app.runs and app.jobs and app.memory and app.model
        report["model"] = await app.model.verify()
        report["memory_health"] = await app.memory.adapter.verify()
        topic = app.store.create_session(title="Phase 9 scheduled live gate")
        marker = "PHASE9-" + new_id()[:12]
        scheduled = app.jobs.create(
            Job(
                origin=Origin.SCHEDULED,
                profile="default",
                session_id=topic.id,
                tools=(),
                schedule="once:" + (utc_now() + timedelta(seconds=2)).isoformat(),
                enabled=True,
                task=f"My synthetic telescope calibration label is {marker}. "
                "Reply with one short sentence confirming this exact label. No tools are needed.",
            )
        )
        deadline = asyncio.get_running_loop().time() + args.wait_seconds
        run_id = None
        while asyncio.get_running_loop().time() < deadline:
            row = app.store.db.execute(
                "SELECT id FROM entities WHERE kind='run' AND json_extract(body,'$.job_id')=?",
                (scheduled.id,),
            ).fetchone()
            if row:
                run_id = row[0]
                break
            await asyncio.sleep(0.2)
        assert run_id is not None, "scheduler did not admit the due job"
        run = await app.runs.wait(run_id, timeout=180)
        report["run"] = run.model_dump(mode="json")
        assert run.state == RunState.COMPLETED
        messages = [
            r
            for r in records(app.store, run_id)
            if isinstance(r, Message) and r.role == "assistant"
        ]
        deliveries = [r for r in records(app.store, run_id) if isinstance(r, Delivery)]
        report["messages"] = [m.model_dump(mode="json") for m in messages]
        report["deliveries"] = [d.model_dump(mode="json") for d in deliveries]
        report["occurrence"] = app.store.occurrence(run.occurrence_id or "").model_dump(mode="json")
        assert len(messages) == 1 and marker in messages[0].content
        assert messages[0].session_id == topic.id and len(deliveries) == 1
        commit = None
        while asyncio.get_running_loop().time() < deadline:
            row = app.store.db.execute(
                "SELECT id FROM entities WHERE kind='memory' AND run_id=?",
                (run_id,),
            ).fetchone()
            if row:
                commit = app.store._load("memory", row[0], MemoryCommit)
                if commit.state in (
                    MemoryState.COMPLETED,
                    MemoryState.FAILED,
                    MemoryState.AMBIGUOUS,
                ):
                    break
            await asyncio.sleep(1)
        report["memory_commit"] = commit.model_dump(mode="json") if commit else None
        assert commit and commit.state == MemoryState.COMPLETED and commit.provider_commit_id
        report["gate"] = "passed"
    except Exception as error:
        report["gate"] = "failed"
        report["error_type"] = type(error).__name__
    finally:
        await app.stop()
        publish(output / "report.json", canonical(report))
    print(json.dumps({"report": str(output / "report.json"), "gate": report["gate"]}))
    if report["gate"] != "passed":
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.example.toml"))
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--wait-seconds", type=int, default=300)
    asyncio.run(probe(parser.parse_args()))


if __name__ == "__main__":
    main()
