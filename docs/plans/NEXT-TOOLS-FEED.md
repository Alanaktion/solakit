# RSS feed-lite

Add a small, operator-controlled RSS reader: manual subscriptions
plus agent-discovered feeds, polled by a deterministic service, read in the
web UI, and summarized on a schedule only by an operator-created digest job.
Explicit subscriptions, recency, and keyword filters determine what the operator
sees. Digests stay in the operator's local topics. Use the
[shared tool plan](README.md#follow-up-tool-plans).

## Scope

- Subscriptions live in `state.db` as versioned entities with JSONL evidence,
  like jobs. The agent can propose a feed. The operator manages subscriptions
  through the authenticated UI and owns the configured limits.
- Polling is deterministic and model-free. The model never polls, and a feed
  item never creates a job, a wake, or a memory extraction on its own.
- Reading happens in the web UI. Scheduled summaries happen only in an
  operator-created digest job, under the normal job budgets, quiet hours, and
  delivery rules. No email, Discord, push, or other external delivery.

This slice excludes OPML import/export, per-feed poll intervals, automatic
article fetching, ranking, recommendations, feed search beyond the item list
filters, and Goals/Ideas integration.

## Records

Add two records in `src/solakit/records.py`, following `Job`:

```python
class FeedSubscription(Record):
    url: str                    # canonical feed URL, syntax checked at add or proposal
    title: str = ""             # operator label, filled by the feed on first poll if empty
    source: Literal["operator", "agent"]
    status: Literal["active", "pending", "paused"]
    keywords: tuple[str, ...] = ()     # empty include filter matches all
    exclude_keywords: tuple[str, ...] = ()
    discovery_reason: str | None = None
    discovered_from: str | None = None # URL or topic that suggested it
    discovered_run_id: RecordID | None = None
    discovered_session_id: RecordID | None = None
    version: int = 1
    deleted: bool = False
    last_fetched_at: Timestamp | None = None
    last_item_at: Timestamp | None = None
    consecutive_failures: int = 0
    last_failure_reason: str | None = None

class FeedItem(Record):
    feed_id: RecordID
    guid_hash: str              # SHA-256 of the identifier or fallback link, qualified by kind
    title: str = ""
    link: str = ""
    summary: str = ""
    published_at: Timestamp | None = None
    fetched_at: Timestamp
    read: bool = False
    digested: bool = False
    version: int = 1
    deleted: bool = False
```

`Store` gains `feed()`, `feeds(include_deleted=False)`, `feed_item()`, and
`feed_items()` with feed, unread, undigested, and ordering options. Follow
`Store.jobs()` for entities projected from JSONL evidence and filtered with
`json_extract`. A deleted subscription keeps its tombstone. Subscribing to
that URL again creates a new feed ID with its own item history.

Reader retention uses `fetched_at` to age out items on the service tick,
including items from paused feeds. After ingestion, evict the oldest items
above the per-feed limit. Pruning sets `deleted=true`, clears display
content, and retains the item ID, `feed_id`, and `guid_hash`. Exclude these
tombstones from reader queries, counts, and digests. Keep them in the dedupe
lookup for the life of the subscription so an entry still served by the
publisher stays pruned. Removing a subscription prunes its visible items.
Reader retention bounds the visible cache. Immutable JSONL retains original
item content, and backups preserve both evidence and the SQLite state used
for dedupe.

## Tool calls

| Tool | Arguments | Result |
| --- | --- | --- |
| `feeds.list` | `status=""`, `limit=50` | Subscriptions newest first, with ID, URL, title, source, status, last fetch, item and unread counts, and failure count. |
| `feeds.propose` | `url`, `reason`, `discovered_from=""` | Creates a `pending` subscription and returns its state. |
| `feeds.items` | `feed_ids=[]`, `unread_only=false`, `undigested_only=false`, `limit=20` | Items with ID, feed ID, title, link, summary, published date, read/digested flags, and `has_more`. Order by `fetched_at` and ID, newest first by default and oldest first when filtering for undigested items. |
| `feeds.acknowledge` | `ids=[]` | Stages coverage for publication by the current digest run. Accepts only item IDs actually returned to that run, with at most 20 distinct IDs staged in total. |

Use the normal tool registry, authority checks, and receipts. `feeds.list`
and `feeds.items` are read-only. Disclose and dispatch `feeds.items` only
when memory is off for the run. Return whole items within the tool-result
budget and set `has_more` if eligible items remain. Treat proposal creation
and staged acknowledgments as consequential local operations under the
existing job rules for quiet hours. `feeds.acknowledge` requires an
authorized job occurrence bound to an isolated topic with session delivery.
Its receipt reports staged IDs.

`feeds.propose` is the model's only subscription mutation. It validates URL
syntax locally and records a pending proposal without DNS resolution or HTTP
requests. This applies to every run origin, including interactive runs. Only
authenticated operator actions can create an active subscription, approve or
reject a proposal, pause or resume a feed, edit or remove it, or request a
manual poll. Opening an item or asking about it leaves subscription status
unchanged. A proposal stays pending until the operator approves or rejects it.
Canonical URLs are unique across subscriptions that have not been removed,
including pending and paused entries. Grant the tools by exact name. Wildcard
grants expand at new admission, so older admissions retain their existing
authority ceiling.

## Configuration

Add a `[feeds]` section to `config.example.toml` and `docs/config.md`.
TOML holds bounds and the enable switch. The subscription list lives in
`state.db`.

| Key | Default | Meaning |
| --- | --- | --- |
| `feeds.enabled` | `false` | Polling and feed tools are ineligible while false. |
| `feeds.max_feeds` | `32` | Total active, pending, and paused subscriptions. |
| `feeds.max_agent_feeds` | `10` | Active, pending, and paused agent-sourced subscriptions. Approval preserves the source. |
| `feeds.max_agent_additions_per_day` | `2` | Agent proposals created per rolling 24 hours, including rejected and removed proposals. |
| `feeds.poll_interval_minutes` | `60` | Minimum `15`. One global interval for this slice. |
| `feeds.item_retention_days` | `30` | Reader retention measured from first fetch. |
| `feeds.max_items_per_feed` | `500` | Maximum visible items per feed. Prune the oldest first. |
| `feeds.max_consecutive_failures` | `5` | Auto-pause after this many failed polls. |

Changing a bound cannot widen an existing admission. A subscription created
under a narrower cap stays valid; a later tighter cap blocks new additions
until the count fits. Disabling `feeds.enabled` stops polling and removes feed
tool authority at admission and dispatch. Existing subscriptions and items
stay readable in the UI.

## Poller

Add a `FeedService` in `src/solakit/engine/feeds.py`, owned by the runtime
like `JobService`. It holds no model client and uses no inference permit.
An APScheduler interval trigger fires every `poll_interval_minutes` and
iterates due `active` subscriptions only. `pending` and `paused` feeds are
never fetched. Each feed poll is one bounded operation with its own
deadline; a slow or failing feed never blocks the others.

- Fetch with `httpx2`, `trust_env=False`, a 15-second total deadline, and a
  1 MiB streamed response cap. Reuse the URL syntax checks from
  `src/solakit/tools/web_fetch.py` at subscription entry and on every request.
  Accept HTTP(S) URLs up to 2,048 characters, reject credentials, local host
  names, non-public IP literals, and port zero.
- The direct poller also enforces a network destination policy. Resolve all
  addresses before connecting. Reject a target whose results include a
  non-public address, including loopback, private, link-local, and multicast
  addresses. Connect only to a validated address, keeping the original
  hostname for HTTP `Host`, TLS server name, and certificate verification.
  A retry or fallback address must pass the same check before use. Disable
  automatic redirects. Resolve each redirect location against the current URL
  and apply the full URL and connection checks before sending the next request.
  Follow at most five redirects. A rejected hop fails the poll without sending
  a request to that destination.
- Parse RSS 2.0 `channel`/`item` and namespaced Atom `feed`/`entry` with
  `xml.etree.ElementTree`. Use RSS `description` and Atom `summary` for
  excerpt text, and RSS `pubDate` and Atom `updated` for dates. Inspect at
  most 100 entries per fetch. Limit titles to 200 characters, links to 2,048,
  and summaries to 2,000. Parse dates with `email.utils.parsedate_to_datetime`
  and `datetime.fromisoformat`, with `fetched_at` as the fallback for missing
  or invalid dates. Malformed XML fails the poll and stores no entries.
- Treat RSS `guid` and Atom `id` as opaque, case-sensitive identifiers. For
  Atom choose the first usable link whose `rel` is `alternate` or omitted.
  Resolve relative links against the final feed URL and inherited `xml:base`
  before validating them. Store only absolute HTTP(S) article links without
  credentials. If an entry has an ID and no usable link, store it without a
  link. If it has no ID, use its canonical usable link as the dedupe key.
  Skip entries that have neither. Keep identifier and link keys distinct.
- Dedupe by `(feed_id, guid_hash)` across visible items and tombstones. The
  same story in two feeds is two items.
- Apply the subscription's include/exclude keywords to title plus summary,
  case-insensitive substring match, before storing. Filtered-out items are
  not stored or counted as seen. A later keyword edit can match them if the
  publisher still serves them on a subsequent poll. Record the subscription
  ID, fetched URL, HTTP status, counts for stored items and duplicates, and
  any failure reason in the poll evidence.
- On success, reset `consecutive_failures`, clear `last_failure_reason`, and
  update `last_fetched_at`. Update `last_item_at` when the poll stores a new
  item. On failure, record the reason, increment the count, and keep the last
  good items. At `max_consecutive_failures`, set status `paused`. The
  operator resumes from the UI. After a restart interrupts a poll, the next
  interval tick picks the feed up as due.
- Polling reads no workspace, no memory, and no other topic. It writes
  only feed records and their evidence. It creates no runs, no jobs, and
  no deliveries.

## Digest

The operator creates a `cron:` job with the existing `jobs.*` tools or the
Settings job editor. Bind it to an isolated topic and use session delivery.
Grant it `feeds.items` and `feeds.acknowledge`. The task can ask for unread
items from selected feeds and a link for each item that has one. The run calls
`feeds.items` with those feed IDs, `undigested_only=true`, and
`unread_only=true` when requested. It summarizes up to 20 undigested items
in order of first fetch time, with ID as a stable tie-breaker. The query
filters on the `digested` flag shared across jobs, so remaining items stay
eligible for later runs. A digest that finds nothing returns `NO_ACTION`.

The model calls `feeds.acknowledge` with the IDs it covered. Store the staged
IDs in durable run evidence and freeze them on the pending delivery when the
job finishes with a publishable message. Set `digested=true` for staged items
still in the reader, in the same SQLite and outbox transaction that publishes
the visible topic message and marks the delivery complete. Items remain
eligible while delivery is pending. Failed or interrupted runs, `NO_ACTION`,
and cancelled, expired, or superseded deliveries leave coverage untouched.
Updating coverage is idempotent by item ID. Digest coverage is independent
of the operator's read state.

Use the normal job budgets, quiet hours, topic cooldown, global pause, and
`[background]` admission caps. Staging can run during quiet hours. Delivery
follows the existing topic rules, and an archived bound topic blocks
admission.

## Web UI

- Settings gains a seventh tab, **Feeds**, with source, status, last fetch,
  item and unread counts, and failure count. The operator can add by URL with
  optional title and keywords, and can pause, resume, edit, or remove a feed.
  Pending proposals appear first with their age, discovery source, reason,
  and approve/reject actions. A failed feed shows its last failure reason.
  A paused feed shows a resume control. **Poll now** is available for active
  feeds when polling is enabled and bypasses the interval.
- A feed items view lists unread items newest first across feeds, or
  filtered to one feed. Show the title, feed name, and published date.
  Opening an item marks it read and shows the summary and any article link.
  **Ask Sola about this** opens a new isolated topic and keeps the operator's
  question distinct from the quoted external excerpt. Record the item ID,
  feed, title, summary, and any link so the excerpt's provenance is visible
  in evidence.
  Offer mark-read for individual items and mark-all-read for the current
  single-feed filter.
- Render feed titles and summaries as plain text. Open an article link only
  on an explicit browser action. Article body extraction remains an explicit
  interactive `web.fetch` call under its normal grant and URL rules.

## Security and trust

- Treat feed URLs and item text as untrusted external content in tool
  results, prompts, and the UI, including after operator approval. Preserve
  the proposal reason, `discovered_from` when supplied, and the discovering
  run and session on every agent-sourced subscription.
- Feed discussions opened from the UI and scheduled digests use isolated
  topics, which disable OpenViking recall and extraction. Enforce memory-off
  eligibility for feed item reads made through tools. Label excerpts as
  external data in both interactive and digest prompts.
- The poller makes no authenticated requests and sends no credentials. It
  never follows a feed link to fetch article bodies. It never posts, and
  it never executes anything from a feed.
- Enforce subscription and proposal caps, the interval floor, response and
  item limits, retention, and failure auto-pause in the service. Rejected
  destinations, oversized responses, and redirect loops fail the poll.

## Acceptance

Use disposable RSS 2.0 and Atom fixtures, an injected resolver and recording
test transport for the destination policy, and a local HTTP fixture server
for response handling. Keep fixture networking out of production settings.

- A normal RSS feed and a normal Atom feed store new items once, with
  titles, links, summaries, and dates, and skip duplicates on re-poll.
- Atom parsing resolves a relative alternate link using `xml:base`, ignores
  a `self` link, and keeps IDs that differ only in case distinct. An entry
  with an ID and a disallowed link has no clickable link. An entry without
  an ID dedupes by its usable link or is skipped if it lacks one.
- A malformed feed fails the poll, stores nothing, and increments the
  failure count. Five consecutive failures auto-pause it by default.
- Add and propose reject a URL with credentials, a local hostname, or a
  private IP literal without a network request. A poll rejects a hostname
  that resolves to a non-public address, including mixed public and private
  answers, and rejects any disallowed redirect hop before connecting to it.
  Verify that the connector uses only validated addresses, while keeping
  the original hostname for HTTP and TLS.
- Interactive and autonomous `feeds.propose` both create pending proposals
  without DNS or HTTP requests. A proposal starts polling only after an
  authenticated operator approves it. Rejecting it leaves it unpolled.
- Paused feeds count toward both applicable feed caps, and a rejected
  proposal still counts against the rolling daily cap. Excess additions
  fail with a clear error and no partial subscription.
- Re-serving an item pruned by either age or count does not restore it to
  the reader. Its original evidence remains readable. Repeat the poll
  after restart or restore to verify that the dedupe tombstone survives.
- Restart after a poll preserves subscriptions, proposals, seen items,
  and read/digested flags, and does not duplicate items on the next poll.
- A digest processes more than 20 eligible items over successive runs in
  oldest-first batches without editing its task. Acknowledgment of an ID
  outside the run's results fails. An empty batch returns `NO_ACTION`
  without delivery.
- While quiet hours or cooldown defer a digest, its staged items remain
  undigested. Publication and coverage become durable together, including
  after restart. Failure after staging, expiry, and coalescing that cancels a
  delivery leave its items eligible. Published coverage leaves read flags
  untouched.
- The UI lists feeds with source, status, and failure reason, approves a
  proposal, opens an item as read, and asks Sola about an item in a new
  isolated topic with a clearly identified external excerpt. Check keyboard
  access and both themes with the existing browser fixtures.
- With OpenViking enabled for shared chat, asking about an item and
  publishing a digest produce no extraction for those runs. A memory-enabled
  run cannot invoke `feeds.items` or see its schema.
- Live smoke test with disposable data: subscribe to two public feeds the
  operator chooses, poll once, open an item, run one digest, and remove
  both subscriptions. Record which feeds were used.

When this plan ships, update `config.example.toml`, `docs/config.md`,
`docs/contracts/tools.md`, `docs/contracts/jobs.md`, `docs/chat-ui.md`,
and this plan's entry in `docs/plans/README.md`, and record the decision
in `docs/decisions.md` as the next `V2-` number.
