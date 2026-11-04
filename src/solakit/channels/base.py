"""Platform-neutral inbound, outbound and adapter seam."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from solakit.core.config import Channel, Config


@dataclass(frozen=True)
class Inbound:
    channel_type: str
    channel_id: str
    author_id: str
    text: str
    external_id: str
    parent_id: str | None = None
    guild_id: str | None = None
    bot: bool = False
    self_message: bool = False
    mentioned: bool = False


@dataclass(frozen=True)
class Outbound:
    text: str
    artifact: bytes | None = None
    filename: str | None = None
    target_id: str | None = None
    message_id: str | None = None


Emit = Callable[[Inbound], Awaitable[object]]


class Adapter(Protocol):
    type: str

    async def start(self, emit: Emit) -> None: ...
    async def stop(self) -> None: ...
    async def deliver(self, channel: Channel, message: Outbound) -> str: ...
    def status(self) -> dict: ...


def route(config: Config, message: Inbound) -> Channel | None:
    if message.bot or message.self_message or not message.text.strip():
        return None
    identity = message.parent_id or message.channel_id
    for channel in config.channels:
        if (
            channel.enabled
            and channel.type == message.channel_type
            and channel.channel_id == identity
            and (not channel.guild_id or channel.guild_id == message.guild_id)
            and message.author_id in channel.trigger_authors
            and (channel.trigger == "all" or message.mentioned)
        ):
            return channel
    return None
