# Workspace file tools

2026-11-24. Base commit `18d8b80` plus working changes. macOS arm64,
Python 3.15.2, locked uv environment.

```sh
uv run --locked pytest -q --tb=short
uv run --locked pytest -q tests/test_tools.py tests/test_config.py tests/test_run_loop.py tests/test_jobs.py
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv run --locked python scripts/smoke_wheel.py
git diff --check
```

The full suite passed 728 tests and the focused suite passed 210. The tests
cover a binary copy, the 1 MiB
copy limit, an inode-preserving move, one-level directory creation, permanent
file deletion, protected paths on both sides of transfers, and a destination
created immediately before rename. They also verify individual grants,
wildcard expansion, and scheduled job dispatch with and without explicit roots.
The attachment API fixture grants no tools for image description so unrelated
schemas do not consume its synthetic prompt budget.

Ruff lint and formatting, strict typing, and clean wheel installation passed.
Rename uses the operating system's exclusive rename flag on both macOS and
Linux. The concurrency test exercised that operation on macOS.
[Apple XNU flags](https://github.com/apple-oss-distributions/xnu/blob/main/bsd/sys/vnode_if.h),
[Linux UAPI flags](https://github.com/torvalds/linux/blob/master/include/uapi/linux/fs.h)

Live model selection and Linux runtime verification remain pending. The test
artifacts are disposable workspaces and the source-controlled regression cases.
