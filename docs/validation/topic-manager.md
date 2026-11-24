# Bulk topic management validation

Implemented and checked on 2026-11-24 in the working tree based on `f674d68`.
Environment was macOS 27.2 arm64, CPython 3.15.2, Node 26.13.1 and headless
Chromium 159.0.8154.6.

| Check | Result |
| --- | --- |
| `uv run --locked pytest -q` | 716 passed, including catalog filters, timestamp bounds, stable paging, OpenAPI and archive recovery |
| `uv run --locked ruff check .`, `uv run --locked ruff format --check .`, `uv run --locked mypy` | Passed |
| `uv run --locked python scripts/smoke_wheel.py` | Passed, including the packaged topic manager module |
| `scripts/check_chat.mjs` against a fresh `scripts/chat_fixture.py` | 29 browser journeys passed with zero page errors. The 320-pixel topic manager had zero axe violations. |

The browser journey combined profile, title and both date filters. It used
keyboard selection and selected all 102 matches across pages. It dismissed the
first confirmation and verified that a new matching topic stayed outside the
confirmed IDs. The fixture committed the second batch and dropped its response.
Retry submitted those same two IDs, and the selected transcript and draft stayed
open with sending disabled.

Browser evidence includes `report.json`, `topic-manager-mobile.png` and
`topic-manager-selection.png` in `/tmp/sola-bulk-topics-acceptance-browser`.
Reproduce with a fresh output directory and fixture. A reused fixture can carry
pinned topics from a prior suite run and affect the startup selection assertion.
[Browser acceptance commands](../chat-ui.md#reproducible-browser-acceptance)

Verification used disposable local data and a synthetic model. Production data,
live model and memory services, and Safari behavior were outside this check.
