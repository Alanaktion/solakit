"""discord.py gateway lifecycle; platform formatting never enters the prompt."""

import asyncio
import io
import re
from urllib.parse import urlsplit

import aiohttp
import discord

from solakit.channels.base import Inbound, Outbound, route
from solakit.core.media import MAX_IMAGE_BYTES, ImageAttachment, MediaError
from solakit.core.schemas import now


def chunks(text: str, limit=2000) -> list[str] | None:
    """Keep fenced blocks intact; oversized blocks use an attached text file."""
    pieces = re.split(r"(```.*?(?:```|\Z))", text, flags=re.DOTALL)
    result, current = [], ""
    for piece in pieces:
        if not piece:
            continue
        if piece.startswith("```"):
            if len(piece) > limit:
                return None
            if len(current) + len(piece) > limit:
                result.append(current)
                current = ""
            current += piece
            continue
        while piece:
            available = limit - len(current)
            if not available:
                result.append(current)
                current = ""
                available = limit
            if len(piece) <= available:
                current += piece
                break
            split = max(piece.rfind("\n", 0, available), piece.rfind(" ", 0, available))
            count = split + 1 if split >= 0 else available
            current += piece[:count]
            piece = piece[count:]
            result.append(current)
            current = ""
    if current:
        result.append(current)
    return result


class DiscordAdapter:
    type = "discord"

    def __init__(self, config, token):
        self.config, self.token = config, token
        self.state = "disabled"
        self.connected_since = None
        self.task = None
        intents = discord.Intents.default()
        intents.message_content = True
        self.client = discord.Client(intents=intents)

    def status(self):
        return {"status": self.state, "connected_since": self.connected_since}

    async def start(self, emit):
        @self.client.event
        async def on_ready():
            self.state = "connected"
            self.connected_since = now().isoformat()

        @self.client.event
        async def on_disconnect():
            self.state = "disconnected"

        @self.client.event
        async def on_message(message):
            user = self.client.user
            inbound = Inbound(
                "discord",
                str(message.channel.id),
                str(message.author.id),
                message.content,
                str(message.id),
                parent_id=str(message.channel.parent_id)
                if isinstance(message.channel, discord.Thread)
                else None,
                guild_id=str(message.guild.id) if message.guild else None,
                bot=message.author.bot,
                self_message=user is not None and message.author.id == user.id,
                mentioned=user is not None and user in message.mentions,
                attachments=tuple(
                    ImageAttachment(
                        attachment.filename,
                        attachment.size,
                        lambda attachment=attachment: self.download(attachment.url),
                    )
                    for attachment in getattr(message, "attachments", [])
                ),
            )
            if route(self.config, inbound) is not None:
                async with message.channel.typing():
                    try:
                        await emit(inbound)
                    except MediaError as exc:
                        await message.channel.send(
                            str(exc), allowed_mentions=discord.AllowedMentions.none()
                        )

        self.state = "connecting"
        self.task = asyncio.create_task(self.client.start(self.token, reconnect=True))
        self.task.add_done_callback(self._ended)

    async def download(self, url):
        try:
            parsed = urlsplit(url)
            if (
                parsed.scheme != "https"
                or parsed.hostname not in {"cdn.discordapp.com", "media.discordapp.net"}
                or parsed.port not in {None, 443}
                or parsed.username is not None
                or parsed.password is not None
                or not parsed.path.startswith(
                    ("/attachments/", "/ephemeral-attachments/")
                )
            ):
                raise MediaError("Invalid Discord attachment location")
            # No request logger, credentials or redirects expose signed URLs.
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
                trust_env=True,
                auto_decompress=False,
            ) as client:
                async with client.get(
                    url, allow_redirects=False, headers={"Accept-Encoding": "identity"}
                ) as response:
                    if (
                        response.status != 200
                        or response.headers.get("Content-Encoding", "identity")
                        != "identity"
                    ):
                        raise MediaError("Image download failed; resend the attachment")
                    data = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        data.extend(chunk)
                        if len(data) > MAX_IMAGE_BYTES:
                            raise MediaError("Image exceeds the 8 MB download limit")
                    return bytes(data)
        except MediaError:
            raise
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise MediaError("Image download failed; resend the attachment") from exc

    def _ended(self, task):
        if not task.cancelled() and task.exception() is not None:
            self.state = "failed"

    async def stop(self):
        await self.client.close()
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.state = "disabled"

    async def deliver(self, channel, message: Outbound):
        destination = message.target_id or channel.channel_id
        target = self.client.get_channel(int(destination))
        if target is None:
            target = await self.client.fetch_channel(int(destination))
        parts = chunks(message.text)
        attachment, filename = message.artifact, message.filename
        if parts is None:
            if attachment is not None:
                raise ValueError(
                    "Reply and artifact both require oversized attachments"
                )
            attachment, filename = message.text.encode(), "reply.txt"
            parts = ["Reply attached."]
        if not parts and attachment is None:
            raise ValueError("Cannot deliver an empty Discord message")
        identity = None
        for part in parts:
            sent = await target.send(
                part, allowed_mentions=discord.AllowedMentions.none()
            )
            identity = str(sent.id)
        if attachment is not None:
            if len(attachment) > 8_000_000:
                raise ValueError("Attachment exceeds delivery limit")
            sent = await target.send(
                file=discord.File(io.BytesIO(attachment), filename=filename),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            identity = str(sent.id)
        return identity
