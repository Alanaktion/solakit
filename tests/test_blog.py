import asyncio
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from test_images import Provider, png, service
from test_tools import Harness, opened

from solakit.blog import BlogService, content_date, frontmatter
from solakit.config import ConfigError, OperatorConfig, resolve_profile, restrict_to_admission
from solakit.evidence import canonical
from solakit.files import GuardedFiles
from solakit.jobs import JobService
from solakit.model import ModelAdapter
from solakit.records import (
    ErrorCode,
    Job,
    ModelStep,
    Occurrence,
    Origin,
    RunState,
    SessionMode,
    ToolReceipt,
    new_id,
)
from solakit.run_loop import RunLoop, requested_tools

POEM = "A cold key\nunder the doorstep\nremembers a hand."


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def data(receipt: ToolReceipt) -> dict[str, Any]:
    assert receipt.status == "succeeded", receipt.failure
    assert isinstance(receipt.result, str)
    result: dict[str, Any] = json.loads(receipt.result)
    return result


@contextmanager
def blog_harness(tmp_path: Path, *, mode: SessionMode = SessionMode.SHARED) -> Iterator[Harness]:
    with opened(tmp_path, blog_repository=tmp_path / "workspace", mode=mode) as h:
        root = h.root
        for directory in ("src/content/blog", "src/content/dreams", "src/assets/blog-images"):
            (root / directory).mkdir(parents=True)
            (root / directory / ".gitkeep").touch()
        (root / "package.json").write_text(json.dumps({"scripts": {"build": "node build.cjs"}}))
        (root / "build.cjs").write_text(
            "const fs = require('fs'); fs.writeFileSync('.built', 'yes'); "
            "process.exit(fs.existsSync('.build-fail') ? 1 : 0);"
        )
        (root / ".gitignore").write_text(".built\n.build-fail\n")
        (root / "source.txt").write_text("original")
        git(root, "init", "--initial-branch=main")
        git(root, "config", "user.name", "Fixture")
        git(root, "config", "user.email", "fixture@example.invalid")
        git(root, "config", "commit.gpgsign", "false")
        git(root, "config", "core.hooksPath", "/dev/null")
        git(root, "add", ".")
        git(root, "commit", "-m", "Initial site")
        remote = tmp_path / "remote.git"
        subprocess.run(
            ["git", "init", "--bare", "--initial-branch=main", str(remote)],
            check=True,
            capture_output=True,
        )
        git(root, "remote", "add", "origin", str(remote))
        git(root, "push", "--set-upstream", "origin", "main")
        h.dispatcher.blog = BlogService()
        yield h


def create(h: Harness, slug: str = "a-post", **changes: Any) -> ToolReceipt:
    return h.dispatch(
        "blog.create",
        **{
            "slug": slug,
            "title": "A title: 雨\n---\nwith punctuation",
            "description": "A description",
            "body": "The body\n\n---\nkeeps its spacing.",
            "date": "2026-11-19T12:00:00-07:00",
            "tags": ["one", "two"],
        }
        | changes,
    )


def test_draft_read_and_batch_publication(tmp_path: Path) -> None:
    with blog_harness(tmp_path) as h:
        root = h.root
        (root / "source.txt").write_text("pending source commit")
        git(root, "add", "source.txt")
        git(root, "commit", "-m", "Pending source change")
        pending_commit = git(root, "rev-parse", "HEAD")
        (root / "source.txt").write_text("leave staged")
        git(root, "add", "source.txt")
        assert data(create(h))["draft"]
        assert not (root / ".built").exists()
        assert data(create(h, "other-draft"))["draft"]
        read = data(h.dispatch("blog.read", slug="a-post"))
        assert read["metadata"]["title"] == "A title: 雨\n---\nwith punctuation"
        assert read["metadata"]["tags"] == ["one", "two"]
        assert read["body"] == "\nThe body\n\n---\nkeeps its spacing.\n"
        dream = root / "src/content/dreams/2026-11-18.md"
        dream.write_text("---\ndate: 2026-11-18\n---\n\n" + POEM)
        (root / "src/assets/blog-images/pending.png").write_bytes(png(64, 64))
        published = data(h.dispatch("blog.publish", slug="a-post"))
        assert published["published"] and not published["draft"]
        assert (root / ".built").exists()
        assert git(tmp_path / "remote.git", "rev-parse", "main") == published["commit"]
        assert git(root, "show", f"{pending_commit}:source.txt") == "pending source commit"
        assert git(tmp_path / "remote.git", "show", "main:source.txt") == "pending source commit"
        assert git(root, "diff", "--cached", "--name-only") == "source.txt"
        assert not data(h.dispatch("blog.read", slug="a-post"))["metadata"]["draft"]
        assert data(h.dispatch("blog.read", slug="other-draft"))["metadata"]["draft"]
        assert git(root, "show", "HEAD:src/content/dreams/2026-11-18.md").endswith(POEM)
        assert "src/assets/blog-images/pending.png" in git(root, "show", "--name-only", "--format=")
        count = git(root, "rev-list", "--count", "HEAD")
        assert data(h.dispatch("blog.publish", slug="a-post"))["commit"] == published["commit"]
        assert git(root, "rev-list", "--count", "HEAD") == count


