# Legacy format reference

The [direct import guide](cutover.md) describes what to do with a remaining
legacy data tree. Legacy history includes active and numbered archived
transcripts, run snapshots and journals, prompt and tool artifacts, job rows
and journals, and pack-operation receipts. Legacy transcript rotation and v2
topic archiving are separate operations.

The [fixture manifest](fixtures/legacy-format/manifest.json) describes synthetic
examples of these formats, including unknown fields, CRLF prompt bytes, a torn
trailing record, a consumed job, and an uncertain pack operation. The
[copied-source manifest](reference-manifest.json) records the source revision and
checksums used to establish the formats.
