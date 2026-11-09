"""`sola init`: write a starting configuration and prompt files from templates.

Never overwrites: existing targets are refused, or with ``force`` written as
``<name>.new`` siblings with a printed diff. Never writes secret values."""

import difflib
import tomllib
from importlib import resources
from pathlib import Path

PRESETS = ("qwen3-35b-a3b-omlx", "generic-openai")
DEFAULTS = {"thinking": "off", "timezone": "America/Denver", "data": "~/sola-data"}
GITIGNORE = ".env\n*.new\n"
ENV_EXAMPLE = (
    "# Fill in and keep private (mode 0600); load with: set -a; source .env; set +a\n"
    "SOLA_OPERATOR_TOKEN=\n"
    "# OMLX_API_KEY=\n"
)


def preset_text(name: str, **values) -> str:
    if name not in PRESETS:
        raise ValueError(f"unknown preset {name}; choose: {', '.join(PRESETS)}")
    import re

    text = resources.files("solakit.presets").joinpath(f"{name}.toml").read_text()
    merged = {**DEFAULTS, **values}
    # Only known placeholders: TOML inline tables also use braces.
    return re.sub(
        r"\{(" + "|".join(DEFAULTS) + r")\}", lambda m: str(merged[m[1]]), text
    )


def template(relative: str) -> str:
    return resources.files("solakit.scaffold").joinpath(relative).read_text()


def plan_files(preset: str, **values) -> dict[str, str]:
    config = preset_text(preset, **values)
    profiles = tomllib.loads(config).get("profiles", {})
    files = {
        "config.toml": config,
        "SOUL.md": template("SOUL.md"),
        "fragments/recall-header.md": template("fragments/recall-header.md"),
        "fragments/playbooks/triage-inbox.md": template(
            "fragments/playbooks/triage-inbox.md"
        ),
        ".gitignore": GITIGNORE,
        ".env.example": ENV_EXAMPLE,
    }
    for name in profiles:
        files[f"fragments/profiles/{name}.md"] = (
            template("fragments/profiles/interactive.md")
            if name == "interactive"
            else ""
        )
    return files


def write(root: Path, files: dict[str, str], *, force: bool = False) -> list[str]:
    """Create files; returns report lines. Refuses existing files unless force."""
    existing = [name for name in files if (root / name).exists()]
    if existing and not force:
        raise FileExistsError(
            "Refusing to overwrite: " + ", ".join(existing) + " (use --force)"
        )
    report = []
    for name, content in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            sibling = target.with_name(target.name + ".new")
            sibling.write_text(content)
            current = target.read_text().splitlines(keepends=True)
            report.append(f"wrote {sibling} (existing file kept)")
            report.extend(
                line.rstrip("\n")
                for line in difflib.unified_diff(
                    current,
                    content.splitlines(keepends=True),
                    str(target),
                    str(sibling),
                )
            )
        else:
            target.write_text(content)
            report.append(f"created {target}")
    return report
