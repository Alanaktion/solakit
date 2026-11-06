# Layout and alignment verification — 2026-11-05

## Findings and changes

The rename editor inherited `.new-session-form`, a creation-specific four-column
grid. On a wide viewport this assigned a full column to Save and another to
Cancel. The input's helper text also influenced their bottom alignment. The
editor now has a bounded field stack and an independent flex action row; Save
and Cancel keep their natural widths, share a 44px height, and sit below the hint.

Shared control rules now give text inputs/selects and buttons consistent 44px
minimum heights and line heights. Button-styled links use inline-flex centering;
action groups center their children, including nested POST forms. Checkbox
options have aligned, clickable labels, and standalone session/cleanup selectors
have 44px label targets. Mobile textareas use 16px type. Error notices have a
consistent gap before their form.

Single-axis card lists, status chips, metadata, and action rows use flex. Grid
remains for designed field tracks and record columns: job forms, session creation,
desktop session records, evidence properties, and the chat/sidebar shell. This
convention is recorded in AGENTS.md. Repeated overrides for button/composer sizes
were consolidated into their original rules.

Session rows switch to a stacked title and wrapping metadata/actions through
850px; long titles no longer compete with a status pill and timestamp for a tiny
column. Chat also uses the single-column shell through 850px. Headings wrap
without squeezing adjacent controls. Next-page links align to the right even
when there is no previous-page link.

The history menu remains anchored to its summary. Only the neighboring variable
session-action row scrolls horizontally; the menu no longer depends on fixed
mobile coordinates or sits inside that scrolling container. Tool summaries use
a flex line for name/state while preserving the native disclosure marker.
Summaries, buttons, and inputs keep aligned text and usable control heights.
The load-earlier link also uses flex centering instead of its older block override.
The conversation establishes a positioning context for visually hidden author
labels; those labels no longer escape into the outer document and create blank
space below the fixed chat shell.

## Browser verification

`tests/browser/layout.cjs` uses the isolated QA server with synthetic session/job
fixtures and mocked model/memory transports. It checks **36 screens/states × 12
widths × 2 themes = 864 rendered cases**. Widths cover 320 through 2048px,
including both sides of 600, 640, and 850px layout breakpoints. Geometry assertions
cover document overflow, control height, button/link/disclosure text centering,
filter/action alignment, metadata bounds, rename sizing/spacing, and popup
placement. Fixed chat screens additionally assert vertical viewport containment.
It captures screenshots separately for visual review. The focused chat matrix
(`SOLA_LAYOUT_ONLY=chat`) checks nine states at the same widths/themes after the
positioning-context fix (216 additional cases).

Coverage: login; sessions and session creation; filters, search, and empty states;
prompt and expanded schemas; runs and pending/completed evidence; operations;
new/edit/delete jobs and recurring/one-time fields; tools and expanded arguments;
rename and validation errors; archive/compact/lifecycle confirmations; current,
numbered-archive, and read-only chats; long drafts, pending composer, expanded
tools, system status and history menus; cleanup review, result, and undo.

Normal/error/disabled states were checked, and representative screenshots were
reviewed at narrow mobile, tablet, desktop, and ultrawide sizes. Browser console
errors are asserted absent. The tests do not load operator configuration or data.
Existing rename/archive behavior and streaming selection/tool-card preservation
are checked separately with `check.cjs rename`, `check.cjs 13`, and
`check.cjs audit`.

## Release checks

- Full core/pack suite: 455 tests passed.
- Ruff check and format check passed.
- Frozen workspace sync and all core/pack builds passed.
- Built UI assets/templates match source.
- No dependencies, API behavior, evidence, credentials, or model configuration changed.
