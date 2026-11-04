# P2 — evidence UI (partially implemented)

Prompt/run inspection remains deferred. Read-only archive browsing was authorized
and implemented with [session archival](13-session-archival.md).

1. Add a prompt inspector using the existing prompt API. Verify its SHA matches
   CLI output for identical profile/session/time inputs; never reassemble prompts
   in the UI. Display escaped, credential-masked presentation only.
2. Add run evidence views and cancellation through existing API operations.
   Preserve diagnostic/tool redaction and distinguish status from assistant text.
3. Done: numbered immutable archive browsing through the existing archive API,
   alongside operator-triggered archival. No prompt/run inspector yet.
4. Verify API parity, unavailable backend behavior, escaping and secret masking,
   plus actual browser use in both color schemes and narrow viewports.

Do not change core loop, prompt assembly, or storage for these presentation tasks.
