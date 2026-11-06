"""Scratch repositories only: exercise public tool dispatch and publication."""

import asyncio
import json
import subprocess
from pathlib import Path

import pytest
import yaml

from solakit.core.config import Config, PathsConfig, Profile
from solakit.core.schemas import Session
from solakit.core.store import Store
from solakit.services.git import Git
from solakit.tools import Registry, ToolContext


def git(path, *args):
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def payload(result):
    if result.content.startswith("<external-content"):
        return json.loads(result.content.split("\n", 1)[1].rsplit("\n", 1)[0])
    return json.loads(result.content)


def metadata(path):
    return yaml.safe_load(path.read_text().split("---\n", 2)[1])


@pytest.fixture
def site(tmp_path):
    root, remote = tmp_path / "site", tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", "--initial-branch=main", str(remote))
    git(tmp_path, "clone", str(remote), str(root))
    git(root, "config", "user.name", "Fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    (root / "package.json").write_text(
        json.dumps({"scripts": {"build": "node build.cjs"}})
    )
    (root / "build.cjs").write_text(
        "require('fs').mkdirSync('dist', {recursive:true});"
        "require('fs').writeFileSync('dist/index.html', 'built');"
    )
    (root / ".gitignore").write_text("dist/\n")
    git(root, "add", "package.json", "build.cjs", ".gitignore")
    git(root, "commit", "-m", "initial")
    git(root, "push", "-u", "origin", "main")
    cfg = Config(
        paths=PathsConfig(data=tmp_path / "evidence"),
        timezone="America/Denver",
        repositories={"site": root},
        enabled_tools=["sola_packs.blog"],
        profiles={
            "writer": Profile(
                tools=["blog.*"],
                workspace=["repo:site"],
                write="workspace",
                effects=["push"],
                tool_output_max_chars=16000,
            )
        },
    )
    registry = Registry(cfg)
    store = Store(cfg.paths.data)
    session = store.create_session(Session(id="blog", default_profile="writer"))
    ctx = ToolContext(session, "writer", cfg, store, "run")

    async def dispatch(name, **args):
        return await registry.dispatch(
            "blog." + name, json.dumps({"repository": "site", **args}), ctx
        )

    return root, remote, ctx, registry, dispatch


def post(**overrides):
    return {
        "operation_id": "post-1",
        "slug": "moon",
        "title": 'Moon: "hello"\n---',
        "description": "A #quiet [night]",
        "body": "# Exact\n\nbody  \n",
        "date": "2026-11-06",
        "tags": ["one: two", "夜"],
        **overrides,
    }


def receipts(ctx):
    return [
        json.loads(line)
        for path in (ctx.store.root / "pack-operations/blog").glob("*.jsonl")
        for line in path.read_text().splitlines()
    ]


def test_one_call_post_dream_and_replay(site):
    root, remote, ctx, registry, call = site

    async def exercise():
        assert len(registry.schemas("writer")) == 7
        result = await call("write_post", **post())
        assert not result.error, result.content
        output = payload(result)
        target = root / "src/content/blog/moon.md"
        assert target.read_text().endswith(post()["body"])
        front = metadata(target)
        assert front["title"] == post()["title"]
        assert front["tags"] == post()["tags"]
        assert front["date"] == "2026-11-06T00:00:00-07:00"
        assert "coverImage" not in front
        assert git(remote, "rev-parse", "main") == output["commit"]
        assert (
            git(root, "show", "--format=", "--name-only", "HEAD")
            == "src/content/blog/moon.md"
        )
        assert (root / "dist/index.html").exists()
        assert git(root, "status", "--porcelain") == ""
        assert payload(await call("write_post", **post())) == output
        assert (await call("write_post", **post(body="changed"))).error
        assert (await call("write_post", **post(operation_id="duplicate"))).error
        dream = await call(
            "write_dream",
            operation_id="dream-1",
            body="quiet\nmoon",
            date="2026-11-06T02:03:04-07:00",
            align="right",
        )
        assert not dream.error, dream.content
        dream_path = root / "src/content/dreams/2026-11-06.md"
        assert metadata(dream_path) == {
            "date": "2026-11-06T02:03:04-07:00",
            "align": "right",
        }
        assert not (await call("read_dream", date="2026-11-06")).error
        assert payload(await call("read_post", slug="moon")) == target.read_text()
        assert any(
            event["stage"] == "committed" and event["commit"] == output["commit"]
            for event in receipts(ctx)
        )

    asyncio.run(exercise())


