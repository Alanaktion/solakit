# Python package reorganization

Validated the working tree based on `4d8d73b` on macOS arm64 with Python 3.15.2.

| Check | Result |
| --- | --- |
| `uv run --locked pytest -q` | 710 passed |
| `uv run --locked ruff check .` and `uv run --locked ruff format --check .` | Passed |
| `uv run --locked mypy` | Passed |
| `uv run --locked python scripts/smoke_wheel.py` | Clean wheel installation, packaged resources, CLI and example pack dispatch passed |
| Fresh interpreter import of each application module | All 35 passed |
| Built-in registry schemas compared with the base commit | All 35 matched |

Validation used disposable local data. Live provider behavior was outside the scope of this refactor.
