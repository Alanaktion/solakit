"""Versioned evaluation cases. Cases and results contain only JSON values."""

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SCORER_VERSION = "1"


def encoded(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(encoded(value)).hexdigest()


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Expected(Record):
    kind: Literal["action", "conversation", "denial", "interrupted"]
    artifacts: dict[str, str] = Field(default_factory=dict)
    effects: list[dict[str, str]] = Field(default_factory=list)
    receipt_statuses: list[str] = Field(default_factory=list)
    alternative_receipt_statuses: list[list[str]] = Field(default_factory=list)
    first_tool: str | None = None
    required_tools: list[str] = Field(default_factory=list)


class Case(Record):
    version: Literal[1]
    id: str = Field(pattern=r"^[a-z0-9-]+$")
    description: str
    messages: list[dict[str, Any]] = Field(min_length=1)
    tools: list[dict[str, Any]]
    resources: dict[str, str]
    denied_tools: list[str] = Field(default_factory=list)
    memory_outage: bool = False
    fixture_only: bool = False
    expected: Expected
    trials: int = Field(ge=2, le=100, default=3)
    max_steps: int = Field(ge=1, le=12, default=6)
    fake_responses: list[dict[str, Any]] = Field(min_length=1)


def load_cases(directory: Path) -> list[Case]:
    cases = [
        Case.model_validate_json(path.read_bytes()) for path in sorted(directory.glob("*.json"))
    ]
    if not cases or len({case.id for case in cases}) != len(cases):
        raise ValueError("case corpus is empty or has duplicate IDs")
    return cases
