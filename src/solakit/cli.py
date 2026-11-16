"""The public CLI. Offline commands never construct the server or clients."""

import argparse
import json
import platform
from importlib.metadata import version


def main() -> None:
    parser = argparse.ArgumentParser(prog="sola")
    parser.add_argument("--version", action="version", version=version("solakit"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="serve the localhost skeleton health endpoint")
    check = commands.add_parser("check", help="check the installed development skeleton")
    check.add_argument("--explain", action="store_true")
    commands.add_parser("prompt", help="offline prompt inspection (Phase 4)")
    commands.add_parser("search", help="offline evidence search (Phase 3)")
    args = parser.parse_args()
    if args.command == "serve":
        import uvicorn

        uvicorn.run("solakit.api:create_app", factory=True, host="127.0.0.1", port=8765, workers=1)
    elif args.command == "check":
        report = {
            "phase": "skeleton",
            "python": platform.python_version(),
            "packages": {name: version(name) for name in ("solakit", "fastapi", "openai")},
            "runtime_configured": False,
        }
        if args.explain:
            report["explanation"] = (
                "Installation check only; runtime configuration arrives in Phase 2."
            )
        print(json.dumps(report, indent=2))
    else:
        parser.error(f"{args.command} is not implemented yet; see its phase plan")
