import asyncio
import json
import subprocess
import sys
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Any

import pytest
from test_git import aliases, result
from test_jobs import Clock, job, occurrences
from test_run_loop import call, harness, reply
from test_tools import Harness, changed, opened

from scripts.letters_work import reconcile, seed, v1_hash
from scripts.probe_git import configure_author, git, repository
from solakit.config import LETTERS_TOOLS
from solakit.evidence import digest
from solakit.files import GuardedFiles
from solakit.git import GitCheckout
from solakit.letters import guarded_checkout
from solakit.records import ErrorCode, LetterPublication, Origin, RunState, ToolReceipt
from solakit.run_loop import records, render_receipt
from solakit.sdk import ToolError

SETTINGS = {"branch": "main", "author": "sola", "peer": "dingus", "directory": "letters"}


@pytest.fixture
def clones(tmp_path: Path) -> tuple[Path, Path, Path]:
    checkout = tmp_path / "workspace" / "correspondence"
    checkout.parent.mkdir()
    remote = repository(tmp_path, tmp_path / "initial")
    peer = tmp_path / "peer"
    for clone in (checkout, peer):
        git(tmp_path, "clone", "--no-hardlinks", str(remote), str(clone))
        configure_author(clone)
    (peer / "letters").mkdir()
    (peer / "letters/README").write_text("Protocol fixture")
    publish_peer(peer, {1: ("dingus", "# Letter 001 — fixture\n\nHello λ\n")})
    git(checkout, "pull", "--ff-only")
    return checkout, peer, remote


def publish_peer(peer: Path, letters: dict[int, tuple[str, str]]) -> None:
    for number, (author, body) in letters.items():
        (peer / "letters" / f"{number:03d}-2026-11-03-{author}.md").write_text(body)
    git(peer, "add", "letters")
    git(peer, "commit", "-m", "Peer update")
    git(peer, "push")


def work(h: Harness) -> LetterPublication:
    row = h.store.db.execute("SELECT id FROM entities WHERE kind='letter_publication'").fetchone()
    return h.store.letter_publication(row[0])


def test_inbox_sequence_and_local_read(tmp_path: Path, clones: tuple[Path, Path, Path]) -> None:
    checkout, peer, _ = clones
    following = {
        number: ("sola" if number % 3 == 0 or number >= 37 else "dingus", f"Body {number}\n")
        for number in range(2, 41)
    }
    publish_peer(peer, following)
    with opened(tmp_path, letters_repository=checkout, letters_settings=SETTINGS) as h:
        head = git(checkout, "rev-parse", "HEAD")
        initial = result(h.dispatch("letters.read", number=1))
        assert initial["complete"] and initial["body"] == "# Letter 001 — fixture\n\nHello λ\n"
        assert h.dispatch("letters.read", number=2).status == "failed"
        assert git(checkout, "rev-parse", "HEAD") == head
        expected = [1] + [number for number, (name, _) in following.items() if name == "dingus"]
        first = result(h.dispatch("letters.inbox"))
        assert first["has_more"] and first["next_after"] == expected[20] - 1
        assert isinstance(first["letters"], list)
        assert [row["number"] for row in first["letters"]] == expected[:20]
        second = result(h.dispatch("letters.inbox", after=first["next_after"]))
        assert not second["has_more"] and second["next_after"] == 40
        assert isinstance(second["letters"], list)
        assert [row["number"] for row in second["letters"]] == expected[20:]
        final = result(h.dispatch("letters.inbox", after=expected[-1]))
        assert final | {"external_content": None} == {
            "letters": [],
            "next_after": 40,
            "has_more": False,
            "external_content": None,
        }
        assert result(h.dispatch("letters.inbox", after=49))["next_after"] == 49
        assert result(h.dispatch("letters.read", number=40))["body"] == "Body 40\n"


