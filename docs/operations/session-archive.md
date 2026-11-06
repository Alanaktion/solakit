# Session archival and imported history

Session archival keeps old history out of everyday lists while retaining the
original files, media and search index. It is a reversible metadata change:
`archived_at` is stored in the existing `<id>.meta.json`. IDs, transcripts,
numbered conversation archives, run evidence and bindings stay in place. Restore
clears the flag; index rebuilds preserve the lifecycle state from metadata.

## Cleaning up an import

Open **Sessions → Clean up inactive sessions**. Start with the default 90 days,
or choose another age. The current title/ID and profile filters narrow the preview.
Review the candidates and deselect anything you want to keep before choosing
**Archive selected sessions**. Each batch handles up to 500 eligible sessions;
preview again if more remain. Protected sessions do not prevent later eligible
sessions from being found.

The results show changes and skipped sessions and offer **Undo this batch**.
Sessions can also be selected from a list page or archived/restored individually.
The **Show sessions** filter switches between Active, Archived and All. Lists
are paginated, ordered by last activity, and searchable by title or ID. Imported
profile names remain available in the filter even if that profile is no longer
configured for new work.

Archived sessions are readable by direct link. Their transcript history and
images remain available; chat shows **Restore session** instead of a composer.
Restore before continuing, rotating or compacting the conversation. Archived
sessions and their runs are excluded from default session/run listings and the
chat sidebar. Explicit session/run history and content search still work.

## Activity and safeguards

Last activity is derived from the latest actual message timestamp across active
and numbered transcript files and the latest run request/admission timestamp.
Metadata creation time is only the fallback when no dated history is available.
Thus importing an ancient conversation today does not make its history recent.
Recovery finishing an old queued run today does not move its activity to today.
Missing legacy message timestamps do not invent activity. Reading, archiving,
restoring and background run observations do not count as new conversation use.

A derived `session_activity` SQLite table makes listing/cleanup queries independent
of transcript size. It updates when messages/runs arrive. Startup reuses indexed
history and refreshes changed evidence; a full rebuild regenerates it from files. Existing metadata files default to active without a migration rewrite.

Archival skips sessions with queued/running turns, sessions pinned to enabled
channels, and sessions used by enabled unfinished jobs (including paused jobs)
or active job dispatch. Consumed one-time jobs do not protect a session after
dispatch has finished. Historical/disabled bindings alone do not prevent cleanup.
Enabled job creation/rearming rejects an archived destination; restore the session
before enabling its future work or a channel that will use it.

Cleanup rechecks activity and current bindings under scheduler/execution admission
locks. Preview submissions carry activity snapshots, so changed sessions are
skipped with a reason. Accepted operations finish durable publication before
propagating cancellation. Bulk undo uses the same checks and never interrupts
new work. No automatic age policy runs in the background; cleanup is explicit.

## Operator API

| Request | Behavior |
| --- | --- |
| `GET /api/sessions` | Active sessions, most recent activity first; default limit 50 |
| `?status=archived` or `?status=all` | Include the requested lifecycle state |
| `?q=cron&profile=legacy&before=2026-01-01T00:00:00Z` | Title/ID substring, profile and exclusive inactivity cutoff |
| `?limit=100&offset=100&page=true` | Page envelope with sessions, total, global active/archived counts and profile names; max limit 500 |
| `POST /api/sessions/{id}/lifecycle` with `{"archived":true}` | Archive an idle, unprotected session; `false` restores |
| `POST /api/sessions/archive-batch` | Preview/apply an archive selection |
| `POST /api/sessions/restore-batch` | Preview/apply restoration of explicit IDs |
| `GET /api/runs?include_archived=true` | Include archived sessions' runs; optionally filter `session_id` |

Bare session listings remain JSON arrays. Add `page=true` for counts and pagination
metadata. All routes use existing Cookie/Bearer operator authentication. Busy or
protected lifecycle changes return 409; invalid filters/selections return 422.
Direct history reads keep their existing routes. Message/context mutations on an
archived session return 409 until restored.

Example cleanup preview:

```json
{"inactive_days":90,"q":"cron","dry_run":true}
```

Apply explicit reviewed IDs with `dry_run:false`. Optional `before` and
`expected_activity` preconditions recheck a preview. Choose either `inactive_days`
or a timezone-aware ISO `before`. Without explicit IDs, an inactivity cutoff is
required. Restoration requires IDs. Responses contain eligible/changed session
rows, archived/restored counts, skipped IDs/reasons and a `more` indicator.

## Search and existing conversation archives

The web UI **Search** page finds sessions by name/ID or full text, including
archived sessions. Name/ID results are paginated; full text shows up to 100
matching messages in relevance order, with escaped snippets and links to the
current transcript or numbered archive. Use terms of at least three characters
and quotes for phrases. Narrow the query if it reaches the result limit.

Full-text history search deliberately includes lifecycle-archived sessions by
default. `/api/search`, offline `sola search`, and granted `sessions.search` calls
retain their existing filters and physical line addresses. No message text is
rewritten, and no model or memory request is made by lifecycle archival.

The existing **Archive conversation** action and `POST /api/sessions/{id}/archive`
have a different purpose: rotate the active transcript to the next numbered file
and start fresh context in the same active session. **Archive session** hides and
retires the whole session, with all of those transcript files retained. Compaction
continues to create summaries according to its own existing policy.

Backups retain session metadata as well as JSONL, run/media evidence and app.db.
Restoring/rebuilding indexes does not remove lifecycle archive flags. This feature
does not purge old data or move files to a second storage tree.

## Session sources

Session metadata records the creator in `source`: `api` by default, `web` for UI
creation, the channel type (for example `discord`) for channel routing, and
`cron` for sessions created by scheduled jobs, including one-time jobs and fresh
job sub-sessions. Delegated children use `delegate`; memory probes use `probe`.
Sources are extensible lowercase identifiers. Reusing a session retains its
original source, even when a job or another channel subsequently uses it.
Legacy metadata without this field reads as `api` without rewriting evidence.

The Sessions page hides `cron` sources by default. Choose **Include cron
sessions** to show them; pagination preserves the selection. The API includes all
sources by default; `GET /api/sessions?exclude_source=cron` excludes scheduled
sessions before counting and pagination. Archival status and profile filters
continue to work independently. Cleanup preview still uses its explicit age,
query and profile criteria and may include scheduled sessions.
