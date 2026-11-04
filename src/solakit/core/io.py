"""Complete accepted filesystem operations before propagating cancellation."""

import asyncio
from functools import partial


async def durable(function, *args, **kwargs):
    task = asyncio.create_task(asyncio.to_thread(partial(function, *args, **kwargs)))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # A thread cannot be cancelled. Wait so no late write races finalization.
        await task
        raise
