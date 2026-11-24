"""One-shot copied legacy TOML migration; produce a private draft and authority diff."""

import argparse
import json
import tomllib
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from solakit.config import Budgets, OperatorConfig, load_config, resolve_profile
from solakit.storage.evidence import canonical, digest, ensure_directory, publish

TOOL_MAP = {
    "files.read": "files.read",
    "files.write": "files.write",
    "runtime.status": "runtime.status",
    "memory.search": "memory.recall",
    "memory.remember": "memory.remember",
    "sessions.search": "transcripts.search",
}


def toml(data: dict[str, Any], prefix: tuple[str, ...] = ()) -> str:
    lines = ["[" + ".".join(json.dumps(k) for k in prefix) + "]"] if prefix else []
    for key, value in data.items():
        if value is not None and not isinstance(value, dict):
            lines.append(f"{json.dumps(key)} = {json.dumps(value, ensure_ascii=False)}")
    for key, value in data.items():
        if isinstance(value, dict):
            lines.extend(("", toml(value, (*prefix, key))))
    return "\n".join(lines) + "\n"


def migrate(
    source: Path,
    output: Path,
    data_dir: Path,
    *,
    profile: str = "interactive",
    candidate: Path | None = None,
    resolutions: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """A candidate can resolve intentionally broader grants; nothing is installed."""
    raw = source.read_bytes()
    legacy = tomllib.loads(raw.decode())
    if profile not in legacy.get("profiles", {}):
        raise ValueError("selected legacy profile does not exist")
    selected = legacy.get("profile_defaults", {}) | legacy["profiles"][profile]
    paths = legacy.get("paths", {})
    model = legacy.get("model", {})
    base = source.resolve().parent

    def path(value: str) -> str:
        return str((base / Path(value).expanduser()).resolve())

    def workspace(value: str) -> str:
        if value.startswith("path:"):
            target = Path(value[5:]).expanduser()
            if not target.is_absolute():
                raise ValueError("legacy path scopes require absolute paths")
            return path(str(target))
        if value.startswith("repo:"):
            return path(legacy["repositories"][value[5:]])
        if value.startswith("collection:"):
            collection = legacy["collections"][value.removeprefix("collection:")]
            root = Path(path(legacy["repositories"][collection["repository"]]))
            target = (root / collection["directory"]).resolve()
            if not target.is_relative_to(root):
                raise ValueError("legacy collection escapes its repository")
            return str(target)
        return path(value)

    read_tier, write_tier = selected.get("read", "workspace"), selected.get("write", "none")
    roots = selected.get("workspace", selected.get("read_scopes", []))
    if not roots and read_tier != "none":
        roots = ["repo:" + name for name in legacy.get("repositories", {})] or [
            legacy.get("server", {}).get("cwd", ".")
        ]
    write_roots = selected.get("write_workspace", selected.get("write_scopes"))
    reads = [workspace(r) for r in roots] if read_tier != "none" else []
    writes = (
        [workspace(r) for r in (write_roots if write_roots is not None else roots)]
        if write_tier != "none" or selected.get("write_scopes")
        else []
    )
    grants = selected.get("tools", [])
    enabled = legacy.get("enabled_tools", [])
    tools = sorted(
        {
            new
            for old, new in TOOL_MAP.items()
            if old.split(".")[0] in enabled and any(fnmatchcase(old, pattern) for pattern in grants)
        }
    )
    if read_tier == "none":
        tools = [t for t in tools if t != "files.read"]
    if not writes:
        tools = [t for t in tools if t != "files.write"]
    # A single v2 root set cannot preserve split read/write roots; draft keeps reads only.
    if writes and set(reads) != set(writes):
        tools = [t for t in tools if t != "files.write"]
    if selected.get("sessions_private") or selected.get("sessions_read_others") is False:
        tools = [t for t in tools if t != "transcripts.search"]
    steps = min(256, selected.get("max_steps", 40))
    budgets = Budgets(
        model_calls=min(16, steps),
        tool_calls=min(32, steps),
        output_tokens=min(4096, model.get("max_tokens", 4096)),
        tool_timeout_seconds=min(60, int(selected.get("tool_timeout_s", 60))),
        tool_result_bytes=min(65536, selected.get("tool_output_max_chars", 20000)),
    )
    draft: dict[str, Any] = {
        "schema_version": 1,
        "data_dir": str(data_dir.resolve()),
        "model": {
            "pin": model.get("model_id", ""),
            "base_url": model.get("base_url", "http://127.0.0.1:8000/v1"),
        },
        "defaults": {
            "workspace_roots": reads,
            "tools": tools,
            "memory_mode": "off",
            "fragments": [],
            "budgets": budgets.model_dump(),
        },
    }
    key_ref = model.get("api_key")
    if isinstance(key_ref, dict) and isinstance(key_ref.get("env"), str):
        draft["model"]["api_key_env"] = key_ref["env"]
    if paths.get("soul"):
        draft["soul_file"] = path(paths["soul"])
    fragments = Path(path(paths["fragments"])) if paths.get("fragments") else None
    if fragments:
        curated = fragments / "memory.md"
        if curated.is_file():
            draft["curated_memory_file"] = str(curated)
        draft["defaults"]["fragments"] = [
            str(p)
            for p in (fragments / "context.md", fragments / "profiles" / f"{profile}.md")
            if p.is_file()
        ]
    unsupported: list[str] = []
    consumed = {
        "enabled_tools",
        "model.model_id",
        "model.base_url",
        "model.api_key.env",
        "paths.soul",
        "paths.data",
        "paths.fragments",
        "server.cwd",
        *{
            f"{prefix}.{key}"
            for prefix in ("profile_defaults", f"profiles.{profile}")
            for key in (
                "tools",
                "workspace",
                "write_workspace",
                "read",
                "write",
                "read_scopes",
                "write_scopes",
                "sessions_private",
                "sessions_read_others",
                "max_steps",
                "tool_timeout_s",
                "tool_output_max_chars",
            )
        },
    }

    def keys(value: dict[str, Any], prefix: str = "") -> None:
        for key, item in value.items():
            name = f"{prefix}.{key}" if prefix else key
            if isinstance(item, dict):
                keys(item, name)
            elif name not in consumed:
                unsupported.append(name)

    keys(legacy)
    # Resolve the draft strictly before publishing any active-looking config.
    config = load_config(candidate) if candidate else OperatorConfig.model_validate(draft)
    config_base = candidate.resolve().parent if candidate else base
    effective = resolve_profile(config, base=config_base)
    all_profiles = {
        name: resolve_profile(config, name, base=config_base)
        for name in ("default", *config.profiles)
    }
    if effective.data_dir != data_dir.resolve():
        raise ValueError("candidate must use the selected v2 data directory")
    if effective.data_dir == Path(path(paths.get("data", "."))):
        raise ValueError("migration requires a separate v2 data directory")
    diff: dict[str, Any] = {
        "data_dir": {"before": path(paths.get("data", ".")), "after": str(effective.data_dir)},
        "roots": {"before": sorted(set(reads)), "after": list(map(str, effective.workspace_roots))},
        "tools": {"before_mapped": tools, "after": sorted(effective.tools)},
        "fragments": {
            "soul_before": draft.get("soul_file"),
            "soul_after": str(effective.soul_file) if effective.soul_file else None,
            "legacy_directory": str(fragments) if fragments else None,
            "before": draft["defaults"]["fragments"],
            "after": list(map(str, effective.fragments)),
            "curated_before": draft.get("curated_memory_file"),
            "curated_after": str(effective.curated_memory_file)
            if effective.curated_memory_file
            else None,
            "ordering": "SOUL, curated memory, context, selected profile; recall header omitted",
        },
        "budgets": {
            "before_steps": selected.get("max_steps", 40),
            "after": effective.budgets.model_dump(),
        },
        "memory": {
            "before_enabled": legacy.get("memory", {}).get("enabled", False),
            "after": effective.memory_mode,
            "scope": effective.memory.model_dump(mode="json", exclude={"api_key_env"}),
        },
    }
    broadened = []
    for root in effective.workspace_roots:
        if read_tier != "anywhere" and not any(root.is_relative_to(Path(r)) for r in reads):
            broadened.append("roots")
        if (
            "files.write" in effective.tools
            and write_tier != "anywhere"
            and not any(root.is_relative_to(Path(r)) for r in writes)
        ):
            broadened.append("write_roots")
    if not effective.tools <= set(tools):
        broadened.append("tools")
    if effective.memory_mode != "off":
        broadened.append("memory")
    if (
        tuple(map(str, effective.fragments)) != tuple(draft["defaults"]["fragments"])
        or effective.curated_memory_file
        != (Path(draft["curated_memory_file"]) if draft.get("curated_memory_file") else None)
        or effective.soul_file != (Path(draft["soul_file"]) if draft.get("soul_file") else None)
    ):
        broadened.append("fragments")
    if any(getattr(effective.budgets, k) > getattr(budgets, k) for k in Budgets.model_fields):
        broadened.append("budgets")
    unresolved = sorted(set(broadened) - resolutions)
    report = {
        "source_sha256": digest(raw),
        "selected_profile": profile,
        "unsupported_keys": sorted(unsupported),
        "authority_diff": diff,
        "broadened": sorted(set(broadened)),
        "resolutions": sorted(resolutions),
        "unresolved": unresolved,
        "status": "blocked" if unresolved else "ready_for_review",
        "notes": [
            "Draft memory stays disabled; select and verify OpenViking scope explicitly.",
            "Legacy resource/effect, Android, channel, shell and domain grants are not activated.",
            "Source copy is private and may contain credentials; report contains no secret values.",
        ],
    }
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    ensure_directory(output)
    publish(output / "legacy.source.toml", raw)
    publish(output / "report.json", canonical(report))
    if not unresolved:
        normalized = config.model_dump(mode="json", exclude_none=True)
        normalized["data_dir"] = str(effective.data_dir)
        for field in ("soul_file", "curated_memory_file"):
            value = getattr(effective, field)
            if value is not None:
                normalized[field] = str(value)
        for name, resolved in all_profiles.items():
            settings = normalized["defaults"] if name == "default" else normalized["profiles"][name]
            if "workspace_roots" in settings:
                settings["workspace_roots"] = list(map(str, resolved.workspace_roots))
            if "fragments" in settings:
                settings["fragments"] = list(map(str, resolved.fragments))
        encoded = toml(normalized).encode()
        # Validate serialized TOML, including tuple/list conversion at the boundary.
        OperatorConfig.model_validate(tomllib.loads(encoded.decode()))
        publish(output / "config.v2.toml", encoded)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Copied legacy TOML")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--profile", default="interactive")
    parser.add_argument("--candidate", type=Path)
    parser.add_argument(
        "--resolve",
        action="append",
        default=[],
        choices=["roots", "write_roots", "tools", "memory", "fragments", "budgets"],
    )
    args = parser.parse_args()
    try:
        report = migrate(
            args.source,
            args.output,
            args.data_dir,
            profile=args.profile,
            candidate=args.candidate,
            resolutions=frozenset(args.resolve),
        )
    except Exception:
        parser.exit(
            1, "Migration failed validation; source and operator configuration are unchanged.\n"
        )
    print(json.dumps({"status": report["status"], "unresolved": report["unresolved"]}))
    if report["unresolved"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
