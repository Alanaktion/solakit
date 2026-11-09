"""Effective configuration views shared with runtime admission code paths."""

import json

from solakit.core.config import Config, load_config
from solakit.tools import Registry


def explain(config: Config, registry: Registry, profile: str) -> dict:
    """Exactly what admission resolves for this profile (same functions)."""
    if profile not in config.profiles:
        raise ValueError(f"unknown profile: {profile}")
    cfg = config.profiles[profile]
    view = registry.view(profile)
    schemas = view.schemas()
    thinking = cfg.model.thinking
    first = "on" if thinking == "auto" else thinking
    return {
        "profile": profile,
        "settings": cfg.model_dump(mode="json"),
        "grants": cfg.tools,
        "effective_tools": list(view.effective),
        "access": registry.access_summary(profile),
        "disclosure": view.metadata(),
        "visible_schemas": [s["function"]["name"] for s in schemas],
        "schema_bytes": len(json.dumps(schemas).encode()),
        "model_request": {
            "model": config.model.model_id,
            "max_tokens": cfg.model.max_tokens or config.model.max_tokens,
            "first_step_params": config.model.request.wire(
                thinking=first, override=cfg.model
            ),
            "thinking": thinking,
        },
        "playbooks": list(cfg.playbooks),
        "compaction": config.compaction.model_dump(),
    }


def grant_view(config: Config) -> dict:
    registry = Registry(config)
    result = {}
    for name, cfg in config.profiles.items():
        access = registry.access_summary(name)
        result[name] = {
            "tools": set(access["tools"]),
            "effects": set(access["effects"]),
            "resources": set(access["resources"]),
            "read": {access["read"], *access["read_roots"]},
            "write": {access["write"], *access["write_roots"]},
        }
    return result


def diff(old_path, new_path) -> list[str]:
    """Access-review lines: profile grants added (+) or removed (-)."""
    old, new = grant_view(load_config(old_path)), grant_view(load_config(new_path))
    lines = []
    for profile in sorted(set(old) | set(new)):
        if profile not in old:
            lines.append(f"+ profile {profile}")
        if profile not in new:
            lines.append(f"- profile {profile}")
            continue
        before = old.get(profile, {k: set() for k in new[profile]})
        for key in ("tools", "effects", "resources", "read", "write"):
            for item in sorted(new[profile][key] - before[key]):
                lines.append(f"+ {profile} {key}: {item}")
            for item in sorted(before[key] - new[profile][key]):
                lines.append(f"- {profile} {key}: {item}")
    return lines