def test_dream_always_publishes_and_rejects_duplicate(tmp_path: Path) -> None:
    with blog_harness(tmp_path) as h:
        dream = data(h.dispatch("dreams.create", body=POEM, date="2026-11-19T23:30:00Z"))
        assert dream["published"] and dream["slug"] == "2026-11-19"
        metadata, body = frontmatter(Path(dream["path"]).read_text())
        assert set(metadata) == {"date"}
        assert body.strip() == POEM
        assert data(h.dispatch("dreams.read", slug=dream["slug"]))["body"].strip() == POEM
        assert h.dispatch("dreams.create", body=POEM, date="2026-11-19").status == "failed"
        assert "dreams.publish" not in h.dispatcher.registry.tools
        assert h.dispatch("dreams.create", body=POEM, auto_publish=False).status == "failed"


def test_blog_auto_publish(tmp_path: Path) -> None:
    with blog_harness(tmp_path) as h:
        assert data(create(h, auto_publish=True))["published"]
        assert not data(h.dispatch("blog.read", slug="a-post"))["metadata"]["draft"]


def test_chronological_pagination_legacy_names_and_unknown_metadata(tmp_path: Path) -> None:
    with blog_harness(tmp_path) as h:
        for slug, at in (("a", "2026-11-20"), ("z", "2025-01-01"), ("b", "2026-11-20")):
            assert data(create(h, slug, date=at))["draft"]
        page = data(h.dispatch("blog.list", limit=2))
        assert [entry["slug"] for entry in page["entries"]] == ["b", "a"]
        assert page["next_offset"] == 2
        assert data(h.dispatch("blog.list", offset=2))["entries"][0]["slug"] == "z"
        assert data(h.dispatch("blog.list", offset=2))["next_offset"] is None
        legacy = h.root / "src/content/dreams/2026-09-15n.md"
        legacy.write_text("---\ndate: '2026-09-15T21:20:00-06:00'\n---\n" + POEM)
        assert data(h.dispatch("dreams.list"))["entries"][0]["slug"] == legacy.stem
        assert data(h.dispatch("dreams.read", slug=legacy.stem))["body"] == POEM
        path = h.root / "src/content/blog/a.md"
        path.write_text(
            path.read_text().replace("draft: true", "draft: true\ncustom: {key: value}")
        )
        before = data(h.dispatch("blog.read", slug="a"))["body"]
        data(h.dispatch("blog.publish", slug="a"))
        after = data(h.dispatch("blog.read", slug="a"))
        assert after["metadata"]["custom"] == {"key": "value"}
        assert after["body"] == before


@pytest.mark.parametrize(
    "changes",
    [
        {"slug": "../escape"},
        {"slug": "UPPER"},
        {"date": "yesterday"},
        {"title": " "},
        {"description": " "},
        {"body": " "},
        {"tags": [" "]},
        {"cover_path": "x", "cover_artifact_id": "0" * 32},
    ],
)
def test_invalid_blog_has_no_content_effect(tmp_path: Path, changes: dict[str, Any]) -> None:
    with blog_harness(tmp_path) as h:
        receipt = create(h, **changes)
        assert receipt.status in {"failed", "denied"}
        assert receipt.effect == "none"
        assert not list((h.root / "src/content/blog").glob("*.md"))


def test_build_failure_and_manual_dream_recovery(tmp_path: Path) -> None:
    with blog_harness(tmp_path) as h:
        before = git(h.root, "rev-parse", "HEAD")
        (h.root / ".build-fail").touch()
        receipt = h.dispatch("dreams.create", body=POEM, date="2026-11-19")
        assert receipt.status == "failed" and receipt.effect == "confirmed"
        assert receipt.failure and "stopped at build" in receipt.failure.message
        assert (h.root / "src/content/dreams/2026-11-19.md").exists()
        assert git(h.root, "rev-parse", "HEAD") == before
        (h.root / ".build-fail").unlink()
        retry = h.dispatch("dreams.create", body=POEM, date="2026-11-19")
        assert (
            retry.status == "failed" and retry.failure and "already exists" in retry.failure.message
        )


