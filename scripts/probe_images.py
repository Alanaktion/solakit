"""Generate one synthetic test image through the real dispatcher, in disposable data."""

import argparse
import asyncio
import json
from pathlib import Path

from solakit.config import OperatorConfig, resolve_profile
from solakit.dispatch import Dispatcher, load_registry
from solakit.evidence import canonical, publish
from solakit.images import ImageService
from solakit.records import ModelStep, RunState, ToolCall, new_id, utc_now
from solakit.runtime import DataDirectoryLock
from solakit.store import Store


async def probe(args: argparse.Namespace) -> None:
    output = args.output.resolve()
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    profile = resolve_profile(
        OperatorConfig.model_validate(
            {
                "data_dir": str(output / "data"),
                "model": {"pin": "image-adapter-probe-no-model-call"},
                "images": {"enabled": True, "base_url": args.endpoint},
                "loaded_tools": ["images.generate"],
                "defaults": {
                    "workspace_roots": [str(output)],
                    "tools": ["images.generate"],
                    "budgets": {"tool_timeout_seconds": 600},
                },
            }
        )
    )
    owner = DataDirectoryLock(profile.data_dir)
    store = Store(profile.data_dir)
    images = ImageService(store, profile.images)
    report: dict[str, object] = {
        "at": utc_now().isoformat(),
        "endpoint": args.endpoint,
        "gate": "pending",
        "scope": "one synthetic test image; no model call",
    }
    owner.acquire()
    try:
        await store.start()
        await images.start()
        dispatcher = Dispatcher(store, load_registry(profile), lambda admitted: profile)
        dispatcher.images = images
        topic = store.create_session(title="Phase 12 image adapter probe")
        run = store.admit(
            topic.id,
            request_id=new_id(),
            content="Synthetic image probe",
            profile=profile.name,
            admission=profile.model_dump(mode="json"),
        )
        store.transition_run(run.id, RunState.RUNNING)
        step = ModelStep(
            origin=run.origin, run_id=run.id, session_id=topic.id, index=1, model=profile.model_pin
        )
        store.append(step)
        arguments = {
            key: value
            for key in ("prompt", "width", "height", "seed")
            if (value := getattr(args, key)) is not None
        }
        receipt = await dispatcher.bind(run.id).dispatch(
            ToolCall(
                origin=run.origin,
                session_id=topic.id,
                run_id=run.id,
                model_step_id=step.id,
                provider_tool_call_id=new_id(),
                name="images.generate",
                arguments=arguments,
            )
        )
        report["receipt"] = receipt.model_dump(mode="json")
        report["images"] = [
            images.get(row[0]).model_dump(mode="json")
            for row in store.db.execute("SELECT id FROM entities WHERE kind='image'")
        ]
        if receipt.status == "succeeded" and receipt.artifact:
            raw = (store.root / receipt.artifact.path).read_bytes()
            publish(output / "image.png", raw)
            store.transition_run(run.id, RunState.COMPLETED)
            report["gate"] = "passed"
        else:
            report["gate"] = "failed"
    finally:
        await images.stop()
        await store.stop()
        owner.release()
        publish(output / "report.json", canonical(report))
    print(json.dumps({"report": str(output / "report.json"), "gate": report["gate"]}))
    if report["gate"] != "passed":
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--endpoint", required=True, help="Named, explicitly selected test ComfyUI endpoint"
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--prompt",
        default="A purple ceramic teapot on a plain cream background, studio photo, no lettering",
    )
    parser.add_argument("--width", type=int)
    parser.add_argument("--height", type=int)
    parser.add_argument("--seed", type=int)
    asyncio.run(probe(parser.parse_args()))


if __name__ == "__main__":
    main()
