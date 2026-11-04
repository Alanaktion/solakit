# Letters pack — next

Status: planned, after journal contract tests.

Build a separate package using the same filesystem/Git services. Do not put
letters tools or protocol rules under `src/solakit/` and do not force letters into
journal conventions. Use a named repository and pack-owned protocol configuration.

- Pull/read new peer letters with external-content markers.
- Encode sequential numbering and `NNN-YYYY-MM-DD-sola.md` in pack code.
- Reply creates exclusively and never edits prior letters, including retries.
- Compose services privately; no model-driven shell or primitive choreography.
- Explicit reply/publish grants; do not grant generic edit/write tools to bypass
  the letters profile's protocol. Preserve local commit identity on uncertain push.
- Scratch tests: concurrent numbering, immutable old letters, external-content
  formatting, peer/author convention, repeat invocation, failed commit/push.

Verify actual repository conventions before live wiring; leave Hermes untouched.
Expose this pack without editing `core/loop.py`. If that becomes necessary, fix
or extend the generic service seam instead of adding a letters branch to the core.
