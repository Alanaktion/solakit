"""Partial edits use the same strict field types and final Job validation."""

from pydantic import ConfigDict, Field, create_model

from solakit.core.config import Job, StrictModel


class JobToolsError(ValueError):
    """A job requests tools outside its profile's current grants."""


class StrictUpdate(StrictModel):
    model_config = ConfigDict(extra="forbid", strict=True)


JobUpdate = create_model(
    "JobUpdate",
    __base__=StrictUpdate,
    **{
        name: (field.annotation, Field(default=None))
        for name, field in Job.model_fields.items()
        if name != "id"
    },
)
