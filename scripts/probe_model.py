"""Probe the pinned oMLX model using disposable v2 evidence, never operator state."""

import argparse
import asyncio
import base64
import json
import os
import shlex
import struct
import zlib
from pathlib import Path

from pydantic import JsonValue

from solakit.config import OperatorConfig, load_config, resolve_profile
from solakit.engine.prompts import PreparedPrompt, PromptInput, assemble
from solakit.records import RunState, new_id, utc_now
from solakit.runtime import compose
from solakit.storage.evidence import canonical, digest, publish


def load_key(path: Path, name: str) -> None:
    """Only literal KEY=value lines; never execute a dotenv file as shell code."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        key, separator, value = line.removeprefix("export ").partition("=")
        if separator and key.strip() == name:
            parts = shlex.split(value, comments=True)
            if len(parts) != 1:
                raise ValueError("credential must be a single literal dotenv value")
            os.environ[name] = parts[0]
            return


def image_probe(prompt: PreparedPrompt) -> PreparedPrompt:
    """Synthetic capability experiment; production attachment admission is Phase 12."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 64, 64, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress((b"\0" + b"\xff\0\0" * 64) * 64)) + chunk(b"IEND", b"")
    body = json.loads(prompt.body)
    body["messages"][-1]["content"] = [
        {"type": "text", "text": body["messages"][-1]["content"]},
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode()},
        },
    ]
    raw = canonical(body)
    manifest = json.loads(prompt.manifest)
    manifest.update(body_sha256=digest(raw), capability_probe="synthetic-red-png-v1")
    manifest["accounting"]["kind"] = "unverified image accounting; capability probe only"
    return PreparedPrompt(raw, canonical(manifest))


async def probe(args: argparse.Namespace) -> None:
    args.output = args.output.resolve()
    config = load_config(args.config)
    load_key(args.env_file, config.model.api_key_env or "OPENAI_API_KEY")
    args.output.mkdir(parents=True, mode=0o700, exist_ok=False)
    config = OperatorConfig.model_validate(
        config.model_dump() | {"data_dir": str(args.output / "data")}
    )
    profile = resolve_profile(config, base=args.config.resolve().parent)
    context = compose(profile)
    report: dict[str, object] = {
        "pin": profile.model_pin,
        "at": utc_now().isoformat(),
        "live_gate": "pending",
        "image_gate": "pending",
        "weight_identity": "unverified; API identity alone is not proof",
        "cases": [],
    }
    cases: list[dict[str, object]] = []
    try:
        await context.start()
        assert context.model is not None and context.store is not None
        report["identity"] = await context.model.verify()
        tools: tuple[dict[str, JsonValue], ...] = (
            {
                "type": "function",
                "function": {
                    "name": "probe_echo",
                    "description": "Return the supplied text (probe only).",
                    "parameters": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                        "additionalProperties": False,
                    },
                },
            },
        )
        for stream in (False, True):
            for kind in ("text", "tools", "reasoning", "image"):
                with_tools = kind == "tools"
                text = (
                    'Call probe_echo with text "café 🐈". Do not answer with prose.'
                    if with_tools
                    else "Reply with exactly: café 🐈"
                )
                if kind == "image":
                    text = (
                        "What color is the square in the attached image? "
                        "Reply with one lowercase word."
                    )
                session = context.store.create_session(title="Disposable Phase 4 probe")
                run = context.store.admit(
                    session.id, request_id=new_id(), content=text, profile=profile.name
                )
                run = context.store.transition_run(run.id, RunState.RUNNING)
                message = context.store.messages(session.id)[0][1]
                frozen = PromptInput(
                    at=utc_now(),
                    input=message,
                    tools=tools if with_tools else (),
                    stream=stream,
                    enable_thinking=kind == "reasoning",
                )
                prepared = assemble(profile, frozen)
                if kind == "image":
                    prepared = image_probe(prepared)
                result = await context.model.complete(prepared, run, 1)
                expected = "red" if kind == "image" else "café 🐈"
                valid = result.step.failure is None and (
                    (
                        len(result.calls) == 1
                        and result.calls[0].name == "probe_echo"
                        and result.calls[0].arguments == {"text": "café 🐈"}
                    )
                    if with_tools
                    else result.step.content.strip() == expected
                )
                if kind == "reasoning":
                    valid = valid and bool(result.step.reasoning.strip())
                cases.append(
                    {
                        "stream": stream,
                        "tools": with_tools,
                        "kind": kind,
                        "passed": valid,
                        "step": result.step.model_dump(mode="json"),
                        "calls": [call.model_dump(mode="json") for call in result.calls],
                    }
                )
                context.store.transition_run(
                    run.id,
                    RunState.COMPLETED if valid else RunState.FAILED,
                    reason=result.step.failure,
                )
        report["live_gate"] = (
            "passed"
            if all(case["passed"] for case in cases if case["kind"] != "image")
            else "failed"
        )
        report["image_gate"] = (
            "passed"
            if all(case["passed"] for case in cases if case["kind"] == "image")
            else "failed"
        )
    except Exception as error:
        # SDK error strings may include private responses or transport credentials.
        report["error_type"] = type(error).__name__
    finally:
        await context.stop()
        report["cases"] = cases
        publish(args.output / "report.json", canonical(report))
    print(
        json.dumps({"report": str(args.output / "report.json"), "live_gate": report["live_gate"]})
    )
    if report["live_gate"] != "passed":
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.example.toml"))
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(probe(parser.parse_args()))


if __name__ == "__main__":
    main()
