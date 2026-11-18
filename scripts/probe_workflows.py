"""Live file/image journeys with pinned Qwen and optional sola-test memory only."""

import argparse
import asyncio
import json
import shutil
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from scripts.probe_model import load_key
from solakit.client import ClientService
from solakit.compaction import active_boundary
from solakit.config import MemorySettings, OperatorConfig, load_config, resolve_profile
from solakit.evidence import canonical, digest, publish
from solakit.records import (
    ImageGeneration,
    Job,
    MemoryCommit,
    MemoryState,
    Message,
    Origin,
    Run,
    RunState,
    ToolCall,
    ToolReceipt,
    new_id,
    utc_now,
)
from solakit.run_loop import records
from solakit.runtime import compose


async def probe(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    load_key(args.env_file, config.model.api_key_env or "OPENAI_API_KEY")
    if config.memory.api_key_env:
        load_key(args.env_file, config.memory.api_key_env)
    output = args.output.resolve()
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    with TemporaryDirectory(prefix="sola-phase12-work-") as temporary:
        workspace = Path(temporary).resolve()
        (workspace / "brief.txt").write_text(
            "Synthetic launch brief: PHASE12-VIOLET. Color: violet. Deadline: December 5.\n"
        )
        (workspace / "palette.txt").write_text("Violet and cream; one ceramic teapot.\n")
        names = [
            "files.list",
            "files.read",
            "files.search",
            "files.write",
            "transcripts.search",
            "runtime.status",
            "images.generate",
        ]
        hour = utc_now().hour
        selected = OperatorConfig.model_validate(
            {
                "data_dir": str(output / "data"),
                "model": config.model.model_dump()
                | {"image_inputs": True, "context_tokens": 65536},
                "images": {"enabled": True, "base_url": args.endpoint},
                "memory": (
                    config.memory.model_dump()
                    | {
                        "enabled": True,
                        "account": "sola-test",
                        "user": "default",
                        "roots": ["viking://user/default/memories"],
                    }
                )
                if args.memory
                else MemorySettings().model_dump(),
                "loaded_tools": names,
                "defaults": {
                    "workspace_roots": [str(workspace)],
                    "tools": names,
                    "budgets": {"tool_timeout_seconds": 600, "output_tokens": 2048},
                },
                "background": {
                    "timezone": "UTC",
                    "quiet_start": (hour - 2) % 24,
                    "quiet_end": (hour - 1) % 24,
                },
            }
        )
        app = compose(resolve_profile(selected))
        cases: list[dict[str, Any]] = []
        thresholds = json.loads(Path("evals/thresholds.json").read_bytes())
        report: dict[str, Any] = {
            "at": utc_now().isoformat(),
            "gate": "pending",
            "cases": cases,
            "thresholds": thresholds,
            "threshold_sha256": digest(canonical(thresholds)),
            "scope": "Disposable workspace/data; named test ComfyUI; sola-test memory only",
            "model": selected.model.pin,
            "endpoint": args.endpoint,
        }
        try:
            await app.start()
            assert app.store and app.runs and app.images and app.jobs and app.model and app.memory
            store, runs = app.store, app.runs
            report["model_identity"] = await app.model.verify()
            if args.memory:
                report["memory_identity"] = await app.memory.adapter.verify()
            topic = store.create_session(title="Phase 12 disposable file/image journey")

            def inspect(
                name: str, run: Run, first: set[str | None], grade: Callable[[list[Any]], bool]
            ) -> list[Any]:
                source = records(store, run.id)
                calls = [r for r in source if isinstance(r, ToolCall)]
                receipts = [r for r in source if isinstance(r, ToolReceipt)]
                passed = run.state == RunState.COMPLETED and grade(source)
                cases.append(
                    {
                        "name": name,
                        "run": run.model_dump(mode="json"),
                        "passed": passed,
                        "first_tool_correct": (calls[0].name if calls else None) in first,
                        "first_tool_applicable": None not in first,
                        "calls": [r.model_dump(mode="json") for r in calls],
                        "receipts": [r.model_dump(mode="json") for r in receipts],
                        "answers": [
                            r.content
                            for r in source
                            if isinstance(r, Message) and r.role == "assistant"
                        ],
                    }
                )
                publish(output / f"case-{len(cases):02d}.json", canonical(cases[-1]))
                print(json.dumps({"case": name, "passed": passed}), flush=True)
                return source

            async def turn(
                name: str,
                task: str,
                first: set[str | None],
                grade: Callable[[list[Any]], bool],
                *,
                image: tuple[str, str] | None = None,
            ) -> list[Any]:
                run = await ClientService(app).submit(
                    topic.id, None, task, new_id(), 0, image=image
                )
                finished = await runs.wait(run.id, timeout=850)
                return inspect(name, finished, first, grade)

            def answer_contains(source: list[Any], text: str) -> bool:
                return any(
                    isinstance(r, Message)
                    and r.role == "assistant"
                    and text.casefold() in r.content.casefold()
                    for r in source
                )

            await turn(
                "ordinary conversation",
                "Reply with exactly: Good morning.",
                {None},
                lambda s: answer_contains(s, "Good morning"),
            )
            await turn(
                "list",
                "Use files.list to list the current directory, then tell me its filenames.",
                {"files.list"},
                lambda s: answer_contains(s, "brief.txt") and answer_contains(s, "palette.txt"),
            )
            await turn(
                "read",
                "Read brief.txt and tell me its deadline.",
                {"files.read"},
                lambda s: answer_contains(s, "December 5"),
            )
            await turn(
                "search",
                "Use files.search to locate PHASE12-VIOLET in local files, then "
                "report the matching filename.",
                {"files.search"},
                lambda s: answer_contains(s, "brief.txt"),
            )
            await turn(
                "unicode write",
                "Write a new unicode.txt file containing exactly: café 猫",
                {"files.write"},
                lambda s: (workspace / "unicode.txt").read_text() == "café 猫",
            )
            await turn(
                "brief and evidence to text artifact",
                "Read brief.txt and palette.txt, search conversation evidence for "
                "PHASE12-VIOLET with transcripts.search, then write campaign.txt "
                "containing exactly: Violet teapot launch: December 5.",
                {"files.read", "transcripts.search"},
                lambda s: (
                    (workspace / "campaign.txt").read_text() == "Violet teapot launch: December 5."
                    and {"files.read", "transcripts.search", "files.write"}
                    <= {r.name for r in s if isinstance(r, ToolCall)}
                ),
            )
            await turn(
                "same topic revision",
                "Read campaign.txt then overwrite it with exactly: Violet and "
                "cream teapot launch: December 5.",
                {"files.read"},
                lambda s: (
                    (workspace / "campaign.txt").read_text()
                    == "Violet and cream teapot launch: December 5."
                ),
            )
            # Synthetic settled context triggers a real Qwen summary; no tool effects are invented.
            for index in range(6):
                padding = store.admit(
                    topic.id,
                    request_id=new_id(),
                    profile="default",
                    content=f"Synthetic context padding {index}. "
                    + "Earlier routine planning detail. " * 65,
                )
                store.transition_run(padding.id, RunState.RUNNING)
                store.append(
                    Message(
                        origin=Origin.INTERACTIVE,
                        session_id=topic.id,
                        run_id=padding.id,
                        role="assistant",
                        content="Synthetic acknowledgement. " * 30,
                    )
                )
                store.transition_run(padding.id, RunState.COMPLETED)
            app.profile = app.profile.model_copy(update={"model_context_tokens": 18000})
            await turn(
                "file work after compaction",
                "Read campaign.txt and brief.txt, then write after-compaction.txt "
                "containing exactly: Violet launch remains December 5.",
                {"files.read"},
                lambda s: (
                    bool(active_boundary(store, topic.id))
                    and (workspace / "after-compaction.txt").read_text()
                    == "Violet launch remains December 5."
                ),
            )
            app.profile = app.profile.model_copy(update={"model_context_tokens": 65536})
            image_records = await turn(
                "prompt-only image",
                "Call images.generate exactly once, supplying only prompt: A "
                "purple ceramic teapot on a plain cream background, studio photo, "
                "no lettering. Then report the artifact reference and resolved "
                "seed.",
                {"images.generate"},
                lambda s: any(
                    isinstance(r, ImageGeneration)
                    and r.state == "downloaded"
                    and r.parameters["width"] == r.parameters["height"] == 768
                    for r in s
                ),
            )
            generated = next(
                r for r in image_records if isinstance(r, ImageGeneration) and r.artifact
            )
            assert generated.artifact
            shutil.copyfile(store.root / generated.artifact.path, output / "interactive.png")
            await turn(
                "image attachment",
                "Without using tools, describe the attached image in one sentence "
                "including the teapot's color.",
                {None},
                lambda s: any(
                    answer_contains(s, color) for color in ("purple", "violet", "lavender")
                ),
                image=(generated.run_id, generated.artifact.path.split("/")[-1]),
            )
            scheduled = app.jobs.create(
                Job(
                    origin=Origin.SCHEDULED,
                    session_id=topic.id,
                    profile="default",
                    tools=tuple(names),
                    workspace_roots=(str(workspace),),
                    enabled=True,
                    schedule="once:" + (utc_now() + timedelta(seconds=2)).isoformat(),
                    task="Read brief.txt. Write scheduled.txt containing exactly: "
                    "PHASE12-SCHEDULED-VIOLET. Generate one revised image with "
                    'images.generate: prompt="A violet teapot with a cream handle on a'
                    ' white tabletop, studio photo", width=512, height=768, '
                    "seed=424242. Report the file, artifact and seed.",
                )
            )

            async def scheduled_run(job_id: str) -> Run:
                async with asyncio.timeout(900):
                    while True:
                        row = store.db.execute(
                            "SELECT id FROM entities WHERE kind='run' AND "
                            "json_extract(body,'$.job_id')=?",
                            (job_id,),
                        ).fetchone()
                        if row:
                            return await runs.wait(row[0], timeout=850)
                        await asyncio.sleep(0.2)

            prepared = await scheduled_run(scheduled.id)
            inspect(
                "scheduled file and image revision",
                prepared,
                {"files.read"},
                lambda s: (
                    (workspace / "scheduled.txt").read_text() == "PHASE12-SCHEDULED-VIOLET"
                    and any(
                        isinstance(r, ImageGeneration)
                        and r.state == "downloaded"
                        and r.parameters
                        == {
                            "prompt": r.parameters["prompt"],
                            "width": 512,
                            "height": 768,
                            "seed": 424242,
                        }
                        for r in s
                    )
                ),
            )
            if args.memory:
                async with asyncio.timeout(300):
                    while True:
                        row = store.db.execute(
                            "SELECT id FROM entities WHERE kind='memory' AND run_id=?",
                            (prepared.id,),
                        ).fetchone()
                        commit = store._load("memory", row[0], MemoryCommit) if row else None
                        if commit and commit.state in (
                            MemoryState.COMPLETED,
                            MemoryState.FAILED,
                            MemoryState.AMBIGUOUS,
                        ):
                            report["scheduled_memory"] = commit.model_dump(mode="json")
                            assert commit.state == MemoryState.COMPLETED
                            break
                        await asyncio.sleep(1)
            quiet_topic = store.create_session(title="Phase 12 no-action test")
            no_action = app.jobs.create(
                Job(
                    origin=Origin.PROACTIVE,
                    session_id=quiet_topic.id,
                    profile="default",
                    tools=(),
                    enabled=True,
                    schedule="once:" + (utc_now() + timedelta(seconds=2)).isoformat(),
                    task="There is no outstanding work or useful notification. Return "
                    "exactly NO_ACTION.",
                )
            )
            finished = await scheduled_run(no_action.id)
            inspect(
                "proactive no-action",
                finished,
                {None},
                lambda s: (
                    store.occurrence(finished.occurrence_id or "").decision == "no_action"
                    and not store.messages(quiet_topic.id)
                ),
            )
            passed = sum(case["passed"] for case in cases) / len(cases)
            tool_cases = [case for case in cases if case["first_tool_applicable"]]
            first = sum(case["first_tool_correct"] for case in tool_cases) / len(tool_cases)
            report["completion_rate"], report["first_tool_accuracy"] = passed, first
            report["gate"] = (
                "passed"
                if passed >= 0.9 and first >= 0.9 and all(case["passed"] for case in cases)
                else "failed"
            )
        except Exception as error:
            report.update(gate="failed", error_type=type(error).__name__)
            print(json.dumps({"error_type": type(error).__name__}), flush=True)
        finally:
            await app.stop()
            shutil.copytree(workspace, output / "workspace")
            publish(output / "report.json", canonical(report))
        print(json.dumps({"report": str(output / "report.json"), "gate": report["gate"]}))
        if report["gate"] != "passed":
            raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--memory",
        action="store_true",
        help="Use only the established sola-test/default test account",
    )
    asyncio.run(probe(parser.parse_args()))


if __name__ == "__main__":
    main()
