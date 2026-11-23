"""The public CLI. Offline commands never construct the server or clients."""

import argparse
import json
import platform
from importlib.metadata import version
from pathlib import Path

from solakit.config import (
    DEFAULT_CONFIG,
    ConfigError,
    EffectiveProfile,
    load_config,
    resolve_profile,
)


def main() -> None:
    parser = argparse.ArgumentParser(prog="sola")
    parser.add_argument("--version", action="version", version=version("solakit"))
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="serve the runtime at the configured address")
    check = commands.add_parser(
        "check", help="validate local configuration without starting services"
    )
    for command in (serve, check):
        command.add_argument("--config", type=Path)
        command.add_argument("--profile", default="default")
    check.add_argument("--explain", action="store_true")
    check.add_argument("--live", action="store_true", help="contact model/memory health only")
    prompt = commands.add_parser("prompt", help="offline prompt inspection (private content)")
    prompt.add_argument("text", nargs="?")
    prompt.add_argument("--config", type=Path)
    prompt.add_argument("--profile", default="default")
    prompt.add_argument("--frozen", type=Path, help="PromptInput JSON with fixed IDs/time/recall")
    prompt.add_argument("--recall-file", type=Path)
    prompt.add_argument("--at", help="explicit UTC ISO timestamp")
    prompt.add_argument("--request-body", type=Path, help="recorded request artifact")
    prompt.add_argument("--manifest", type=Path, help="recorded manifest artifact")
    prompt.add_argument("--hashes", action="store_true")
    search_command = commands.add_parser("search", help="offline evidence search")
    search_command.add_argument("query")
    search_command.add_argument("--config", type=Path)
    search_command.add_argument("--data-dir", type=Path)
    search_command.add_argument("--limit", type=int, default=20)
    search_command.add_argument("--session", dest="session_id")
    args = parser.parse_args()
    if args.command == "serve":
        import uvicorn

        from solakit.api import create_app
        from solakit.runtime import compose

        def current_profile(admitted: EffectiveProfile) -> EffectiveProfile:
            return resolve_profile(
                load_config(args.config),
                admitted.name,
                base=(args.config or DEFAULT_CONFIG).resolve().parent,
                origin=admitted.origin,
                mode=admitted.mode,
            )

        try:
            config = load_config(args.config)
            profile = resolve_profile(
                config,
                args.profile,
                base=(args.config or DEFAULT_CONFIG).resolve().parent,
            )
        except ConfigError as error:
            parser.error(str(error))
        uvicorn.run(
            create_app(
                compose(profile, current_profile=current_profile),
                profile_names=tuple(config.profiles),
            ),
            host=profile.http.host,
            port=profile.http.port,
            workers=1,
            proxy_headers=False,
            access_log=False,
        )
    elif args.command == "check":
        report: dict[str, object] = {
            "phase": "api-cli-events",
            "python": platform.python_version(),
            "packages": {name: version(name) for name in ("solakit", "fastapi", "openai")},
            "runtime_configured": False,
        }
        live_results: dict[str, dict[str, str]] = {}
        try:
            profile = resolve_profile(
                load_config(args.config),
                args.profile,
                base=(args.config or DEFAULT_CONFIG).resolve().parent,
            )
        except ConfigError as error:
            report["configuration_error"] = str(error)
        else:
            report["runtime_configured"] = True
            if args.explain:
                report["effective_config"] = profile.explain()
            if args.live:
                import asyncio

                live_results = asyncio.run(live_check(profile))
                report["live"] = live_results
        report["verification"] = "Local validation only; no services started or contacted."
        if args.live:
            report["verification"] = (
                "Live health/model-ID checks only; no worker, scheduler or inference started."
            )
        print(json.dumps(report, indent=2))
        if args.live and (
            not report["runtime_configured"]
            or any(value.get("status") == "failed" for value in live_results.values())
        ):
            raise SystemExit(2)
        if not report["runtime_configured"] and (
            args.config is not None or DEFAULT_CONFIG.exists()
        ):
            raise SystemExit(2)
    elif args.command == "prompt":
        from datetime import datetime

        from solakit.prompts import PreparedPrompt, PromptInput, assemble, inspect_prompt
        from solakit.records import Message, new_id, utc_now

        try:
            if args.request_body or args.manifest:
                if not (args.request_body and args.manifest) or any(
                    value is not None
                    for value in (args.text, args.frozen, args.recall_file, args.at)
                ):
                    parser.error("recorded inspection requires only --request-body and --manifest")
                prepared = PreparedPrompt(
                    args.request_body.read_bytes(), args.manifest.read_bytes()
                )
            else:
                profile = resolve_profile(
                    load_config(args.config),
                    args.profile,
                    base=(args.config or DEFAULT_CONFIG).resolve().parent,
                )
                if args.frozen:
                    if any(value is not None for value in (args.text, args.recall_file, args.at)):
                        parser.error(
                            "--frozen cannot be combined with text, recall or time overrides"
                        )
                    frozen = PromptInput.model_validate_json(args.frozen.read_bytes())
                else:
                    if args.text is None:
                        parser.error("prompt requires text or --frozen")
                    frozen = PromptInput(
                        at=datetime.fromisoformat(args.at) if args.at else utc_now(),
                        input=Message(
                            origin=profile.origin,
                            session_id=new_id(),
                            run_id=new_id(),
                            role="user",
                            content=args.text,
                        ),
                        recall=args.recall_file.read_bytes().decode() if args.recall_file else None,
                    )
                prepared = assemble(profile, frozen)
            print(
                json.dumps(
                    inspect_prompt(prepared, hashes=args.hashes), ensure_ascii=False, indent=2
                )
            )
        except ValueError, OSError:
            parser.error("prompt input/configuration is invalid or unavailable")
    elif args.command == "search":
        from solakit.evidence import CorruptionError, StorageError
        from solakit.search import search

        try:
            root = args.data_dir
            if root is None:
                config = load_config(args.config)
                base = (args.config or DEFAULT_CONFIG).resolve().parent
                root = Path(config.data_dir).expanduser()
                if not root.is_absolute():
                    root = base / root
            results = search(
                root.resolve(), args.query, limit=args.limit, session_id=args.session_id
            )
        except (StorageError, ConfigError, OSError, ValueError) as error:
            status = "corrupt" if isinstance(error, CorruptionError) else "unavailable"
            print(json.dumps({"status": status, "error": str(error)}))
            raise SystemExit(2) from None
        print(json.dumps({"status": "available", "results": results}, ensure_ascii=False, indent=2))
    else:
        parser.error(f"{args.command} is not implemented yet; see its phase plan")


async def live_check(profile: EffectiveProfile) -> dict[str, dict[str, str]]:
    import asyncio

    from solakit.memory import OpenViking
    from solakit.model import ModelAdapter
    from solakit.redaction import Redactor
    from solakit.store import Store

    redactor = Redactor()
    model = ModelAdapter(profile, Store(profile.data_dir), asyncio.Semaphore(1), redactor=redactor)
    memory = OpenViking(profile.memory, redactor)
    results: dict[str, dict[str, str]] = {}
    try:
        try:
            await model.start()
            await model.verify()
            results["model"] = {"status": "available", "pin": profile.model_pin}
        except Exception:
            results["model"] = {"status": "failed", "error": "Model health/pin check failed"}
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
        for name, adapter in (("memory", memory), ("model", model)):
            try:
                await adapter.stop()
            except Exception:
                results[name] = {"status": "failed", "error": "Client cleanup failed"}
    return results
