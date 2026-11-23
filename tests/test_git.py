import asyncio
import json
import os
from pathlib import Path
from typing import cast

import pytest
from test_tools import changed, opened

from scripts.probe_git import configure_author, git, repository
from solakit.config import EffectiveProfile, OperatorConfig, resolve_profile, restrict_to_admission
from solakit.dispatch import Invocation
from solakit.git import GitCheckout
from solakit.records import Origin, ToolReceipt


def aliases(**checkouts: Path) -> dict[str, dict[str, str]]:
    return {name: {"path": str(path), "branch": "main"} for name, path in checkouts.items()}


@pytest.fixture
def project(tmp_path: Path) -> tuple[Path, Path]:
    checkout = tmp_path / "workspace" / "project"
    checkout.parent.mkdir()
    return checkout, repository(tmp_path, checkout)


def result(receipt: ToolReceipt) -> dict[str, object]:
    assert receipt.status == "succeeded", receipt.failure
    assert isinstance(receipt.result, str)
    return cast(dict[str, object], json.loads(receipt.result))


def test_status_and_diff(tmp_path: Path, project: tuple[Path, Path]) -> None:
    checkout, _ = project
    git(checkout, "rm", "obsolete.txt")
    (checkout / "README.md").write_text("modified fixture\n")
    (checkout / "new\nfile").write_text("not tracked")
    (checkout / ".git" / "index").touch()
    before = (checkout / ".git" / "index").stat().st_mtime_ns
    with opened(tmp_path, git_repositories=aliases(project=checkout)) as h:
        assert result(h.dispatch("git.status", repository="project")) == {
            "branch": "main",
            "staged": ["obsolete.txt"],
            "unstaged": ["README.md"],
            "untracked": ["new\nfile"],
            "truncated": False,
        }
        all_diff = result(h.dispatch("git.diff", repository="project"))
        assert isinstance(all_diff["diff"], str)
        assert "+modified fixture" in all_diff["diff"] and "-Old fixture" in all_diff["diff"]
        one_diff = result(h.dispatch("git.diff", repository="project", path="obsolete.txt"))
        assert isinstance(one_diff["diff"], str)
        assert "-Old fixture" in one_diff["diff"] and "modified fixture" not in one_diff["diff"]
        (checkout / "README.md").write_text("x" * 50000)
        assert result(h.dispatch("git.diff", repository="project", path="README.md"))["truncated"]
    assert (checkout / ".git" / "index").stat().st_mtime_ns == before


def test_pull_fast_forward_and_refusal(tmp_path: Path, project: tuple[Path, Path]) -> None:
    checkout, remote = project
    peer = tmp_path / "peer"
    git(tmp_path, "clone", "--no-hardlinks", str(remote), str(peer))
    configure_author(peer)
    old = git(checkout, "rev-parse", "HEAD")
    (peer / "README.md").write_text("From peer\n")
    git(peer, "commit", "-am", "Peer update")
    git(peer, "push")
    upstream = git(remote, "rev-parse", "main")
    with opened(tmp_path, git_repositories=aliases(project=checkout)) as h:
        dirty = checkout / "untracked.txt"
        dirty.touch()
        denied = h.dispatch("git.pull", repository="project")
        assert denied.status == "denied" and denied.effect == "none"
        assert git(checkout, "rev-parse", "HEAD") == old
        dirty.unlink()
        assert result(h.dispatch("git.pull", repository="project")) == {
            "old_commit": old,
            "new_commit": upstream,
        }
        assert (checkout / "README.md").read_text() == "From peer\n"
        (checkout / "README.md").write_text("Local divergence\n")
        git(checkout, "commit", "-am", "Local update")
        local = git(checkout, "rev-parse", "HEAD")
        git(peer, "commit", "--allow-empty", "-m", "Another peer update")
        git(peer, "push")
        failed = h.dispatch("git.pull", repository="project")
        assert failed.status == "failed" and failed.failure
        assert "upstream fetched" in failed.failure.message
        assert git(checkout, "rev-parse", "HEAD") == local


