"""Operator configuration review and synthetic diagnostics."""

import asyncio
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic

from pydantic import JsonValue

from solakit.adapters.memory import OpenViking
from solakit.adapters.model import ModelAdapter, ModelError
from solakit.adapters.vision import VisionAdapter
from solakit.config import EffectiveProfile, load_config, resolve_profile
from solakit.engine.prompts import PromptInput, PromptTurn, assemble
from solakit.records import RunState, new_id, utc_now
from solakit.redaction import Redactor
from solakit.storage.evidence import canonical
from solakit.storage.store import Store
from solakit.tools.registry import load_registry


def explain_profile(profile: EffectiveProfile) -> dict[str, object]:
    registry = load_registry(profile.tool_modules)
    tools = registry.eligible(profile)
    return profile.explain() | {
        "visible_tools": sorted(tools),
        "schema_bytes": len(canonical(registry.visible(tools))),
    }


def config_diff(old: Path, new: Path) -> dict[str, object]:
    def grants(path: Path) -> dict[str, dict[str, list[str]]]:
        config = load_config(path)
        registry = load_registry(config.tool_modules)
        result = {}
        for name in ("default", *config.profiles):
            profile = resolve_profile(config, name, base=path.resolve().parent)
            result[name] = {
                "granted_tools": sorted(profile.granted_tools),
                "visible_tools": sorted(registry.eligible(profile)),
                "workspace_roots": sorted(map(str, profile.workspace_roots)),
            }
        return result

    before, after = grants(old), grants(new)
    return {
        name: {"before": before.get(name), "after": after.get(name)}
        for name in sorted(before.keys() | after.keys())
        if before.get(name) != after.get(name)
    }


async def live_check(profile: EffectiveProfile) -> dict[str, dict[str, str]]:
    redactor = Redactor()
    model = ModelAdapter(profile, Store(profile.data_dir), asyncio.Semaphore(1), redactor=redactor)
    memory = OpenViking(profile.memory, redactor)
    results: dict[str, dict[str, str]] = {}
    vision = (
        VisionAdapter(VisionAdapter.model_profile(profile), model.store, model.inference)
        if profile.vision
        else None
    )
    try:
        for name, endpoint in (("model", model), ("vision", vision)):
            if endpoint is None:
                results[name] = {"status": "disabled"}
                continue
            try:
                await endpoint.start()
                await endpoint.verify()
                results[name] = {"status": "available", "pin": endpoint.profile.model_pin}
            except ModelError as error:
                results[name] = {"status": "failed", "error": error.code.value}
            except Exception:
                results[name] = {"status": "failed", "error": "Check endpoint URL and credentials"}
        if profile.memory.enabled:
            try:
                await memory.start()
                verified = await memory.verify()
                results["memory"] = {"status": "available", "version": str(verified["version"])}
            except Exception:
                results["memory"] = {
                    "status": "failed",
                    "error": "Memory health/version check failed",
                }
        else:
            results["memory"] = {"status": "disabled"}
    finally:
        for name, adapter in (("memory", memory), ("vision", vision), ("model", model)):
            if adapter is None:
                continue
            try:
                await adapter.stop()
            except Exception:
                results[name] = {"status": "failed", "error": "Client cleanup failed"}
    return results


PROBE_TOOL: dict[str, JsonValue] = {
    "type": "function",
    "function": {
        "name": "lookup_code",
        "description": "Look up the code for a numbered locker.",
        "parameters": {
            "type": "object",
            "properties": {"locker": {"type": "integer"}},
            "required": ["locker"],
            "additionalProperties": False,
        },
    },
}


