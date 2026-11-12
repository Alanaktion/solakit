import asyncio
import json

import pytest
from pydantic import ValidationError

from solakit.core.config import Profile, session_readable
from solakit.core.schemas import Message, Origin, Run, Session
from solakit.core.store import Store
from solakit.tools import Registry, ToolContext


def profiles(**flags):
    base = {
        "ops": Profile(effects=["operate"], tools=["sessions.*"]),
        "diary": Profile(sessions_private=True, effects=["operate"]),
        "walled": Profile(sessions_read_others=False, effects=["operate"]),
    }
    for name, values in flags.items():
        base[name] = Profile(effects=["operate"], **values)
    return base


def test_defaults_preserve_open_access():
    p = Profile()
    assert (p.sessions_private, p.sessions_read_others) == (False, True)
    with pytest.raises(ValidationError):
        Profile(sessions_private="maybe")


def test_readability_matrix_directions_are_independent():
    cfg = profiles()
    assert session_readable(cfg, "ops", "ops")
    assert not session_readable(cfg, "ops", "diary")  # private blocks others
    assert session_readable(cfg, "diary", "ops")  # private does not block reading
    assert session_readable(cfg, "diary", "diary")
    assert not session_readable(cfg, "walled", "ops")  # walled cannot read out
    assert session_readable(cfg, "ops", "walled")  # but others can read walled
    assert session_readable(cfg, "walled", "walled")


@pytest.fixture
def world(config):
    config.enabled_tools = ["sessions"]
    for name in ("ops", "diary", "walled"):
        (config.paths.fragments / f"profiles/{name}.md").write_text("")
    config.profiles = profiles()
    config.profiles["ops"] = Profile(tools=["sessions.*"], effects=["operate"])
    config.profiles["diary"] = Profile(
        tools=["sessions.*"], effects=["operate"], sessions_private=True
    )
    config.profiles["walled"] = Profile(
        tools=["sessions.*"], effects=["operate"], sessions_read_others=False
    )
    store = Store(config.paths.data)
    for owner in ("ops", "diary", "walled"):
        store.create_session(Session(id=f"{owner}-s", default_profile=owner))
        store.append_message(
            f"{owner}-s",
            Message(
                role="user", content=f"moonlit {owner}", origin=Origin(), run_id="x"
            ),
        )
    # A diary-profile run inside an ops-default session: owned by diary per run.
    run = Run(session="ops-s", profile="diary", origin=Origin())
    store.save_run(run)
    store.append_message(
        "ops-s",
        Message(role="user", content="moonlit secret", origin=Origin(), run_id=run.id),
    )
    return config, store


def found(world, reader, **kwargs):
    config, store = world
    ctx = ToolContext(store.session(f"{reader}-s"), reader, config, store, "r")
    result = asyncio.run(
        Registry(config).dispatch(
            "sessions.search_all", json.dumps({"query": "moonlit", **kwargs}), ctx
        )
    )
    assert not result.error, result.content
    matches = json.loads(result.content.split("\n")[1])["matches"]
    return {(m["session_id"], m["snippet"].split()[-1]) for m in matches}


def test_private_profile_hidden_from_others_by_owning_run(world):
    assert found(world, "ops") == {("ops-s", "ops"), ("walled-s", "walled")}
    # Explicit targeting cannot reach it either.
    assert found(world, "ops", session_id="diary-s") == set()


def test_private_profile_still_reads_others_and_itself(world):
    assert found(world, "diary") == {
        ("ops-s", "ops"),
        ("ops-s", "secret"),
        ("diary-s", "diary"),
        ("walled-s", "walled"),
    }


def test_walled_profile_reads_only_own_sessions(world):
    assert found(world, "walled") == {("walled-s", "walled")}


def test_jobs_cannot_bind_into_isolated_sessions(world):
    from solakit.core.config import Job
    from solakit.tools import jobs

    config, store = world
    ctx = ToolContext(store.session("ops-s"), "ops", config, store, "r")
    job = Job(id="j", cron="0 * * * *", task="t", profile="ops", bind_session="diary-s")
    assert jobs.session_isolated(ctx, job)
    assert not jobs.permitted(ctx, job)
    own = job.model_copy(update={"profile": "diary"})
    assert not jobs.session_isolated(ctx, own)
    walled = job.model_copy(update={"profile": "walled", "bind_session": "ops-s"})
    assert not jobs.permitted(ctx, walled)
