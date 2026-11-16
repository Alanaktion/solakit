"""The public CLI. Offline commands never construct the server or clients."""

import argparse
import json
import platform
from importlib.metadata import version
from pathlib import Path

from solakit.config import DEFAULT_CONFIG, ConfigError, load_config, resolve_profile


def main() -> None:
    parser = argparse.ArgumentParser(prog="sola")
    parser.add_argument("--version", action="version", version=version("solakit"))
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="serve the configured localhost runtime")
    check = commands.add_parser(
        "check", help="validate local configuration without starting services"
    )
    for command in (serve, check):
        command.add_argument("--config", type=Path)
        command.add_argument("--profile", default="default")
    check.add_argument("--explain", action="store_true")
    commands.add_parser("prompt", help="offline prompt inspection (Phase 4)")
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

        try:
            profile = resolve_profile(
                load_config(args.config),
                args.profile,
                base=(args.config or DEFAULT_CONFIG).resolve().parent,
            )
        except ConfigError as error:
            parser.error(str(error))
        uvicorn.run(create_app(compose(profile)), host="127.0.0.1", port=8765, workers=1)
    elif args.command == "check":
        report: dict[str, object] = {
            "phase": "evidence-search",
            "python": platform.python_version(),
            "packages": {name: version(name) for name in ("solakit", "fastapi", "openai")},
            "runtime_configured": False,
        }
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
        report["verification"] = "Local validation only; no services started or contacted."
        print(json.dumps(report, indent=2))
        if not report["runtime_configured"] and (
            args.config is not None or DEFAULT_CONFIG.exists()
        ):
            raise SystemExit(2)
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
