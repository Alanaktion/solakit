"""HTTP boundary; runtime composition will be added with the core services."""

from fastapi import FastAPI


def create_app() -> FastAPI:
    app = FastAPI(title="Sola", version="0.1.0")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "phase": "skeleton"}

    return app
