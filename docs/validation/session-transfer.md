# Session transfer validation

Verified on 2026-11-25 with Python 3.15.2 and disposable Solakit stores.

- Round-tripped a topic between stores and into its source store. Verified new
  IDs, delegated links, original prompt and artifact bytes, image previews,
  search results, and a second export from the copy.
- Continued a compacted topic with its existing summary and later turns, created
  a new compaction in the copy, and transferred the resulting history again.
- Reopened copied history with unfinished memory, delivery, image, and tool
  evidence. Runtime workers left those records inert. A copied scheduled reply
  remained visible and its occurrence remained available in the export.
- Exercised authentication, CSRF, content type, malformed archives, invalid
  lineage, request size, active runs, and retried imports. Simulated interruption
  both before evidence flush and partway through archived-topic flush. Restart
  recovered the same copied session for the original request key.
- Used Chromium with the loopback fixture to export a Manage topics result by
  keyboard while the current conversation stayed selected. Checked how the row
  displays an active-run error. Opened Import topics from the manager and checked
  that cancel returns focus to the launcher. Submitted three ZIPs in order and
  dropped the second response. The third file still imported, and retry sent only
  the second file with its original key. The last selected topic opened when the
  batch finished. The mobile import dialog passed the automated axe scan. The
  browser suite also checked 320-pixel layouts, 200% text, forced colors, and focus
  behavior.
- The locked test suite, Ruff lint and format checks, mypy, and the clean wheel
  smoke check passed.

The runtime fixtures use synthetic model and image data. Live Qwen, oMLX,
OpenViking, and ComfyUI were outside this verification.
