import asyncio
import json
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient
from test_tools import context

from solakit.core.config import CollectionConfig, Config, Profile
from solakit.main import create_app
from solakit.tools import Registry, ToolError, scope_root


def git(path, *arguments):
    return subprocess.run(
        ["git", "-C", str(path), *arguments],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()


@pytest.fixture
def repository(config, tmp_path):
    remote, root = tmp_path / "remote.git", tmp_path / "worktree"
    git(tmp_path, "init", "--bare", "--initial-branch=main", str(remote))
    git(tmp_path, "clone", str(remote), str(root))
    git(root, "config", "user.name", "Fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    (root / "README").write_text("original\n")
    git(root, "add", "README")
    git(root, "commit", "-m", "initial")
    git(root, "push", "-u", "origin", "main")
    config.repositories = {"site": root}
    config.profiles["interactive"] = Profile(
        write_scopes=["repo:site"], capabilities=["git.push"]
    )
    return root, remote


def test_external_dotted_pack_and_builtin_shorthand(config, tmp_path, monkeypatch):
    package = tmp_path / "outside_pack"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "probe.py").write_text("""
from solakit.tools import tool, ToolResult
@tool(name="probe.read", profiles=["*"])
async def read(ctx, path: str):
    return await ctx.fs.read(path)
""")
    monkeypatch.syspath_prepend(str(tmp_path))
    config.enabled_tools = ["outside_pack.probe", "solakit.tools.files", "runtime"]
    config.profiles["custom"] = Profile(
        tools=["probe.read"], read_scopes=[f"path:{tmp_path}"]
    )
    registry = Registry(config)
    assert registry.allowed("probe.read", "custom")
    assert not registry.allowed("probe.read", "interactive")
    assert {"files.read", "runtime.status", "probe.read"} <= registry.tools.keys()
    assert "outside_pack.probe" in sys.modules
    # An external package is executable, operator-trusted code, not model data.
    ctx = context(config)
    ctx.profile = "custom"
    text = tmp_path / "input.txt"
    text.write_text("Read through a shared service")
    result = asyncio.run(
        registry.dispatch("probe.read", json.dumps({"path": str(text)}), ctx)
    )
    assert result.content == text.read_text()


def test_files_edit_append_list_and_failure_atomicity(config, tmp_path):
    config.enabled_tools = ["files"]
    config.profiles["interactive"] = Profile(
        tools=["files.edit", "files.append", "files.list"],
        write_scopes=[f"path:{tmp_path}"],
    )
    registry, ctx = Registry(config), context(config)
    path = tmp_path / "entry.txt"
    path.write_text("one typo two")
    path.chmod(0o640)

    async def exercise():
        assert not (
            await registry.dispatch(
                "files.edit",
                json.dumps(
                    {
                        "path": str(path),
                        "old": "typo",
                        "new": "word",
                    }
                ),
                ctx,
            )
        ).error
        assert path.read_text() == "one word two"
        assert path.stat().st_mode & 0o777 == 0o640
        for old in ("", "missing", "o"):
            assert (
                await registry.dispatch(
                    "files.edit",
                    json.dumps(
                        {
                            "path": str(path),
                            "old": old,
                            "new": "bad",
                        }
                    ),
                    ctx,
                )
            ).error
            assert path.read_text() == "one word two"
        assert not (
            await registry.dispatch(
                "files.append",
                json.dumps(
                    {
                        "path": str(path),
                        "content": "\nappended",
                    }
                ),
                ctx,
            )
        ).error
        assert path.read_text().endswith("\nappended")
        entries = json.loads(
            (
                await registry.dispatch(
                    "files.list",
                    json.dumps(
                        {
                            "path": str(tmp_path),
                            "limit": 1,
                        }
                    ),
                    ctx,
                )
            ).content
        )
        assert entries["more"]
        assert len(entries["entries"]) == 1
        config.profiles["interactive"].write_scopes = []
        with pytest.raises(ToolError):
            await ctx.fs.edit(path, "word", "denied")

    asyncio.run(exercise())


def test_collection_validation_and_scope_confinement(config, repository, tmp_path):
    root, _ = repository
    config.collections = {
        "dreams": CollectionConfig(repository="site", directory="_dreams")
    }
    config.profiles["interactive"] = Profile(write_scopes=["collection:dreams"])
    ctx = context(config)
    directory = root / "_dreams"
    directory.mkdir()
    (directory / "escape").symlink_to(tmp_path)
    assert scope_root("collection:dreams", config) == directory
    assert (
        ctx.collection("dreams").path("entry.md", write=True) == directory / "entry.md"
    )
    for path in (
        root / "README",
        directory / "escape/private",
        directory / "../README",
    ):
        with pytest.raises(ToolError):
            ctx.path(str(path), write=True)
    for data in (
        {"directory": "../private"},
        {"directory": "/absolute"},
        {"directory": ".git"},
        {"filename": "{slug.__class__}.md"},
        {"filename": "../{slug}.md"},
        {"layout": "{body!r}"},
    ):
        with pytest.raises(ValueError):
            CollectionConfig.model_validate(
                {"repository": "site", "directory": "_dreams", **data}
            )
    with pytest.raises(ValueError, match="unknown repository"):
        Config(
            collections={
                "bad": CollectionConfig(repository="missing", directory="entries")
            }
        )


def test_git_named_files_capabilities_and_reads(config, repository):
    root, _ = repository
    ctx = context(config)
    (root / "literal[1].txt").write_text("named\n")
    (root / "literal1.txt").write_text("unrelated\n")

    async def exercise():
        assert "literal[1].txt" in (await ctx.git("site").status()).content
        before = git(root, "rev-parse", "HEAD")
        for paths in (["."], ["../private"], [".git/config"], [str(root / "README")]):
            with pytest.raises(ToolError):
                await ctx.git("site").commit_push(paths, "must fail")
            assert git(root, "rev-parse", "HEAD") == before
        result = await ctx.git("site").commit_push(
            ["literal[1].txt"], "Publish named file"
        )
        assert "Published commit" in result.content
        assert git(root, "show", "--format=", "--name-only", "HEAD") == "literal[1].txt"
        assert "literal1.txt" in git(root, "status", "--short")
        assert "Publish named file" in (await ctx.git("site").log()).content
        (root / "README").write_text("changed\n")
        assert "+changed" in (await ctx.git("site").diff()).content
        config.profiles["interactive"].capabilities = []
        with pytest.raises(ToolError, match="git.push"):
            await ctx.git("site").commit_push(["README"], "denied")
        config.profiles["interactive"].write_scopes = [f"path:{root}"]
        with pytest.raises(ToolError, match="Repository write"):
            await ctx.git("site").pull()

    asyncio.run(exercise())


def test_git_dirty_index_and_failed_push_do_not_publish_unrelated_changes(
    config, repository
):
    root, _ = repository
    ctx = context(config)
    (root / "README").write_text("staged unrelated\n")
    git(root, "add", "README")
    (root / "new.txt").write_text("new\n")

    async def exercise():
        initial = git(root, "rev-parse", "HEAD")
        with pytest.raises(ToolError, match="empty index"):
            await ctx.git("site").commit_push(["new.txt"], "must fail")
        assert git(root, "rev-parse", "HEAD") == initial
        git(root, "reset", "--", "README")
        git(root, "remote", "set-url", "--push", "origin", str(root / "missing.git"))
        with pytest.raises(ToolError, match="exists locally; push failed"):
            await ctx.git("site").commit_push(["new.txt"], "local commit")
        committed = git(root, "rev-parse", "HEAD")
        assert committed != initial
        with pytest.raises(ToolError, match="Unpublished commits"):
            await ctx.git("site").commit_push(["README"], "must not retry")
        assert git(root, "rev-parse", "HEAD") == committed

    asyncio.run(exercise())


def test_git_pull_fast_forward_and_bounded_output(config, repository, tmp_path):
    root, remote = repository
    other = tmp_path / "other"
    git(tmp_path, "clone", str(remote), str(other))
    git(other, "config", "user.name", "Fixture")
    git(other, "config", "user.email", "fixture@example.invalid")
    (other / "large.txt").write_text("x" * 200000)
    git(other, "add", "large.txt")
    git(other, "commit", "-m", "remote update")
    git(other, "push")
    ctx = context(config)

    async def exercise():
        await ctx.git("site").pull()
        assert (root / "large.txt").exists()
        result = await ctx.git("site")._run("show", "HEAD:large.txt")
        assert not result.content_complete
        assert len(result.content) <= 80000
        (root / "README").write_text("dirty")
        with pytest.raises(ToolError, match="clean worktree"):
            await ctx.git("site").pull()

    asyncio.run(exercise())


def test_runtime_status_is_live_and_profile_scoped(config, mock_model):
    config.enabled_tools = ["runtime"]
    config.profiles["interactive"].tools = ["runtime.status"]
    app = create_app(config=config, model=mock_model)
    with TestClient(app):
        ctx = context(config)
        result = asyncio.run(app.state.registry.dispatch("runtime.status", "{}", ctx))
        snapshot = json.loads(result.content)
        assert snapshot["model"] == {"id": config.model.model_id, "available": True}
        assert snapshot["granted_tools"] == ["runtime.status"]
        assert (
            snapshot["tool_distributions"]
            == app.state.registry.catalog()["distributions"]
        )
        assert snapshot["tool_distributions"][0]["distribution"] == "solakit"
        assert snapshot["channels"] == [] and snapshot["jobs"] == []
        app.state.model.available = False
        snapshot = json.loads(
            asyncio.run(
                app.state.registry.dispatch("runtime.status", "{}", ctx)
            ).content
        )
        assert snapshot["model"]["available"] is False


def test_separate_commit_push_rechecks_collection_and_publish_grants(
    config, repository
):
    root, _ = repository
    directory = root / "_dreams"
    directory.mkdir()
    (directory / "entry.md").write_text("Dream")
    config.collections = {
        "dreams": CollectionConfig(repository="site", directory="_dreams")
    }
    ctx = context(config)

    async def exercise():
        commit = await ctx.git("site").commit(["_dreams/entry.md"], "Create dream")
        # Creation and publishing remain separate, even when packs compose them.
        config.profiles["interactive"].write_scopes = ["collection:dreams"]
        config.profiles["interactive"].capabilities = []
        with pytest.raises(ToolError, match="git.push"):
            await ctx.collection("dreams").git().push(commit)
        config.profiles["interactive"].capabilities = ["git.push"]
        assert (
            "Published commit"
            in (await ctx.collection("dreams").git().push(commit)).content
        )
        # An unrelated commit cannot be laundered through a narrower resource.
        config.profiles["interactive"].write_scopes = ["repo:site"]
        (root / "README").write_text("unrelated edit")
        unrelated = await ctx.git("site").commit(["README"], "Change unrelated path")
        config.profiles["interactive"].write_scopes = ["collection:dreams"]
        with pytest.raises(ToolError):
            await ctx.collection("dreams").git().push(unrelated)

    asyncio.run(exercise())


def test_exact_edit_rejects_overlapping_matches_and_preserves_other_bytes(
    config, tmp_path
):
    config.profiles["interactive"].write_scopes = [f"path:{tmp_path}"]
    ctx = context(config)
    path = tmp_path / "crlf.txt"
    source = b"aaa\r\nfirst typo\r\nlast\r\n"
    path.write_bytes(source)

    async def exercise():
        with pytest.raises(ToolError, match="exactly once"):
            await ctx.fs.edit(path, "aa", "ambiguous")
        assert path.read_bytes() == source
        await ctx.fs.edit(path, "typo", "word")
        assert path.read_bytes() == source.replace(b"typo", b"word")

    asyncio.run(exercise())