def test_push_failure_preserves_commit_and_reports_uncertainty(tmp_path: Path) -> None:
    with blog_harness(tmp_path) as h:
        git(h.root, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
        receipt = create(h, auto_publish=True)
        assert receipt.status == "uncertain" and receipt.effect == "uncertain"
        commit = git(h.root, "rev-parse", "HEAD")
        assert receipt.failure and "stopped at push" in receipt.failure.message
        assert commit in receipt.failure.message
        assert (h.root / "src/content/blog/a-post.md").exists()


def test_diverged_remote_rejects_push_without_reconciliation(tmp_path: Path) -> None:
    with blog_harness(tmp_path) as h:
        remote_commit = git(
            h.root,
            "commit-tree",
            git(h.root, "rev-parse", "HEAD^{tree}"),
            "-p",
            "HEAD",
            "-m",
            "Remote-only change",
        )
        git(h.root, "push", "origin", f"{remote_commit}:main")
        receipt = create(h, auto_publish=True)
        assert receipt.status == "uncertain" and receipt.failure
        assert "stopped at push" in receipt.failure.message
        assert git(tmp_path / "remote.git", "rev-parse", "main") == remote_commit
        assert git(h.root, "rev-parse", "HEAD") != remote_commit
        assert "src/content/blog/a-post.md" in git(h.root, "ls-tree", "-r", "--name-only", "HEAD")


def test_failed_commit_preserves_staged_content(tmp_path: Path) -> None:
    with blog_harness(tmp_path) as h:
        hooks = tmp_path / "hooks"
        hooks.mkdir()
        hook = hooks / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o700)
        git(h.root, "config", "core.hooksPath", str(hooks))
        original = git(h.root, "rev-parse", "HEAD")
        receipt = create(h, auto_publish=True)
        assert receipt.status == "uncertain" and receipt.failure
        assert "stopped at commit" in receipt.failure.message
        assert git(h.root, "rev-parse", "HEAD") == original
        assert "src/content/blog/a-post.md" in git(h.root, "diff", "--cached", "--name-only")


def test_ignored_target_does_not_claim_publication(tmp_path: Path) -> None:
    with blog_harness(tmp_path) as h:
        (h.root / ".git/info/exclude").write_text("src/content/blog/a-post.md\n")
        before = git(tmp_path / "remote.git", "rev-parse", "main")
        receipt = create(h, auto_publish=True)
        assert receipt.status == "uncertain" and receipt.failure
        assert "stopped at stage" in receipt.failure.message
        assert git(tmp_path / "remote.git", "rev-parse", "main") == before
        assert (h.root / "src/content/blog/a-post.md").exists()


def test_local_cover_and_invalid_cover(tmp_path: Path) -> None:
    with blog_harness(tmp_path) as h:
        source = h.root / "cover.png"
        source.write_bytes(png(64, 64))
        data(create(h, cover_path=str(source)))
        destination = h.root / "src/assets/blog-images/a-post-cover.png"
        assert destination.read_bytes() == source.read_bytes()
        assert data(h.dispatch("blog.read", slug="a-post"))["metadata"]["coverImage"] == (
            "../../assets/blog-images/a-post-cover.png"
        )
        source.write_bytes(b"bad PNG")
        receipt = create(h, "bad-cover", cover_path=str(source))
        assert receipt.status == "failed" and receipt.effect == "none"
        assert not (h.root / "src/content/blog/bad-cover.md").exists()


