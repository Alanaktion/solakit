# Git-backed letters validation

2026-11-23 on macOS arm64, Python 3.15.2 and Git 2.56.1. Changes were tested
in the working tree based on `c5033b2`.

## Deterministic checks

`uv run --locked pytest -q` passed all 697 tests. The letters cases use a
bare Git remote and two disposable clones to check:

- Full local reads and inbox pagination past 20 peers, including interleaved
  and trailing local letters, own-only pages, redaction, UTF-8 prefixes, JSON
  escaping, both byte limits and metadata-only budget failure.
- Strict arguments, clean worktree and index, branch and checkout validation,
  numbered filenames, duplicate numbers, unsafe aliases and the scan cap.
- Current and admitted workspace roots, older admissions, the whole-checkout
  reservation, scheduled discovery and local read, explicit job roots and
  denied scheduled publication.
- Send and reply idempotence through restart, one reply per peer target,
  filename dates near a timezone boundary, and exact body-plus-LF bytes.
- A competing peer push, abrupt process exits after creation and after
  commit, durable allocations and uncertain receipts after restart, and
  blocked reconciliation for changed bytes or absent remote commits.
  Reconciliation records the existing commit after the operator publishes it,
  including recovery when the original process never persisted its commit ID.
- v1 history-only and completed receipt seeds, unresolved receipt blocking and
  later verification of the original publication.

Ruff lint and formatting checks, strict mypy, and clean wheel installation
with external pack dispatch also passed.

## Live interactive model

Passed with the configured `Qwen3.6-35B-A3B-AntiLoop-NVFP4` pin on oMLX.

```sh
uv run --locked python -m scripts.probe_letters --env-file ~/.config/sola/.env
```

The model called `letters.inbox`, `letters.read` and `letters.reply` in order.
It used peer number 017 from the tools as `reply_to`, answered the question,
and published letter 018. The disposable bare remote verified commit
`9c91ffa19215baf2dda188e4b0150e0abd47709d` on the final run, with subject
`Reply to letter 017 as sola` and the submitted UTF-8 body plus one added LF.
The scheduled scenario used the deterministic model transport.

## v1 cutover rehearsal

Passed on a fresh bare copy of the locally available v1 `main` tip, which
matched the checkout's recorded `origin/main`. The source held three numbered
files under `letters/`, including the reply to 001 and a later send. The
peer letter's heading was preserved as part of its body.

The operator script seeded an isolated v2 state database through
`--seed-v1 --legacy-checkout <original-checkout>`. It corroborated the actual
imported completed receipt for reply 001 against the commit and file bytes.
A matching dispatch reused the verified result. Two synthetic cases on the
copy exercised a history-only reply and an unresolved receipt. Both reserved
their respective targets. The three original files remained byte-for-byte
identical, and the copied worktree was clean at the end. The rehearsal used
the disposable remote. Production seeding and activation remain pending.
