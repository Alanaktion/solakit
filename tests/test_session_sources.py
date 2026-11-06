import asyncio
import json

from fastapi.testclient import TestClient
from test_channels import FakeAdapter
from test_execution import model_list, setup
from test_ui import new_session, sign_in

from solakit.channels import Channels
from solakit.core.config import Channel, Job
from solakit.core.routing import SessionRouter
from solakit.core.schemas import Session
from solakit.core.store import Store
from solakit.jobs import Jobs
from solakit.main import create_app


def test_source_defaults_legacy_and_index_rebuild(config):
    store = Store(config.paths.data)
    legacy = store.root / "sessions/legacy.meta.json"
    legacy.write_text(
        json.dumps({"id": "legacy", "created_at": "2020-01-01T00:00:00Z"})
    )
    original = legacy.read_bytes()
    store.create_session(Session(id="scheduled", source="cron"))
    store.rebuild()
    assert store.session("legacy").source == "api"
    assert store.list_sessions(exclude_source="cron")["sessions"][0]["source"] == "api"
    assert store.list_sessions(exclude_source="cron")["total"] == 1
    assert store.list_sessions()["total"] == 2
    assert legacy.read_bytes() == original
    store.create_session(Session(id="scheduled", source="web"))
    assert store.session("scheduled").source == "cron"


def test_api_web_creation_and_default_ui_filter(config, mock_model, auth):
    app = create_app(config=config, model=mock_model)
    with TestClient(app) as client:
        created = client.post("/api/sessions", headers=auth, json={"id": "api-created"})
        assert created.json()["source"] == "api"
        app.state.store.create_session(Session(id="scheduled-hidden", source="cron"))
        sign_in(client)
        new_session(client, "web-created")
        assert app.state.store.session("web-created").source == "web"
        page = client.get("/").text
        assert 'href="/sessions/scheduled-hidden"' not in page
        assert 'href="/sessions/web-created"' in page
        assert (
            'href="/sessions/scheduled-hidden"' in client.get("/?exclude_source=").text
        )
        assert len(client.get("/api/sessions").json()) == 3
        assert len(client.get("/api/sessions?exclude_source=cron").json()) == 2
        # Filter before pagination, preserving inclusion choice in page links.
        for i in range(51):
            app.state.store.create_session(Session(id=f"cron-{i}", source="cron"))
        assert 'rel="next"' not in client.get("/").text
        assert (
            "exclude_source=&amp;q=&amp;profile=&amp;offset=50"
            in client.get("/?exclude_source=").text
        )


def test_channel_and_job_creators_preserve_existing_source(config):
    config.channels = [
        Channel(id="room", type="discord", channel_id="1", session="room")
    ]
    store = Store(config.paths.data)
    assert SessionRouter(config, store).channel("discord", "1").source == "discord"

    async def exercise():
        execution = setup(config, lambda r: model_list(config))
        channels = Channels(config, execution, {"discord": FakeAdapter()})
        jobs = Jobs(config, execution, channels)
        for binding in ("ephemeral", "fresh", "room"):
            job = Job(
                id="job",
                cron="0 * * * *",
                profile="interactive",
                task="test",
                bind_session=binding,
            )
            session = await jobs._session(job)
            assert session.source == ("discord" if binding == "room" else "cron")
            job.sub_session = True
            child = await jobs._session(job)
            assert child.source == "cron"
            if binding != "ephemeral":
                assert child.parent_session == session.id
        execution.store.create_session(Session(id="existing", source="web"))
        job = Job(
            id="existing-job",
            cron="0 * * * *",
            profile="interactive",
            task="test",
            bind_session="existing",
        )
        assert (await jobs._session(job)).source == "web"
        await execution.model.close()

    asyncio.run(exercise())
