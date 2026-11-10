"""Partial edits use the same strict field types and final Job validation."""

from typing import TYPE_CHECKING, Any

from pydantic import ConfigDict, Field, create_model

from solakit.core.config import Job, StrictModel


class JobToolsError(ValueError):
    """A job requests tools outside its profile's current grants."""


class StrictUpdate(StrictModel):
    model_config = ConfigDict(extra="forbid", strict=True)


# Dynamic field definitions retain the original Pydantic runtime annotations.
_fields: dict[str, Any] = {
    name: (field.annotation, Field(default=None))
    for name, field in Job.model_fields.items()
    if name != "id"
}

if TYPE_CHECKING:
    # The generated subclass adds optional Job fields; consumers use BaseModel's
    # validation/dump interface, while FastAPI sees the full runtime model.
    class JobUpdate(StrictUpdate):
        pass
else:
    JobUpdate = create_model(
        "JobUpdate",
        __base__=StrictUpdate,
        **_fields,
    )
