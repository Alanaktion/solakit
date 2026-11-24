"""Check whether the configured interactive model finds and answers a disposable peer letter."""

import argparse
import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.probe_git import configure_author, git, repository
from scripts.probe_model import load_key
from solakit.config import (
    DEFAULT_CONFIG,
    ModelSettings,
    OperatorConfig,
    load_config,
    resolve_profile,
)
from solakit.engine.run_loop import records
from solakit.records import RunState, SessionMode, ToolCall, ToolReceipt, new_id
from solakit.runtime import compose


async def probe(model: ModelSettings) -> None:
    with TemporaryDirectory(prefix="sola-letters-") as directory:
        root = Path(directory).resolve()
        remote = repository(root, root / "initial")
        checkout, peer = root / "checkout", root / "peer"
        for clone in (checkout, peer):
            git(root, "clone", "--no-hardlinks", str(remote), str(clone))
            configure_author(clone)
        peer_letter = "017-2026-11-03-dingus.md"
        (peer / peer_letter).write_text(
            "# A question for Sola\n\nWhat is the sum of 8 and 5?\n", encoding="utf-8"
        )
        git(peer, "add", peer_letter)
        git(peer, "commit", "-m", "Deliver a disposable peer letter")
        git(peer, "push")
        profile = resolve_profile(
            OperatorConfig.model_validate(
                {
                    "model": model.model_dump(),
                    "data_dir": str(root / "data"),
                    "letters": {
                        "repository": str(checkout),
                        "branch": "main",
                        "author": "sola",
                        "peer": "dingus",
                    },
                    "defaults": {
                        "workspace_roots": [str(checkout)],
                        "tools": ["letters.inbox", "letters.read", "letters.reply"],
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
                    "Check the disposable correspondence inbox for any new peer letter. "
                    "Read the letter locally by its number and answer its question in one reply. "
                    "Use the number you found as reply_to. Confirm publication in chat."
                ),
            )
            settled = await app.runs.wait(run.id, timeout=180)
            assert settled.state == RunState.COMPLETED, settled.reason
            evidence = records(app.store, run.id)
            calls = [r for r in evidence if isinstance(r, ToolCall)]
            receipt = {r.tool_call_id: r for r in evidence if isinstance(r, ToolReceipt)}
            assert [c.name for c in calls] == ["letters.inbox", "letters.read", "letters.reply"], (
                calls
            )
            inbox, read, reply = calls
            assert read.arguments["number"] == reply.arguments["reply_to"] == 17
            assert all(receipt[c.id].status == "succeeded" for c in calls)
            assert receipt[inbox.id].effect == receipt[reply.id].effect == "confirmed"
            assert receipt[read.id].effect == "none"
            raw = receipt[reply.id].result
            assert isinstance(raw, str)
            result = json.loads(raw)
            commit = git(remote, "rev-parse", "main")
            assert result["number"] == 18 and commit == result["commit"]
            assert git(remote, "log", "-1", "--format=%s", commit) == "Reply to letter 017 as sola"
            body = reply.arguments["body"]
            assert isinstance(body, str) and ("13" in body or "thirteen" in body.casefold())
            assert (checkout / result["path"]).read_bytes() == (body + "\n").encode()
            print(
                json.dumps(
                    {
                        "model": profile.model_pin,
                        "calls": [c.name for c in calls],
                        "reply_to": 17,
                        "number": 18,
                        "commit": commit,
                    }
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
    asyncio.run(probe(config.model))


if __name__ == "__main__":
    main()
