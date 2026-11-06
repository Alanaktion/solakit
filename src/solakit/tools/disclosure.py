"""Run-local schema disclosure. Discovery never grants execution authority."""

import hashlib
import inspect
import json
import re
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, ValidationError

DISCLOSURE_STEPS = 8
META_NAMES = ("tools.search", "tools.load")


class Search(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    query: str
    limit: int = Field(default=5, ge=1, le=5)


class Load(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    names: list[str] = Field(min_length=1, max_length=5)


def meta_schemas():
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": arguments.model_json_schema(),
            },
        }
        for name, description, arguments in (
            (
                "tools.search",
                "Search granted tools and activate matching full schemas "
                "for the next request.",
                Search,
            ),
            (
                "tools.load",
                "Activate exact granted tool names for the next request (up to five).",
                Load,
            ),
        )
    ]


def tokens(text):
    return set(re.findall(r"[a-z0-9]+", text.lower()))


@dataclass(frozen=True)
class ToolView:
    profile: str
    mode: str
    effective: tuple[str, ...]
    resident: tuple[str, ...]
    entries: tuple[tuple[str, str, tuple[str, ...], str, bool], ...]
    index_text: str
    index_sha256: str
    catalog_json: str

    def schemas(self, active=None):
        catalog = json.loads(self.catalog_json)
        names = (
            self.effective
            if self.mode == "eager"
            else (*META_NAMES, *(self.resident if active is None else active))
        )
        return [catalog[name] for name in names]

    def metadata(self):
        return {
            "profile": self.profile,
            "mode": self.mode,
            "effective": list(self.effective),
            "resident": list(self.resident),
            "index_sha256": self.index_sha256,
            "activations": [],
            "schema_steps": [],
            "disclosure_steps": 0,
            "work_steps": 0,
        }

    def diagnostics(self, budget):
        catalog = json.loads(self.catalog_json)

        def size(names):
            return len(json.dumps([catalog[n] for n in names]).encode())

        initial = len(json.dumps(self.schemas()).encode()) + len(
            self.index_text.encode()
        )
        threshold = 0.25 if self.mode == "deferred" else 0.5
        warning = (
            "Initial deferred tool disclosure exceeds 25% of context_budget; "
            "review resident tools."
            if self.mode == "deferred"
            else "Eager schemas exceed 50% of context_budget; "
            "consider deferred disclosure."
        )
        return {
            "profile": self.profile,
            "mode": self.mode,
            "effective_count": len(self.effective),
            "effective_schema_bytes": size(self.effective),
            "resident_count": len(self.resident),
            "resident_schema_bytes": size(self.resident),
            "index_bytes": len(self.index_text.encode()),
            "initial_visible_bytes": initial,
            "deferred_count": len(self.effective) - len(self.resident)
            if self.mode == "deferred"
            else 0,
            "warnings": [warning] if initial > budget * threshold else [],
        }


def resolve(
    registry, profile, tools=None, mode="inherit", resident=None, *, strict=True
):
    if profile not in registry.config.profiles:
        raise ValueError("Unknown profile")
    cfg = registry.config.profiles[profile]
    effective = tuple(registry.effective_tools(profile, tools))
    mode = cfg.tool_disclosure if mode == "inherit" else mode
    if mode not in ("eager", "deferred"):
        raise ValueError("Invalid tool disclosure mode")
    selected = cfg.tool_resident if resident is None else resident
    try:
        if strict and len(selected) != len(set(selected)):
            raise ValueError("duplicate resident entries")
        selected = registry.expand_grants(selected, profile, strict=strict)
    except ValueError as exc:
        raise ValueError(f"Profile {profile}: resident tools: {exc}") from exc
    # Profile residents must be valid against profile grants; inherited residents
    # may subsequently be narrowed by a job's ceiling.
    grants = registry.effective_tools(profile)
    if strict and (
        len(selected) != len(set(selected))
        or any(n not in (grants if resident is None else effective) for n in selected)
    ):
        raise ValueError(
            f"Profile {profile}: resident tools must be unique and granted "
            f"to this run: {selected}"
        )
    residents = tuple(dict.fromkeys(n for n in selected if n in effective))
    entries = tuple(
        (
            n,
            registry.summary(n),
            registry.tools[n].manifest.keywords,
            inspect.getdoc(registry.tools[n].function) or "",
            bool(
                registry.tools[n].manifest.writes or registry.tools[n].manifest.effect
            ),
        )
        for n in effective
    )
    index = ""
    if mode == "deferred":
        lines = [
            "<tool-index>",
            "Only the following tools are granted. Use tools.search or tools.load "
            "to load full schemas before calling a tool.",
            "Initially loaded: " + ", ".join((*META_NAMES, *residents)),
        ]
        for family in sorted({n.split(".")[0] for n in effective}):
            lines.append(f"{family}:")
            lines.extend(
                f"  {n} — {summary}" + (" [writes]" if writes else "")
                for n, summary, _, _, writes in sorted(entries)
                if n.split(".")[0] == family
            )
        index = "\n".join([*lines, "</tool-index>"])
    catalog = {n: registry.tools[n].schema() for n in effective}
    if mode == "deferred":
        catalog.update({s["function"]["name"]: s for s in meta_schemas()})
    return ToolView(
        profile,
        mode,
        effective,
        residents,
        entries,
        index,
        hashlib.sha256(index.encode()).hexdigest(),
        json.dumps(catalog),
    )


def disclose(name, arguments, ctx):
    from solakit.tools import ToolResult

    try:
        args = (Search if name == "tools.search" else Load).model_validate_json(
            arguments
        )
    except (ValidationError, ValueError):
        return ToolResult(
            content=(
                "Invalid discovery arguments; search requires query and limit "
                "1..5; load requires 1..5 exact names."
            ),
            error=True,
        )
    view = ctx.tool_view
    if name == "tools.search":
        query = args.query.lower()
        query_tokens = tokens(query)
        matches = []
        for n, summary, keywords, doc, writes in view.entries:
            score = len(query_tokens & tokens(" ".join((n, summary, *keywords, doc))))
            if query in (n.lower(), n.replace(".", "_").lower()):
                score += 1000
            if score:
                matches.append((score, n, summary, writes))
        matches.sort(key=lambda row: (-row[0], row[1]))
        matches = matches[: args.limit]
        ctx.activate_tools([row[1] for row in matches])
        result = {
            "matches": [
                {
                    "name": n,
                    "summary": summary,
                    "family": n.split(".")[0],
                    "writes": writes,
                    "activated": True,
                }
                for _, n, summary, writes in matches
            ]
        }
        if not matches:
            result.update(
                families=sorted({n.split(".")[0] for n in view.effective}),
                suggestion="Use tools.load with an exact name from the tool index.",
            )
        return ToolResult(content=json.dumps(result))
    loaded, already, rejected = [], [], []
    for n in args.names:
        if n not in view.effective:
            rejected.append(
                {
                    "name": n,
                    "reason": "not granted to this run"
                    if n in ctx.configured_tool_names
                    else "unknown tool",
                }
            )
        elif n in ctx.active_tools:
            already.append(n)
        else:
            ctx.activate_tools([n])
            loaded.append(n)
    return ToolResult(
        content=json.dumps(
            {"loaded": loaded, "already_loaded": already, "rejected": rejected}
        ),
        error=not (loaded or already),
    )
