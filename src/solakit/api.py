"""Thin HTTP boundary over the explicitly owned application context."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from solakit.config import DEFAULT_CONFIG, EffectiveProfile, load_config, resolve_profile
from solakit.runtime import ApplicationContext, compose


def create_app(context: ApplicationContext | None = None) -> FastAPI:
    if context is None:

        def current_profile(admitted: EffectiveProfile) -> EffectiveProfile:
            return resolve_profile(
                load_config(),
                admitted.name,
                base=DEFAULT_CONFIG.parent,
                origin=admitted.origin,
                mode=admitted.mode,
            )

        profile = resolve_profile(load_config(), base=DEFAULT_CONFIG.parent)
        context = compose(profile, current_profile=current_profile)

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
        return {"status": context.state, "phase": "run-loop"}

    return app
