# Solakit

## Project Structure & Module Organization

Solakit is a local personal-agent runtime targeting Python 3.15. Application code
lives in `src/solakit/`; packaged Lit components, JavaScript, and styles live in
`src/solakit/web/`. Tests and disposable fixtures belong in `tests/` and
`tests/fixtures/`. Developer probes and maintenance utilities live in `scripts/`,
reliability evaluations in `evals/`, and the public SDK example in
`examples/phase5_pack/`.

Start with `docs/plans/README.md`, then consult `docs/product.md` and
`docs/decisions.md`. Later confirmed decisions supersede earlier proposals.

## Build, Test, and Development Commands

Run commands from the repository root:

- `uv sync --locked`: install the locked environment.
- `uv run sola check --explain`: inspect installation and effective configuration.
- `uv run sola serve --config ~/.config/sola/config.toml`: start the local server.
- `uv run --locked pytest`: run deterministic tests.
- `uv run --locked ruff check .`: lint Python code.
- `uv run --locked ruff format --check .`: verify formatting; omit `--check` to format.
- `uv run --locked mypy`: run strict type checking.
- `uv run --locked python scripts/smoke_wheel.py`: verify wheel packaging and clean installation.

## Coding Style & Naming Conventions

Use four-space Python indentation, type annotations, `snake_case` functions and
modules, and `PascalCase` classes. Ruff sets a 100-character line length.
Keep the core plain async Python, FastAPI routes thin, and SDK types at adapter
boundaries. Reuse existing helpers and avoid speculative abstractions.
Preserve one process, one event loop, and one shared inference semaphore.

## Testing Guidelines

Use pytest with `test_*.py` files and `test_*` functions. Run focused checks with
`uv run --locked pytest tests/test_tools.py`. Cover changed behavior, failure
paths, and permission boundaries using disposable data. No numerical coverage
minimum is configured. Mocked checks do not establish live Qwen, oMLX, or
OpenViking verification. Record material validation and limitations under
`docs/validation/`.

## Commit & Pull Request Guidelines

Recent commits use short descriptive subjects such as `Split chat.js into modules`;
no fixed prefix convention is evident. Keep changes small and reviewable.
PR descriptions should explain the problem, resulting behavior, checks performed,
and limitations. Link relevant plans or issues; include screenshots for UI changes.
Update configuration examples, contracts, and decisions when affected.

## Security & Configuration

Start from `config.example.toml`; preserve operator configuration and identity
fragments. Never commit credentials or private prompt/evidence artifacts.
Keep JSONL evidence immutable, operational state in `state.db`, and rebuildable
search in `search.db`. Enforce tool permissions outside the model; autonomous
work must never create further wake-ups.
