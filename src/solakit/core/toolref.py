"""Generate docs/tools.md from the registry (python -m solakit.core.toolref)."""

import inspect

from solakit.core.config import Config

MODULES = [
    "datetime",
    "delegate",
    "feeds",
    "files",
    "github",
    "jobs",
    "math",
    "memory",
    "messages",
    "notes",
    "playbook",
    "plan",
    "repo_git",
    "runtime",
    "sessions",
    "shell",
    "web",
    "wiki",
    *[
        f"sola_packs.{p}"
        for p in ("journal", "letters", "email", "printer", "image", "calendar", "blog")
    ],
]


def render() -> str:
    from solakit.tools import Registry

    registry = Registry(Config(enabled_tools=MODULES))
    lines = [
        "# Tool reference",
        "",
        "Generated from the registry by `python -m solakit.core.toolref > "
        "docs/tools.md`; a test fails when it is stale. Grants, effects and write "
        "tiers are configured per profile (SPEC §8).",
        "",
    ]
    family = None
    for name, item in sorted(registry.tools.items()):
        current = name.split(".")[0]
        if current != family:
            family = current
            lines += [f"## {family}", ""]
        manifest = item.manifest
        flags = []
        if manifest.effect:
            flags.append(f"effect `{manifest.effect}`")
        if manifest.writes:
            flags.append("writes files")
        if manifest.deprecated:
            flags.append(f"deprecated → `{manifest.deprecated}`")
        lines.append(f"### `{name}`")
        lines.append("")
        suffix = f" ({'; '.join(flags)})" if flags else ""
        lines.append(registry.summary(name) + suffix)
        lines.append("")
        schema = item.schema()["function"]["parameters"]
        required = set(schema.get("required", []))
        for parameter, spec in schema.get("properties", {}).items():
            mark = " (required)" if parameter in required else ""
            lines.append(f"- `{parameter}`{mark}: {spec.get('description', '')}")
        if schema.get("properties"):
            lines.append("")
        doc = inspect.getdoc(item.function) or ""
        if doc:
            lines += [doc, ""]
    return "\n".join(lines).rstrip() + "\n"


if __name__ == "__main__":
    print(render(), end="")