def test_publishes_only_selected_paths(tmp_path: Path, project: tuple[Path, Path]) -> None:
    checkout, remote = project
    (checkout / "README.md").write_text("Selected edit\n")
    (checkout / "obsolete.txt").write_text("Unrelated tracked edit\n")
    (checkout / "-new.md").write_text("Selected addition\n")
    (checkout / "scratch.txt").touch()
    git(checkout, "add", "obsolete.txt")
    hook = checkout / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    with opened(tmp_path, git_repositories=aliases(project=checkout)) as h:
        changed_paths = ["README.md", "-new.md"]
        denied = h.dispatch(
            "git.publish", repository="project", paths=changed_paths, message="Edit"
        )
        assert denied.status == "denied" and denied.effect == "none"
        git(checkout, "restore", "--staged", "obsolete.txt")
        published = h.dispatch(
            "git.publish", repository="project", paths=changed_paths, message="Edit"
        )
        assert published.effect == "confirmed"
        data = result(published)
        commit = git(remote, "rev-parse", "main")
        assert data == {"commit": commit, "push": "succeeded"}
        assert set(
            git(remote, "diff-tree", "--no-commit-id", "--name-only", "-r", commit).splitlines()
        ) == set(changed_paths)
        assert git(remote, "show", f"{commit}:README.md") == "Selected edit"
        assert git(remote, "show", f"{commit}:-new.md") == "Selected addition"
        assert git(remote, "show", f"{commit}:obsolete.txt") == "Old fixture"
        assert git(checkout, "status", "--porcelain") == "M obsolete.txt\n?? scratch.txt"

        (checkout / "obsolete.txt").unlink()
        deletion = h.dispatch(
            "git.publish", repository="project", paths=["obsolete.txt"], message="Delete"
        )
        assert deletion.status == "succeeded", deletion.failure
        assert git(remote, "ls-tree", "--name-only", "main") == "-new.md\nREADME.md"


def test_rejected_push_retains_commit_and_refuses_republication(
    tmp_path: Path, project: tuple[Path, Path]
) -> None:
    checkout, remote = project
    old = git(remote, "rev-parse", "main")
    hook = remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'https://secret-token@secret.invalid' >&2\nexit 1\n")
    hook.chmod(0o755)
    with opened(tmp_path, git_repositories=aliases(project=checkout)) as h:
        (checkout / "README.md").write_text("Attempted change\n")
        args = {"repository": "project", "paths": ["README.md"], "message": "Publish"}
        failed = h.dispatch("git.publish", **args)
        commit = git(checkout, "rev-parse", "HEAD")
        assert failed.status == "failed" and failed.effect == "confirmed" and failed.failure
        assert f"local commit {commit}" in failed.failure.message
        assert "secret" not in failed.failure.message and str(remote) not in failed.failure.message
        assert commit != old and git(remote, "rev-parse", "main") == old
        hook.unlink()
        (checkout / "README.md").write_text("Second attempted change\n")
        retry = h.dispatch("git.publish", **args)
        assert retry.failure and "freshly fetched" in retry.failure.message
        assert git(checkout, "rev-parse", "HEAD") == commit
        assert git(remote, "rev-parse", "main") == old


