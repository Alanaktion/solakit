# P3 — operations UI (deferred)

Prerequisite: P2 evidence UI and explicit decision to begin operations UI.

1. Display jobs and channels through existing APIs, accurately reporting current
   state without inventing live health from configuration.
2. Add existing job run/pause/resume operations and actionable API error feedback.
3. Test authentication, status transitions, validation, and read-only channel
   display. Keep config editing/creation out of the UI.

Live integrations remain subject to existing scratch-memory and acceptance gates.
Markdown, profile selection in chat, SSE, and shared UI/Discord session archival
remain separate v2 work. Archival must preserve channel pairing and immutable
history; settle fresh-context and operator-only semantics before implementation.
