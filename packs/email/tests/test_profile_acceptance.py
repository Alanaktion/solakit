"""SPEC §14.4: loaded shell/mail tools cannot escape a blog profile."""

import asyncio
import json

import httpx
import pytest

from solakit.core.config import Profile
from solakit.core.loop import Execution
from solakit.core.model import ModelClient


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("shell.exec", {"argv": ["touch", "forbidden"], "cwd": "/tmp"}),
        ("shell.run", {"command": "touch forbidden", "cwd": "/tmp"}),
        ("email.search", {"mailbox": "personal"}),
        ("email.read", {"mailbox": "personal", "message_id": "123:1"}),
        (
            "email.reply",
            {
                "mailbox": "personal",
                "message_id": "123:1",
                "body": "Forbidden reply",
                "operation_id": "forbidden",
            },
        ),
    ],
)
def test_blog_profile_refuses_loaded_tools_and_preserves_evidence(
    pack_context, tmp_path, name, arguments
):
    names = ["shell.exec", "shell.run", "email.search", "email.read", "email.reply"]
    registry, ctx = pack_context(
        ["shell", "runtime", "sola_packs.email"],
        ["runtime.status"],
        profile="blog",
    )
    config = ctx.config
    # These tools are genuinely loaded and grantable in another configured
    # profile, rather than unknown names that happen to fail dispatch.
    config.profiles["operator"] = Profile(
        tools=names,
        write_scopes=[f"path:{tmp_path}"],
        capabilities=[
            "shell.exec",
            "email.reply",
            "mailbox:personal:read",
            "mailbox:personal:reply",
        ],
    )
    registry.validate()
    assert all(registry.allowed(tool, "operator") for tool in names)
    assert all(not registry.allowed(tool, "blog") for tool in names)
    # Fail if dispatch reaches any forbidden implementation, regardless of its
    # argument validation, mailbox configuration or availability of host tools.
    invoked = []

    async def forbidden(**kwargs):
        invoked.append(kwargs)
        raise AssertionError("Forbidden implementation executed")

    for tool in names:
        registry.tools[tool].function = forbidden
    config.paths.soul = tmp_path / "SOUL.md"
    config.paths.soul.write_text("Profile acceptance fixture.")
    config.paths.fragments = tmp_path / "fragments"
    (config.paths.fragments / "profiles").mkdir(parents=True)
    (config.paths.fragments / "profiles/blog.md").write_text("")
    captured = []
    raw_arguments = json.dumps(arguments)

    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": config.model.model_id}]})
        captured.append(json.loads(request.content))
        message = {"role": "assistant", "content": "I cannot use that tool."}
        if len(captured) == 1:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "forbidden-call",
                        "type": "function",
                        "function": {"name": name, "arguments": raw_arguments},
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
            run = await execution.submit("test", "Attempt the forbidden operation.")
            assert run.status == "completed"
            assert len(captured) == 2
            for request in captured:
                assert {t["function"]["name"] for t in request["tools"]} == {
                    "runtime.status"
                }
            history = ctx.store.transcript("test")
            assert [m["role"] for m in history] == [
                "user",
                "assistant",
                "tool",
                "assistant",
            ]
            assert history[1]["tool_calls"][0]["function"] == {
                "name": name,
                "arguments": raw_arguments,
            }
            result = json.loads(history[2]["content"])
            assert history[2]["tool_call_id"] == "forbidden-call"
            assert result["error"] and "not permitted" in result["content"]
            evidence = ctx.store.run(run.id)["tools"]
            assert len(evidence) == 1
            assert evidence[0]["name"] == name
            assert evidence[0]["args"] == raw_arguments
            assert evidence[0]["result"]["error"]
            assert not invoked
        finally:
            await execution.close()

    asyncio.run(exercise())