def test_competing_push_leaves_remote_at_peer_commit(
    tmp_path: Path, project: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout, remote = project
    peer = tmp_path / "peer"
    git(tmp_path, "clone", "--no-hardlinks", str(remote), str(peer))
    configure_author(peer)
    git(peer, "commit", "--allow-empty", "-m", "Competing peer commit")
    peer_commit = git(peer, "rev-parse", "HEAD")
    checked = GitCheckout.checked

    async def race(
        self: GitCheckout, *argv: str, phase: str, effect: Invocation | None = None
    ) -> bytes:
        if argv[0] == "push":
            git(peer, "push")
        return await checked(self, *argv, phase=phase, effect=effect)

    monkeypatch.setattr(GitCheckout, "checked", race)
    with opened(tmp_path, git_repositories=aliases(project=checkout)) as h:
        (checkout / "README.md").write_text("Local fixture update\n")
        failed = h.dispatch(
            "git.publish", repository="project", paths=["README.md"], message="Local update"
        )
        local = git(checkout, "rev-parse", "HEAD")
        assert failed.status == "failed" and failed.failure
        assert f"local commit {local}" in failed.failure.message
        assert git(remote, "rev-parse", "main") == peer_commit != local
        assert git(remote, "show", f"{peer_commit}:README.md") == "Original fixture"


def test_checkout_cannot_contain_current_prompt(tmp_path: Path, project: tuple[Path, Path]) -> None:
    checkout, _ = project
    with opened(tmp_path, git_repositories=aliases(project=checkout)) as h:
        current = changed(h.profile, soul_file=checkout / "README.md")
        h.dispatcher.current = lambda admitted: current
        denied = h.dispatch("git.diff", repository="project")
        assert denied.status == "denied" and denied.failure
        assert "protected" in denied.failure.message


@pytest.mark.parametrize(
    "path",
    [
        ".git/config",
        "./README.md",
        "../README.md",
        "*.md",
        "alias",
        "hardlink",
        "alias-dir/readme",
        "directory",
        "/README.md",
        "missing",
    ],
)
def test_rejects_unsafe_supplied_paths(
    tmp_path: Path, project: tuple[Path, Path], path: str
) -> None:
    checkout, _ = project
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / "readme"
    outside_file.write_text("external fixture")
    (checkout / "alias").symlink_to(outside_file)
    (checkout / "alias-dir").symlink_to(outside, target_is_directory=True)
    (checkout / "directory").mkdir()
    os.link(outside_file, checkout / "hardlink")
    with opened(tmp_path, git_repositories=aliases(project=checkout)) as h:
        for tool, extra in (
            ("git.diff", {"path": path}),
            ("git.publish", {"paths": [path], "message": "Unsafe"}),
        ):
            denied = h.dispatch(tool, repository="project", **extra)
            assert denied.status == "denied" and denied.effect == "none"


def test_rejects_aliased_metadata_nested_and_linked_checkouts(
    tmp_path: Path, project: tuple[Path, Path]
) -> None:
    checkout, _ = project
    nested = checkout / "nested"
    linked = checkout.parent / "linked"
    git(checkout, "worktree", "add", "-b", "linked-branch", str(linked))
    git(checkout, "init", str(nested))
    with opened(
        tmp_path, git_repositories=aliases(project=checkout, nested=nested, linked=linked)
    ) as h:
        for name in ("project", "nested", "linked"):
            denied = h.dispatch("git.status", repository=name)
            assert denied.status == "denied"
        (nested / ".git").rename(tmp_path / "nested.git")
        metadata = checkout / ".git"
        original = metadata / "config"
        copy = tmp_path / "aliased-config"
        original.rename(copy)
        original.symlink_to(copy)
        assert h.dispatch("git.status", repository="project").status == "denied"


def test_read_only_origins_unconfigured_and_frozen_authority(
    tmp_path: Path, project: tuple[Path, Path]
) -> None:
    checkout, _ = project
    configured = OperatorConfig.model_validate(
        {
            "model": {"pin": "fixture"},
            "data_dir": str(tmp_path / "data"),
            "git": {"repositories": aliases(project=checkout)},
            "defaults": {"workspace_roots": [str(checkout)], "tools": ["git.*"]},
        }
    )
    interactive = resolve_profile(configured)
    assert interactive.tools == {"git.status", "git.diff", "git.pull", "git.publish"}
    scheduled = resolve_profile(configured, origin=Origin.SCHEDULED)
    assert scheduled.tools == {"git.status", "git.diff"}
    old_values = interactive.model_dump(exclude={"git_repositories", "git_reserved"})
    older_admission = EffectiveProfile.model_validate(old_values)
    assert not restrict_to_admission(interactive, older_admission).tools
    with opened(tmp_path) as h:
        assert not any(name.startswith("git.") for name in h.session.eligible())
        assert h.dispatch("git.status", repository="project").status == "denied"


def test_reserved_checkout_can_be_read_but_not_mutated(
    tmp_path: Path, project: tuple[Path, Path]
) -> None:
    checkout, _ = project
    case_alias = checkout.with_name(checkout.name.upper())
    reservation = case_alias if case_alias.is_dir() else checkout
    with opened(
        tmp_path, git_repositories=aliases(project=checkout), letters_repository=reservation
    ) as h:
        assert result(h.dispatch("git.status", repository="project"))["branch"] == "main"
        assert "git.publish" not in h.session.eligible()
        assert h.dispatch("git.pull", repository="project").status == "denied"
        denied = h.dispatch(
            "git.publish", repository="project", paths=["README.md"], message="Edit"
        )
        assert denied.status == "denied"


def test_revocation_after_waiting_for_blog_checkout_lock(
    tmp_path: Path, project: tuple[Path, Path]
) -> None:
    checkout, remote = project
    old = git(remote, "rev-parse", "main")
    (checkout / "README.md").write_text("Must not reach remote\n")
    with opened(
        tmp_path, git_repositories=aliases(project=checkout), blog_repository=checkout
    ) as h:
        current = h.profile
        h.dispatcher.current = lambda admitted: current

        async def wait_then_revoke() -> ToolReceipt:
            assert h.profile.blog_repository == checkout
            lock = h.dispatcher.checkout_lock(h.profile.blog_repository)
            async with lock:
                task = asyncio.create_task(
                    h.session.dispatch(
                        h.call(
                            "git.publish", repository="project", paths=["README.md"], message="Edit"
                        )
                    )
                )
                await asyncio.sleep(0)
                assert not task.done()
                nonlocal current
                current = changed(current, granted_tools=current.granted_tools - {"git.publish"})
            return await task

        denied = asyncio.run(wait_then_revoke())
        assert denied.status == "denied" and denied.effect == "none"
        assert git(remote, "rev-parse", "main") == old
