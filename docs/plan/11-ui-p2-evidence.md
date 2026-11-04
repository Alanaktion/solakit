# P2 — evidence UI (deferred)

Prerequisite: P1 operator feedback. Not part of v1; no placeholder navigation.

1. Add a prompt inspector using the existing prompt API. Verify its SHA matches
   CLI output for identical profile/session/time inputs; never reassemble prompts
   in the UI. Display escaped, credential-masked presentation only.
2. Add run evidence views and cancellation through existing API operations.
   Preserve diagnostic/tool redaction and distinguish status from assistant text.
3. Add numbered immutable archive browsing through the existing archive API.
   This is read-only browsing, separate from future operator-triggered archival.
4. Verify API parity, unavailable backend behavior, escaping and secret masking,
   plus actual browser use in both color schemes and narrow viewports.

Do not change core loop, prompt assembly, or storage for these presentation tasks.
