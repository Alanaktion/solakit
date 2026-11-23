"""Verify configured Qwen can inspect and publish one file to a disposable Git remote."""

import argparse
import asyncio
import json
import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.probe_model import load_key
from solakit.config import DEFAULT_CONFIG, OperatorConfig, load_config, resolve_profile
from solakit.records import RunState, SessionMode, ToolCall, ToolReceipt, new_id
from solakit.runtime import compose


def git(directory: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-c", "core.hooksPath=/dev/null", "-C", str(directory), *args],
        text=True,
        stderr=subprocess.PIPE,
        env=os.environ | {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"},
    ).strip()


def configure_author(checkout: Path) -> None:
    git(checkout, "config", "user.name", "Sola fixture")
    git(checkout, "config", "user.email", "sola-fixture@example.invalid")


def repository(root: Path, checkout: Path) -> Path:
    remote = root / "remote.git"
    git(root, "init", "--bare", "-b", "main", str(remote))
    git(root, "init", "-b", "main", str(checkout))
    configure_author(checkout)
    (checkout / "README.md").write_text("Original fixture\n")
    (checkout / "obsolete.txt").write_text("Old fixture\n")
    git(checkout, "add", "--", "README.md", "obsolete.txt")
    git(checkout, "commit", "-m", "Initial fixture")
    git(checkout, "remote", "add", "origin", str(remote))
    git(checkout, "push", "origin", "main")
    return remote


async def probe(config: OperatorConfig) -> None:
    with TemporaryDirectory(prefix="sola-git-") as directory:
        root = Path(directory).resolve()
        project = root / "workspace" / "project"
        project.parent.mkdir()
        remote = repository(root, project)
        update = "Publication fixture reviewed by Sola"
        (project / "README.md").write_text(update + "\n")
        (project / "private.txt").write_text("Unrelated fixture\n")
        profile = resolve_profile(
            OperatorConfig.model_validate(
                {
                    "model": config.model.model_dump(),
                    "data_dir": str(root / "data"),
                    "git": {"repositories": {"project": {"path": str(project), "branch": "main"}}},
                    "defaults": {
                        "workspace_roots": [str(project)],
                        "tools": ["git.status", "git.diff", "git.publish"],
                        "memory_mode": "off",
                    },
                }
            ),
            mode=SessionMode.ISOLATED,
        )
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
                    "In the disposable repository alias project, check status and inspect "
                    "the README.md diff. Then publish only README.md with message "
                    '"Update fixture README". Leave private.txt alone. Report the commit ID.'
                ),
            )
            settled = await app.runs.wait(run.id, timeout=180)
            assert settled.state == RunState.COMPLETED, settled.reason
            evidence = [record for _, record in app.store.run_records(run.id)]
            calls = [r for r in evidence if isinstance(r, ToolCall)]
            receipts = [r for r in evidence if isinstance(r, ToolReceipt)]
            published = [call for call in calls if call.name == "git.publish"]
            assert {"git.status", "git.diff", "git.publish"} <= {call.name for call in calls}
            assert len(published) == 1 and published[0].arguments["paths"] == ["README.md"]
            receipt = next(r for r in receipts if r.tool_call_id == published[0].id)
            assert receipt.status == "succeeded" and receipt.effect == "confirmed", receipt.failure
            assert isinstance(receipt.result, str)
            commit = json.loads(receipt.result)["commit"]
            assert git(remote, "rev-parse", "refs/heads/main") == commit
            assert (
                git(remote, "diff-tree", "--no-commit-id", "--name-only", "-r", commit)
                == "README.md"
            )
            assert git(remote, "show", f"{commit}:README.md") == update
            assert git(project, "status", "--short") == "?? private.txt"
            print(
                json.dumps(
                    {"model": profile.model_pin, "tools": [c.name for c in calls], "commit": commit}
                )
            )
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
