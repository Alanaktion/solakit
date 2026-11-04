"""Thin operator CLI: prompt inspection is an authenticated API read."""

import argparse
import json
import os
import sys

import httpx

from solakit.core.config import load_config
from solakit.core.startup import ConfigurationError, prepare


def main() -> None:
    parser = argparse.ArgumentParser(prog="sola")
    parser.add_argument(
        "--url", default=os.environ.get("SOLA_URL", "http://127.0.0.1:8000")
    )
    parser.add_argument(
        "--trust-env",
        action="store_true",
        help="Honor proxy and certificate environment variables",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    prompt = commands.add_parser("prompt")
    prompt.add_argument("--profile", default="interactive")
    prompt.add_argument("--session", default="inspection")
    prompt.add_argument("--json", action="store_true")
    commands.add_parser(
        "check", help="Validate config, secrets, prompt files and tool grants offline"
    )
    args = parser.parse_args()
    if args.command == "check":
        try:
            prepare()
        except ConfigurationError as exc:
            parser.exit(1, f"Configuration error: {exc}\n")
        print("Configuration valid.")
        return
    try:
        token = load_config().operator_token.resolve()
        response = httpx.get(
            f"{args.url.rstrip('/')}/api/prompt",
            params={"profile": args.profile, "session": args.session},
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
            trust_env=args.trust_env,
        )
        response.raise_for_status()
        data = response.json()
    except (OSError, ValueError, httpx.HTTPError):
        parser.exit(
            1, "Prompt inspection failed; check configuration and API status.\n"
        )
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        # No print newline: stdout is exactly the prompt's UTF-8 bytes.
        sys.stdout.buffer.write(data["prompt"].encode("utf-8"))


if __name__ == "__main__":
    main()
