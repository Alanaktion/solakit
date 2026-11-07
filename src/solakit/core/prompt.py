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
    memory: str | None
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


class AssembledPrompt(str):
    """Literal prompt bytes with assembly-time fragment evidence."""

    def __new__(cls, fragments: list[tuple[str, str]]):
        instance = super().__new__(cls, SEPARATOR.join(text for _, text in fragments))
        instance.fragments = [
            {
                "name": name,
                "chars": len(text),
                "sha256": hashlib.sha256(text.encode()).hexdigest(),
            }
            for name, text in fragments
        ]
        return instance


def build_prompt(parts: PromptInput) -> str:
    if parts.at.tzinfo is None:
        raise ValueError("prompt time must be timezone-aware")
    fragments = [("soul", parts.soul)]
    if parts.memory is not None:
        fragments.append(("memory", parts.memory))
    if parts.context is not None:
        fragments.append(("context", parts.context))
    if parts.recall:
        fragments.append(
            ("recall", f"{parts.recall_header}\n<recall>\n{parts.recall}\n</recall>")
        )
    fragments.append(("profile", parts.profile_fragment))
    if parts.tool_index:
        fragments.append(("tool_index", parts.tool_index))
    local = parts.at.astimezone(ZoneInfo(parts.timezone))
    fragments.append(
        (
            "run",
            f"<run>\ntime: {local.isoformat()}\ntimezone: {parts.timezone}\n"
            f"profile: {parts.profile}\nsession: {parts.session}\n"
            f"origin: {parts.origin}\n</run>",
        )
    )
    return AssembledPrompt(fragments)


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
    memory = root / "memory.md"
    context = root / "context.md"
    header = root / "recall-header.md"
    profile_path = root / "profiles" / f"{profile}.md"
    return build_prompt(
        PromptInput(
            tool_index=tool_index,
            soul=config.paths.soul.read_bytes().decode("utf-8"),
            memory=memory.read_bytes().decode("utf-8") if memory.exists() else None,
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


def inspection(prompt: str, tools: list[dict], fragments=None) -> dict:
    return {
        "prompt": prompt,
        "fragments": fragments
        if fragments is not None
        else getattr(prompt, "fragments", None),
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
    for name in ("memory.md", "context.md"):
        path = config.paths.fragments / name
        if path.exists():
            required.append(path)
    for path in required:
        try:
            path.read_bytes().decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise ValueError(
                f"Prompt input must be a readable UTF-8 file: {path}"
            ) from exc


def front_page_status(config: Config) -> dict:
    """Read the live front page without applying its offline curation limit."""
    path = config.paths.fragments / "memory.md"
    chars = len(path.read_bytes().decode("utf-8")) if path.exists() else 0
    cap = config.memory.front_page_max_chars
    return {"chars": chars, "cap": cap, "over": chars > cap}


def check_front_page(config: Config) -> str | None:
    status = front_page_status(config)
    detail = (
        f"{config.paths.fragments / 'memory.md'}: {status['chars']} characters, "
        f"cap {status['cap']}; prune the curated front page"
    )
    if status["over"]:
        raise ValueError(detail)
    if status["chars"] * 4 >= status["cap"] * 3:
        return "Warning: " + detail
    return None
