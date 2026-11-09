"""Configured channels share the existing queue and stored-message delivery."""

import asyncio
import hashlib
import json
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
    outbound = False  # API consumers read stored runs; nothing is posted.

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

    async def post(self, channel_id: str, text: str, operation_id: str, run_id: str):
        """Post tool-authored text once; uncertain outcomes are never replayed.

        Receipts are keyed by channel and operation ID and bound to the exact text.
        A run's own final-message delivery is unaffected.
        """
        channel = next(
            (c for c in self.config.channels if c.id == channel_id and c.enabled), None
        )
        adapter = self.adapters.get(channel.type) if channel else None
        if channel is None or adapter is None:
            raise LookupError("Channel is unknown, disabled or not connected")
        if not getattr(adapter, "outbound", True):
            raise LookupError("Channel type has no outbound message surface")
        store = self.execution.store
        key = hashlib.sha256(
            json.dumps([channel_id, operation_id]).encode()
        ).hexdigest()
        request_hash = hashlib.sha256(text.encode()).hexdigest()
        lock = self.delivery_locks.setdefault("post-" + key, asyncio.Lock())
        async with lock:
            events = await durable(store.channel_post_events, key)
            if events and events[0].get("request_hash") != request_hash:
                raise ValueError("Operation ID already used for different text")
            if events and events[-1]["stage"] == "delivered":
                return {**events[-1]["result"], "replayed": True}
            if events:
                raise RuntimeError(
                    "Earlier post is pending or uncertain; inspect the channel "
                    "before retrying with a new operation ID"
                )
            record = {
                "channel": channel_id,
                "operation_id": operation_id,
                "run_id": run_id,
                "request_hash": request_hash,
            }
            await durable(store.log_channel_post, key, {**record, "stage": "sending"})
            try:
                identity = await adapter.deliver(channel, Outbound(text))
            except asyncio.CancelledError:
                await durable(
                    store.log_channel_post, key, {**record, "stage": "uncertain"}
                )
                raise
            except Exception as exc:
                # Adapters may fail after a partial multi-chunk send: uncertain.
                await durable(
                    store.log_channel_post,
                    key,
                    {
                        **record,
                        "stage": "uncertain",
                        "error_type": type(exc).__name__,
                    },
                )
                raise RuntimeError(
                    "Post outcome uncertain; inspect the channel before retrying"
                ) from exc
            result = {"channel": channel_id, "external_id": identity}
            await durable(
                store.log_channel_post,
                key,
                {**record, "stage": "delivered", "result": result},
            )
            return result

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
        if run.status in {RunStatus.queued, RunStatus.running}:
            return
        if run.origin.job and run.status not in {
            RunStatus.completed,
            RunStatus.tool_error,
        }:
            return  # Scheduled failures remain gaps, as required by the jobs contract.
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
            if run.status != RunStatus.completed:
                outbound = Outbound(
                    f"[Solakit status: {run.status.value}] "
                    f"Run {run.id} did not complete."
                )
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
