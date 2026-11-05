import asyncio
import json

import pytest
from test_execution import completion, model_list, setup

from solakit.channels import Channels
from solakit.channels.base import Inbound
from solakit.channels.discord import DiscordAdapter, chunks
from solakit.core.config import Channel, Delivery, SecretRef
from solakit.core.schemas import RunStatus
from solakit.core.startup import ConfigurationError, prepare


class FakeAdapter:
    type = "discord"

    def __init__(self):
        self.sent = []
        self.started = False

    async def start(self, emit):
        self.emit, self.started = emit, True

    async def stop(self):
        self.started = False

    def status(self):
        return {"status": "connected" if self.started else "disabled"}

    async def deliver(self, channel, message):
        self.sent.append((channel.id, message))
        await asyncio.sleep(0)
        return f"external-{len(self.sent)}"


def configured(config):
    config.channels = [
        Channel(
            id=n,
            type="discord",
            channel_id=n,
            session=n,
            enabled=True,
            trigger_authors=["alan"],
            trigger="mention",
        )
        for n in ("one", "two")
    ]


def test_gating_parent_pinning_context_and_stored_delivery(config):
    configured(config)
    captured = []

    def model(request):
        if request.url.path.endswith("/models"):
            return model_list(config)
        captured.append(json.loads(request.content)["messages"])
        return completion(config, "stored final")

    execution, adapter = setup(config, model), FakeAdapter()
    channels = Channels(config, execution, {"discord": adapter})

    async def exercise():
        await channels.start()
        for options in (
            {"bot": True},
            {"self_message": True},
            {"author_id": "stranger"},
            {"mentioned": False},
            {"channel_id": "unknown"},
        ):
            values = dict(
                channel_type="discord",
                channel_id="one",
                author_id="alan",
                text="ignored",
                external_id="ignored",
                mentioned=True,
            )
            assert await channels.receive(Inbound(**(values | options))) is None
        assert not adapter.sent
        assert execution.store.list_records("sessions")[0]["id"] == "s"
        first = await channels.receive(
            Inbound(
                "discord",
                "thread",
                "alan",
                "first",
                "1",
                parent_id="one",
                mentioned=True,
            )
        )
        assert first.session == "one"
        assert first.delivery_status == "delivered"
        assert adapter.sent[0][1].text == "stored final"
        assert adapter.sent[0][1].target_id == "thread"
        assert len(execution.store.transcript("one")) == 2
        await channels.receive(
            Inbound("discord", "two", "alan", "other room", "2", mentioned=True)
        )
        await channels.receive(
            Inbound("discord", "one", "alan", "follow up", "3", mentioned=True)
        )
        assert [m["content"] for m in captured[-1][1:]] == [
            "first",
            "stored final",
            "follow up",
        ]
        assert captured[-2][1]["content"] == "other room"
        assert len(channels.status()) == 2
        await channels.stop()
        assert not adapter.started
        await execution.close()

    asyncio.run(exercise())


def test_artifact_silent_and_duplicate_delivery(config, tmp_path):
    configured(config)
    config.profiles["interactive"].read_scopes = [f"path:{tmp_path}"]
    path = tmp_path / "post.md"
    path.write_text("# Draft")

    def model(request):
        return (
            model_list(config)
            if request.url.path.endswith("/models")
            else completion(config, "final")
        )

    execution, adapter = setup(config, model), FakeAdapter()
    channels = Channels(config, execution, {"discord": adapter})

    async def exercise():
        run = await execution.submit("s", "task")
        run.artifacts = [str(path)]
        await asyncio.gather(
            channels.deliver(run, Delivery(to="one", mode="artifact")),
            channels.deliver(run, Delivery(to="one", mode="artifact")),
        )
        assert len(adapter.sent) == 1
        assert adapter.sent[0][1].artifact == b"# Draft"
        assert run.delivery["external_id"] == "external-1"
        silent = await execution.submit("s", "silent")
        await channels.deliver(silent, Delivery(to="one", mode="silent"))
        assert silent.delivery_status == "silent"
        assert len(adapter.sent) == 1
        broken = await execution.submit("s", "broken")
        broken.status = RunStatus.tool_error
        await channels.deliver(broken, Delivery(to="one"))
        assert "tool failed" in adapter.sent[-1][1].text
        await execution.close()

    asyncio.run(exercise())


