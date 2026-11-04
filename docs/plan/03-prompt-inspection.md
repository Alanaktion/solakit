# Phase 3: Prompt sovereignty and dry-run inspection

Status: planned

Depends on: phase 2

- Implement the pure prompt builder with the specified fragment order and exact separator.
- Read the live `SOUL.md` by reference; preserve its content verbatim.
- Make time and run facts explicit inputs for deterministic tests.
- Return the exact prompt, hash, and enabled tool schemas through `/api/prompt`.
- Add the thin `sola prompt` CLI. Inspection must call neither the model nor OpenViking.

**Exit check:** Golden fixtures match byte-for-byte, including supplied recall. No framework, channel, or tool boilerplate appears in the system prompt.

## Completion record

Record implementation, validation, and remaining limitations here before marking complete.
