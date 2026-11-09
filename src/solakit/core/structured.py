"""Structured final answers: JSON extraction, a small JSON-Schema subset
validator, and sandboxed deterministic rendering (code owns layout)."""

import json
import re

TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "null": type(None),
}
FENCE = re.compile(r"\A\s*```(?:json)?\s*\n(.*?)\n\s*```\s*\Z", re.S)
MAX_SCHEMA_BYTES = 8000


def check_schema(schema: dict) -> None:
    if not isinstance(schema, dict) or schema.get("type") not in TYPES | {
        "number": 0,
        "integer": 0,
    }:
        raise ValueError("output_schema needs a top-level type")
    if len(json.dumps(schema)) > MAX_SCHEMA_BYTES:
        raise ValueError(f"output_schema is limited to {MAX_SCHEMA_BYTES} bytes")


def extract(content: str):
    """The answer as JSON: the whole reply, or one fenced json block."""
    text = (content or "").strip()
    match = FENCE.match(text)
    if match:
        text = match.group(1)
    return json.loads(text)


def errors(schema: dict, value, path: str = "$") -> list[str]:
    kind = schema.get("type")
    found = []
    if kind == "integer":
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif kind == "number":
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    elif kind in TYPES:
        ok = isinstance(value, TYPES[kind]) and not (
            kind != "boolean" and isinstance(value, bool)
        )
    else:
        ok = True
    if not ok:
        return [f"{path}: expected {kind}"]
    if "enum" in schema and value not in schema["enum"]:
        found.append(f"{path}: must be one of {schema['enum']}")
    if kind == "object":
        properties = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                found.append(f"{path}.{key}: required")
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in properties:
                    found.append(f"{path}.{key}: not allowed")
        for key, sub in properties.items():
            if key in value:
                found += errors(sub, value[key], f"{path}.{key}")
    if kind == "array" and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            found += errors(schema["items"], item, f"{path}[{index}]")
    return found


def render(template: str, value) -> str:
    from jinja2.sandbox import SandboxedEnvironment

    environment = SandboxedEnvironment(autoescape=False, keep_trailing_newline=True)
    context = value if isinstance(value, dict) else {}
    return environment.from_string(template).render(**context, data=value)


def check_render(template: str) -> None:
    from jinja2 import TemplateSyntaxError
    from jinja2.sandbox import SandboxedEnvironment

    if len(template) > 8000:
        raise ValueError("render template is limited to 8000 characters")
    try:
        SandboxedEnvironment().parse(template)
    except TemplateSyntaxError as exc:
        raise ValueError("render template is invalid") from exc
