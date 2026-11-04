"""Install this separate package to run its scratch-only contract tests."""

import asyncio
import json
import subprocess

import pytest

from solakit.core.config import CollectionConfig, Config, PathsConfig, Profile
from solakit.core.schemas import Session
from solakit.core.store import Store
from solakit.tools import Registry, ToolContext


def git(path, *arguments):
    return subprocess.run(
        ["git", "-C", str(path), *arguments], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def fixture(tmp_path):
    remote, root = tmp_path / "remote.git", tmp_path / "site"
    git(tmp_path, "init", "--bare", "--initial-branch=main", str(remote))
    git(tmp_path, "clone", str(remote), str(root))
    git(root, "config", "user.name", "Fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    (root / "README").write_text("untouched")
    git(root, "add", "README")
    git(root, "commit", "-m", "initial")
    git(root, "push", "-u", "origin", "main")
    config = Config(
        paths=PathsConfig(data=tmp_path / "evidence"),
        repositories={"site": root},
        collections={
            "dreams": CollectionConfig(repository="site", directory="_dreams"),
            "poems": CollectionConfig(repository="site", directory="_poems"),
        },
        enabled_tools=["sola_packs.journal"],
        profiles={
            "writer": Profile(
                tools=["journal.create", "journal.list_recent", "journal.publish"],
                write_scopes=["collection:dreams"],
                capabilities=["git.push"],
            )
        },
    )
    registry = Registry(config)
    store = Store(config.paths.data)
    session = store.create_session(Session(id="journal", default_profile="writer"))
    return root, config, registry, ToolContext(session, "writer", config, store, "run")


def test_create_list_publish_and_scope_denials(fixture):
    root, config, registry, ctx = fixture

    async def dispatch(name, **arguments):
        return await registry.dispatch(name, json.dumps(arguments), ctx)

    async def exercise():
        arguments = {
            "collection": "dreams",
            "slug": "first-dream",
            "title": 'Dream "one"',
            "body": "An original dream.",
            "date": "2026-11-04",
        }
        empty = await dispatch("journal.list_recent", collection="dreams")
        assert json.loads(empty.content)["entries"] == []
        result = await dispatch("journal.create", **arguments)
        assert not result.error
        target = root / "_dreams/2026-11-04-first-dream.md"
        assert target.exists()
        assert json.dumps(arguments["title"]) in target.read_text()
        source = target.read_bytes()
        assert (await dispatch("journal.create", **arguments)).error
        assert target.read_bytes() == source
        listed = await dispatch("journal.list_recent", collection="dreams")
        assert json.loads(listed.content)["entries"][0]["name"] == target.name
        assert (
            await dispatch("journal.create", **{**arguments, "collection": "poems"})
        ).error
        assert (
            await dispatch("journal.create", **{**arguments, "slug": "../escape"})
        ).error
        assert (
            await dispatch(
                "journal.publish",
                collection="dreams",
                paths=["../README"],
                message="denied",
            )
        ).error
        published = await dispatch(
            "journal.publish",
            collection="dreams",
            paths=[target.name],
            message="Publish dream",
        )
        assert not published.error, published.content
        assert "Published commit" in published.content
        assert (
            git(root, "show", "--format=", "--name-only", "HEAD")
            == "_dreams/" + target.name
        )
        assert (root / "README").read_text() == "untouched"
        config.profiles["writer"].capabilities = []
        assert (
            await dispatch(
                "journal.publish",
                collection="dreams",
                paths=[target.name],
                message="denied",
            )
        ).error

    asyncio.run(exercise())


def test_second_collection_is_configuration_only(fixture):
    root, config, registry, ctx = fixture
    config.profiles["writer"].write_scopes = ["collection:poems"]

    async def exercise():
        result = await registry.dispatch(
            "journal.create",
            json.dumps(
                {
                    "collection": "poems",
                    "slug": "night",
                    "title": "Night",
                    "body": "Moonlight.",
                    "date": "2026-11-04",
                }
            ),
            ctx,
        )
        assert not result.error
        assert (root / "_poems/2026-11-04-night.md").exists()
        assert not (root / "_dreams").exists()

    asyncio.run(exercise())
