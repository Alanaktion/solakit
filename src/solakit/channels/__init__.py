"""Configured channels share the existing queue and stored-message delivery."""

import asyncio
import os
import stat
import weakref
from dataclasses import replace

from solakit.channels.base import Adapter, Inbound, Outbound, route
from solakit.core.config import Config, Delivery
from solakit.core.io import durable
from solakit.core.loop import Execution
from solakit.core.media import Media
from solakit.core.routing import SessionRouter
from solakit.core.schemas import Origin, Run, RunStatus
from solakit.tools import ToolContext


class ApiAdapter:
    type = "api"

    async def start(self, emit):
        self.emit = emit

    async def stop(self):
        pass

    def status(self):
        return {"status": "ready"}

    async def deliver(self, channel, message):
        # The API consumer reads the same stored run; never append a second copy.
        return message.message_id


def artifact_bytes(path, maximum=8_000_000):
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("Artifact must be a regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            payload = stream.read(maximum + 1)
        if len(payload) > maximum:
            raise ValueError("Artifact exceeds delivery limit")
        return payload
    finally:
        os.close(descriptor)


class Channels:
    def __init__(
        self, config: Config, execution: Execution, adapters: dict[str, Adapter]
    ):
        self.config, self.execution, self.adapters = config, execution, adapters
        self.router = SessionRouter(config, execution.store)
        self.last_message: dict[str, str] = {}
        # Per-run locks disappear once no delivery holds or awaits them.
        self.delivery_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )
        self.media = Media(execution.store.root)
        self.media_slots = asyncio.Semaphore(2)

    async def start(self):
        started = []
        try:
            for adapter in self.adapters.values():
                await adapter.start(self.receive)
                started.append(adapter)
        except BaseException:
            await asyncio.gather(*(a.stop() for a in started), return_exceptions=True)
            raise

    async def stop(self):
        await asyncio.gather(*(a.stop() for a in self.adapters.values()))

    def status(self):
        return [
            {
                "id": channel.id,
                "type": channel.type,
                "enabled": channel.enabled,
                **(
                    self.adapters[channel.type].status()
                    if channel.enabled and channel.type in self.adapters
                    else {"status": "disabled"}
                ),
                "last_message": self.last_message.get(channel.id),
            }
            for channel in self.config.channels
        ]

    async def receive(self, message: Inbound):
        channel = route(self.config, message)
        if channel is None:
            return None
        images = []
        if message.attachments:
            async with self.media_slots:
                images = await self.media.ingest(message.attachments)
        session = await durable(self.router.channel, channel.type, channel.channel_id)
        self.last_message[channel.id] = message.external_id
        run = await self.execution.submit(
            session.id,
            message.text,
            images=images,
            profile=channel.profile,
            origin=Origin(
                channel=channel.type,
                channel_id=message.channel_id,
                author_id=message.author_id,
                external_id=message.external_id,
            ),
        )
        await self.deliver(run, Delivery(to=channel.id))
        return run

    async def deliver(self, run: Run, delivery: Delivery):
        lock = self.delivery_locks.setdefault(run.id, asyncio.Lock())
        async with lock:
            await self._deliver(run, delivery)

    async def _deliver(self, run: Run, delivery: Delivery):
        if delivery.mode == "silent":
            run.delivery_status = "silent"
            await durable(
                self.execution.store.update_run,
                run.id,
                {"delivery_status": run.delivery_status},
            )
            return
        if run.status not in {RunStatus.completed, RunStatus.tool_error}:
            return
        store = self.execution.store
        # An intent without acknowledgment is not automatically replayed.
        if await durable(store.delivery_events, run.id):
            return
        try:
            channel = next(
                c for c in self.config.channels if c.id == delivery.to and c.enabled
            )
            adapter = self.adapters[channel.type]
            session = await durable(store.session, run.session)
            if run.status == RunStatus.tool_error:
                outbound = Outbound("I couldn't complete this because a tool failed.")
            else:
                history = await durable(store.all_messages, run.session)
                finals = [
                    m
                    for m in history
                    if m["run_id"] == run.id
                    and m["role"] == "assistant"
                    and not m.get("tool_calls")
                    and m.get("kind", "message") == "message"
                ]
                if not finals:
                    raise ValueError("No stored final message to deliver")
                outbound = Outbound(finals[-1].get("content") or "")
                if outbound.text.strip() == "[SILENT]":
                    run.delivery_status = "silent"
                    await durable(
                        store.update_run, run.id, {"delivery_status": "silent"}
                    )
                    return
                if delivery.mode == "artifact":
                    if not run.artifacts:
                        raise ValueError("Run produced no artifact")
                    session = await durable(store.session, run.session)
                    ctx = ToolContext(session, run.profile, self.config, store, run.id)
                    path = ctx.path(run.artifacts[0])
                    payload = await durable(artifact_bytes, path)
                    outbound = Outbound(outbound.text, payload, path.name)
            target_id = (
                run.origin.channel_id
                if run.origin.channel == channel.type
                and not run.origin.job
                and session.bound_channel == channel.id
                else channel.channel_id
            )
            outbound = replace(outbound, target_id=target_id, message_id=run.id)
            await durable(
                store.log_delivery,
                run.id,
                {"stage": "sending", "to": channel.id, "mode": delivery.mode},
            )
            run.delivery_status = "sending"
            await durable(store.update_run, run.id, {"delivery_status": "sending"})
            identity = await adapter.deliver(channel, outbound)
            run.delivery_status = "delivered"
            run.delivery = {
                "to": channel.id,
                "external_id": identity,
                "mode": delivery.mode,
            }
            await durable(
                store.log_delivery, run.id, {"stage": "delivered", **run.delivery}
            )
        except Exception as exc:
            run.delivery_status = "failed_or_uncertain"
            await durable(
                store.log_delivery,
                run.id,
                {
                    "stage": "failed_or_uncertain",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
            )
        await durable(
            store.update_run,
            run.id,
            {"delivery_status": run.delivery_status, "delivery": run.delivery},
        )
