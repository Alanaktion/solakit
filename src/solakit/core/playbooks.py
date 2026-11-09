"""Operator-owned playbooks: short procedures loaded on demand (SPEC §5.3).

A playbook is ``<fragments>/playbooks/<name>.md`` with a small front matter::

    ---
    name: triage-inbox
    summary: Check mail, report urgent items, mark handled messages read
    tools: [email.search, email.read, email.mark_read]
    ---
    1. ...

Playbooks are data, not authority: ``tools`` is advisory text shown to the
model and never widens grants. Only the one-line index enters the stable prompt
prefix; full text arrives through ``playbook.load`` (or up front for a job).
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
MAX_CHARS = 8000


class PlaybookError(ValueError):
    pass


@dataclass(frozen=True)
class Playbook:
    name: str
    summary: str
    tools: tuple[str, ...] = field(default_factory=tuple)
    body: str = ""

    def text(self) -> str:
        tools = f"\ntools: {', '.join(self.tools)}" if self.tools else ""
        return (
            f'<playbook name="{self.name}">\nsummary: {self.summary}{tools}\n\n'
            f"{self.body.strip()}\n</playbook>"
        )


def _list(value: str) -> tuple[str, ...]:
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    return tuple(item.strip().strip("'\"") for item in value.split(",") if item.strip())


def parse(name: str, source: str) -> Playbook:
    if not NAME.match(name):
        raise PlaybookError(f"invalid playbook name: {name}")
    if len(source) > MAX_CHARS:
        raise PlaybookError(f"playbook {name} exceeds {MAX_CHARS} characters")
    match = re.match(r"\A---\r?\n(.*?)\r?\n---\r?\n?(.*)\Z", source, re.S)
    if not match:
        raise PlaybookError(f"playbook {name} needs --- front matter ---")
    meta = {}
    for line in match.group(1).splitlines():
        if not line.strip():
            continue
        key, sep, value = line.partition(":")
        if not sep:
            raise PlaybookError(f"playbook {name}: bad front matter line")
        meta[key.strip()] = value.strip()
    if meta.get("name", name) != name:
        raise PlaybookError(f"playbook {name}: front matter name must match file")
    summary = meta.get("summary", "")
    if not summary or len(summary) > 100 or "\n" in summary:
        raise PlaybookError(f"playbook {name}: summary must be 1–100 characters")
    unknown = set(meta) - {"name", "summary", "tools"}
    if unknown:
        raise PlaybookError(f"playbook {name}: unknown keys {sorted(unknown)}")
    return Playbook(name, summary, _list(meta.get("tools", "")), match.group(2))


def directory(config) -> Path:
    return config.paths.fragments / "playbooks"


def load(config, name: str) -> Playbook:
    if not NAME.match(name):
        raise PlaybookError(f"invalid playbook name: {name}")
    path = directory(config) / f"{name}.md"
    try:
        source = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise PlaybookError(f"playbook {name} is missing or unreadable") from exc
    return parse(name, source)


def available(config) -> list[str]:
    root = directory(config)
    if not root.is_dir():
        return []
    return sorted(p.stem for p in root.glob("*.md") if NAME.match(p.stem))


def index_text(config, names: list[str]) -> str:
    """Stable one-line-per-playbook index for the system prompt."""
    if not names:
        return ""
    lines = [
        "<playbooks>",
        "Procedures you can load with playbook.load(name) before a matching task:",
    ]
    for name in names:
        lines.append(f"- {name} — {load(config, name).summary}")
    lines.append("</playbooks>")
    return "\n".join(lines)


def validate(config, granted: dict[str, list[str]] | None = None) -> list[str]:
    """Errors for missing/invalid playbooks; warnings for ungranted tools."""
    warnings = []
    for profile, cfg in config.profiles.items():
        for name in cfg.playbooks:
            playbook = load(config, name)
            if granted is not None:
                missing = [t for t in playbook.tools if t not in granted[profile]]
                if missing:
                    warnings.append(
                        f"Profile {profile}: playbook {name} names ungranted "
                        f"tools: {', '.join(missing)}"
                    )
    return warnings