def test_save_then_publish_listing_and_body(site):
    root, remote, ctx, _, call = site
    initial = git(remote, "rev-parse", "main")

    async def exercise():
        result = await call("write_post", **post(publish=False, draft=True))
        assert not result.error, result.content
        assert payload(result)["status"] == "saved"
        assert not (root / "dist").exists()
        assert git(remote, "rev-parse", "main") == initial
        listed = payload(await call("list_posts", limit=1))
        assert listed["entries"][0]["draft"] is True
        assert listed["entries"][0]["title"] == post()["title"]
        result = await call(
            "publish", operation_id="publish-saved", kind="post", identifier="moon"
        )
        assert not result.error, result.content
        assert metadata(root / "src/content/blog/moon.md")["draft"] is True
        assert payload(await call("list_dreams"))["entries"] == []
        for day in ("2026-11-01", "2026-11-03", "2026-11-02"):
            result = await call(
                "write_dream",
                operation_id=day,
                body="line one\nline two  ",
                date=day,
                publish=False,
            )
            assert not result.error, result.content
        listed = payload(await call("list_dreams", limit=1, offset=1))
        assert listed["total"] == 3 and listed["more"]
        assert listed["entries"][0]["identifier"] == "2026-11-02"
        assert listed["entries"][0]["align"] == "center"
        assert set(metadata(root / "src/content/dreams/2026-11-01.md")) == {"date"}
        assert (
            (root / "src/content/dreams/2026-11-01.md")
            .read_text()
            .endswith("line one\nline two  ")
        )

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "changes",
    [
        {"slug": "../escape"},
        {"slug": "UPPER"},
        {"body": " "},
        {"title": ""},
        {"description": ""},
        {"date": "2026-02-30"},
        {"date": "2026-11-06T03:00:00"},
        {"tags": ["x" * 101]},
    ],
)
def test_invalid_create_has_no_content_files(site, changes):
    root, _, _, _, call = site
    result = asyncio.run(call("write_post", **post(**changes)))
    assert result.error
    assert not (root / "src").exists()


def test_scope_push_and_symlink_denials(site, tmp_path):
    root, _, ctx, _, call = site

    async def exercise():
        ctx.config.profiles["writer"].effects = []
        result = await call("write_post", **post())
        assert result.error and "push" in result.content
        assert not (root / "src").exists()
        # Save-only needs no push effect, and can use a narrower write root.
        ctx.config.profiles["writer"].write_workspace = [
            f"path:{root}/src/content/blog"
        ]
        result = await call("write_post", **post(operation_id="save", publish=False))
        assert not result.error, result.content
        assert (
            await call(
                "write_dream", operation_id="narrow", body="dream", publish=False
            )
        ).error
        assert (await call("read_post", slug="../moon")).error
        (root / "src/content/blog/alias.md").symlink_to(
            root / "src/content/blog/moon.md"
        )
        assert (await call("read_post", slug="alias")).error
        assert payload(await call("list_posts"))["total"] == 1
        (root / "src/content/dreams").symlink_to(tmp_path)
        assert (
            await call(
                "write_dream", operation_id="symlink", body="dream", publish=False
            )
        ).error

    asyncio.run(exercise())


def test_dirty_worktree_and_staged_index_refused(site):
    root, _, _, _, call = site
    (root / "unrelated.txt").write_text("keep")
    result = asyncio.run(call("write_post", **post()))
    assert result.error and "clean" in result.content
    assert not (root / "src").exists()
    git(root, "add", "unrelated.txt")
    result = asyncio.run(call("write_post", **post(operation_id="staged")))
    assert result.error and "index" in result.content
    assert git(root, "diff", "--cached", "--name-only") == "unrelated.txt"


def test_build_failure_and_later_publish(site):
    root, remote, ctx, _, call = site
    (root / "build.cjs").write_text("console.error('failed'); process.exit(1)")
    git(root, "add", "build.cjs")
    git(root, "commit", "-m", "break fixture build")
    git(root, "push")
    initial = git(root, "rev-parse", "HEAD")

    async def exercise():
        result = await call("write_post", **post())
        assert result.error and "build failed" in result.content
        assert git(root, "rev-parse", "HEAD") == initial
        assert git(remote, "rev-parse", "main") == initial
        assert (root / "src/content/blog/moon.md").exists()
        assert (await call("write_post", **post())).error
        assert any(
            e["stage"] == "built" and e["build"]["exit_code"] == 1
            for e in receipts(ctx)
        )
        (root / "build.cjs").write_text("process.exit(0)")
        git(root, "add", "build.cjs")
        git(root, "commit", "-m", "fix fixture build")
        git(root, "push")
        result = await call(
            "publish", operation_id="recover", kind="post", identifier="moon"
        )
        assert not result.error, result.content

    asyncio.run(exercise())


