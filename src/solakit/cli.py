"""The public CLI. Offline commands never construct the server or clients."""

import argparse
import json
import platform
import signal
from contextlib import suppress
from importlib.metadata import version
from pathlib import Path
from types import FrameType

from solakit.config import (
    DEFAULT_CONFIG,
    ConfigError,
    EffectiveProfile,
    load_config,
    resolve_profile,
)


def selected_profile(path: Path | None, name: str) -> EffectiveProfile:
    return resolve_profile(load_config(path), name, base=(path or DEFAULT_CONFIG).resolve().parent)


def main() -> None:
    parser = argparse.ArgumentParser(prog="sola")
    parser.add_argument("--version", action="version", version=version("solakit"))
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="serve the runtime at the configured address")
    check = commands.add_parser(
        "check", help="validate local configuration without starting services"
    )
    doctor = commands.add_parser("doctor", help="run synthetic live model and service diagnostics")
    doctor.add_argument(
        "--quick", action="store_true", help="skip reasoning and repeated-prefix probes"
    )
    doctor.add_argument("--json", action="store_true", help="print machine-readable results")
    configuration = commands.add_parser("config", help="inspect effective configuration offline")
    actions = configuration.add_subparsers(dest="action", required=True)
    explain = actions.add_parser("explain", help="show effective profile and visible schema size")
    explain.add_argument("profile", nargs="?", default="default")
    explain.add_argument("--config", type=Path)
    difference = actions.add_parser("diff", help="compare effective grants across two TOML files")
    difference.add_argument("old", type=Path)
    difference.add_argument("new", type=Path)
    for command in (serve, check, doctor):
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

        class Server(uvicorn.Server):
            def handle_exit(self, sig: int, frame: FrameType | None) -> None:
                if self.should_exit:
                    # Uvicorn can still wait for active sockets after setting force_exit.
                    signal.signal(sig, signal.SIG_DFL)
                    signal.raise_signal(sig)
                else:
                    super().handle_exit(sig, frame)

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
        server_config = uvicorn.Config(
            create_app(
                compose(profile, current_profile=current_profile),
                profile_names=tuple(config.profiles),
            ),
            host=profile.http.host,
            port=profile.http.port,
            workers=1,
            proxy_headers=False,
            access_log=False,
            timeout_graceful_shutdown=profile.shutdown_timeout_seconds,
        )
        with suppress(KeyboardInterrupt):
            Server(server_config).run()
    elif args.command == "check":
        report: dict[str, object] = {
            "phase": "api-cli-events",
            "python": platform.python_version(),
            "packages": {name: version(name) for name in ("solakit", "fastapi", "openai")},
            "runtime_configured": False,
        }
        live_results: dict[str, dict[str, str]] = {}
        try:
            profile = selected_profile(args.config, args.profile)
        except ConfigError as error:
            report["configuration_error"] = str(error)
        else:
            report["runtime_configured"] = True
            if args.explain:
                report["effective_config"] = profile.explain()
            if args.live:
                import asyncio

                from solakit.diagnostics import live_check

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
    elif args.command == "config":
        from solakit.diagnostics import config_diff, explain_profile
        from solakit.tools.registry import RegistryError

        try:
            if args.action == "diff":
                details = config_diff(args.old, args.new)
            else:
                profile = selected_profile(args.config, args.profile)
                details = explain_profile(profile)
        except (ConfigError, RegistryError) as error:
            parser.error(str(error))
        print(json.dumps(details, indent=2))
    elif args.command == "doctor":
        import asyncio

        from solakit.diagnostics import run_doctor

        try:
            profile = selected_profile(args.config, args.profile)
        except ConfigError as error:
            parser.error(str(error))
        doctor_results = asyncio.run(run_doctor(profile, quick=args.quick))
        if args.json:
            print(json.dumps(doctor_results, indent=2))
        else:
            for name, result in doctor_results.items():
                detail = "  ".join(
                    f"{key}={value}" for key, value in result.items() if key != "status"
                )
                print(f"{result['status'].upper():9} {name}  {detail}".rstrip())
        if any(result["status"] == "failed" for result in doctor_results.values()):
            raise SystemExit(1)
    elif args.command == "prompt":
        from datetime import datetime

        from solakit.engine.prompts import PreparedPrompt, PromptInput, assemble, inspect_prompt
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
                profile = selected_profile(args.config, args.profile)
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
        from solakit.storage.evidence import CorruptionError, StorageError
        from solakit.storage.search import search

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
