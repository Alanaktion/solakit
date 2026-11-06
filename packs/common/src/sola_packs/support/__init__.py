"""Pack-owned config and grants; the core knows no letters/mail/device schemas."""

import hashlib
import html
import json
import os
import re
import threading
import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from solakit.core.config import SecretRef
from solakit.tools import ToolError

_SECRET_LOCK = threading.Lock()
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def resource(ctx, section, alias, model, action):
    if not IDENTIFIER.fullmatch(alias):
        raise ToolError("Invalid resource alias")
    prefix = {
        "letters": "letters",
        "mailboxes": "mailbox",
        "printers": "printer",
        "calendars": "calendar",
        "images": "image",
    }[section]
    profile = ctx.config.profiles[ctx.profile]
    if f"{prefix}:{alias}" not in profile.resources:
        raise ToolError(f"Missing resource: {prefix}:{alias}")
    effect = {"read": None, "reply": "send", "print": "print", "generate": None}[action]
    if effect and effect not in profile.effects:
        raise ToolError(f"Missing effect: {effect}")
    if action == "generate" and profile.write == "none":
        raise ToolError("Image generation requires filesystem write tier")
    try:
        path = Path(os.environ["SOLA_PACKS_CONFIG"]).expanduser().resolve()
        if path.stat().st_size > 1_000_000:
            raise ValueError("oversized config")
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        result = model.model_validate(data[section][alias])
        for name in type(result).model_fields:
            value = getattr(result, name)
            if isinstance(value, SecretRef) and value.file is not None:
                expanded = value.file.expanduser()
                value.file = (
                    expanded if expanded.is_absolute() else path.parent / expanded
                )
        return result
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ToolError(
            "Pack resource unavailable or invalid; check SOLA_PACKS_CONFIG"
        ) from exc


def secret(ctx, reference):
    try:
        value = reference.resolve()
    except (OSError, ValueError) as exc:
        raise ToolError(
            "Pack credential unavailable; check env/file reference and permissions"
        ) from exc
    # Runtime logging/UI share the Store's redactor. Register before connecting,
    # including credentials loaded by packages the core has never imported.
    with _SECRET_LOCK:
        redactor = ctx.store.redactor
        redactor.values = tuple(
            sorted(set((*redactor.values, value)), key=len, reverse=True)
        )
    return value


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def external(source, value):
    payload = (
        json.dumps(value, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )
    label = html.escape(source, quote=True)
    return (
        f'<external-content source="{label}" encoding="json">\n'
        f"{payload}\n</external-content>"
    )
