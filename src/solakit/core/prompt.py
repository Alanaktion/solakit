"""Pure prompt assembly. No model, retrieval, framework or channel context."""

import hashlib
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from solakit.core.config import Config
from solakit.core.store import safe_id

SEPARATOR = "\n\n---\n\n"


@dataclass(frozen=True)
class PromptInput:
    soul: str
    context: str | None
    recall: str | None
    recall_header: str
    profile_fragment: str
    profile: str
    session: str
    origin: str
    at: datetime
    timezone: str
    tool_index: str = ""


def build_prompt(parts: PromptInput) -> str:
    if parts.at.tzinfo is None:
        raise ValueError("prompt time must be timezone-aware")
    fragments = [parts.soul]
    if parts.context is not None:
        fragments.append(parts.context)
    if parts.recall:
        fragments.append(f"{parts.recall_header}\n<recall>\n{parts.recall}\n</recall>")
    fragments.append(parts.profile_fragment)
    if parts.tool_index:
        fragments.append(parts.tool_index)
    local = parts.at.astimezone(ZoneInfo(parts.timezone))
    fragments.append(
        f"<run>\ntime: {local.isoformat()}\ntimezone: {parts.timezone}\n"
        f"profile: {parts.profile}\nsession: {parts.session}\n"
        f"origin: {parts.origin}\n</run>"
    )
    return SEPARATOR.join(fragments)


def read_prompt(
    config: Config,
    *,
    profile: str,
    session: str,
    origin: str,
    at: datetime,
    recall: str | None = None,
    tool_index: str = "",
) -> str:
    """Snapshot current files, preserving line endings and trailing whitespace."""
    if profile not in config.profiles:
        raise ValueError("unknown profile")
    safe_id(profile)
    root = config.paths.fragments
    context = root / "context.md"
    header = root / "recall-header.md"
    profile_path = root / "profiles" / f"{profile}.md"
    return build_prompt(
        PromptInput(
            tool_index=tool_index,
            soul=config.paths.soul.read_bytes().decode("utf-8"),
            context=context.read_bytes().decode("utf-8") if context.exists() else None,
            recall=recall,
            recall_header=header.read_bytes().decode("utf-8") if recall else "",
            profile_fragment=profile_path.read_bytes().decode("utf-8"),
            profile=profile,
            session=session,
            origin=origin,
            at=at,
            timezone=config.timezone,
        )
    )


def inspection(prompt: str, tools: list[dict]) -> dict:
    return {
        "prompt": prompt,
        "sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "tokens": None,
        "tools": tools,
    }


def validate_prompt_files(config: Config) -> None:
    """Fail startup on absent/unreadable required UTF-8 inputs; never invent text."""
    required = [config.paths.soul]
    for profile in config.profiles:
        safe_id(profile)
        required.append(config.paths.fragments / "profiles" / f"{profile}.md")
    if config.memory.enabled:
        required.append(config.paths.fragments / "recall-header.md")
    context = config.paths.fragments / "context.md"
    if context.exists():
        required.append(context)
    for path in required:
        try:
            path.read_bytes().decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise ValueError(
                f"Prompt input must be a readable UTF-8 file: {path}"
            ) from exc