async def probe_model(model: ModelAdapter, kind: str) -> dict[str, str]:
    store, profile = model.store, model.profile
    tool = kind == "tool_round_trip"
    expected = "ready"
    question = (
        "What is the code for locker 3? You must call lookup_code with integer locker=3. "
        "Wait for the tool result before answering. Return the code verbatim."
        if tool
        else "Reply with exactly the word ready."
    )
    if kind == "repeated_prefix":
        expected = "SOLA-" + new_id()[:8]
        question = (
            f"The access code is {expected}.\n"
            + "This is an ordinary line in the diagnostic document.\n" * 64
            + "What was the access code? Reply with the code only."
        )
    session = store.create_session(title="Synthetic diagnostic")
    run = store.admit(session.id, request_id=new_id(), content=question, profile=profile.name)
    run = store.transition_run(run.id, RunState.RUNNING)
    frozen = PromptInput(
        input=store.messages(session.id)[0][1],
        at=utc_now(),
        tools=(PROBE_TOOL,) if tool else (),
        stream=not tool,
        enable_thinking=kind == "reasoning",
    )
    prompt = assemble(profile, frozen)
    started = monotonic()
    result = await model.complete(prompt, run, 1)
    metrics = {}
    if kind == "repeated_prefix" and not result.step.failure:
        if result.calls or result.step.content.strip() != expected:
            return {"status": "failed", "error": "Model did not recall the code from the prefix"}
        metrics["first_request"] = f"{monotonic() - started:.2f}s"
        started = monotonic()
        result = await model.complete(prompt, run, 2)
        metrics["repeat_request"] = f"{monotonic() - started:.2f}s"
    if tool and not result.step.failure:
        if not result.calls:
            return {
                "status": "failed",
                "error": "Model returned text without a structured tool call",
            }
        if (
            len(result.calls) != 1
            or result.calls[0].name != "lookup_code"
            or canonical(result.calls[0].arguments) != b'{"locker":3}'
        ):
            return {"status": "failed", "error": "Check the tool parser and typed arguments"}
        expected = "SOLA-" + new_id()[:8]
        continuation = (
            PromptTurn(
                source_id=result.step.id,
                session_id=session.id,
                role="assistant",
                content=result.step.content,
                tool_calls=result.step.tool_calls,
            ),
            PromptTurn(
                source_id=new_id(),
                session_id=session.id,
                role="tool",
                content=expected,
                tool_call_id=result.calls[0].provider_tool_call_id,
            ),
        )
        frozen = frozen.model_copy(update={"continuation": continuation})
        result = await model.complete(assemble(profile, frozen), run, 2)
    if result.step.failure:
        return {"status": "failed", "error": result.step.failure.code.value}
    if tool:
        if result.calls or not re.search(rf"\b{re.escape(expected)}\b", result.step.content):
            return {
                "status": "failed",
                "error": "Final answer did not use the synthetic tool result",
            }
    elif result.calls or result.step.content.strip() != expected:
        return {"status": "failed", "error": "Unexpected synthetic reply"}
    if kind == "reasoning" and not result.step.reasoning.strip():
        return {"status": "failed", "error": "Check the template's separate reasoning field"}
    return {"status": "passed", **metrics}


async def run_doctor(profile: EffectiveProfile, *, quick: bool) -> dict[str, dict[str, str]]:
    results = await live_check(profile)
    registry = load_registry(profile.tool_modules)
    visible = registry.visible(registry.eligible(profile))
    results["tool_schemas"] = {
        "status": "available",
        "detail": f"{len(visible)} tools, {len(canonical(visible))} schema bytes",
    }
    probes = ["streaming", "tool_round_trip"]
    if not quick:
        probes.extend(("reasoning", "repeated_prefix"))
    if results["model"]["status"] != "available":
        results.update(
            {kind: {"status": "skipped", "reason": "Model unavailable"} for kind in probes}
        )
        return results
    try:
        with TemporaryDirectory(prefix="sola-doctor-") as directory:
            scratch = profile.model_copy(
                update={
                    "data_dir": Path(directory),
                    "name": "doctor",
                    "soul_file": None,
                    "curated_memory_file": None,
                    "fragments": (),
                    "budgets": profile.budgets.model_copy(
                        update={
                            "output_tokens": min(profile.budgets.output_tokens, 512),
                        }
                    ),
                }
            )
            store = Store(scratch.data_dir)
            model = ModelAdapter(scratch, store, asyncio.Semaphore(1))
            try:
                await store.start()
                await model.start()
                for kind in probes:
                    try:
                        started = monotonic()
                        results[kind] = await probe_model(model, kind)
                        results[kind]["elapsed"] = f"{monotonic() - started:.2f}s"
                    except Exception:
                        results[kind] = {"status": "failed", "error": "Synthetic probe failed"}
            finally:
                try:
                    await model.stop()
                finally:
                    await store.stop()
    except Exception:
        results["probe_environment"] = {
            "status": "failed",
            "error": "Probe setup or cleanup failed",
        }
    return results