def test_byte_budget_redaction_utf8_and_cursor(
    tmp_path: Path, clones: tuple[Path, Path, Path]
) -> None:
    checkout, peer, _ = clones
    secret = "private-" + "x" * 900
    huge = '"λ\\\n' * 25_000
    publish_peer(
        peer,
        {
            2: ("dingus", "a" * 8188 + secret + "\n"),
            3: ("sola", "skip"),
            4: ("dingus", huge),
            5: ("sola", "skip before next peer"),
            6: ("dingus", "(tiny)"),
            7: ("sola", "tail"),
        },
    )
    with opened(
        tmp_path,
        letters_repository=checkout,
        letters_settings=SETTINGS,
        budgets={"tool_result_bytes": 650},
    ) as h:
        h.dispatcher.redactor.register(secret)
        after = 0
        observed = []
        for expected_cursor in (1, 3, 5, 7):
            receipt = h.dispatch("letters.inbox", after=after)
            page = result(receipt)
            assert page["next_after"] == expected_cursor
            after = expected_cursor
            assert page["has_more"] == (after < 7)
            assert len(render_receipt(receipt, 650).encode()) <= 650
            assert isinstance(page["letters"], list)
            row = page["letters"][0]
            observed.append(row["number"])
            if row["number"] in (2, 4):
                assert row["complete"] is False
                assert len(row["body"]) > 0
                if row["number"] == 4:
                    assert huge.startswith(row["body"])
        assert observed == [1, 2, 4, 6]
    with opened(
        tmp_path,
        letters_repository=checkout,
        letters_settings=SETTINGS,
        budgets={"tool_result_bytes": 8_500},
    ) as h:
        h.dispatcher.redactor.register(secret)
        response = result(h.dispatch("letters.read", number=2))
        assert response["complete"] and response["body"] == "a" * 8188 + "<redacted>\n"
    with opened(
        tmp_path,
        letters_repository=checkout,
        letters_settings=SETTINGS,
        budgets={"tool_result_bytes": 120},
    ) as h:
        denied = h.dispatch("letters.inbox", after=3)
        assert denied.failure and denied.failure.code == ErrorCode.BUDGET_EXCEEDED
        assert denied.result is None
    with opened(
        tmp_path,
        letters_repository=checkout,
        letters_settings=SETTINGS,
        budgets={"tool_result_bytes": 512_000},
    ) as h:
        inbox = h.dispatch("letters.inbox", after=3)
        page = result(inbox)
        assert len(render_receipt(inbox, 512_000).encode()) <= 65_536
        assert isinstance(page["letters"], list)
        assert page["letters"][0]["complete"] is False
        full = result(h.dispatch("letters.read", number=4))
        assert full["complete"] and full["body"] == huge


