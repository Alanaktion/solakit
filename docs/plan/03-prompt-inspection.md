# Phase 3: Prompt sovereignty and dry-run inspection

Status: complete

Depends on: phase 2

- Implement the pure prompt builder with the specified fragment order and exact separator.
- Read the live `SOUL.md` by reference; preserve its content verbatim.
- Make time and run facts explicit inputs for deterministic tests.
- Return the exact prompt, hash, and enabled tool schemas through `/api/prompt`.
- Add the thin `sola prompt` CLI. Inspection must call neither the model nor OpenViking.

**Exit check:** Golden fixtures match byte-for-byte, including supplied recall. No framework, channel, or tool boilerplate appears in the system prompt.

## Completion record

Implemented pure prompt assembly, live verbatim file reads, exact separator and
fixed run facts, SHA-256 inspection API, and thin authenticated sola prompt CLI.
A checked-in golden fixture includes supplied recall and tests byte equality.
Dry-run tests forbid external HTTP calls and verify no history writes.
Validated with 12 passing tests, Ruff lint and format checks. Tool schemas are
empty until phase 4 supplies the registry; no hidden tools are advertised.