def test_push_failure_keeps_commit_and_never_replays(site):
    root, remote, ctx, _, call = site
    initial = git(remote, "rev-parse", "main")
    git(root, "remote", "set-url", "--push", "origin", str(root / "missing.git"))

    async def exercise():
        result = await call("write_post", **post())
        assert result.error and "uncertain" in result.content
        commit = git(root, "rev-parse", "HEAD")
        assert commit != initial and commit in result.content
        assert receipts(ctx)[-1]["commit"] == commit
        assert (await call("write_post", **post())).error
        assert (
            await call("publish", operation_id="retry", kind="post", identifier="moon")
        ).error
        assert git(remote, "rev-parse", "main") == initial
        assert git(root, "rev-parse", "HEAD") == commit

    asyncio.run(exercise())


def test_concurrent_publish_serialized(site):
    root, remote, _, _, call = site

    async def exercise():
        results = await asyncio.gather(
            call("write_post", **post()),
            call("write_dream", operation_id="dream", body="dream", date="2026-11-06"),
        )
        assert all(not r.error for r in results), [r.content for r in results]
        assert git(root, "rev-parse", "HEAD") == git(remote, "rev-parse", "main")
        assert git(root, "rev-list", "--count", "HEAD") == "3"

    asyncio.run(exercise())


def test_cancellation_during_push_retains_identity(site, monkeypatch):
    root, remote, ctx, _, call = site
    entered = asyncio.Event()

    async def blocked_push(self, commit):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(Git, "_push", blocked_push)

    async def exercise():
        task = asyncio.create_task(call("write_post", **post()))
        await asyncio.wait_for(entered.wait(), 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        commit = git(root, "rev-parse", "HEAD")
        assert receipts(ctx)[-1]["commit"] == commit
        assert git(remote, "rev-parse", "main") != commit
        assert (await call("write_post", **post())).error

    asyncio.run(exercise())


def test_cover_composition_and_later_publish(site, monkeypatch):
    from sola_packs import image

    root, _, _, _, call = site
    png = b"fixture png bytes"

    async def completed(ctx, workflow, operation_id, image_index):
        assert (workflow, operation_id, image_index) == ("studio", "cover", 1)
        return png

    monkeypatch.setattr(image, "completed_png", completed)

    async def exercise():
        result = await call(
            "write_post",
            **post(
                publish=False,
                cover={
                    "workflow": "studio",
                    "operation_id": "cover",
                    "image_index": 1,
                },
            ),
        )
        assert not result.error, result.content
        cover = root / "src/assets/blog-images/moon-cover.png"
        assert cover.read_bytes() == png
        assert (
            metadata(root / "src/content/blog/moon.md")["coverImage"]
            == "../../assets/blog-images/moon-cover.png"
        )
        result = await call(
            "publish", operation_id="publish-cover", kind="post", identifier="moon"
        )
        assert not result.error, result.content
        assert git(root, "show", "--format=", "--name-only", "HEAD").splitlines() == [
            "src/assets/blog-images/moon-cover.png",
            "src/content/blog/moon.md",
        ]

    asyncio.run(exercise())


def test_historical_metadata_errors_and_plain_reading(site):
    root, _, _, _, call = site
    directory = root / "src/content/blog"
    directory.mkdir(parents=True)
    (directory / "legacy.md").write_text(
        "---\ntitle: Legacy\ndate: 2026-09-14T10:30:00\n---\nold"
    )
    (directory / "broken.md").write_text("literal text\nno metadata")

    async def exercise():
        listing = payload(await call("list_posts"))
        assert listing["total"] == 2
        assert listing["entries"][0]["title"] == "Legacy"
        assert "metadata_error" in listing["entries"][1]
        assert (
            payload(await call("read_post", slug="broken"))
            == "literal text\nno metadata"
        )
        assert (await call("list_posts", limit=0)).error
        assert (await call("list_posts", offset=-1)).error
        assert (
            await call(
                "write_dream", operation_id="bad-align", body="dream", align="justify"
            )
        ).error
        assert (
            await call("write_dream", operation_id="no-title", body="dream", title="no")
        ).error

    asyncio.run(exercise())


def test_missing_image_dependency_does_not_affect_plain_content(site, monkeypatch):
    import builtins

    root, _, _, _, call = site
    original = builtins.__import__

    def without_image(name, *args, **kwargs):
        if name == "sola_packs.image":
            raise ImportError("fixture: optional image pack absent")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_image)

    async def exercise():
        result = await call("write_post", **post(publish=False))
        assert not result.error, result.content
        result = await call(
            "write_post",
            **post(
                operation_id="cover",
                slug="cover",
                publish=False,
                cover={"workflow": "studio", "operation_id": "generated"},
            ),
        )
        assert result.error and "[image]" in result.content
        assert not (root / "src/content/blog/cover.md").exists()

    asyncio.run(exercise())


