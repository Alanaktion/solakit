"""Run by smoke_wheel in its clean environment, outside the source checkout."""

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

from solakit.config import OperatorConfig, resolve_profile
from solakit.records import ModelStep, RunState, ToolCall, new_id
from solakit.storage.store import Store
from solakit.tools.dispatch import Dispatcher
from solakit.tools.registry import load_registry


async def main() -> None:
    with TemporaryDirectory(prefix="sola-pack-") as temporary:
        root = Path(temporary).resolve()
        profile = resolve_profile(
            OperatorConfig.model_validate(
                {
                    "data_dir": str(root / "data"),
                    "model": {"pin": "fixture"},
                    "tool_modules": ["sola_example"],
                    "defaults": {"workspace_roots": [str(root)], "tools": ["example.*"]},
                }
            )
        )
        store = Store(profile.data_dir)
        await store.start()
        try:
            session = store.create_session()
            run = store.admit(
                session.id,
                request_id=new_id(),
                content="pack fixture",
                profile=profile.name,
                admission=profile.model_dump(mode="json"),
            )
            store.transition_run(run.id, RunState.RUNNING)
            step = ModelStep(
                origin=run.origin,
                session_id=session.id,
                run_id=run.id,
                index=1,
                model=profile.model_pin,
            )
            store.append(step)
            dispatch = Dispatcher(
                store, load_registry(profile.tool_modules), lambda admitted: profile
            ).bind(run.id)
            for name, arguments in (
                ("example.write", {"path": "hello.txt", "content": "external pack"}),
                ("example.read", {"path": "hello.txt"}),
                ("example.image", {"path": "image.svg", "prompt": "fixture"}),
            ):
                receipt = await dispatch.dispatch(
                    ToolCall(
                        origin=run.origin,
                        session_id=session.id,
                        run_id=run.id,
                        model_step_id=step.id,
                        provider_tool_call_id=new_id(),
                        name=name,
                        arguments=dict(arguments),
                    )
                )
                assert receipt.status == "succeeded", receipt
            assert (root / "hello.txt").read_text() == "external pack"
            assert "Fake: fixture" in (root / "image.svg").read_text()
            events = store.events(session.id)
            assert sum(event.type == "tool.intent" for event in events) == 3
            assert sum(event.type == "tool.receipt" for event in events) == 3
        finally:
            await store.stop()


if __name__ == "__main__":
    asyncio.run(main())
