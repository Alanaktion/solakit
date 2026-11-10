"""Request field shapes; timezone-aware Job validation runs in the manager."""

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

_create_fields: dict[str, Any] = {
    name: (field.annotation, field) for name, field in Job.model_fields.items()
}

# A factory omits a misleading static timezone default from the HTTP schema.
# The manager resolves omission before validating local one-time schedule inputs.
_create_fields["timezone"] = (
    str,
    Field(
        default_factory=lambda: Job.model_fields["timezone"].default,
        description="IANA timezone; omit to inherit the configured timezone.",
    ),
)

if TYPE_CHECKING:
    # Request shapes use BaseModel validation/dump methods. Scheduling validation
    # runs after the manager injects the configured timezone into omitted fields.
    class JobCreate(StrictUpdate):
        pass

    class JobUpdate(StrictUpdate):
        pass
else:
    JobCreate = create_model("JobCreate", __base__=StrictUpdate, **_create_fields)
    JobUpdate = create_model("JobUpdate", __base__=StrictUpdate, **_fields)