def test_fenced_blocks_never_split_and_oversized_blocks_attach():
    text = "paragraph\n" * 160 + "```python\nprint('ok')\n```" + "\nend" * 700
    parts = chunks(text)
    assert "".join(parts) == text
    assert all(len(p) <= 2000 for p in parts)
    assert all(p.count("```") in (0, 2) for p in parts)
    assert chunks("```\n" + "x" * 2000 + "\n```") is None
    assert chunks("```\n" + "x" * 2000) is None


def test_enabled_discord_requires_token_but_no_rollout_flag(config, monkeypatch):
    configured(config)
    with pytest.raises(ConfigurationError, match="bot-token secret reference"):
        prepare(config)
    monkeypatch.setenv("TEST_DISCORD_TOKEN", "test-bot-token")
    config.discord_token = SecretRef(env="TEST_DISCORD_TOKEN")
    assert prepare(config).discord_token == "test-bot-token"


def test_discord_lifecycle_without_a_gateway(config, monkeypatch):
    class Client:
        def __init__(self, **kwargs):
            self.events = {}
            self.closed = asyncio.Event()
            self.user = None

        def event(self, function):
            self.events[function.__name__] = function

        async def start(self, token, reconnect):
            assert token == "fixture-token" and reconnect
            await self.events["on_ready"]()
            await self.closed.wait()

        async def close(self):
            self.closed.set()

    monkeypatch.setattr("solakit.channels.discord.discord.Client", Client)

    async def exercise():
        adapter = DiscordAdapter(config, "fixture-token")
        await adapter.start(lambda message: None)
        await asyncio.sleep(0)
        assert adapter.status()["status"] == "connected"
        await adapter.client.events["on_disconnect"]()
        assert adapter.status()["status"] == "disconnected"
        await adapter.stop()
        assert adapter.status()["status"] == "disabled"
        assert adapter.task.done()

    asyncio.run(exercise())


def test_discord_delivers_oversized_fence_as_exact_attachment(config):
    from types import SimpleNamespace

    from solakit.channels.base import Outbound

    sent = []

    class Target:
        async def send(self, *args, **kwargs):
            sent.append((args, kwargs))
            return SimpleNamespace(id=len(sent))

    async def exercise():
        adapter = DiscordAdapter.__new__(DiscordAdapter)
        adapter.client = SimpleNamespace(get_channel=lambda identifier: Target())
        text = "```python\n" + "x" * 2500 + "\n```"
        channel = Channel(id="one", type="discord", channel_id="100", session="s")
        identity = await adapter.deliver(channel, Outbound(text))
        assert identity == "2"
        assert sent[0][0] == ("Reply attached.",)
        attachment = sent[1][1]["file"]
        assert attachment.fp.read() == text.encode()
        assert sent[0][1]["allowed_mentions"].everyone is False
        attachment.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("acknowledged", [False, True])
def test_delivery_intent_recovery_never_replays_remote_send(config, acknowledged):
    from solakit.core.schemas import Origin, Run
    from solakit.core.store import Store

    execution = setup(config, lambda request: None)
    run = Run(
        session="s",
        profile="interactive",
        origin=Origin(),
        status="completed",
        delivery_status="sending",
    )
    execution.store.save_run(run)
    execution.store.log_delivery(run.id, {"stage": "sending", "to": "room"})
    if acknowledged:
        execution.store.log_delivery(
            run.id,
            {
                "stage": "delivered",
                "to": "room",
                "external_id": "known-id",
                "mode": "final",
            },
        )
    restarted = Store(config.paths.data)
    record = restarted.run(run.id)
    assert record["delivery_status"] == (
        "delivered" if acknowledged else "failed_or_uncertain"
    )
    if acknowledged:
        assert record["delivery"]["external_id"] == "known-id"
    asyncio.run(execution.close())