def test_default_date_duplicate_dream_and_torn_receipt(site):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    root, _, ctx, _, call = site

    async def exercise():
        before = datetime.now(ZoneInfo(ctx.config.timezone)).date().isoformat()
        result = await call(
            "write_dream", operation_id="today", body="today", publish=False
        )
        after = datetime.now(ZoneInfo(ctx.config.timezone)).date().isoformat()
        assert not result.error, result.content
        filename = payload(result)["paths"][0]
        assert filename in {f"src/content/dreams/{day}.md" for day in (before, after)}
        day = Path(filename).stem
        result = await call(
            "write_dream",
            operation_id="duplicate",
            body="other",
            date=day,
            publish=False,
        )
        assert result.error and "exists" in result.content
        assert (root / filename).read_text().endswith("today")
        # Select the original receipt rather than the failed duplicate.
        receipt = next(
            path
            for path in (ctx.store.root / "pack-operations/blog").glob("*.jsonl")
            if '"completed"' in path.read_text()
        )
        with receipt.open("ab") as stream:
            stream.write(b'{"torn"')
        result = await call(
            "write_dream", operation_id="today", body="today", publish=False
        )
        assert result.error and "recovery" in result.content

    asyncio.run(exercise())


def test_build_timeout_and_new_unrelated_changes_refuse_commit(site, monkeypatch):
    from sola_packs import blog

    root, remote, ctx, _, call = site
    initial = git(remote, "rev-parse", "main")

    async def timed_out(*args):
        return {
            "exit_code": -9,
            "timed_out": True,
            "stdout": "",
            "stderr": "",
            "stdout_complete": True,
            "stderr_complete": True,
        }

    async def changed(*args):
        (root / "unrelated.txt").write_text("keep")
        return {
            "exit_code": 0,
            "timed_out": False,
            "stdout": "fixture-secret",
            "stderr": "",
            "stdout_complete": True,
            "stderr_complete": True,
        }

    async def exercise():
        monkeypatch.setattr(blog, "execute", timed_out)
        result = await call("write_post", **post())
        assert result.error and "build failed" in result.content
        assert git(root, "rev-parse", "HEAD") == initial
        monkeypatch.setattr(blog, "execute", changed)
        ctx.store.redactor.values = ("fixture-secret",)
        result = await call(
            "publish", operation_id="changed", kind="post", identifier="moon"
        )
        assert result.error and "Unrelated" in result.content
        assert git(root, "rev-parse", "HEAD") == initial
        assert "fixture-secret" not in json.dumps(receipts(ctx))
        assert "[redacted]" in json.dumps(receipts(ctx))
        assert (root / "unrelated.txt").read_text() == "keep"

    asyncio.run(exercise())


def test_total_deadline_retains_receipt_and_releases_lock(site, monkeypatch):
    from sola_packs import blog

    _, _, ctx, _, call = site

    async def slow(*args):
        await asyncio.Event().wait()

    monkeypatch.setattr(blog, "execute", slow)
    monkeypatch.setattr(blog, "TOTAL_TIMEOUT", 0.15)

    async def exercise():
        result = await call("write_post", **post())
        assert result.error and "timed out" in result.content
        assert receipts(ctx)[-1]["stage"] == "interrupted_or_failed"
        # Saving again proves the repository lock was released.
        result = await call(
            "write_dream", operation_id="after", body="dream", publish=False
        )
        assert not result.error, result.content

    asyncio.run(exercise())


def test_publish_rejects_escape_cover_and_unrelated_saved_entry(site):
    root, _, _, _, call = site

    async def exercise():
        assert not (await call("write_post", **post(publish=False))).error
        target = root / "src/content/blog/moon.md"
        original = target.read_text()
        target.write_text(
            original.replace(
                "---\n\n", "coverImage: ../../../../outside.png\n---\n\n", 1
            )
        )
        result = await call(
            "publish", operation_id="unsafe", kind="post", identifier="moon"
        )
        assert result.error and "Cover" in result.content
        target.write_text(original)
        assert not (
            await call(
                "write_dream", operation_id="saved-dream", body="dream", publish=False
            )
        ).error
        result = await call(
            "publish", operation_id="unrelated", kind="post", identifier="moon"
        )
        assert result.error and "Only the selected" in result.content

    asyncio.run(exercise())
