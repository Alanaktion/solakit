# Letters pack

Status: partial — locally implemented in `packs/letters`; actual protocol/live check pending.

## Audit checklist

- [x] Independent pack and local bare-remote protocol/receipt regression tests.
- [ ] Inspect the actual repository's numbering, authors, layout and existing files.
- [ ] Deliberately validate pull/read/reply/publish against the agreed live protocol;
  retain immutable old-letter bytes and publication evidence.

Build a separate package using the same filesystem/Git services. Do not put
letters tools or protocol rules under `src/solakit/` and do not force letters into
journal conventions. Use a named repository and pack-owned protocol configuration.

- [x] Pull/read new peer letters with external-content markers.
- [x] Encode sequential numbering and `NNN-YYYY-MM-DD-sola.md` in pack code.
- [x] Reply creates exclusively and never edits prior letters, including retries.
- [x] Compose services privately; no model-driven shell or primitive choreography.
- [x] Explicit reply/publish grants; do not grant generic edit/write tools to bypass
  the letters profile's protocol. Preserve local commit identity on uncertain push.
- [x] Scratch tests: concurrent numbering, immutable old letters, external-content
  formatting, peer/author convention, repeat invocation, failed commit/push.

Verify actual repository conventions before live wiring; leave Hermes untouched.
Expose this pack without editing `core/loop.py`. If that becomes necessary, fix
or extend the generic service seam instead of adding a letters branch to the core.

## Implementation record

`sola_packs.letters` implements pull_new/read/reply using the shared Git/filesystem
services. Config is pack-owned: named repository, directory, local/peer author,
layout and scan bound. Numbering is global across authors. Reply intent is durable
before create; request fingerprints and resource locks prevent duplicate replies
and numbering races. Failed/uncertain publication preserves local evidence and
blocks automatic replay. Existing letters are never modified.

Tests use local bare remotes: original bytes preserved, external markers, duplicate
sequence refusal, changed-request and resource/capability denials, concurrent
number allocation, busy operations, torn receipts and failed push. Live conventions
and production correspondence have not been checked or modified.
