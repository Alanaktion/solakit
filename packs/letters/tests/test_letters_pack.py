import asyncio
import json

import pytest
from sola_packs import letters
from sola_packs.support.evidence import Lease, Operation


@pytest.fixture
def fixture(pack_context, git_repo, tmp_path, monkeypatch):
    git, root, remote = git_repo
    settings = tmp_path / "packs.toml"
    settings.write_text('[letters.correspondence]\nrepository = "letters"\n')
    monkeypatch.setenv("SOLA_PACKS_CONFIG", str(settings))
    registry, ctx = pack_context(
        ["sola_packs.letters"],
        ["letters.pull_new", "letters.read", "letters.reply"],
        profile="letters",
        write=["repo:letters"],
        repositories={"letters": root},
        capabilities=[
            "git.push",
            "letters.reply",
            "letters:correspondence:read",
            "letters:correspondence:reply",
        ],
    )
    return git, root, remote, registry, ctx


def test_peer_content_numbering_and_idempotent_immutable_reply(fixture):
    git, root, _, registry, ctx = fixture
    source = (root / "001-2026-11-01-dingus.md").read_bytes()

    async def exercise():
        pulled = await registry.dispatch(
            "letters.pull_new", '{"resource_name":"correspondence"}', ctx
        )
        assert not pulled.error, pulled.content
        assert '<external-content source="letters:correspondence"' in pulled.content
        assert "\\u003c/external-content\\u003e" in pulled.content
        request = json.dumps(
            {
                "resource_name": "correspondence",
                "number": 1,
                "body": "An original reply.",
            }
        )
        first = await registry.dispatch("letters.reply", request, ctx)
        assert not first.error, first.content
        result = json.loads(first.content)
        assert result["number"] == 2
        target = root / result["filename"]
        assert target.name.startswith("002-") and target.name.endswith("-sola.md")
        reply_bytes = target.read_bytes()
        head = git(root, "rev-parse", "HEAD")
        repeated = await registry.dispatch("letters.reply", request, ctx)
        assert not repeated.error
        assert json.loads(repeated.content) == result
        assert git(root, "rev-parse", "HEAD") == head
        assert target.read_bytes() == reply_bytes
        assert (root / "001-2026-11-01-dingus.md").read_bytes() == source
        changed = await registry.dispatch(
            "letters.reply",
            json.dumps(
                {
                    "resource_name": "correspondence",
                    "number": 1,
                    "body": "Changed body",
                }
            ),
            ctx,
        )
        assert changed.error and "different request" in changed.content
        assert len(list(root.glob("*-sola.md"))) == 1
        assert (
            await registry.dispatch(
                "letters.read", '{"resource_name":"correspondence","number":2}', ctx
            )
        ).error is False

    asyncio.run(exercise())


def test_failed_push_preserves_letter_and_never_allocates_a_second_reply(fixture):
    git, root, _, registry, ctx = fixture
    git(root, "remote", "set-url", "--push", "origin", str(root / "missing.git"))
    request = (
        '{"resource_name":"correspondence","number":1,"body":"Keep this immutable."}'
    )

    async def exercise():
        result = await registry.dispatch("letters.reply", request, ctx)
        assert result.error and "uncertain" in result.content
        files = list(root.glob("*-sola.md"))
        assert len(files) == 1
        data = files[0].read_bytes()
        head = git(root, "rev-parse", "HEAD")
        assert head in result.content
        again = await registry.dispatch("letters.reply", request, ctx)
        assert again.error and "already started" in again.content
        assert files[0].read_bytes() == data
        assert len(list(root.glob("*-sola.md"))) == 1
        assert git(root, "rev-parse", "HEAD") == head

    asyncio.run(exercise())


def test_duplicate_numbers_and_cross_resource_denials(fixture):
    git, root, _, registry, ctx = fixture
    (root / "001-2026-11-02-sola.md").write_text("Conflicting sequence")
    git(root, "add", ".")
    git(root, "commit", "-m", "duplicate number")

    async def exercise():
        result = await registry.dispatch(
            "letters.read", '{"resource_name":"correspondence","number":1}', ctx
        )
        assert result.error and "Duplicate letter number" in result.content
        result = await registry.dispatch(
            "letters.read", '{"resource_name":"other","number":1}', ctx
        )
        assert result.error and "Missing capability" in result.content
        ctx.config.profiles["letters"].capabilities.remove("git.push")
        result = await registry.dispatch(
            "letters.reply",
            '{"resource_name":"correspondence","number":1,"body":"Denied"}',
            ctx,
        )
        assert result.error
        assert not list(root.glob("002-*-sola.md"))

    asyncio.run(exercise())


def test_in_progress_and_torn_receipts_fail_closed(fixture):
    _, root, _, registry, ctx = fixture

    async def exercise():
        with Lease(ctx, "letters-locks", str(root)):
            result = await registry.dispatch(
                "letters.reply",
                '{"resource_name":"correspondence","number":1,"body":"Reply"}',
                ctx,
            )
            assert result.error and "already in progress" in result.content
        cfg = letters.settings(ctx, "correspondence", "reply")[0]
        request = {
            "number": 1,
            "body": "Reply",
            "author": cfg.author,
            "peer": cfg.peer,
            "layout": cfg.layout,
            "repository": str(root),
        }
        with Operation(ctx, "letters", [str(root), 1], request) as operation:
            path = operation.path
        with path.open("ab") as stream:
            stream.write(b'{"torn":')
        result = await registry.dispatch(
            "letters.reply",
            '{"resource_name":"correspondence","number":1,"body":"Reply"}',
            ctx,
        )
        assert result.error and "requires recovery" in result.content
        assert not list(root.glob("*-sola.md"))

    asyncio.run(exercise())


def test_concurrent_replies_serialize_number_allocation(fixture):
    git, root, _, registry, ctx = fixture
    peer = root / "002-2026-11-02-dingus.md"
    peer.write_text("A second peer letter")
    git(root, "add", peer.name)
    git(root, "commit", "-m", "second peer letter")
    git(root, "push")

    async def send(number):
        return await registry.dispatch(
            "letters.reply",
            json.dumps(
                {
                    "resource_name": "correspondence",
                    "number": number,
                    "body": f"Reply to {number}",
                }
            ),
            ctx,
        )

    async def exercise():
        results = await asyncio.gather(send(1), send(2))
        assert sum(not result.error for result in results) == 1
        denied = next(
            number
            for number, result in zip((1, 2), results, strict=True)
            if result.error
        )
        assert not (await send(denied)).error
        names = sorted(path.name for path in root.glob("*-sola.md"))
        assert len(names) == 2
        assert names[0].startswith("003-") and names[1].startswith("004-")

    asyncio.run(exercise())


def test_zero_based_existing_protocol_is_discovered_without_skipping_first_letter(
    fixture,
):
    git, root, _, registry, ctx = fixture
    git(root, "mv", "001-2026-11-01-dingus.md", "000-2026-11-01-dingus.md")
    git(root, "commit", "-m", "zero-based protocol")
    git(root, "push")

    async def exercise():
        result = await registry.dispatch(
            "letters.pull_new", '{"resource_name":"correspondence"}', ctx
        )
        assert not result.error
        data = json.loads(result.content.split("\n", 1)[1].rsplit("\n", 1)[0])
        assert data["letters"][0]["number"] == 0
        result = await registry.dispatch(
            "letters.reply",
            '{"resource_name":"correspondence","number":0,"body":"First reply"}',
            ctx,
        )
        assert not result.error
        assert json.loads(result.content)["number"] == 1
        assert list(root.glob("001-*-sola.md"))

    asyncio.run(exercise())
