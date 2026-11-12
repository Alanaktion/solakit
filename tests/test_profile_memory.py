import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError
from test_memory import memory_config

from solakit.core.config import Config, ProfileMemory
from solakit.core.memory import MemoryClient, MemoryUnavailable
from solakit.core.schemas import Session

OVERRIDE = ProfileMemory(user="private", roots=["viking://user/private/memories"])


def recording(seen, *, hits=()):
    def handle(request):
        seen.append(
            (
                request.url.path,
                request.headers.get("X-OpenViking-User"),
                json.loads(request.content) if request.content else None,
            )
        )
        result = {"memories": list(hits)} if request.url.path.endswith("find") else {}
        return httpx.Response(200, json={"status": "ok", "result": result})

    return handle


def client(seen, **kwargs):
    config = memory_config()
    config.user = "sola"
    return MemoryClient(
        config,
        transport=httpx.MockTransport(recording(seen, **kwargs)),
        overrides={"private": OVERRIDE},
    )


def test_override_requires_enabled_memory_and_canonical_roots():
    with pytest.raises(ValidationError, match="overrides memory"):
        Config.model_validate({"profiles": {"p": {"memory": OVERRIDE.model_dump()}}})
    with pytest.raises(ValidationError, match="canonical"):
        ProfileMemory(user="x", roots=["viking://user/../x"])
    with pytest.raises(ValidationError):
        ProfileMemory(user="x", roots=[])


def test_search_uses_override_user_and_roots_only_for_that_profile():
    seen = []
    memory = client(seen)
    session = Session(id="s")

    async def exercise():
        await memory.scoped(session, "private").search("q")
        await memory.scoped(session, "other").search("q")
        await memory.close()

    asyncio.run(exercise())
    (_, user_a, body_a), (_, user_b, body_b) = seen
    assert (user_a, body_a["target_uri"]) == ("private", OVERRIDE.roots)
    assert user_b == "sola"
    assert body_b["target_uri"] == memory.config.roots


def test_override_rejects_uris_outside_its_roots():
    seen = []
    memory = client(seen)
    scoped = memory.scoped(Session(id="s"), "private")
    default_root = memory.config.roots[0]
    with pytest.raises(MemoryUnavailable, match="outside configured roots"):
        scoped.check_uri(default_root + "/x")
    scoped.check_uri(OVERRIDE.roots[0] + "/x")
    # The default scope cannot reach the override's roots either.
    with pytest.raises(MemoryUnavailable, match="outside configured roots"):
        memory.scoped(Session(id="s"), "other").check_uri(OVERRIDE.roots[0] + "/x")


def test_foreign_hit_in_override_search_fails_closed():
    seen = []
    memory = client(
        seen, hits=[{"uri": memory_config().roots[0] + "/leak", "abstract": "x"}]
    )

    async def exercise():
        with pytest.raises(MemoryUnavailable):
            await memory.scoped(Session(id="s"), "private").search("q")
        await memory.close()

    asyncio.run(exercise())


def test_preflight_checks_override_roots_with_override_user():
    seen = []
    memory = client(seen)
    asyncio.run(memory.preflight())
    assert {user for _, user, _ in seen} == {"sola", "private"}
