"""Real-model evaluation harness: scenario schema, isolated runner, scoring.

Scenarios drive the real configured model through the real ``Execution`` loop.
Tools keep their real schemas and descriptions (what the model sees is exactly
production), but their implementations are replaced with deterministic fakes
returning canned results, unless a scenario explicitly asks for a real
read-only built-in operating on a throwaway workspace. Evaluation never uses
the operator's data directory, channels, jobs or (by default) memory.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import random
import re
import statistics
import subprocess
import tempfile
import time
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from solakit.core.compaction import request_bytes
from solakit.core.config import Config, PathsConfig
from solakit.core.schemas import Message, Origin, Session

BUILTIN_MODULES = (
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
)
PACK_MODULES = tuple(
    f"sola_packs.{name}"
    for name in (
        "journal",
        "letters",
        "email",
        "printer",
        "image",
        "calendar",
        "blog",
    )
)
DEFAULT_RESPONSE = "No results."
MATRIX_ALIASES = {
    "disclosure": "profile.tool_disclosure",
    "thinking": "profile.model.thinking",
    "result_format": "profile.tool_result_format",
    "aging": "profile.tool_result_aging",
    "max_tokens": "model.max_tokens",
    "concurrency": "model.concurrency",
}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FakeResponse(_Strict):
    """Canned output for a fake tool; first response whose ``match`` fits wins."""

    match: dict[str, Any] = Field(default_factory=dict)
    content: str
    error: bool = False


class ExpectedCall(_Strict):
    name: str
    args_subset: dict[str, Any] = Field(default_factory=dict)


class HistoryMessage(_Strict):
    role: Literal["user", "assistant"]
    content: str


class Scenario(_Strict):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    family: str = "general"
    description: str = ""
    profile: str = "interactive"
    prompt: str
    followups: list[str] = Field(default_factory=list)
    history: list[HistoryMessage] = Field(default_factory=list)
    filler_chars: int = Field(default=0, ge=0, le=500_000)
    tools: list[str] = Field(default_factory=list)
    real_tools: list[str] = Field(default_factory=list)
    files: dict[str, str] = Field(default_factory=dict)
    responses: dict[str, list[FakeResponse]] = Field(default_factory=dict)
    expect_first_tool: str | None = None
    expect_calls: list[ExpectedCall] = Field(default_factory=list)
    allow_tools: list[str] = Field(default_factory=list)
    forbid_tools: list[str] = Field(default_factory=list)
    expect_no_tools: bool = False
    expect_status: str = "completed"
    expect_final_contains: list[str] = Field(default_factory=list)
    expect_final_any: list[str] = Field(default_factory=list)
    max_steps: int = Field(default=8, ge=1, le=200)
    quick: bool = False
    overrides: dict[str, Any] = Field(default_factory=dict)


def load_scenarios(
    directory: Path,
    *,
    only: list[str] | None = None,
    families: list[str] | None = None,
    quick: bool = False,
) -> list[Scenario]:
    scenarios = []
    for path in sorted(Path(directory).glob("*.toml")):
        try:
            scenario = Scenario.model_validate(tomllib.loads(path.read_text()))
        except (ValueError, tomllib.TOMLDecodeError) as exc:
            raise ValueError(f"Invalid scenario {path.name}: {exc}") from None
        if path.stem != scenario.id:
            raise ValueError(f"Scenario file {path.name} must be named {scenario.id}")
        if only and scenario.id not in only:
            continue
        if families and scenario.family not in families:
            continue
        if quick and not scenario.quick:
            continue
        scenarios.append(scenario)
    ids = [s.id for s in scenarios]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate scenario ids")
    return scenarios


def available_modules() -> list[str]:
    """Every built-in and installed pack module that imports cleanly."""
    import importlib

    from solakit.tools.distributions import installed

    result = []
    for module in BUILTIN_MODULES:
        try:
            importlib.import_module(f"solakit.tools.{module}")
            result.append(module)
        except ImportError:
            continue
    for module in PACK_MODULES:
        try:
            installed(module)
            importlib.import_module(module)
            result.append(module)
        except (ImportError, ValueError):
            continue
    return result


def _value(text: str):
    try:
        return json.loads(text)
    except ValueError:
        return text


def parse_matrix(entries: list[str]) -> list[dict[str, Any]]:
    """``key=a,b`` axes → cross-product of override dicts (dotted keys)."""
    cells: list[dict[str, Any]] = [{}]
    for entry in entries:
        key, sep, values = entry.partition("=")
        if not sep or not key or not values:
            raise ValueError(f"Matrix axis must look like key=a,b: {entry}")
        key = MATRIX_ALIASES.get(key, key)
        cells = [
            {**cell, key: _value(value.strip())}
            for cell in cells
            for value in values.split(",")
        ]
    return cells


def _set_path(target: dict, dotted: str, value) -> None:
    parts = dotted.split(".")
    for part in parts[:-1]:
        target = target.setdefault(part, {})
        if not isinstance(target, dict):
            raise ValueError(f"Cannot override {dotted}")
    target[parts[-1]] = value


def eval_config(
    base: Config,
    scenario: Scenario,
    workspace: Path,
    data: Path,
    overrides: dict[str, Any] | None = None,
    *,
    allow_memory: bool = False,
) -> Config:
    """An isolated config: temp data, one profile, no channels/jobs/memory."""
    profile_name = (
        scenario.profile if scenario.profile in base.profiles else "interactive"
    )
    profile = (
        base.profiles[profile_name].model_dump(exclude_unset=False)
        if profile_name in base.profiles
        else {}
    )
    profile.update(
        tools=list(dict.fromkeys(scenario.tools + scenario.real_tools)),
        read="workspace",
        write="workspace",
        workspace=[f"path:{workspace}"],
        write_workspace=[f"path:{workspace}"],
        effects=["push", "send", "print", "shell", "operate"],
        resources=[],
        max_steps=scenario.max_steps,
    )
    profile.pop("tool_resident", None)
    values = base.model_dump(
        exclude={"channels", "jobs", "profiles", "profile_defaults", "channel_defaults"}
    )
    values["profiles"] = {profile_name: profile}
    values["channels"] = []
    values["jobs"] = []
    values["delegation"] = {"allowed_profiles": [], "timeout_s": 900}
    values["enabled_tools"] = available_modules()
    values["server"] = {**values["server"], "cwd": str(workspace)}
    values["paths"] = {
        "data": str(data),
        "soul": str(base.paths.soul),
        "fragments": str(base.paths.fragments),
    }
    if not allow_memory:
        values["memory"] = {**values["memory"], "enabled": False}
    for key, value in {**scenario.overrides, **(overrides or {})}.items():
        if key.startswith("profile."):
            _set_path(values["profiles"][profile_name], key[8:], value)
        else:
            _set_path(values, key, value)
    config = Config.model_validate(values)
    # Secret references and paths were already resolved by the operator's load.
    config.model.api_key = base.model.api_key
    config.memory.api_key = base.memory.api_key
    config.tool_apis = base.tool_apis
    config.paths = PathsConfig(
        data=data, soul=base.paths.soul, fragments=base.paths.fragments
    )
    return config


FILLER_TOPICS = (
    "garden irrigation schedule",
    "bike maintenance log",
    "sourdough hydration notes",
    "quarterly budget categories",
    "reading list for winter",
    "home network layout",
)


def filler_history(chars: int) -> list[dict]:
    """Deterministic, unremarkable prior conversation of about ``chars`` chars."""
    result, total, index = [], 0, 0
    while total < chars:
        topic = FILLER_TOPICS[index % len(FILLER_TOPICS)]
        user = f"Note {index}: let's talk about my {topic}. " + (
            f"Detail {index} is routine and needs no action. " * 12
        )
        reply = f"Noted about the {topic} (item {index}). " + (
            "Nothing here needs follow-up. " * 12
        )
        result += [
            {"role": "user", "content": user},
            {"role": "assistant", "content": reply},
        ]
        total += len(user) + len(reply)
        index += 1
    return result


def _matches(expected: dict, actual: dict | None) -> bool:
    if not isinstance(actual, dict):
        return False
    for key, value in expected.items():
        if key not in actual:
            return False
        got = actual[key]
        if isinstance(value, str) and isinstance(got, str):
            if value.casefold() not in got.casefold():
                return False
        elif value != got:
            return False
    return True


def install_fakes(registry, scenario: Scenario, calls: list) -> None:
    """Replace implementations with canned results; schemas stay byte-identical."""
    from solakit.tools import ToolError, ToolResult

    real = set(scenario.real_tools)
    for name in dict.fromkeys(scenario.tools):
        if name in real:
            continue
        registered = registry.tools.get(name)
        if registered is None:
            raise ValueError(f"Scenario {scenario.id}: unknown tool {name}")
        original = registered.function
        responses = scenario.responses.get(name, [])

        def respond(values, name=name, responses=responses):
            calls.append({"name": name, "args": values})
            for response in responses:
                if _matches(response.match, values):
                    if response.error:
                        raise ToolError(response.content)
                    return ToolResult(content=response.content)
            return ToolResult(content=DEFAULT_RESPONSE)

        if inspect.iscoroutinefunction(original):

            async def fake(ctx, _respond=respond, **values):
                return _respond(values)
        else:

            def fake(ctx, _respond=respond, **values):
                return _respond(values)

        fake.__doc__ = original.__doc__
        fake.__sola_tool__ = registered.manifest
        registered.function = fake


FIRST_TOKEN = re.compile(
    rb'"(?:content|reasoning_content|reasoning)":\s*"[^"]|"tool_calls":\s*\['
)


class TimingTransport(httpx.AsyncBaseTransport):
    """Record per-request time to first response byte and total time."""

    def __init__(self):
        self.inner = httpx.AsyncHTTPTransport()
        self.samples: list[dict] = []

    async def handle_async_request(self, request):
        started = time.monotonic()
        response = await self.inner.handle_async_request(request)
        if not request.url.path.endswith("chat/completions"):
            return response
        sample = {"started": started, "ttfb": None, "seconds": None}
        self.samples.append(sample)
        inner_stream = response.stream

        class Timed(httpx.AsyncByteStream):
            async def __aiter__(self):
                async for chunk in inner_stream:
                    # First generated token, not headers or empty keepalives.
                    if sample["ttfb"] is None and FIRST_TOKEN.search(chunk):
                        sample["ttfb"] = time.monotonic() - started
                    yield chunk
                sample["seconds"] = time.monotonic() - started

            async def aclose(self):
                await inner_stream.aclose()

        return httpx.Response(
            response.status_code,
            headers=response.headers,
            stream=Timed(),
            request=request,
            extensions=response.extensions,
        )

    async def aclose(self):
        await self.inner.aclose()


def _instrument(model, config: Config, samples: list) -> None:
    """Record request bytes, usage and finish reason for every completion."""
    complete = model.complete

    async def measured(messages, tools, **kwargs):
        size = request_bytes(
            config.model.model_id, messages, tools, config.model.max_tokens
        )
        data = await complete(messages, tools, **kwargs)
        usage = data.get("usage") if isinstance(data, dict) else None
        samples.append(
            {
                "request_bytes": size,
                "prompt_tokens": (usage or {}).get("prompt_tokens"),
                "completion_tokens": (usage or {}).get("completion_tokens"),
                "finish_reason": data["choices"][0].get("finish_reason"),
            }
        )
        return data

    model.complete = measured


def _final_reply(messages: list[dict]) -> str | None:
    for message in reversed(messages):
        if message["role"] == "assistant" and not message.get("tool_calls"):
            return message.get("content")
    return None


def score(scenario: Scenario, run: dict, calls: list[dict]) -> dict:
    """Score one run (one user turn) of a scenario from its evidence."""
    messages = [m for m in run["messages"] if m.get("kind", "message") == "message"]
    called = [
        c["function"]["name"]
        for m in messages
        if m["role"] == "assistant"
        for c in (m.get("tool_calls") or [])
    ]
    logs = run.get("tools") or []
    executed = [
        log
        for log in logs
        if log["name"] not in ("tools.search", "tools.load")
        and not (log["result"].get("error") and log["result"].get("repairable"))
    ]
    repairs = sum(
        1
        for log in logs
        if log["result"].get("error") and log["result"].get("repairable")
    )
    work_calls = [n for n in called if n not in ("tools.search", "tools.load")]
    expected_names = (
        {c.name for c in scenario.expect_calls}
        | set(scenario.allow_tools)
        | ({scenario.expect_first_tool} if scenario.expect_first_tool else set())
    )
    spurious = (
        len(work_calls)
        if scenario.expect_no_tools
        else sum(1 for n in work_calls if n not in expected_names)
    )
    forbidden = sum(1 for n in work_calls if n in scenario.forbid_tools)
    first = work_calls[0] if work_calls else None
    final = _final_reply(messages) or ""
    status = run["status"]
    failures = []
    if status != scenario.expect_status:
        failures.append(f"status {status}")
    if scenario.expect_first_tool and first != scenario.expect_first_tool:
        failures.append(f"first tool {first}")
    for expected in scenario.expect_calls:
        if not any(
            log["name"] == expected.name
            and _matches(expected.args_subset, log.get("validated_args"))
            for log in executed
        ):
            failures.append(f"missing call {expected.name}")
    if forbidden:
        failures.append("forbidden call")
    if scenario.expect_no_tools and work_calls:
        failures.append("unexpected tool use")
    for text in scenario.expect_final_contains:
        if text.casefold() not in final.casefold():
            failures.append(f"final lacks {text!r}")
    if scenario.expect_final_any and not any(
        text.casefold() in final.casefold() for text in scenario.expect_final_any
    ):
        failures.append("final lacks any expected text")
    error = run.get("error") or ""
    return {
        "passed": not failures,
        "failures": failures,
        "status": status,
        "error": run.get("error"),
        "first_tool": first,
        "first_tool_ok": (
            None
            if scenario.expect_first_tool is None
            else first == scenario.expect_first_tool
        ),
        "tool_calls": len(work_calls),
        "valid_calls": len(work_calls) - repairs if work_calls else 0,
        "repairs": repairs,
        "spurious": spurious,
        "forbidden": forbidden,
        "steps": run.get("steps", 0),
        "length_stop": status == "budget_exceeded" and "max_tokens" in error,
        "empty_reply": status == "completed" and not final.strip(),
        "tokens": run.get("tokens"),
        "final": final[:2000],
        "calls": [{"name": log["name"], "args": log.get("args")} for log in logs[:40]],
    }


async def run_trial(
    base: Config,
    scenario: Scenario,
    trial: int,
    overrides: dict | None = None,
    *,
    allow_memory: bool = False,
    transport=None,
) -> dict:
    """One isolated execution of a scenario (all turns) against the model."""
    from solakit.core.loop import Execution
    from solakit.core.model import ModelClient
    from solakit.core.store import Store
    from solakit.tools import Registry

    with tempfile.TemporaryDirectory(prefix="sola-eval-") as temp:
        root = Path(temp)
        workspace, data = root / "workspace", root / "data"
        workspace.mkdir()
        for relative, content in scenario.files.items():
            target = (workspace / relative).resolve()
            if not target.is_relative_to(workspace.resolve()):
                raise ValueError("Scenario files must stay inside the workspace")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        config = eval_config(
            base, scenario, workspace, data, overrides, allow_memory=allow_memory
        )
        store = Store(data)
        registry = Registry(config)
        fake_calls: list[dict] = []
        install_fakes(registry, scenario, fake_calls)
        timing = transport or TimingTransport()
        model = ModelClient(config.model, transport=timing)
        samples: list[dict] = []
        _instrument(model, config, samples)
        memory = None
        if allow_memory and config.memory.enabled:
            from solakit.core.memory import MemoryClient

            key = config.memory.api_key.resolve() if config.memory.api_key else None
            memory = MemoryClient(config.memory, key)
        execution = Execution(config, store, registry, model, memory)
        session = Session(id=f"eval-{scenario.id}-{trial}")
        store.create_session(session)
        for item in scenario.history:
            store.append_message(
                session.id,
                Message(
                    role=item.role,
                    content=item.content,
                    origin=Origin(channel="eval"),
                    run_id="eval-history",
                ),
            )
        for item in filler_history(scenario.filler_chars):
            store.append_message(
                session.id,
                Message(**item, origin=Origin(channel="eval"), run_id="eval-history"),
            )
        turns = []
        started = time.monotonic()
        try:
            for index, text in enumerate([scenario.prompt, *scenario.followups]):
                before = len(getattr(timing, "samples", []))
                turn_started = time.monotonic()
                run = await execution.submit(
                    session.id, text, origin=Origin(channel="eval")
                )
                record = store.run(run.id)
                result = score(scenario, record, fake_calls)
                timed = getattr(timing, "samples", [])[before:]
                result["seconds"] = round(time.monotonic() - turn_started, 3)
                result["ttfb_first"] = (
                    round(timed[0]["ttfb"], 3) if timed and timed[0]["ttfb"] else None
                )
                result["turn"] = index
                result["run_id"] = run.id
                turns.append(result)
                # Later turns score only the final expectations of the last turn.
        finally:
            await execution.close()
            store.close()
        last = turns[-1]
        return {
            "scenario": scenario.id,
            "family": scenario.family,
            "trial": trial,
            "overrides": overrides or {},
            **{k: v for k, v in last.items() if k not in ("turn",)},
            "turns": turns,
            "total_seconds": round(time.monotonic() - started, 3),
            "requests": samples,
        }


def _mean(values):
    values = [v for v in values if v is not None]
    return round(statistics.fmean(values), 4) if values else None


def summarize(trials: list[dict]) -> dict:
    calls = sum(t["tool_calls"] for t in trials)
    ratios = [
        r["request_bytes"] / r["prompt_tokens"]
        for t in trials
        for r in t.get("requests", [])
        if r.get("prompt_tokens")
    ]
    first = [t["first_tool_ok"] for t in trials if t["first_tool_ok"] is not None]
    second_turn = [
        t["turns"][1]["ttfb_first"] / t["turns"][0]["ttfb_first"]
        for t in trials
        if len(t["turns"]) > 1
        and t["turns"][0]["ttfb_first"]
        and t["turns"][1]["ttfb_first"]
    ]
    return {
        "trials": len(trials),
        "pass_rate": _mean([float(t["passed"]) for t in trials]),
        "first_tool_accuracy": _mean([float(v) for v in first]),
        "arg_valid_rate": (
            round(sum(t["valid_calls"] for t in trials) / calls, 4) if calls else None
        ),
        "repairs_per_run": _mean([t["repairs"] for t in trials]),
        "steps_to_finish": _mean([t["steps"] for t in trials]),
        "spurious_call_rate": (
            round(sum(t["spurious"] for t in trials) / calls, 4) if calls else 0.0
        ),
        "forbidden_call_rate": (
            round(sum(t["forbidden"] for t in trials) / calls, 4) if calls else 0.0
        ),
        "length_stop_rate": _mean([float(t["length_stop"]) for t in trials]),
        "empty_reply_rate": _mean([float(t["empty_reply"]) for t in trials]),
        "prompt_tokens": _mean(
            [(t.get("tokens") or {}).get("prompt_tokens") for t in trials]
        ),
        "completion_tokens": _mean(
            [(t.get("tokens") or {}).get("completion_tokens") for t in trials]
        ),
        "seconds": _mean([t["total_seconds"] for t in trials]),
        "bytes_per_token": _mean(ratios),
        "second_turn_ttfb_ratio": _mean(second_turn),
    }


def report(trials: list[dict]) -> dict:
    by_cell: dict[str, list[dict]] = {}
    for trial in trials:
        cell = json.dumps(trial["overrides"], sort_keys=True)
        by_cell.setdefault(cell, []).append(trial)
    cells = {}
    for cell, items in by_cell.items():
        scenarios, families = {}, {}
        for item in items:
            scenarios.setdefault(item["scenario"], []).append(item)
            families.setdefault(item["family"], []).append(item)
        cells[cell] = {
            "overall": summarize(items),
            "families": {k: summarize(v) for k, v in sorted(families.items())},
            "scenarios": {k: summarize(v) for k, v in sorted(scenarios.items())},
        }
    return cells


def provenance(config: Config) -> dict:
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=Path(__file__).parent,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        sha = ""
    model = config.model.model_dump(exclude={"api_key"}, mode="json")
    config_hash = hashlib.sha256(
        json.dumps(
            {
                "model": model,
                "profiles": config.model_dump(mode="json", include={"profiles"}),
            },
            sort_keys=True,
            default=str,
        ).encode()
    ).hexdigest()[:12]
    return {
        "git_sha": sha or "unknown",
        "config_hash": config_hash,
        "model": model,
        "created_at": datetime.now(UTC).isoformat(),
    }


async def run_suite(
    base: Config,
    scenarios: list[Scenario],
    *,
    trials: int = 5,
    matrix: list[dict] | None = None,
    allow_memory: bool = False,
    progress=None,
    transport_factory=None,
) -> dict:
    meta = provenance(base)
    results = []
    for cell in matrix or [{}]:
        for scenario in scenarios:
            for trial in range(trials):
                try:
                    result = await run_trial(
                        base,
                        scenario,
                        trial,
                        cell,
                        allow_memory=allow_memory,
                        transport=transport_factory() if transport_factory else None,
                    )
                except Exception as exc:  # A harness fault is a failed trial.
                    result = {
                        "scenario": scenario.id,
                        "family": scenario.family,
                        "trial": trial,
                        "overrides": cell,
                        "passed": False,
                        "failures": [f"harness: {type(exc).__name__}: {exc}"],
                        "status": "harness_error",
                        "first_tool": None,
                        "first_tool_ok": False if scenario.expect_first_tool else None,
                        "tool_calls": 0,
                        "valid_calls": 0,
                        "repairs": 0,
                        "spurious": 0,
                        "forbidden": 0,
                        "steps": 0,
                        "length_stop": False,
                        "empty_reply": False,
                        "tokens": None,
                        "turns": [],
                        "total_seconds": 0.0,
                        "requests": [],
                    }
                results.append(result)
                if progress:
                    progress(result)
    return {
        "provenance": meta,
        "trials_per_scenario": trials,
        "summary": report(results),
        "trials": results,
    }


def markdown(result: dict) -> str:
    lines = []
    columns = (
        "pass_rate",
        "first_tool_accuracy",
        "arg_valid_rate",
        "repairs_per_run",
        "steps_to_finish",
        "spurious_call_rate",
        "forbidden_call_rate",
        "length_stop_rate",
        "prompt_tokens",
        "seconds",
    )
    for cell, data in result["summary"].items():
        lines.append(f"### Cell {cell}")
        lines.append("")
        lines.append("| scenario | " + " | ".join(columns) + " |")
        lines.append("|---|" + "---:|" * len(columns))
        rows = [*data["scenarios"].items(), ("**overall**", data["overall"])]
        for name, stats in rows:
            values = ["—" if stats.get(c) is None else f"{stats[c]:g}" for c in columns]
            lines.append(f"| {name} | " + " | ".join(values) + " |")
        lines.append("")
    lines.append(
        f"N={result['trials_per_scenario']} trials per scenario: only large "
        "differences are meaningful."
    )
    return "\n".join(lines)


def _bootstrap(a: list[float], b: list[float], rounds: int = 2000, seed: int = 7):
    rng = random.Random(seed)
    deltas = sorted(
        statistics.fmean(rng.choices(b, k=len(b)))
        - statistics.fmean(rng.choices(a, k=len(a)))
        for _ in range(rounds)
    )
    return deltas[int(rounds * 0.025)], deltas[int(rounds * 0.975)]


def compare(a: dict, b: dict) -> str:
    """Per-scenario pass-rate deltas with a 95% bootstrap interval."""

    def passes(result):
        table: dict[str, list[float]] = {}
        for trial in result["trials"]:
            key = trial["scenario"]
            if trial["overrides"]:
                key += " " + json.dumps(trial["overrides"], sort_keys=True)
            table.setdefault(key, []).append(float(trial["passed"]))
        return table

    left, right = passes(a), passes(b)
    lines = ["| scenario | A | B | Δ | 95% CI |", "|---|---:|---:|---:|---|"]
    every_a, every_b = [], []
    for key in sorted(set(left) & set(right)):
        x, y = left[key], right[key]
        every_a += x
        every_b += y
        low, high = _bootstrap(x, y)
        delta = statistics.fmean(y) - statistics.fmean(x)
        flag = " *" if low > 0 or high < 0 else ""
        lines.append(
            f"| {key} | {statistics.fmean(x):.2f} | {statistics.fmean(y):.2f} | "
            f"{delta:+.2f}{flag} | [{low:+.2f}, {high:+.2f}] |"
        )
    if every_a and every_b:
        low, high = _bootstrap(every_a, every_b)
        lines.append(
            f"| **overall** | {statistics.fmean(every_a):.2f} | "
            f"{statistics.fmean(every_b):.2f} | "
            f"{statistics.fmean(every_b) - statistics.fmean(every_a):+.2f} | "
            f"[{low:+.2f}, {high:+.2f}] |"
        )
    only = sorted(set(left) ^ set(right))
    if only:
        lines.append("")
        lines.append("Not in both: " + ", ".join(only))
    lines.append("")
    lines.append(
        "* marks intervals excluding zero. Small trial counts make only large "
        "deltas trustworthy."
    )
    return "\n".join(lines)


def result_path(directory: Path, result: dict) -> Path:
    meta = result["provenance"]
    stamp = re.sub(r"[^0-9T]", "", meta["created_at"][:19])
    return Path(directory) / f"{stamp}-{meta['git_sha']}-{meta['config_hash']}.json"
