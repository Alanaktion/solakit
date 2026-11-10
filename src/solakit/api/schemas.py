"""Operator request bodies."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

from solakit.core.schemas import Session


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32000)
    ]
    profile: str | None = None


class CreateSessionRequest(Session):
    model_config = ConfigDict(extra="forbid")


class UpdateSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    title: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)]


class MemoryProbeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fact: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)
    ]
    query: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)
    ]
    expected: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)
    ]