@pytest.mark.parametrize("mode", list(SessionMode))
def test_generated_cover_scope_and_hash(tmp_path: Path, mode: SessionMode) -> None:
    with blog_harness(tmp_path, mode=mode) as h:

        async def scenario() -> None:
            images = service(h.store, Provider())
            h.dispatcher.images = images
            await images.start()
            try:
                generated = await h.session.dispatch(h.call("images.generate", prompt="cover"))
                assert generated.artifact
                identity = Path(generated.artifact.path).name
                receipt = await h.session.dispatch(
                    h.call(
                        "blog.create",
                        slug="generated",
                        title="Generated",
                        description="Cover",
                        body="Body",
                        cover_artifact_id=identity,
                    )
                )
                assert data(receipt)["draft"]
                (h.store.root / generated.artifact.path).write_bytes(b"corrupt")
                corrupt = await h.session.dispatch(
                    h.call(
                        "blog.create",
                        slug="corrupt",
                        title="Corrupt",
                        description="Cover",
                        body="Body",
                        cover_artifact_id=identity,
                    )
                )
                assert corrupt.status == "failed" and corrupt.failure
                assert corrupt.failure.code == ErrorCode.CORRUPTION
                session = h.store.create_session(mode=mode)
                run = h.store.admit(
                    session.id,
                    request_id=new_id(),
                    content="fixture",
                    profile=h.profile.name,
                    admission=h.profile.model_dump(mode="json"),
                )
                h.store.transition_run(run.id, RunState.RUNNING)
                step = ModelStep(
                    origin=run.origin,
                    session_id=session.id,
                    run_id=run.id,
                    index=1,
                    model=h.profile.model_pin,
                )
                h.store.append(step)
                call = h.call(
                    "blog.create",
                    slug="foreign",
                    title="Foreign",
                    description="Cover",
                    body="Body",
                    cover_artifact_id=identity,
                ).model_copy(
                    update={
                        "session_id": session.id,
                        "run_id": run.id,
                        "model_step_id": step.id,
                    }
                )
                foreign = await h.dispatcher.bind(run.id).dispatch(call)
                assert foreign.status == "denied" and foreign.effect == "none"
            finally:
                await images.stop()

        asyncio.run(scenario())


def test_grants_roots_and_protected_paths(tmp_path: Path) -> None:
    with blog_harness(tmp_path) as h:
        h.dispatcher.current = lambda admitted: h.profile.model_copy(update={"tools": frozenset()})
        assert create(h).status == "denied"
        sub = h.root / "src/content/blog"
        h.dispatcher.current = lambda admitted: h.profile.model_copy(
            update={"workspace_roots": (sub,)}
        )
        assert create(h).status == "denied"
        h.dispatcher.current = lambda admitted: h.profile
        h.dispatcher.protected += (h.root,)
        assert create(h).status == "denied"


def test_config_resolution_and_freeze(tmp_path: Path) -> None:
    config = OperatorConfig.model_validate(
        {
            "model": {"pin": "fixture"},
            "defaults": {"workspace_roots": [str(tmp_path)]},
            "blog": {"repository": "."},
        }
    )
    profile = resolve_profile(config, base=tmp_path)
    assert profile.blog_repository == tmp_path.resolve()
    assert dict(profile.sources)["blog_repository"] == "blog"
    assert not profile.tools
    with pytest.raises(ConfigError, match="blog repository changed"):
        restrict_to_admission(profile.model_copy(update={"blog_repository": None}), profile)
    old = profile.model_dump(mode="json")
    del old["blog_repository"]
    assert type(profile).model_validate_json(canonical(old)).blog_repository is None
    assert content_date("2026-11-20T02:00:00Z", "America/Denver").date().isoformat() == "2026-11-19"
    assert (
        content_date(None, "America/Denver").date()
        == datetime.now().astimezone(content_date(None, "America/Denver").tzinfo).date()
    )


def test_process_cancellation_reaps_children(tmp_path: Path) -> None:
    with blog_harness(tmp_path) as h:
        files = GuardedFiles(lambda _: h.profile, (), lambda _: None, lambda: None)
        script = (
            "import pathlib, subprocess, sys, time; "
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
            "pathlib.Path('.child').write_text(str(child.pid)); time.sleep(60)"
        )

        async def scenario() -> None:
            command = asyncio.create_task(
                BlogService().command(h.root, files, sys.executable, "-c", script)
            )
            async with asyncio.timeout(5):
                while not (h.root / ".child").exists():
                    await asyncio.sleep(0.01)
            command.cancel()
            with pytest.raises(asyncio.CancelledError):
                await command

        asyncio.run(scenario())
        pid = int((h.root / ".child").read_text())
        # A killed grandchild may briefly remain a zombie until its OS reaper runs.
        process = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True
        )
        assert process.returncode != 0 or process.stdout.strip().startswith("Z")


@pytest.mark.parametrize("body", ["one line", "\n".join(["line"] * 16), "   "])
def test_invalid_dream_lines(tmp_path: Path, body: str) -> None:
    with blog_harness(tmp_path) as h:
        receipt = h.dispatch("dreams.create", body=body)
        assert receipt.status == "failed" and receipt.effect == "none"


