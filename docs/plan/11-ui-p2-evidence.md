# P2 — evidence UI

Status: partial — implementation complete; targeted browser verification remains.

## Audit checklist

- [x] Prompt/run inspection and cancellation implemented; HTTP parity, masking and
  active-run cancellation checks pass.
- [x] Operator reports general functionality looks good.
- [ ] Check narrow-screen overflow, light/dark readability, expandable evidence and
  cancellation feedback in a real browser.

General approval is not recorded as completion of the specific browser matrix.

- `/prompt`: profiles/session/origin/time inputs call the existing prompt API.
  Original SHA is displayed without recomputing or reconstructing bytes. Prompt
  text is escaped and credential-masked; the SHA still refers to the original.
  This is dry-run assembly, not a live recall or model inference operation.
- `/runs`: recorded status/session filters and links to individual evidence.
- `/runs/{id}`: metadata, stored prompt/hash, messages, redacted tool-log cards,
  diagnostics, memory/delivery events and artifacts. No raw transcript tool
  args/results are rendered. Pending runs offer POST cancellation through the
  existing durable API operation; terminal runs show their actual status.
- Numbered immutable archive browsing already shipped with session archival.
- Navigation and chat run links expose these real pages; no placeholder actions.

Checks cover auth, fixed-time prompt/API SHA and byte parity, credential masking,
escaping, redacted tool source selection, filters, missing records and actual
mocked active-run cancellation. Core prompt/loop/storage are unchanged.

Still pending: the targeted browser checks above. Node keyboard tests do not
establish browser visual behavior.
