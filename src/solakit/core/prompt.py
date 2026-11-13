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
    layout: str = "stable"
    playbook_index: str = ""
    playbook: str = ""
    notes: str = ""
    resources: tuple[str, ...] = ()


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
    """System message. The stable layout holds only per-session constants so the
    system message and replayed history form a byte-identical cacheable prefix;
    per-run values (time, origin, recall) go into the turn block instead."""
    if parts.at.tzinfo is None:
        raise ValueError("prompt time must be timezone-aware")
    fragments = [("soul", parts.soul)]
    if parts.memory is not None:
        fragments.append(("memory", parts.memory))
    if parts.context is not None:
        fragments.append(("context", parts.context))
    if parts.recall and parts.layout == "legacy":
        fragments.append(
            ("recall", f"{parts.recall_header}\n<recall>\n{parts.recall}\n</recall>")
        )
    fragments.append(("profile", parts.profile_fragment))
    if parts.resources:
        fragments.append(
            (
                "resources",
                "Granted named resources (use the alias after ':' in tool arguments; "
                "these are grants, not live availability checks):\n"
                + "\n".join(f"- {name}" for name in parts.resources),
            )
        )
    if parts.tool_index:
        fragments.append(("tool_index", parts.tool_index))
    if parts.playbook_index:
        fragments.append(("playbooks", parts.playbook_index))
    if parts.playbook and parts.layout == "legacy":
        fragments.append(("playbook", parts.playbook))
    if parts.layout == "legacy":
        local = parts.at.astimezone(ZoneInfo(parts.timezone))
        run = (
            f"<run>\ntime: {local.isoformat()}\ntimezone: {parts.timezone}\n"
            f"profile: {parts.profile}\nsession: {parts.session}\n"
            f"origin: {parts.origin}\n</run>"
        )
    else:
        run = (
            f"<run>\ntimezone: {parts.timezone}\nprofile: {parts.profile}\n"
            f"session: {parts.session}\n</run>"
        )
    fragments.append(("run", run))
    return AssembledPrompt(fragments)


def build_turn(parts: PromptInput) -> str | None:
    """Per-run preamble placed before the user's verbatim text (stable layout).

    Minute precision keeps the block short; it changes every turn by design and
    sits after the cached history prefix."""
    if parts.layout == "legacy":
        return None
    local = parts.at.astimezone(ZoneInfo(parts.timezone)).replace(
        second=0, microsecond=0
    )
    lines = [
        "<turn>",
        f"time: {local.isoformat(timespec='minutes')}",
        f"origin: {parts.origin}",
    ]
    if parts.recall:
        lines += [
            "",
            f"{parts.recall_header}\n<recall>\n{parts.recall}\n</recall>",
        ]
    if parts.notes:
        lines += ["", parts.notes]
    if parts.playbook:
        lines += ["", parts.playbook]
    lines.append("</turn>")
    return "\n".join(lines)


def prompt_input(
    config: Config,
    *,
    profile: str,
    session: str,
    origin: str,
    at: datetime,
    recall: str | None = None,
    tool_index: str = "",
    playbook: str | None = None,
    notes: dict | None = None,
) -> PromptInput:
    """Snapshot current files, preserving line endings and trailing whitespace."""
    from solakit.core import playbooks

    if profile not in config.profiles:
        raise ValueError("unknown profile")
    safe_id(profile)
    root = config.paths.fragments
    memory = root / "memory.md"
    context = root / "context.md"
    header = root / "recall-header.md"
    profile_path = root / "profiles" / f"{profile}.md"
    return PromptInput(
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
        layout=config.profiles[profile].prompt_layout,
        playbook_index=playbooks.index_text(config, config.profiles[profile].playbooks),
        playbook=playbooks.load(config, playbook).text() if playbook else "",
        notes=notes_text(notes or {}),
        resources=tuple(config.profiles[profile].resources),
    )


NOTES_MAX_CHARS = 2000


def notes_text(notes: dict) -> str:
    """Session notes for the turn block: newest first within a size cap."""
    if not notes:
        return ""
    lines, used = [], 0
    for key, event in reversed(list(notes.items())):
        line = f"- {key}: {event['value']}"
        if used + len(line) > NOTES_MAX_CHARS:
            lines.append(f"- … {len(notes) - len(lines)} older notes (notes.list)")
            break
        lines.append(line)
        used += len(line)
    return "Session notes (your own saved facts; data):\n" + "\n".join(reversed(lines))


def read_turn(config: Config, **kwargs) -> tuple[str, str | None]:
    """System prompt and turn block from one consistent file snapshot."""
    parts = prompt_input(config, **kwargs)
    return build_prompt(parts), build_turn(parts)


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
    """The system message only; see ``read_turn`` for the per-run preamble."""
    return build_prompt(
        prompt_input(
            config,
            profile=profile,
            session=session,
            origin=origin,
            at=at,
            recall=recall,
            tool_index=tool_index,
        )
    )


def inspection(
    prompt: str, tools: list[dict], fragments=None, turn: str | None = None
) -> dict:
    return {
        "prompt": prompt,
        "turn": turn,
        "fragments": fragments
        if fragments is not None
        else getattr(prompt, "fragments", None),
        "sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "tokens": None,
        "tools": tools,
    }


def validate_prompt_files(config: Config) -> None:
    """Fail startup on absent/unreadable required UTF-8 inputs; never invent text."""
    from solakit.core import playbooks

    for profile in config.profiles.values():
        for name in profile.playbooks:
            try:
                playbooks.load(config, name)
            except playbooks.PlaybookError as exc:
                raise ValueError(str(exc)) from exc
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
