"""messages.send posts once to granted channels with non-replaying receipts."""

import asyncio
import json

import pytest
from test_channels import FakeAdapter, configured
from test_execution import completion, model_list, setup
from test_tools import context

from solakit.channels import ApiAdapter, Channels
from solakit.core.config import Channel, Config, Profile
from solakit.tools import Registry


def build(config, *, effects=("send",), resources=("channel:two",)):
    configured(config)
    config.enabled_tools = ["messages"]
    config.profiles["interactive"] = Profile(
        tools=["messages.*"], effects=list(effects), resources=list(resources)
    )

    def model(request):
        if request.url.path.endswith("/models"):
            return model_list(config)
        return completion(config, "unused")

    execution, adapter = setup(config, model), FakeAdapter()
    channels = Channels(config, execution, {"discord": adapter})
    registry = Registry(config)
    registry.channel_poster = channels.post
    ctx = context(config)
    ctx.store = execution.store

    async def call(name, **arguments):
        return await registry.dispatch(name, json.dumps(arguments), ctx)

    return call, adapter, execution, channels


def test_post_is_granted_receipted_and_idempotent(config):
    call, adapter, execution, _ = build(config)

    async def exercise():
        listed = await call("messages.channels")
        assert json.loads(listed.content) == [{"id": "two", "type": "discord"}]
        arguments = {"channel": "two", "text": "Hello ☾", "operation_id": "hello-1"}
        first = await call("messages.send", **arguments)
        assert not first.error, first.content
        assert json.loads(first.content)["external_id"] == "external-1"
        channel_id, message = adapter.sent[0]
        assert channel_id == "two" and message.text == "Hello ☾"
        assert message.target_id is None and message.artifact is None
        again = await call("messages.send", **arguments)
        assert not again.error and json.loads(again.content)["replayed"] is True
        assert len(adapter.sent) == 1
        changed = await call("messages.send", **{**arguments, "text": "Different"})
        assert changed.error and "different text" in changed.content
        assert len(adapter.sent) == 1
        # Tool posts never block the run's own final-message delivery.
        assert execution.store.delivery_events("r") == []
        denied = await call(
            "messages.send", channel="one", text="x", operation_id="denied"
        )
        assert denied.error and "Missing resource: channel:one" in denied.content
        assert len(adapter.sent) == 1
        await execution.close()

    asyncio.run(exercise())


def test_post_requires_send_effect(config):
    configured(config)
    config.enabled_tools = ["messages"]
    config.profiles["interactive"] = Profile(
        tools=["messages.send"], resources=["channel:two"]
    )
    with pytest.raises(ValueError, match="requires effect 'send'"):
        Registry(config)


def test_failed_post_is_uncertain_and_never_replayed(config):
    call, adapter, execution, _ = build(config)
    failures = []

    async def broken(channel, message):
        failures.append(message.text)
        raise ConnectionError("gateway dropped mid-send")

    adapter.deliver = broken

    async def exercise():
        arguments = {"channel": "two", "text": "Once", "operation_id": "once"}
        first = await call("messages.send", **arguments)
        assert first.error and "uncertain" in first.content
        second = await call("messages.send", **arguments)
        assert second.error and "pending or uncertain" in second.content
        assert failures == ["Once"]
        receipts = list((execution.store.root / "channel-posts").glob("*.jsonl"))
        stages = [
            json.loads(line)["stage"] for line in receipts[0].read_text().splitlines()
        ]
        assert stages == ["sending", "uncertain"]
        await execution.close()

    asyncio.run(exercise())


def test_disabled_api_and_unknown_channels_are_refused(config):
    call, adapter, execution, channels = build(
        config, resources=("channel:two", "channel:one")
    )
    config.channels[0].enabled = False
    config.channels.append(
        Channel(id="web", type="api", channel_id="web", session="web", enabled=True)
    )
    channels.adapters["api"] = ApiAdapter()
    config.profiles["interactive"].resources.append("channel:web")

    async def exercise():
        result = await call("messages.send", channel="one", text="x", operation_id="a")
        assert result.error and "Missing resource" in result.content
        result = await call("messages.send", channel="web", text="x", operation_id="b")
        assert result.error and "no outbound" in result.content
        assert not adapter.sent
        await execution.close()

    asyncio.run(exercise())


def test_unknown_channel_grant_fails_configuration():
    with pytest.raises(ValueError, match="unknown channel grant"):
        Config(profiles={"interactive": Profile(resources=["channel:missing"])})
