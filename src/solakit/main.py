from typing import Annotated

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, StringConstraints

from solakit.runtime import AgentNotConfigured, Runtime


class RunRequest(BaseModel):
    message: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32000)
    ]


class RunResponse(BaseModel):
    response: str


def create_app(runtime: Runtime | None = None) -> FastAPI:
    app = FastAPI(title="Solakit", version="0.1.0")
    app.state.runtime = runtime if runtime is not None else Runtime()

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/runs", response_model=RunResponse)
    async def run(request: RunRequest) -> RunResponse:
        try:
            response = await app.state.runtime.run(request.message)
        except AgentNotConfigured as exc:
            raise HTTPException(503, "No agent configured") from exc
        return RunResponse(response=response)

    return app


app = create_app()
