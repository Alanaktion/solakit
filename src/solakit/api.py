"""Thin HTTP boundary over the explicitly owned application context."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from solakit.config import DEFAULT_CONFIG, load_config, resolve_profile
from solakit.runtime import ApplicationContext, compose


def create_app(context: ApplicationContext | None = None) -> FastAPI:
    context = context or compose(resolve_profile(load_config(), base=DEFAULT_CONFIG.parent))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await context.start()
        try:
            yield
        finally:
            await context.stop()

    app = FastAPI(title="Sola", version="0.1.0", lifespan=lifespan)
    app.state.context = context

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": context.state, "phase": "records-configuration"}

    return app
