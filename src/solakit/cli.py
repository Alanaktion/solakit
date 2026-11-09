"""Operator CLI: authenticated API inspection and offline local-index search."""

import argparse
import json
import os
import sqlite3
import sys

import httpx

from solakit.core.config import ServerConfig, load_config
from solakit.core.startup import ConfigurationError, prepare


def main() -> None:
    parser = argparse.ArgumentParser(prog="sola")
    parser.add_argument(
        "--url", default=os.environ.get("SOLA_URL", "http://127.0.0.1:8382")
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
    prompt.add_argument("--job")
    prompt.add_argument("--run-id")
    prompt.add_argument("--step", type=int)
    search = commands.add_parser(
        "search", help="Search the local message index offline"
    )
    search.add_argument("query")
    for field in ("session-id", "role", "kind", "after", "before"):
        search.add_argument("--" + field)
    search.add_argument("--limit", type=int, default=50)
    search.add_argument("--json", action="store_true")
    commands.add_parser(
        "check", help="Validate config, secrets, prompt files and tool grants offline"
    )
    memory = commands.add_parser(
        "memory-test", help="Explicit live scratch memory round-trip"
    )
    memory.add_argument("--fact", required=True)
    memory.add_argument("--query", required=True)
    memory.add_argument("--expected", required=True)
    evaluate = commands.add_parser(
        "eval", help="Run real-model evaluation scenarios in an isolated data dir"
    )
    evaluate.add_argument("--scenarios", default="evals/scenarios")
    evaluate.add_argument("--only", help="Comma-separated scenario ids")
    evaluate.add_argument("--family", help="Comma-separated scenario families")
    evaluate.add_argument("--trials", type=int)
    evaluate.add_argument(
        "--quick", action="store_true", help="Flagged subset with 2 trials"
    )
    evaluate.add_argument(
        "--matrix",
        action="append",
        default=[],
        help="Override axis key=a,b (e.g. disclosure=eager,deferred)",
    )
    evaluate.add_argument("--out", default="evals/results")
    evaluate.add_argument("--allow-memory", action="store_true")
    evaluate.add_argument("--json", action="store_true")
    evaluate_commands = evaluate.add_subparsers(dest="eval_command")
    compare = evaluate_commands.add_parser("compare", help="Compare two result files")
    compare.add_argument("a")
    compare.add_argument("b")
    serve = commands.add_parser("serve", help="Run the server (default 127.0.0.1:8382)")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument(
        "--log-level", choices=["debug", "info", "warning", "error", "critical"]
    )
    args = parser.parse_args()
    if args.command == "eval":
        run_eval(parser, args)
        return
    if args.command == "search":
        from solakit.core.search import SearchUnavailable, search_index
        from solakit.core.secrets import SENSITIVE, Redactor

        try:
            config = load_config()
        except (OSError, ValueError):
            parser.exit(
                1, "Search failed: cannot load configuration; check SOLA_CONFIG.\n"
            )
        try:
            values = [
                value for key, value in os.environ.items() if SENSITIVE.search(key)
            ]
            for reference in (
                config.operator_token,
                config.model.api_key,
                config.memory.api_key,
                config.discord_token,
            ):
                if reference is not None:
                    try:
                        values.append(reference.resolve())
                    except (OSError, ValueError):
                        pass  # Search does not require working integration credentials.
            results = search_index(
                config.paths.data,
                args.query,
                session_id=args.session_id,
                role=args.role,
                kind=args.kind,
                after=args.after,
                before=args.before,
                limit=args.limit,
                redactor=Redactor(values),
            )
        except (OSError, ValueError, SearchUnavailable) as exc:
            parser.exit(1, f"Search failed: {exc}\n")
        if args.json:
            print(json.dumps(results, ensure_ascii=False, indent=2))
        else:
            for hit in results:
                print(
                    f"{hit['session_id']}/{hit['archive']}:{hit['line_no']} "
                    f"{hit['role']} {hit['ts']} (rank {hit['rank']:.6g})"
                )
                print(hit["snippet"])
        return
    if args.command == "serve":
        try:
            prepared = prepare()
            defaults = prepared.config.server
            prepared.config.server = ServerConfig(
                host=args.host if args.host is not None else defaults.host,
                port=args.port if args.port is not None else defaults.port,
                log_level=args.log_level or defaults.log_level,
            )
        except (ConfigurationError, ValueError) as exc:
            if isinstance(exc, ConfigurationError):
                parser.exit(1, f"Configuration error: {exc}\n")
            parser.exit(1, "Invalid server host, port or log level.\n")
        import uvicorn

        from solakit.main import create_app

        server = prepared.config.server
        uvicorn.run(
            create_app(config=prepared.config),
            host=server.host,
            port=server.port,
            log_level=server.log_level,
            workers=1,
        )
        return
    if args.command == "check":
        from solakit.core.prompt import check_front_page

        try:
            prepared = prepare()
            warning = check_front_page(prepared.config)
            if warning:
                print(warning)
        except (ConfigurationError, OSError, ValueError) as exc:
            parser.exit(1, f"Configuration error: {exc}\n")
        from solakit.tools.distributions import description

        for provider in prepared.registry.distributions:
            print("Tool module " + description(provider))
        from solakit.jobs.definitions import inspect_definitions

        for name in prepared.config.profiles:
            print(prepared.registry.summary_access(name))
            diagnostics = prepared.registry.view(name).diagnostics(
                prepared.config.profiles[name].context_budget
            )
            print("Tool disclosure profile " + name + ": " + json.dumps(diagnostics))
        try:
            inspected_jobs = inspect_definitions(
                prepared.config.paths.data / "app.db", prepared.config.jobs
            )
        except (OSError, ValueError, sqlite3.Error):
            parser.exit(
                1, "Configuration error: cannot inspect stored job definitions\n"
            )
        for job in inspected_jobs:
            if job.profile not in prepared.config.profiles:
                print("Tool disclosure job " + job.id + ": unknown profile")
                continue
            view = prepared.registry.view(
                job.profile,
                job.tools,
                job.tool_disclosure,
                job.tool_resident,
                strict=False,
            )
            print(
                "Tool disclosure job "
                + job.id
                + ": "
                + json.dumps(
                    view.diagnostics(
                        prepared.config.profiles[job.profile].context_budget
                    )
                )
            )
        print("Configuration valid.")
        return
    try:
        token = load_config().operator_token.resolve()
        if args.command == "memory-test":
            response = httpx.post(
                f"{args.url.rstrip('/')}/api/memory/probe",
                json={
                    "fact": args.fact,
                    "query": args.query,
                    "expected": args.expected,
                },
                headers={"Authorization": f"Bearer {token}"},
                timeout=None,
                trust_env=args.trust_env,
            )
            response.raise_for_status()
            data = response.json()
            print(json.dumps(data, ensure_ascii=False, indent=2))
            if not data["passed"]:
                parser.exit(
                    1, "Memory verification did not pass; inspect its run evidence.\n"
                )
            return
        response = httpx.get(
            f"{args.url.rstrip('/')}/api/prompt",
            params={
                k: v
                for k, v in {
                    "profile": args.profile,
                    "session": args.session,
                    "job": args.job,
                    "run_id": args.run_id,
                    "step": args.step,
                }.items()
                if v is not None
            },
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


def run_eval(parser, args) -> None:
    import asyncio
    from pathlib import Path

    from solakit.core import eval as evaluation

    if args.eval_command == "compare":
        try:
            a = json.loads(Path(args.a).read_text())
            b = json.loads(Path(args.b).read_text())
        except (OSError, ValueError) as exc:
            parser.exit(1, f"Compare failed: {exc}\n")
        print(evaluation.compare(a, b))
        return
    try:
        config = load_config()
        scenarios = evaluation.load_scenarios(
            Path(args.scenarios),
            only=args.only.split(",") if args.only else None,
            families=args.family.split(",") if args.family else None,
            quick=args.quick,
        )
        matrix = evaluation.parse_matrix(args.matrix)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Evaluation setup failed: {exc}\n")
    if not scenarios:
        parser.exit(1, "No scenarios selected.\n")
    if config.memory.enabled and args.allow_memory:
        print("Warning: evaluation may write to the configured memory server.")
    trials = args.trials or (2 if args.quick else 5)

    def progress(result):
        mark = "PASS" if result["passed"] else "FAIL"
        reasons = "; ".join(result["failures"])
        print(
            f"{mark} {result['scenario']} #{result['trial']} "
            f"{json.dumps(result['overrides']) if result['overrides'] else ''}"
            f"{result['status']} steps={result['steps']} {reasons}",
            file=sys.stderr,
            flush=True,
        )

    result = asyncio.run(
        evaluation.run_suite(
            config,
            scenarios,
            trials=trials,
            matrix=matrix,
            allow_memory=args.allow_memory,
            progress=progress,
        )
    )
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    path = evaluation.result_path(out, result)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if args.json:
        print(json.dumps(result["summary"], indent=2))
    else:
        print(evaluation.markdown(result))
    print(f"Results: {path}")


if __name__ == "__main__":
    main()
