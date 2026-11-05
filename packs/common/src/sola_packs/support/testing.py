"""Optional pytest fixtures for pack contract tests; not imported at runtime."""

import itertools

import pytest

from solakit.core.config import Config, PathsConfig, Profile
from solakit.core.schemas import Session
from solakit.core.store import Store
from solakit.tools import Registry, ToolContext


@pytest.fixture
def pack_context(tmp_path):
    sequence = itertools.count()

    def make(
        modules,
        tools,
        *,
        profile="interactive",
        read=(),
        write=(),
        capabilities=(),
        repositories=None,
    ):
        cfg = Config(
            paths=PathsConfig(data=tmp_path / f"evidence-{next(sequence)}"),
            profiles={
                profile: Profile(
                    tools=list(tools),
                    read_scopes=list(read),
                    write_scopes=list(write),
                    capabilities=list(capabilities),
                )
            },
            repositories=repositories or {},
            enabled_tools=list(modules),
        )
        store = Store(cfg.paths.data)
        session = store.create_session(Session(id="test", default_profile=profile))
        return Registry(cfg), ToolContext(session, profile, cfg, store, "run")

    return make