def test_symlinks_hardlinks_and_duplicate_covers(tmp_path: Path) -> None:
    with blog_harness(tmp_path) as h:
        path = h.root / "src/content/blog/linked.md"
        path.symlink_to(h.root / "source.txt")
        assert h.dispatch("blog.read", slug="linked").status == "denied"
        path.unlink()
        os.link(h.root / "source.txt", path)
        assert h.dispatch("blog.read", slug="linked").status == "denied"
        path.unlink()
        cover = h.root / "cover.png"
        cover.write_bytes(png(64, 64))
        data(create(h, cover_path=str(cover)))
        saved = (h.root / "src/assets/blog-images/a-post-cover.png").read_bytes()
        assert create(h, cover_path=str(cover)).status == "failed"
        assert (h.root / "src/assets/blog-images/a-post-cover.png").read_bytes() == saved
        directory = h.root / "src/content/dreams"
        directory.rename(h.root / "dreams-saved")
        directory.symlink_to(h.root / "dreams-saved", target_is_directory=True)
        assert h.dispatch("dreams.create", body=POEM).status == "denied"


def test_deleted_and_pre_staged_content(tmp_path: Path) -> None:
    with blog_harness(tmp_path) as h:
        data(create(h, "to-delete", auto_publish=True))
        (h.root / "src/content/blog/to-delete.md").unlink()
        data(create(h, "staged-draft"))
        git(h.root, "add", "src/content/blog/staged-draft.md")
        data(h.dispatch("dreams.create", body=POEM, date="2026-11-19"))
        assert "to-delete.md" not in git(h.root, "ls-tree", "-r", "--name-only", "HEAD")
        assert "staged-draft.md" in git(h.root, "ls-tree", "-r", "--name-only", "HEAD")


@pytest.mark.parametrize("quiet,explicit_roots", [(True, True), (False, False), (False, True)])
def test_background_quiet_hours_and_explicit_roots(
    tmp_path: Path,
    quiet: bool,
    explicit_roots: bool,
) -> None:
    with blog_harness(tmp_path) as h:
        profile = h.profile.model_copy(update={"origin": Origin.PROACTIVE})
        definition = Job(
            origin=Origin.PROACTIVE,
            profile=profile.name,
            task="fixture",
            schedule="once:2026-11-20T10:00:00Z",
            session_id=h.step.session_id,
            enabled=True,
            workspace_roots=(str(h.root),) if explicit_roots else None,
        )
        h.store._commit((definition,))
        occurrence = Occurrence(
            job_id=definition.id,
            due_at=datetime(2026, 11, 20, 10, tzinfo=UTC),
            origin=Origin.PROACTIVE,
            definition=definition.model_dump(mode="json"),
        )
        run = h.store.admit(
            h.step.session_id,
            request_id=new_id(),
            content="fixture",
            profile=profile.name,
            origin=Origin.PROACTIVE,
            job_id=definition.id,
            occurrence_id=occurrence.id,
            admission=profile.model_dump(mode="json"),
            claim=(definition, occurrence, definition.model_copy(update={"consumed": True})),
        )
        h.store.transition_run(run.id, RunState.RUNNING)
        step = ModelStep(
            origin=Origin.PROACTIVE,
            session_id=run.session_id,
            run_id=run.id,
            index=1,
            model=profile.model_pin,
        )
        h.store.append(step)
        h.dispatcher.current = lambda admitted: profile
        runs = RunLoop(h.store, ModelAdapter(profile, h.store, asyncio.Semaphore(1)), h.dispatcher)
        h.dispatcher.jobs = JobService(
            h.store,
            runs,
            lambda: profile,
            clock=lambda: datetime(2026, 11, 20, 10 if quiet else 18, tzinfo=UTC),
        )
        call = h.call("dreams.create", body=POEM, date="2026-11-19").model_copy(
            update={
                "run_id": run.id,
                "origin": Origin.PROACTIVE,
                "model_step_id": step.id,
            }
        )
        receipt = asyncio.run(h.dispatcher.bind(run.id).dispatch(call))
        if quiet or not explicit_roots:
            assert receipt.status == "denied" and receipt.effect == "none"
            assert not list((h.root / "src/content/dreams").glob("*.md"))
        else:
            assert data(receipt)["published"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Generate a list of ideas.", set()),
        ("Create a poem about cats.", set()),
        ("Create a blog post.", {"blog.create"}),
        ("Create a dream poem.", {"dreams.create"}),
        ("Use blog.read for a-post.", {"blog.read"}),
    ],
)
def test_content_tool_matching(text: str, expected: set[str]) -> None:
    assert (
        requested_tools(
            text,
            (
                "blog.list",
                "blog.read",
                "blog.create",
                "blog.publish",
                "dreams.list",
                "dreams.read",
                "dreams.create",
            ),
        )
        == expected
    )
