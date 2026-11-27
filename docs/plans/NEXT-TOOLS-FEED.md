# RSS feed-lite

Add a small, operator-controlled RSS reader: manual subscriptions
plus agent-discovered feeds, polled by a deterministic service, read in the
web UI, and summarized only by an operator-created digest job. This is the
Feed concept without ranking, recommendations, or any external delivery:
relevance comes from explicit subscriptions, recency and keyword filters
replace a taste model, and every new item is local until the operator opens
it or asks about it. Use the [shared tool plan](README.md#follow-up-tool-plans).

## Scope

- Subscriptions live in `state.db` as versioned entities with JSONL evidence,
  like jobs. The agent can manage the list; the operator owns the bounds.
- Polling is deterministic and model-free. The model never polls, and a feed
  item never creates a job, a wake, or a memory extraction on its own.
- Reading happens in the web UI. Summaries happen only in a digest job the
  operator created, under the normal job budgets, quiet hours, and delivery
  rules. No email, Discord, push, or other external delivery.

Out of scope for this slice: OPML import/export, per-feed poll intervals,
full-article fetching by default, ranking or recommendations, feed search
across items beyond the item list filters, and Goals/Ideas integration.

## Records

Add two records in `src/solakit/records.py`, following `Job`:

```python
class FeedSubscription(Record):
    url: str                    # canonical public feed URL, validated at add
    title: str = ""             # operator/agent label; feed title fills it on first poll
    source: Literal["operator", "agent"]
    status: Literal["active", "pending", "paused"] = "active"
    trusted: bool               # operator-added: true; agent-added: false until promoted
    topic_id: RecordID | None = None   # digest/read topic when bound, else None
    keywords: tuple[str, ...] = ()     # include filter; empty matches all
    exclude_keywords: tuple[str, ...] = ()
    discovered_from: str | None = None # URL or topic that suggested it
    discovered_run_id: RecordID | None = None
    discovered_session_id: RecordID | None = None
    version: int = 1
    deleted: bool = False
    last_fetched_at: Timestamp | None = None
    last_item_at: Timestamp | None = None
    consecutive_failures: int = 0
    expires_at: Timestamp | None = None  # untrusted active feeds only

class FeedItem(Record):
    feed_id: RecordID
    guid_hash: str              # sha256 of canonical guid, else canonical link
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
`feed_items(feed_id, unread_only=False, after=None, limit=50)`, following
`Store.jobs()`: entities table rows projected from JSONL evidence, with
`json_extract` filters. A deleted subscription keeps its tombstone; its URL
may be added again only as a new id. Items are pruned by setting
`deleted=true` after retention, without rewriting JSONL evidence. A deleted
subscription's items are pruned with it. Backup/restore already covers
`state.db` plus evidence, so subscriptions and seen items survive both.

## Tool calls

| Tool | Arguments | Result |
| --- | --- | --- |
| `feeds.list` | `status=""`, `limit=50` | Subscriptions newest first: id, url, title, source, status, trusted, last fetch, item and unread counts, failure count, expiry. |
| `feeds.add` | `url`, `title=""`, `topic_id=""`, `keywords=[]`, `exclude_keywords=[]` | The created subscription. Interactive origin: status `active`. Scheduled/proactive origin: status `pending`, never polled. |
| `feeds.propose` | `url`, `reason`, `discovered_from=""` | A `pending` subscription. Available to all origins; this is the only way autonomous work records a discovery. |
| `feeds.approve` | `id` | Sets status `active` and `trusted=true`, clears expiry. Interactive only. |
| `feeds.update` | `id`, `title`, `keywords`, `exclude_keywords`, `topic_id`, `status` | The updated subscription, version incremented. Interactive only. |
| `feeds.remove` | `id` | Tombstones the subscription and prunes its items. Interactive only. |
| `feeds.items` | `feed_id=""`, `unread_only=false`, `after=""`, `limit=20` | Items newest first: id, feed id, title, link, summary, published date, read/digested flags, `has_more`. |
| `feeds.acknowledge` | `ids=[]` | Marks the listed items `digested=true`. Scoped to items returned in the current run, at most 20. |
| `feeds.poll` | `id` | One manual poll of that subscription, bypassing its poll interval but not auto-pause. Interactive only. |

Follow the `jobs.*` tool pattern in `src/solakit/tools/builtins.py`:
`feeds.list` and `feeds.items` are read-only and available to all origins;
mutations are consequential and interactive-only, except `feeds.propose`
and `feeds.acknowledge` as defined above. `feeds.add` from a scheduled or
proactive run must not activate anything: the dispatcher records a `pending`
subscription and returns that state, so the model can report the proposal
without claiming the feed is live. Autonomous work cannot approve, enable,
or rearm a feed, and a feed item cannot create a job or a wake. Grant the
tools by exact name; wildcard grants expand only at new admission, as with
other tools. Older admissions without the feed tools keep them ineligible.

Promotion: any operator interaction with an untrusted feed promotes it.
Opening an item, asking Sola about an item, or `feeds.approve` sets
`trusted=true` and clears `expires_at`. Until then an untrusted active feed
expires `probationary_days` after creation and becomes `paused`, visible in
the UI with its discovery source. A `pending` proposal never expires on its
own, but the UI surfaces its age and source so stale proposals can be
removed. Operator-added subscriptions are `trusted=true` with no expiry.

## Configuration

Add a `[feeds]` section to `config.example.toml` and `docs/config.md`.
TOML holds bounds and the enable switch only; the subscription list itself
lives in `state.db`. Optional `seed_urls` are imported once at first startup
with source `operator`, status `active`, `trusted=true`, deduped by canonical
URL, and recorded in evidence. Later edits to `seed_urls` do not re-import.

| Key | Default | Meaning |
| --- | --- | --- |
| `feeds.enabled` | `false` | Polling and feed tools are ineligible while false. |
| `feeds.agent_add` | `"pending"` | `"pending"`: agent additions become proposals. `"probationary"`: interactive agent additions go active and untrusted; autonomous additions still become proposals. |
| `feeds.max_feeds` | `32` | Total active plus pending subscriptions. |
| `feeds.max_agent_feeds` | `10` | Active plus pending agent-sourced subscriptions. |
| `feeds.max_agent_additions_per_day` | `2` | Agent additions and proposals per rolling 24 hours. |
| `feeds.poll_interval_minutes` | `60` | Minimum `15`. One global interval for this slice. |
| `feeds.probationary_days` | `14` | Expiry for untrusted active feeds. |
| `feeds.item_retention_days` | `30` | Items older than this are pruned. |
| `feeds.max_items_per_feed` | `500` | Oldest items pruned first when exceeded. |
| `feeds.max_consecutive_failures` | `5` | Auto-pause after this many failed polls. |
| `feeds.seed_urls` | `[]` | One-time operator seed list, see above. |

Changing a bound cannot widen an existing admission. A subscription created
under a narrower cap stays valid; a later tighter cap blocks new additions
until the count fits. Disabling `feeds.enabled` stops polling and makes the
tools ineligible at new admission; existing subscriptions and items stay
readable in the UI.

## Poller

Add a `FeedService` in `src/solakit/engine/feeds.py`, owned by the runtime
like `JobService`. It holds no model client and uses no inference permit.
An APScheduler interval trigger fires every `poll_interval_minutes` and
iterates due `active` subscriptions only. `pending` and `paused` feeds are
never fetched. Each feed poll is one bounded operation with its own
deadline; a slow or failing feed never blocks the others.

- Fetch with `httpx2`, `trust_env=False`, a 15-second deadline, and a 1 MiB
  response cap. Follow at most 5 redirects and re-validate the final URL
  with the same public-URL rules as `web.fetch`: public HTTP(S) only, no
  credentials, no `localhost` or `.local` host, global IP literals only,
  port not zero, URL at most 2,048 characters. Reuse the validator from
  `src/solakit/tools/web_fetch.py` rather than copying its rules.
- Parse with stdlib `xml.etree.ElementTree`: RSS 2.0 (`channel`/`item`,
  `guid`/`link`/`title`/`pubDate`/`description`) and Atom (`entry`, `id`,
  `link`, `title`, `updated`, `summary`). Cap at 100 items per fetch,
  title 200 characters, link 2,048, summary 2,000. Parse dates with
  `email.utils.parsedate_to_datetime` and `datetime.fromisoformat`;
  unparseable dates fall back to `fetched_at`. Malformed XML fails the
  whole poll with a receipt and a failure count; no partial items are
  stored. Missing `guid` falls back to the canonical link for dedupe.
- Dedupe by `guid_hash` within the feed. A duplicate is skipped silently.
  The same story in two feeds is two items; dedupe is per feed, not global.
- Apply the subscription's include/exclude keywords to title plus summary,
  case-insensitive substring match, before storing. Filtered-out items are
  not stored and do not count as seen; a later keyword edit does not
  backfill them. Record the poll in evidence: subscription id, fetched URL,
  HTTP status, items stored, items skipped as duplicates, and failure reason
  when present.
- On success, reset `consecutive_failures` and update `last_fetched_at`
  and `last_item_at`. On failure, increment the count and leave the last
  good items untouched. At `max_consecutive_failures`, set status `paused`
  and record the reason; the operator resumes from the UI. A poll that is
  interrupted by restart is not retried blindly; the next interval tick
  picks the feed up as due.
- Polling reads no workspace, no memory, and no other topic. It writes
  only feed records and their evidence. It creates no runs, no jobs, and
  no deliveries.

## Digest

The poller does not summarize and does not notify. A digest is an ordinary
job the operator creates with the existing `jobs.*` tools or the Settings
job editor, bound to a topic, with a `cron:` schedule and a task such as
"Summarize unread items from feeds X and Y; link each item; skip anything
already covered." Grant that job only `feeds.items` and
`feeds.acknowledge`, plus whatever read tools the summary needs. The digest
run uses `feeds.items(unread_only=true, after=<last digest cursor>)`,
summarizes at most 20 items within the normal proactive budgets, and calls
`feeds.acknowledge` for the items it covered. It never marks items read;
the operator marks items read by opening them. Quiet hours, the
one-notification-per-topic-per-hour cooldown, global pause, and the
`[background]` admission caps apply exactly as for any other job, and a
digest that finds nothing new returns `NO_ACTION` with evidence and no
empty message. A digest job whose bound topic is archived blocks admission
rather than rerouting, as with other jobs.

The digest cursor is the newest covered item's id, kept by the operator in
the job task or by the UI when it creates the job; the job service does not
gain feed-specific state. If a digest run is interrupted after summarizing
but before acknowledging, the next run may cover the same items again; that
is a duplicate summary, not a duplicate effect, and the acknowledge call
makes the normal case idempotent by item id.

## Web UI

- Settings gains a seventh tab, **Feeds**, for management: list with source
  badge (`operator` / `agent`), status, trusted state, last fetch, item and
  unread counts, failure count, and expiry for untrusted feeds. Add by URL
  with optional title, topic, and keywords. Approve, pause, resume, edit,
  and remove each subscription. Pending proposals appear first with their
  discovery source and reason, and approve/reject actions. A failed or
  auto-paused feed shows its last failure reason and a resume control.
- A feed items view lists unread items newest first across feeds, or
  filtered to one feed: title, feed name, published date, and a link that
  opens the original in the browser. Opening an item marks it read and
  promotes its feed when untrusted. **Ask Sola about this** opens a new
  shared topic whose first message quotes the item title, feed, link, and
  summary, plus the operator's question, so the item enters evidence as
  user-supplied context. The view offers mark-read and mark-all-read for
  the current filter only, never across feeds.
- Feed items are external content in the UI and in prompts: titles and
  summaries render as text, never as instructions, and never as raw HTML.
  Item links do not fetch in the UI. Fetching an article body is an
  explicit interactive `web.fetch` call from a topic, under its normal
  grant and URL rules, not a feed action.

## Security and trust

- Feed URLs and feed content are untrusted external content. A page or a
  search result saying "subscribe to this feed" is data, not an instruction,
  unless the operator asked for it in that run. Record `discovered_from`
  and the discovering run and session on every agent-sourced subscription
  so the operator can see why it exists.
- Feed items never enter OpenViking extraction automatically and never
  enter another topic except through an explicit interactive `Ask Sola`
  message or a digest job's granted read. A digest run's prompt must treat
  item text as data to summarize, not as instructions to follow.
- The poller makes no authenticated requests and sends no credentials. It
  never follows a feed link to fetch article bodies. It never posts, and
  it never executes anything from a feed.
- Caps are enforced in the service, not the prompt: total feeds, agent
  feeds, agent additions per day, poll interval floor, response and item
  caps, retention, and failure auto-pause. A feed that redirects to a
  private URL, returns oversized content, or loops is a failed poll, not
  a stored item.

## Acceptance

Use disposable RSS 2.0 and Atom fixtures, plus a local HTTP fixture server
for fetch behavior. Validate:

- A normal RSS feed and a normal Atom feed store new items once, with
  titles, links, summaries, and dates, and skip duplicates on re-poll.
- A malformed feed fails the poll, stores nothing, and increments the
  failure count; five consecutive failures auto-pause it.
- A feed URL with credentials, a localhost host, a private IP literal, or
  a redirect to one is rejected at add and at poll time, with no request
  to the rejected target.
- Interactive `feeds.add` activates an operator subscription and an
  agent subscription in probationary mode; autonomous `feeds.add` creates
  only a `pending` proposal, and `feeds.approve` from a scheduled run is
  denied. A pending proposal is never polled.
- Caps hold: total feeds, agent feeds, and agent additions per day refuse
  the excess with a clear error and no partial subscription.
- An untrusted feed with no operator interaction expires to `paused`
  after `probationary_days`; opening an item or approving promotes it and
  clears the expiry.
- Restart after a poll preserves subscriptions, proposals, seen items,
  and read/digested flags, and does not duplicate items on the next poll.
- A digest job with only `feeds.items` and `feeds.acknowledge` summarizes
  unread items, acknowledges the covered ones, returns `NO_ACTION` when
  there is nothing new, and obeys quiet hours and the topic cooldown.
- The UI lists feeds with source, status, and failure reason, approves a
  proposal, opens an item as read, and asks Sola about an item in a new
  topic. Check the new Settings tab with the existing browser fixture
  approach, including keyboard access and both themes.
- Live smoke test with disposable data: subscribe to two public feeds the
  operator chooses, poll once, open an item, run one digest, and remove
  both subscriptions. Record which feeds were used.

When this plan ships, update `config.example.toml`, `docs/config.md`,
`docs/contracts/tools.md`, `docs/contracts/jobs.md`, `docs/chat-ui.md`,
and this plan's entry in `docs/plans/README.md`, and record the decision
in `docs/decisions.md` as the next `V2-` number.