def test_publication_validation_and_restart_idempotence(
    tmp_path: Path, clones: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout, _, remote = clones

    class Boundary:
        @staticmethod
        def now(tz: tzinfo | None = None) -> datetime:
            return datetime(2026, 11, 4, 2, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr("solakit.letters.datetime", Boundary)
    body = "# Letter 002 — from Sola\n\nExact words.\n"
    with opened(tmp_path, letters_repository=checkout, letters_settings=SETTINGS) as h:
        original = git(checkout, "rev-parse", "HEAD")
        for name, args in (
            ("reply", {"body": body}),
            ("reply", {"body": body, "reply_to": None}),
            ("send", {"body": body, "reply_to": 1}),
            ("send", {"body": ""}),
            ("send", {"body": " \t\n"}),
            ("send", {"body": "a" * 64_001}),
            ("send", {"body": "λ" * 40_000}),
            ("read", {"number": -1}),
            ("reply", {"body": body, "reply_to": -1}),
            ("reply", {"body": body, "reply_to": 99}),
            ("inbox", {"after": False}),
            ("inbox", {"limit": 101}),
        ):
            receipt = h.dispatch("letters." + name, **args)
            assert receipt.failure and receipt.failure.code == ErrorCode.INVALID_INPUT
        assert git(checkout, "rev-parse", "HEAD") == git(remote, "rev-parse", "main") == original
        assert not h.store.db.execute(
            "SELECT 1 FROM entities WHERE kind='letter_publication'"
        ).fetchone()
        sent = result(h.dispatch("letters.send", body=body))
        assert sent["path"] == "letters/002-2026-11-03-sola.md"
        assert (checkout / str(sent["path"])).read_bytes() == (body + "\n").encode()
        assert git(checkout, "log", "-1", "--format=%s") == "Send letter 002 as sola"
        assert result(h.dispatch("letters.send", body=body)) == sent
        assert h.dispatch("letters.reply", body="Local target.", reply_to=2).status == "failed"
        answered = result(h.dispatch("letters.reply", body="Answer.", reply_to=1))
        assert answered["reply_to"] == 1 and answered["number"] == 3
        assert git(checkout, "log", "-1", "--format=%s") == "Reply to letter 001 as sola"
        assert result(h.dispatch("letters.reply", body="Answer.", reply_to=1)) == answered
        denied = h.dispatch("letters.reply", body="Changed.", reply_to=1)
        assert denied.failure and "Inspect letter work" in denied.failure.message
    with opened(tmp_path, letters_repository=checkout, letters_settings=SETTINGS) as h:
        assert result(h.dispatch("letters.send", body=body)) == sent
        assert result(h.dispatch("letters.reply", body="Answer.", reply_to=1)) == answered
        assert git(remote, "rev-parse", "main") == answered["commit"]


def test_authority_checkout_refusals(tmp_path: Path, clones: tuple[Path, Path, Path]) -> None:
    checkout, _, _ = clones
    alias = checkout.parent / "alias"
    alias.symlink_to(checkout, target_is_directory=True)
    with opened(
        tmp_path,
        letters_repository=alias,
        letters_settings=SETTINGS,
        git_repositories=aliases(other=checkout),
        blog_repository=alias,
    ) as h:
        assert LETTERS_TOOLS <= h.session.eligible()
        for name, args in (
            ("files.write", {"path": str(checkout / "blocked.txt"), "content": "no"}),
            ("files.write", {"path": str(alias / "blocked.txt"), "content": "no"}),
            ("git.pull", {"repository": "other"}),
            ("git.publish", {"repository": "other", "paths": ["README.md"], "message": "no"}),
            ("blog.publish", {"slug": "example"}),
            (
                "blog.create",
                {"slug": "example", "title": "Example", "description": "No", "body": "No"},
            ),
            ("dreams.create", {"body": "none"}),
        ):
            assert h.dispatch(name, **args).status == "denied"
        assert h.dispatch("git.status", repository="other").status == "succeeded"
        for mode in ("untracked", "staged", "wrong", "detached"):
            local = checkout / "dirty"
            if mode in ("staged", "untracked"):
                local.touch()
                if mode == "staged":
                    git(checkout, "add", "dirty")
            elif mode == "wrong":
                git(checkout, "switch", "-c", "other")
            else:
                git(checkout, "switch", "--detach")
            receipt = h.dispatch("letters.inbox")
            assert receipt.status in ("denied", "failed") and receipt.effect == "none"
            if mode == "untracked":
                local.unlink()
            elif mode == "staged":
                git(checkout, "rm", "-f", "dirty")
            else:
                git(checkout, "switch", "main")
        alternate = h.root / "outside"
        alternate.mkdir()
        h.dispatcher.current = lambda _: changed(h.profile, workspace_roots=(alternate,))
        assert "letters.inbox" not in h.session.eligible()
        assert h.dispatch("letters.inbox").status == "denied"
        h.dispatcher.current = lambda _: h.profile
        h.session.admitted = changed(h.profile, workspace_roots=(alternate,))
        assert h.dispatch("letters.read", number=1).status == "denied"
        h.session.admitted = changed(h.profile, letters=None)
        assert h.dispatch("letters.read", number=1).status == "denied"


@pytest.mark.parametrize(
    "mode", ["duplicate", "symlink", "hardlink", "calendar", "cap", "directory"]
)
def test_invalid_scan(tmp_path: Path, clones: tuple[Path, Path, Path], mode: str) -> None:
    checkout, _, _ = clones
    directory = checkout / "letters"
    original = next(directory.glob("001-*"))
    if mode == "duplicate":
        (directory / "0001-2026-10-01-sola.md").touch()
    elif mode == "calendar":
        (directory / "002-2026-02-30-sola.md").touch()
    elif mode == "symlink":
        (directory / "002-2026-11-04-sola.md").symlink_to(original)
    elif mode == "hardlink":
        (directory / "002-2026-11-04-sola.md").hardlink_to(original)
    elif mode == "cap":
        for number in range(9_999):
            (directory / f"unrelated-{number}").touch()
    with opened(tmp_path, letters_repository=checkout, letters_settings=SETTINGS) as h:
        if mode == "directory":
            directory = directory.rename(checkout / "target")
            (checkout / "letters").symlink_to(directory, target_is_directory=True)
        assert h.dispatch("letters.read", number=1).status in ("failed", "denied")


def operator_checkout(h: Harness) -> GitCheckout:
    assert h.profile.letters
    files = GuardedFiles(lambda _: h.profile, (), lambda _: None, lambda: None)
    repo = guarded_checkout(h.profile.letters, files.authority, files, write=False)
    asyncio.run(repo.validate())
    asyncio.run(repo.verify_branch())
    return repo


@pytest.mark.parametrize("interruption", ["created", "commit", "push_race"])
def test_interrupted_publication(
    tmp_path: Path,
    clones: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    interruption: str,
) -> None:
    checkout, peer, remote = clones
    before = git(checkout, "rev-parse", "HEAD")
    body = "Preserve this exact draft.\n"
    if interruption == "push_race":
        original_command = GitCheckout.command

        async def competing_push(
            repo: GitCheckout, *argv: str, **kwargs: Any
        ) -> tuple[int, bytes, bool]:
            if argv[0] == "push":
                publish_peer(peer, {5: ("dingus", "Concurrent letter\n")})
            return await original_command(repo, *argv, **kwargs)

        with opened(tmp_path, letters_repository=checkout, letters_settings=SETTINGS) as h:
            monkeypatch.setattr(GitCheckout, "command", competing_push)
            receipt = h.dispatch("letters.reply", reply_to=1, body=body)
            assert receipt.failure and "Last verified letter phase" in receipt.failure.message
        monkeypatch.setattr(GitCheckout, "command", original_command)
    else:
        child = subprocess.run(
            [
                sys.executable,
                "-c",
                """
import os
import sys
from pathlib import Path
sys.path.insert(0, "tests")
from test_letters import opened, SETTINGS
root, checkout, interruption, body = sys.argv[1:]
def stop(point):
    if point == f"letter_{interruption}":
        os._exit(27)
with opened(Path(root), letters_repository=Path(checkout), letters_settings=SETTINGS) as h:
    h.store.fault = stop
    h.dispatch("letters.reply", reply_to=1, body=body)
""",
                str(tmp_path),
                str(checkout),
                interruption,
                body,
            ],
            check=False,
        )
        assert child.returncode == 27

    with opened(tmp_path, letters_repository=checkout, letters_settings=SETTINGS) as h:
        pending = work(h)
        assert pending.path
        path = checkout / pending.path
        assert path.read_bytes() == (body + "\n").encode()
        local = git(checkout, "rev-parse", "HEAD")
        assert (local == before) == (interruption == "created")
        assert pending.phase == ("committed" if interruption == "push_race" else "created")
        assert (pending.commit == local) == (interruption == "push_race")
        if interruption != "push_race":
            assert any(
                entry.record.get("status") == "uncertain"
                for _, entry in h.store.log.scan()
                if entry.kind == "receipt"
            )
        assert h.dispatch("letters.reply", reply_to=1, body=body).status == "failed"
        assert work(h) == pending
        assert git(checkout, "rev-parse", "HEAD") == local
        assert path.read_bytes() == (body + "\n").encode()
        if interruption == "created":
            assert not pending.commit and pending.base == before
            path.unlink()
            assert h.dispatch("letters.reply", reply_to=1, body=body).status == "failed"
            assert work(h) == pending and not path.exists()
            assert h.dispatch("letters.send", body="New request").status == "failed"
            return
        repo = operator_checkout(h)
        settings = h.profile.letters
        assert settings
        with pytest.raises(ToolError):
            asyncio.run(reconcile(h.store, repo, settings, pending.origin_hash, pending.id))
        assert work(h) == pending
        if interruption == "push_race":
            # The operator preserves both histories, including the original letter commit.
            git(checkout, "fetch", "origin", "main")
            git(checkout, "merge", "--no-edit", "FETCH_HEAD")
        git(checkout, "push", "origin", "main")
        path.write_text("changed")
        with pytest.raises(ToolError, match="bytes differ"):
            asyncio.run(reconcile(h.store, repo, settings, pending.origin_hash, pending.id))
        assert work(h) == pending
        path.write_bytes((body + "\n").encode())
        recovered = asyncio.run(reconcile(h.store, repo, settings, pending.origin_hash, pending.id))
        assert recovered.phase == "confirmed" and recovered.commit == local
        assert result(h.dispatch("letters.reply", reply_to=1, body=body))["commit"] == local
        assert git(remote, "merge-base", "--is-ancestor", local, "main") == ""
        phases = [
            entry.record["phase"]
            for _, entry in h.store.log.scan()
            if entry.kind == "letter_publication"
        ]
        assert phases == (
            ["prepared", "created", "committed", "confirmed"]
            if interruption == "push_race"
            else ["prepared", "created", "confirmed"]
        )


def test_seed_history_completed_and_unresolved_receipts(
    tmp_path: Path, clones: tuple[Path, Path, Path]
) -> None:
    checkout, peer, _ = clones
    publish_peer(peer, {7: ("dingus", "Two\n"), 9: ("dingus", "Three\n")})
    git(checkout, "pull", "--ff-only")
    replies = {
        1: ("010-2026-11-04-sola.md", "# Letter 010\n\nExact body λ\n"),
        7: ("011-2026-11-04-sola.md", "History only"),
        9: ("012-2026-11-04-sola.md", "Interrupted"),
    }
    commits = {}
    for target in (1, 7):
        filename, body = replies[target]
        path = checkout / "letters" / filename
        path.write_bytes((body + "\n").encode())
        git(checkout, "add", str(path))
        git(checkout, "commit", "-m", f"Reply to letter {target:03d} as sola")
        commits[target] = git(checkout, "rev-parse", "HEAD")
    git(checkout, "push")
    operations = tmp_path / "pack-operations"
    (operations / "letters").mkdir(parents=True)
    original = tmp_path / "old-checkout-location"
    for target in (1, 9):
        filename, body = replies[target]
        evidence_path = (
            operations / "letters" / (v1_hash([str(original / "letters"), target]) + ".jsonl")
        )
        request = v1_hash(
            {
                "number": target,
                "body": body,
                "author": "sola",
                "peer": "dingus",
                "layout": "{body}\n",
                "repository": str(original),
            }
        )
        events: list[dict[str, Any]] = [
            {"stage": "prepared", "filename": filename, "reply_to": target, "request_hash": request}
        ]
        if target in commits:
            events.append(
                {
                    "stage": "published",
                    "request_hash": request,
                    "result": {
                        "number": 10,
                        "reply_to": target,
                        "filename": filename,
                        "publication": "Published commit " + commits[target],
                    },
                }
            )
        evidence_path.write_text("".join(json.dumps(event) + "\n" for event in events))
    with opened(tmp_path, letters_repository=checkout, letters_settings=SETTINGS) as h:
        assert h.profile.letters
        repo = operator_checkout(h)
        origin_hash = digest(asyncio.run(repo.origin_url(publishing=True)))

        def seed_work() -> dict[int | None, LetterPublication]:
            assert h.profile.letters
            return {
                row.reply_to: row
                for row in asyncio.run(
                    seed(h.store, repo, h.profile.letters, origin_hash, operations, original)
                )
            }

        head = git(checkout, "rev-parse", "HEAD")
        saved = seed_work()
        assert saved[1].phase == saved[7].phase == "confirmed"
        assert saved[1].request_hash and not saved[7].request_hash
        assert saved[9].phase == "prepared"
        assert seed_work() == saved
        assert (
            result(h.dispatch("letters.reply", reply_to=1, body=replies[1][1]))["commit"]
            == commits[1]
        )
        for target in (7, 9):
            assert (
                h.dispatch("letters.reply", reply_to=target, body=replies[target][1]).status
                == "failed"
            )
        assert git(checkout, "rev-parse", "HEAD") == head
        assert git(checkout, "status", "--porcelain") == ""
        filename, body = replies[9]
        path = checkout / "letters" / filename
        path.write_bytes((body + "\n").encode())
        git(checkout, "add", str(path))
        git(checkout, "commit", "-m", "Reply to letter 009 as sola")
        git(checkout, "push")
        assert seed_work()[9].phase == "confirmed"
        assert result(h.dispatch("letters.reply", reply_to=9, body=body))["number"] == 12


def test_scheduled_job_discovers_and_reads(tmp_path: Path, clones: tuple[Path, Path, Path]) -> None:
    checkout, peer, _ = clones
    publish_peer(peer, {8: ("dingus", "New scheduled discovery\n")})
    with opened(
        tmp_path,
        letters_repository=checkout,
        letters_settings=SETTINGS,
        grants=sorted(LETTERS_TOOLS),
    ) as h:
        profile = changed(h.profile, data_dir=tmp_path / "jobs", model_pin="fixture-qwen")

    async def scenario() -> None:
        clock = Clock()
        app, requests = harness(
            profile,
            [
                reply("", calls=[call("letters.inbox", '{"after":1}')]),
                reply("", calls=[call("letters.read", '{"number":8}')]),
                reply("Found number eight."),
                reply("", calls=[call("letters.inbox")]),
                reply("NO_ACTION"),
                reply("", calls=[call("letters.reply", '{"reply_to":8,"body":"denied"}')]),
                reply("NO_ACTION"),
            ],
        )
        assert app.jobs and app.runs and app.store
        app.jobs.clock = clock
        await app.start()
        try:
            queued = job(
                app,
                clock,
                origin=Origin.SCHEDULED,
                enabled=True,
                workspace_roots=(str(checkout),),
                tools=("letters.inbox", "letters.read"),
            )
            await app.jobs.tick()
            (occurrence,) = occurrences(app)
            assert occurrence.job_id == queued.id and occurrence.run_id
            settled = await app.runs.wait(occurrence.run_id, timeout=5)
            assert settled.state == RunState.COMPLETED, settled.reason
            receipts = [
                r for r in records(app.store, occurrence.run_id) if isinstance(r, ToolReceipt)
            ]
            assert [(r.status, r.effect) for r in receipts] == [
                ("succeeded", "confirmed"),
                ("succeeded", "none"),
            ]
            assert result(receipts[1])["body"] == "New scheduled discovery\n"
            assert {t["function"]["name"] for t in requests[0]["tools"]} == {
                "letters.inbox",
                "letters.read",
            }
            with pytest.raises(ValueError, match="grants"):
                job(
                    app,
                    clock,
                    origin=Origin.SCHEDULED,
                    workspace_roots=(str(checkout),),
                    tools=("letters.send",),
                )
            no_roots = job(
                app, clock, origin=Origin.SCHEDULED, enabled=True, tools=("letters.inbox",)
            )
            await app.jobs.tick()
            occurrence = next(o for o in occurrences(app) if o.job_id == no_roots.id)
            assert occurrence.run_id
            await app.runs.wait(occurrence.run_id, timeout=5)
            (denied,) = [
                r for r in records(app.store, occurrence.run_id) if isinstance(r, ToolReceipt)
            ]
            assert denied.status == "denied" and denied.effect == "none"
            assert "letters.inbox" not in str(requests[3].get("tools", []))
            guessed = job(
                app,
                clock,
                origin=Origin.SCHEDULED,
                enabled=True,
                workspace_roots=(str(checkout),),
                tools=("letters.inbox", "letters.read"),
            )
            await app.jobs.tick()
            occurrence = next(o for o in occurrences(app) if o.job_id == guessed.id)
            assert occurrence.run_id
            await app.runs.wait(occurrence.run_id, timeout=5)
            (denied,) = [
                r for r in records(app.store, occurrence.run_id) if isinstance(r, ToolReceipt)
            ]
            assert denied.status == "denied" and denied.effect == "none"
            assert len(occurrences(app)) == 3 and len(app.jobs.list()) == 3
        finally:
            await app.stop()

    asyncio.run(scenario())
