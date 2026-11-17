"""Live OpenViking extraction/retrieval gate in an explicitly selected test account."""

import argparse
import asyncio
import json
import subprocess
from pathlib import Path

from pydantic import JsonValue

from solakit.config import MemorySettings, OperatorConfig, resolve_profile
from solakit.evidence import canonical, publish
from solakit.memory import MemoryService, OpenViking
from solakit.records import MemoryCommit, Message, RunState, new_id, utc_now
from solakit.redaction import Redactor
from solakit.store import Store


async def probe(args: argparse.Namespace) -> None:
    if args.account == "default":
        raise ValueError("live probe requires an isolated test account")
    args.output.mkdir(parents=True, mode=0o700, exist_ok=False)
    settings = MemorySettings(
        enabled=True,
        base_url=args.url,
        account=args.account,
        user="default",
        roots=("viking://user/default/memories",),
        api_key_env=args.api_key_env,
        timeout_seconds=60,
    )
    profile = resolve_profile(
        OperatorConfig(
            data_dir=str(args.output / "data"),
            memory=settings,
            model={"pin": "memory-probe-no-inference"},  # type: ignore[arg-type]
            defaults={"workspace_roots": [str(args.output)]},  # type: ignore[arg-type]
        )
    )
    store = Store(profile.data_dir)
    adapter = OpenViking(settings, Redactor())
    service = MemoryService(store, adapter, lambda admitted: profile)
    report: dict[str, object] = {
        "at": utc_now().isoformat(),
        "version": None,
        "account": args.account,
        "user": "default",
        "root": settings.roots[0],
        "gate": "pending",
        "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "production_memory": "not used; all requests carry the selected test account",
    }
    try:
        await store.start()
        await adapter.start()
        report["health"] = health = await adapter.verify()
        report["version"] = health["version"]
        # Include the live OpenAPI contract without touching other accounts or listing their data.
        assert adapter.client
        response = await adapter.client.get("/openapi.json")
        response.raise_for_status()
        publish(args.output / "openapi.json", response.content)
        report["before"] = await adapter.recall(profile, "What is my observatory locker code?")
        token = "SOLATEST-" + new_id()[:12]
        session = store.create_session(title="Isolated Phase 7 fact")
        run = store.admit(
            session.id,
            request_id=new_id(),
            profile=profile.name,
            admission=profile.model_dump(mode="json"),
            content=f"Remember this: my observatory locker code is {token}.",
        )
        store.transition_run(run.id, RunState.RUNNING)
        store.append(
            Message(
                origin=run.origin,
                session_id=session.id,
                run_id=run.id,
                role="assistant",
                content="I will retain the observatory locker code in memory.",
            )
        )
        run = store.transition_run(run.id, RunState.COMPLETED)
        service.enqueue(run)
        deadline = asyncio.get_running_loop().time() + args.wait_seconds
        while True:
            await service.tick()
            row = store.db.execute("SELECT id FROM entities WHERE kind='memory'").fetchone()
            commit = store._load("memory", row[0], MemoryCommit)
            report["commit_record"] = commit.model_dump(mode="json")
            if commit.state.value not in ("pending", "running", "accepted"):
                break
            if asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(2)
        provider_session = await adapter.request(
            "GET",
            "/api/v1/sessions/sola-" + commit.id,
        )
        report["provider_session_identity"] = {
            key: provider_session.get(key) for key in ("session_id", "user", "uri")
        }
        report["extraction"] = commit.state.value
        # Fresh local session; the query intentionally does not contain the distinctive code.
        fresh = store.create_session(title="Fresh-session recall")
        fresh_run = store.admit(
            fresh.id,
            request_id=new_id(),
            profile=profile.name,
            admission=profile.model_dump(mode="json"),
            content="What is my observatory locker code?",
        )
        store.transition_run(fresh_run.id, RunState.RUNNING)
        sources: tuple[dict[str, JsonValue], ...] = ()
        while asyncio.get_running_loop().time() < deadline:
            observation = await service.recall(
                fresh_run, profile, "What is my observatory locker code?"
            )
            sources = observation.sources
            if token in json.dumps(sources):
                break
            await asyncio.sleep(2)
        report["fresh_session"] = fresh.id
        report["retrieval"] = sources
        report["distinctive_fact_retrieved"] = token in json.dumps(sources)
        report["gate"] = (
            "passed"
            if commit.state.value == "completed" and report["distinctive_fact_retrieved"]
            else "failed"
        )
        store.transition_run(fresh_run.id, RunState.COMPLETED)
    except Exception as error:
        report["gate"] = "failed"
        report["error"] = type(error).__name__ + ": " + str(error)
    finally:
        await adapter.stop()
        await store.stop()
        publish(args.output / "report.json", canonical(report))
    print(
        json.dumps(
            {
                key: report.get(key)
                for key in ("gate", "extraction", "distinctive_fact_retrieved", "error")
            }
        )
    )
    if report["gate"] != "passed":
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:1933")
    parser.add_argument("--account", required=True)
    parser.add_argument("--api-key-env")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--wait-seconds", type=int, default=600)
    asyncio.run(probe(parser.parse_args()))


if __name__ == "__main__":
    main()
