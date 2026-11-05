import asyncio
import hashlib
import html
import re
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
from fastapi.testclient import TestClient
from test_ui import new_session, sign_in

from solakit.core.config import Channel, Job
from solakit.core.model import ModelClient
from solakit.core.schemas import Message, Origin, Run, RunStatus
from solakit.main import create_app


def test_evidence_and_operations_require_auth(config, mock_model):
    with TestClient(create_app(config=config, model=mock_model)) as client:
        for path in ("/prompt", "/runs", "/runs/unknown", "/operations"):
            assert (
                client.get(path, follow_redirects=False).headers["location"] == "/login"
            )
        for path in ("/runs/unknown/cancel", "/operations/jobs/job/run"):
            assert (
                client.post(path, follow_redirects=False).headers["location"]
                == "/login"
            )


def test_prompt_ui_api_hash_parity_and_masking(config, mock_model):
    config.paths.soul.write_text('Persona <script>example</script> & "quoted"\r\n')
    query = {
        "profile": "interactive",
        "session": "inspection",
        "origin": "api",
        "at": "2026-11-04T19:00:00Z",
    }
    with TestClient(create_app(config=config, model=mock_model)) as client:
        sign_in(client)
        api = client.get("/api/prompt", params=query).json()
        page = client.get("/prompt", params=query)
        assert page.status_code == 200
        assert api["sha256"] == hashlib.sha256(api["prompt"].encode()).hexdigest()
        assert api["sha256"] in page.text
        displayed = html.unescape(
            re.search(
                r'<pre class="evidence-text"><code>(.*?)</code>', page.text, re.S
            )[1]
        )
        assert displayed.encode() == api["prompt"].encode()
        assert "<script>example</script>" not in page.text
        assert "Not measured" in page.text
        assert client.get("/api/runs").json() == []
        config.paths.soul.write_text("Credential operator-test-token")
        page = client.get("/prompt", params=query)
        assert "operator-test-token" not in page.text
        assert "[redacted]" in page.text
        assert (
            "operator-test-token"
            in client.get("/api/prompt", params=query).json()["prompt"]
        )
        assert client.get("/prompt", params={"profile": "unknown"}).status_code == 422
        assert (
            client.get("/prompt", params={"at": "<script>bad</script>"}).status_code
            == 422
        )


def test_run_view_uses_redacted_logs_and_links(config, mock_model):
    app = create_app(config=config, model=mock_model)
    with TestClient(app) as client:
        sign_in(client)
        new_session(client)
        run = Run(
            session="evening",
            profile="interactive",
            origin=Origin(),
            status=RunStatus.completed,
        )
        app.state.store.save_run(run)
        app.state.store.write_prompt(run.id, "Prompt operator-test-token")
        app.state.store.append_message(
            "evening",
            Message(
                role="assistant",
                origin=Origin(),
                run_id=run.id,
                tool_calls=[
                    {
                        "id": "call",
                        "type": "function",
                        "function": {
                            "name": "example",
                            "arguments": '{"PASSWORD":"raw-secret"}',
                        },
                    }
                ],
            ),
        )
        app.state.store.append_message(
            "evening",
            Message(
                role="tool",
                content="raw-result-must-not-render",
                tool_call_id="call",
                origin=Origin(),
                run_id=run.id,
            ),
        )
        app.state.store.log_tool(
            run.id,
            {
                "name": "example",
                "args": {"PASSWORD": "raw-secret"},
                "result": {"content": "<script>safe text</script>", "error": False},
            },
        )
        page = client.get(f"/runs/{run.id}")
        assert page.status_code == 200
        for value in (
            "raw-secret",
            "raw-result-must-not-render",
            "operator-test-token",
            "<script>safe text</script>",
        ):
            assert value not in page.text
        assert "&lt;script&gt;safe text&lt;/script&gt;" in page.text
        assert "[redacted]" in page.text
        assert f'action="/runs/{run.id}/cancel"' not in page.text
        assert run.id in client.get("/runs", params={"session": "evening"}).text
        assert run.id not in client.get("/runs", params={"status": "failed"}).text
        assert f'href="/runs/{run.id}"' in client.get("/sessions/evening").text
        assert client.get("/runs/missing").status_code == 404


def test_run_cancellation_uses_existing_durable_operation(config):
    entered, release = threading.Event(), threading.Event()

    async def backend(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": config.model.model_id}]})
        entered.set()
        await asyncio.to_thread(release.wait, 3)
        return httpx.Response(
            200,
            json={
                "model": config.model.model_id,
                "choices": [{"message": {"role": "assistant", "content": "Done"}}],
            },
        )

    model = ModelClient(config.model, transport=httpx.MockTransport(backend))
    with TestClient(create_app(config=config, model=model)) as client:
        sign_in(client)
        new_session(client)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                client.post, "/api/sessions/evening/messages", json={"message": "Wait"}
            )
            try:
                assert entered.wait(2)
                identity = client.get("/api/runs").json()[0]["id"]
                page = client.get(f"/runs/{identity}")
                assert f'action="/runs/{identity}/cancel"' in page.text
                cancelled = client.post(
                    f"/runs/{identity}/cancel", follow_redirects=False
                )
                assert cancelled.status_code == 303
                assert future.result(timeout=2).json()["status"] == "cancelled"
                assert (
                    client.get(f"/api/runs/{identity}").json()["status"] == "cancelled"
                )
            finally:
                release.set()


def test_operations_controls_enabled_and_disabled_jobs(config, mock_model):
    config.jobs = [
        Job(
            id="sample",
            cron="0 0 1 1 *",
            profile="interactive",
            task="Test job",
            bind_session="ephemeral",
            enabled=True,
        )
    ]
    config.channels = [
        Channel(
            id="offline",
            type="api",
            channel_id="test",
            session="offline",
            enabled=False,
        )
    ]
    with TestClient(create_app(config=config, model=mock_model)) as client:
        sign_in(client)
        page = client.get("/operations")
        assert page.status_code == 200
        assert "offline" in page.text and "disabled" in page.text
        assert "America/Denver" in page.text and "sample" in page.text
        assert (
            client.post(
                "/operations/jobs/sample/pause", follow_redirects=False
            ).status_code
            == 303
        )
        assert client.get("/api/jobs").json()[0]["paused"]
        assert (
            client.post(
                "/operations/jobs/sample/resume", follow_redirects=False
            ).status_code
            == 303
        )
        assert not client.get("/api/jobs").json()[0]["paused"]
        result = client.post("/operations/jobs/sample/run", follow_redirects=False)
        assert result.status_code == 303 and result.headers["location"].startswith(
            "/runs/"
        )
        assert client.get(result.headers["location"]).status_code == 200
        run = client.get(
            "/api/runs/" + result.headers["location"].rsplit("/", 1)[-1]
        ).json()
        client.get(f"/sessions/{run['session']}/events")
        assert (
            client.patch("/api/jobs/sample", json={"enabled": False}).status_code == 200
        )
        failed = client.post("/operations/jobs/sample/run")
        assert failed.status_code == 409 and "disabled" in failed.text
        assert client.get("/operations/jobs/sample/delete").status_code == 200
        assert (
            client.post(
                "/operations/jobs/sample/delete", follow_redirects=False
            ).status_code
            == 303
        )
        assert client.get("/api/jobs").json() == []
        assert client.post("/operations/jobs/missing/run").status_code == 404
