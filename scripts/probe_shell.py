"""Verify that configured Qwen reads a disposable project's README with one command."""

import argparse
import asyncio
import json
import secrets
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.probe_model import load_key
from solakit.config import DEFAULT_CONFIG, OperatorConfig, load_config, resolve_profile
from solakit.records import Message, RunState, SessionMode, ToolCall, ToolReceipt, new_id
from solakit.runtime import compose


async def probe(config: OperatorConfig) -> None:
    with TemporaryDirectory(prefix="sola-shell-") as directory:
        root = Path(directory).resolve()
        project = root / "project"
        project.mkdir()
        marker = f"sola-fixture-{secrets.token_hex(4)}"
        (project / "README.md").write_text(f"Project identifier: {marker}\n")
        selected = OperatorConfig.model_validate(
            {
                "model": config.model.model_dump(),
                "data_dir": str(root / "data"),
                "shell": {"enabled": True},
                "defaults": {
                    "workspace_roots": [str(root)],
                    "tools": ["shell.exec"],
                    "budgets": {"tool_calls": 1},
                    "memory_mode": "off",
                },
            }
        )
        profile = resolve_profile(selected, mode=SessionMode.ISOLATED)
        app = compose(profile)
        assert app.store and app.runs
        await app.start()
        try:
            topic = app.store.create_session(mode=SessionMode.ISOLATED)
            run = app.runs.submit(
                topic.id,
                profile=profile,
                request_id=new_id(),
                content=(
                    'Use shell.exec once with argv ["/bin/cat", "README.md"] and cwd '
                    '"project" to inspect the disposable project. '
                    "Then report the exact project identifier from the output."
                ),
            )
            settled = await app.runs.wait(run.id, timeout=180)
            assert settled.state == RunState.COMPLETED, settled.reason
            evidence = [record for _, record in app.store.run_records(run.id)]
            calls = [r for r in evidence if isinstance(r, ToolCall)]
            receipts = [r for r in evidence if isinstance(r, ToolReceipt)]
            answers = [
                r.content for r in evidence if isinstance(r, Message) and r.role == "assistant"
            ]
            assert len(calls) == len(receipts) == 1 and calls[0].name == "shell.exec"
            assert calls[0].arguments == {"argv": ["/bin/cat", "README.md"], "cwd": "project"}
            receipt = receipts[0]
            assert receipt.status == "succeeded" and receipt.effect == "confirmed"
            assert isinstance(receipt.result, str)
            output = json.loads(receipt.result)
            assert output["exit_code"] == 0 and marker in output["stdout"]
            assert answers and marker in answers[-1]
            print(json.dumps({"model": profile.model_pin, "shell_calls": 1, "observed": marker}))
        finally:
            await app.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args()
    config = load_config(args.config)
    if args.env_file:
        load_key(args.env_file, config.model.api_key_env or "OPENAI_API_KEY")
    asyncio.run(probe(config))


if __name__ == "__main__":
    main()
