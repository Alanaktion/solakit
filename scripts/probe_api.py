"""One live pinned-Qwen conversation over authenticated HTTP in disposable data."""

import argparse
import asyncio
import json
import os
import secrets
import socket
import subprocess
from pathlib import Path

import httpx2
import uvicorn

from scripts.probe_model import load_key
from solakit.api import create_app
from solakit.config import HTTPSettings, OperatorConfig, load_config, resolve_profile
from solakit.evidence import canonical, publish
from solakit.records import new_id, utc_now
from solakit.runtime import compose


async def probe(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    load_key(args.env_file, config.model.api_key_env or "OPENAI_API_KEY")
    output = args.output.resolve()
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    report: dict[str, object] = {
        "at": utc_now().isoformat(),
        "gate": "pending",
        "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "scope": "Disposable data; memory disabled; no tools or production state",
    }
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
    credential_name = "SOLA_PHASE10_PROBE_PASSWORD"
    password = secrets.token_urlsafe(32)
    previous = os.environ.get(credential_name)
    os.environ[credential_name] = password
    selected = OperatorConfig.model_validate(
        {
            "data_dir": str(output / "data"),
            "model": config.model.model_dump(),
            "defaults": {"workspace_roots": [str(output)], "memory_mode": "off"},
            "http": HTTPSettings(
                allowed_origins=(origin,), password_env=credential_name
            ).model_dump(),
        }
    )
    context = compose(resolve_profile(selected))
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(context), log_level="critical", access_log=False, proxy_headers=False
        )
    )
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if serving.done():
                    await serving
                    raise RuntimeError("HTTP startup failed")
                await asyncio.sleep(0.01)
        async with httpx2.AsyncClient(
            base_url=origin, trust_env=False, timeout=args.wait_seconds
        ) as client:
            logged = await client.post(
                "/api/v1/auth/login",
                headers={"Origin": origin},
                json={"password": password, "transport": "bearer"},
            )
            logged.raise_for_status()
            token = logged.json()["token"]
            client.headers["Authorization"] = "Bearer " + token
            topic = await client.post(
                "/api/v1/sessions", json={"title": "Phase 10 live HTTP gate", "mode": "isolated"}
            )
            topic.raise_for_status()
            session = topic.json()["id"]
            marker = "PHASE10-" + new_id()[:12]
            content = (
                f"My synthetic calibration label is {marker}. "
                "Reply with one short sentence confirming this exact label."
            )
            body = {"session_id": session, "content": content}
            headers = {"Idempotency-Key": new_id()}
            admitted = await client.post("/api/v1/runs", json=body, headers=headers)
            assert admitted.status_code == 202
            run_id = admitted.json()["run"]["id"]
            terminal_events = 0
            cursor = f"{session}:0"
            async with asyncio.timeout(args.wait_seconds):
                async with client.stream(
                    "GET", f"/api/v1/sessions/{session}/events", headers={"Last-Event-ID": cursor}
                ) as stream:
                    stream.raise_for_status()
                    async for line in stream.aiter_lines():
                        if line.startswith("id: "):
                            cursor = line[4:]
                        if line == "event: run.terminal":
                            terminal_events += 1
                        if terminal_events and not line:
                            break
            completed = await client.get(f"/api/v1/runs/{run_id}")
            completed.raise_for_status()
            assert completed.json()["state"] == "completed"
            retry = await client.post("/api/v1/runs", json=body, headers=headers)
            assert retry.json()["run"]["id"] == run_id
            messages = (await client.get(f"/api/v1/sessions/{session}/messages")).json()["items"]
            assert [item["record"]["role"] for item in messages] == ["user", "assistant"]
            assert marker in messages[-1]["record"]["content"]
            prompts = (await client.get(f"/api/v1/runs/{run_id}/prompts")).json()["items"]
            reference = prompts[0]["record"]["body"]
            artifact = await client.get(
                f"/api/v1/runs/{run_id}/{reference['path']}", params={"limit": 64}
            )
            assert artifact.status_code == 200 and len(artifact.content) == 64
            report.update(
                gate="passed",
                pin=context.profile.model_pin,
                session_id=session,
                run_id=run_id,
                terminal_events=terminal_events,
                cursor=cursor,
                marker=marker,
                artifact_sha256=reference["sha256"],
            )
            await client.post("/api/v1/auth/logout")
            assert (await client.get("/api/v1/sessions")).status_code == 401
    except Exception as error:
        report.update(gate="failed", error=type(error).__name__)
        raise
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(serving, 10)
        finally:
            listener.close()
            if previous is None:
                os.environ.pop(credential_name, None)
            else:
                os.environ[credential_name] = previous
            publish(output / "report.json", canonical(report))
    print(json.dumps({"report": str(output / "report.json"), "gate": report["gate"]}))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.example.toml"))
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--wait-seconds", type=int, default=180)
    args = parser.parse_args()
    asyncio.run(probe(args))


if __name__ == "__main__":
    main()
