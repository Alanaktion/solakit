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
        config.profiles["writer"].effects = []
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
    config.profiles["writer"].write = "workspace"
    config.profiles["writer"].write_workspace = ["collection:poems"]

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


def test_runner_creates_and_publishes_to_scratch_remote(fixture, tmp_path):
    import httpx

    from solakit.core.loop import Execution
    from solakit.core.model import ModelClient

    root, config, registry, ctx = fixture
    config.paths.soul = tmp_path / "SOUL.md"
    config.paths.soul.write_text("Scratch journal acceptance.")
    config.paths.fragments = tmp_path / "fragments"
    (config.paths.fragments / "profiles").mkdir(parents=True)
    (config.paths.fragments / "profiles/writer.md").write_text("")
    filename = "2026-11-04-moon.md"
    arguments = [
        {
            "collection": "dreams",
            "slug": "moon",
            "title": "Moon",
            "body": "A silver moon.",
            "date": "2026-11-04",
        },
        {"collection": "dreams", "paths": [filename], "message": "Publish moon"},
    ]
    names = ["journal.create", "journal.publish"]
    captured = []

    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": config.model.model_id}]})
        captured.append(json.loads(request.content))
        step = len(captured) - 1
        message = {"role": "assistant", "content": "Published the moon entry."}
        if step < 2:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"journal-{step}",
                        "type": "function",
                        "function": {
                            "name": names[step],
                            "arguments": json.dumps(arguments[step]),
                        },
                    }
                ],
            }
        return httpx.Response(
            200,
            json={"model": config.model.model_id, "choices": [{"message": message}]},
        )

    model = ModelClient(config.model, transport=httpx.MockTransport(handler))
    execution = Execution(config, ctx.store, registry, model)

    async def exercise():
        try:
            run = await execution.submit("journal", "Write and publish a moon entry.")
            assert run.status == "completed"
            assert run.steps == 3
            history = ctx.store.transcript("journal")
            assert [item["role"] for item in history] == [
                "user",
                "assistant",
                "tool",
                "assistant",
                "tool",
                "assistant",
            ]
            for step in range(2):
                envelope, result = history[1 + step * 2 : 3 + step * 2]
                assert envelope["tool_calls"][0]["function"] == {
                    "name": names[step],
                    "arguments": json.dumps(arguments[step]),
                }
                assert result["tool_call_id"] == f"journal-{step}"
            created = json.loads(history[2]["content"])
            assert not created["error"]
            assert created["content"] == str(root / "_dreams" / filename)
            assert not json.loads(history[4]["content"])["error"]
            assert "Published commit" in history[4]["content"]
            assert history[-1]["content"] == "Published the moon entry."
            for step, body in enumerate(captured):
                assert body["messages"][1:] == [
                    {
                        key: item[key]
                        for key in ("role", "content", "tool_calls", "tool_call_id")
                        if key in item and item[key] is not None
                    }
                    for item in history[: 1 + step * 2]
                ]
                assert {tool["function"]["name"] for tool in body["tools"]} == set(
                    config.profiles["writer"].tools
                )
            record = ctx.store.run(run.id)
            assert [item["name"] for item in record["tools"]] == names
            assert git(root, "status", "--porcelain") == ""
            assert git(root, "rev-parse", "HEAD") == git(
                tmp_path / "remote.git", "rev-parse", "main"
            )
            assert (
                git(tmp_path / "remote.git", "show", f"main:_dreams/{filename}")
                == (root / "_dreams" / filename).read_text().strip()
            )
            assert (
                git(root, "show", "--format=", "--name-only", "HEAD")
                == f"_dreams/{filename}"
            )
            assert (root / "README").read_text() == "untouched"
        finally:
            await execution.close()

    asyncio.run(exercise())
