"""Check a short query and a returned citation with local Qwen and disposable data."""

import argparse
import asyncio
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx2

from scripts.probe_model import load_key
from solakit.config import DEFAULT_CONFIG, OperatorConfig, load_config, resolve_profile
from solakit.records import Message, RunState, SessionMode, ToolCall, ToolReceipt, new_id
from solakit.runtime import compose
from solakit.web_search import BRAVE_WEB_SEARCH_URL

FIXTURE_ENV = "SOLA_WEB_SEARCH_PROBE_FIXTURE_KEY"
PRIVATE_LABEL = "TRACKING-PRIVATE-HARRIER-27"


def fixture(request: httpx2.Request) -> httpx2.Response:
    assert request.url.copy_with(query=None) == httpx2.URL(BRAVE_WEB_SEARCH_URL)
    return httpx2.Response(
        200,
        json={
            "type": "search",
            "web": {
                "results": [
                    {
                        "title": "Status of Python versions",
                        "url": "https://devguide.python.org/versions/#supported-versions",
                        "description": "Python 3.15 has entered the bugfix phase.",
                    }
                ]
            },
        },
    )


async def probe(config: OperatorConfig, key_env: str, *, live: bool) -> None:
    with TemporaryDirectory(prefix="sola-web-search-") as directory:
        selected = OperatorConfig.model_validate(
            {
                "data_dir": str(Path(directory) / "data"),
                "model": config.model.model_dump(),
                "web_search": {"api_key_env": key_env},
                "defaults": {
                    "workspace_roots": [directory],
                    "tools": ["web.search"],
                    "memory_mode": "off",
                },
            }
        )
        profile = resolve_profile(selected, mode=SessionMode.ISOLATED)
        context = compose(profile)
        assert context.tools and context.store and context.runs
        if not live:
            context.tools.web_search.transport = httpx2.MockTransport(fixture)
        await context.start()
        try:
            session = context.store.create_session(mode=SessionMode.ISOLATED)
            run = context.runs.submit(
                session.id,
                profile=profile,
                request_id=new_id(),
                content=(
                    "Search the web for the current support status of Python 3.15. "
                    "Cite the source URL in your answer. "
                    + ("" if live else f"My private tracking label is {PRIVATE_LABEL}.")
                ),
            )
            result = await context.runs.wait(run.id, timeout=300)
            records = [record for _, record in context.store.run_records(run.id, limit=100)]
            calls = [record for record in records if isinstance(record, ToolCall)]
            receipts = [record for record in records if isinstance(record, ToolReceipt)]
            answers = [
                r.content for r in records if isinstance(r, Message) and r.role == "assistant"
            ]
            assert result.state == RunState.COMPLETED, result.reason
            assert calls and all(call.name == "web.search" for call in calls)
            queries = [str(call.arguments["query"]) for call in calls]
            assert all(0 < len(q) <= 120 and len(q.split()) <= 12 for q in queries)
            assert all(PRIVATE_LABEL not in q.upper() for q in queries)
            urls = {
                hit["url"]
                for receipt in receipts
                if receipt.status == "succeeded" and isinstance(receipt.result, str)
                for hit in json.loads(receipt.result)
            }
            citations = sorted(url for url in urls if answers and url in answers[-1])
            assert citations, "The final answer did not cite a returned source URL"
            print(
                json.dumps(
                    {
                        "model": profile.model_pin,
                        "live_brave": live,
                        "query_lengths": [len(q) for q in queries],
                        "citations": citations,
                    }
                )
            )
        finally:
            await context.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--live-brave", action="store_true", help="Send a public query to Brave")
    args = parser.parse_args()
    config = load_config(args.config)
    if args.env_file:
        load_key(args.env_file, config.model.api_key_env or "OPENAI_API_KEY")
    key_env = FIXTURE_ENV
    if args.live_brave:
        key_env = config.web_search.api_key_env or "BRAVE_SEARCH_API_KEY"
        if args.env_file:
            load_key(args.env_file, key_env)
        if not os.environ.get(key_env, "").strip():
            parser.error(f"Live Brave smoke requires the environment variable {key_env}")
    else:
        os.environ[key_env] = "synthetic-web-search-probe-token"
    asyncio.run(probe(config, key_env, live=args.live_brave))


if __name__ == "__main__":
    main()
