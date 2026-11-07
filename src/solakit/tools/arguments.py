"""Conservative tool input normalization and schema-owned retry guidance."""

import json
import math
import re

from pydantic import BaseModel, ValidationError


def _resolve(schema: dict, root: dict) -> dict:
    reference = schema.get("$ref", "")
    if reference.startswith("#/$defs/"):
        return root.get("$defs", {}).get(reference.removeprefix("#/$defs/"), {})
    return schema


def _normalize(value, schema: dict, root: dict):
    schema = _resolve(schema, root)
    branches = schema.get("anyOf", [])
    if branches:
        # Normalize optional fields, but do not guess between union alternatives.
        branches = [s for s in branches if s.get("type") != "null"]
        if value is not None and len(branches) == 1:
            return _normalize(value, branches[0], root)
        return value
    kind = schema.get("type")
    if isinstance(value, str):
        scalar = value.strip()
        if kind == "integer" and re.fullmatch(r"[+-]?[0-9]{1,100}", scalar):
            return int(scalar)
        if kind == "boolean" and scalar.lower() in ("true", "false"):
            return scalar.lower() == "true"
        if (
            kind == "number"
            and len(scalar) <= 100
            and re.fullmatch(
                r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", scalar
            )
        ):
            number = float(scalar)
            # Do not turn a nonzero decimal into zero through float underflow.
            nonzero = any(c in "123456789" for c in re.split("[eE]", scalar)[0])
            if math.isfinite(number) and (number != 0 or not nonzero):
                return number
    if kind == "object" and isinstance(value, dict):
        properties = schema.get("properties", {})
        additional = schema.get("additionalProperties", {})
        return {
            key: _normalize(
                item,
                properties.get(key, additional if isinstance(additional, dict) else {}),
                root,
            )
            for key, item in value.items()
        }
    if kind == "array" and isinstance(value, list):
        return [_normalize(item, schema.get("items", {}), root) for item in value]
    return value


def validate_arguments(model: type[BaseModel], arguments) -> BaseModel:
    schema = model.model_json_schema()
    return model.model_validate(_normalize(arguments, schema, schema))


def argument_error(model: type[BaseModel], exc: ValueError) -> str:
    """Never echo input, custom messages, error context or arbitrary map keys."""
    root = model.model_json_schema()
    if not isinstance(exc, ValidationError):
        explanation = (
            "expected a JSON object. Use double-quoted property names and "
            "strings, with no trailing commas"
        )
    else:
        issues = []
        for issue in exc.errors(include_input=False, include_url=False)[:8]:
            schema = root
            location = []
            for part in issue["loc"]:
                schema = _resolve(schema, root)
                branches = [
                    s for s in schema.get("anyOf", []) if s.get("type") != "null"
                ]
                if len(branches) == 1:
                    schema = _resolve(branches[0], root)
                if isinstance(part, int):
                    location.append(str(part))
                    schema = schema.get("items", {})
                elif part in schema.get("properties", {}):
                    location.append(part)
                    schema = schema["properties"][part]
                else:
                    location.append("<argument>")
                    additional = schema.get("additionalProperties", {})
                    schema = additional if isinstance(additional, dict) else {}
            schema = _resolve(schema, root)
            branches = [s for s in schema.get("anyOf", []) if s.get("type") != "null"]
            if len(branches) == 1:
                schema = _resolve(branches[0], root)
            code = issue["type"]
            hints = {
                "missing": "provide this required argument",
                "extra_forbidden": "remove unsupported arguments",
                "int_type": "use a JSON integer, such as 3; not a boolean or fraction",
                "float_type": "use a JSON number, such as 0.5",
                "bool_type": "use true or false; not yes/no or 0/1",
                "string_type": "use a JSON string",
                "list_type": "use a JSON array, even for one item",
                "dict_type": "use a JSON object",
                "model_type": "send arguments as one JSON object",
                "literal_error": "choose one of the declared choices exactly",
                "enum": "choose one of the declared choices exactly",
                "too_short": "meet the declared minimum number of items or characters",
                "too_long": "stay within the declared maximum item count or length",
                "string_too_short": "meet the declared minimum string length",
                "string_too_long": "stay within the declared maximum string length",
                "string_pattern_mismatch": "use a string matching the declared pattern",
                "finite_number": "use a finite number",
            }
            hint = hints.get(code, "match the declared argument schema")
            bounds = {
                "greater_than": ("exclusiveMinimum", "use a value greater than"),
                "greater_than_equal": ("minimum", "use a value at least"),
                "less_than": ("exclusiveMaximum", "use a value less than"),
                "less_than_equal": ("maximum", "use a value at most"),
                "multiple_of": ("multipleOf", "use a multiple of"),
            }
            if code in bounds:
                key, instruction = bounds[code]
                if key in schema:
                    hint = instruction + " " + json.dumps(schema[key])
            constraints = {
                key: schema[key]
                for key in (
                    "type",
                    "enum",
                    "const",
                    "minimum",
                    "maximum",
                    "exclusiveMinimum",
                    "exclusiveMaximum",
                    "multipleOf",
                    "minLength",
                    "maxLength",
                    "minItems",
                    "maxItems",
                    "format",
                    "pattern",
                )
                if key in schema
            }
            if constraints:
                hint += (
                    "; expected " + json.dumps(constraints, ensure_ascii=False)[:400]
                )
            # Custom validator error codes can also contain submitted text.
            safe_code = (
                code
                if code in hints
                or code
                in {
                    "greater_than",
                    "greater_than_equal",
                    "less_than",
                    "less_than_equal",
                    "literal_error",
                    "enum",
                    "too_short",
                    "too_long",
                    "string_too_short",
                    "string_too_long",
                    "string_pattern_mismatch",
                    "multiple_of",
                    "finite_number",
                }
                else "invalid_value"
            )
            issues.append(f"{'.'.join(location) or 'arguments'}: {safe_code} — {hint}")
        explanation = "; ".join(issues)
    properties = root.get("properties", {})
    accepted = (
        ", ".join(
            name + (" (required)" if name in root.get("required", []) else "")
            for name in list(properties)[:12]
        )
        or "none"
    )
    if len(properties) > 12:
        accepted += ", … (see tool schema)"
    return (
        "Invalid tool arguments: " + explanation + ". "
        "The tool was not executed. Correct the arguments and retry. "
        "Accepted arguments: " + accepted + "."
    )
