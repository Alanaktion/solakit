"""Explicit runtime dependencies shared by resource routers."""

from typing import Annotated, Protocol, cast

from fastapi import Depends, Request

from solakit.channels import Channels
from solakit.core.config import Config
from solakit.core.loop import Execution
from solakit.core.model import ModelClient
from solakit.core.secrets import Redactor
from solakit.core.session_lifecycle import SessionLifecycle
from solakit.core.store import Store
from solakit.jobs import Jobs
from solakit.tools import Registry


class Runtime(Protocol):
    config: Config
    store: Store
    execution: Execution
    model: ModelClient
    registry: Registry
    jobs: Jobs
    channels: Channels
    redactor: Redactor
    session_lifecycle: SessionLifecycle


def runtime(request: Request) -> Runtime:
    return cast(Runtime, request.app.state)


RuntimeDep = Annotated[Runtime, Depends(runtime)]
