# Follow-ups

Support two explicit requests. “Ask that researcher a narrower question”
continues a child topic. “Revisit this tomorrow at nine” creates a one-time job
in the current topic. Each uses the original context and the existing run or
schedule evidence.

## Continue delegated work

- Add `delegate.continue(session_id, task)` after
  [delegation](NEXT-AGENT-DELEGATION.md). The model copies `session_id` from a
  delegated result in the same parent topic. Use the same result shape as
  `delegate.run`. Reuse the child's local transcript and original worker
  profile, so the follow-up can refer to its earlier work.
- Before admission, verify direct parent-topic ownership and an idle, unarchived
  child. Intersect the original worker ceiling with current worker settings and
  the new parent run's frozen and current grants. Use the same slot reservation,
  budgets, origin restrictions and cancellation behavior as initial delegation.
- Include the previous child run ID in the new run's lineage. Link both runs in
  the parent evidence. A fresh interactive parent turn can continue prior work
  after a restart. An interrupted attempt still needs a fresh explicit request.

## Schedule a timed follow-up

- Add `jobs.follow_up(task, at)` as the small interactive tool for a user-requested
  one-time reminder or conversation revisit. `at` is an ISO timestamp with an
  offset. Use the configured background timezone when the operator omits one.
  Instruct the model to call the tool for explicit requests and ask for a time
  when the request leaves it unresolved. Return the accepted job ID and due
  time with offset so the model can confirm it.
- Validate a future instant and create an enabled `once:` job through the
  existing job service. Bind it to the active shared or isolated topic, retain
  a reference to the source run, and publish to the same topic. This first
  slice uses an empty job tool ceiling and no workspace roots. The existing
  Settings schedule editor covers broader work.
- Use the job service's origin checks and bind the definition to the admission
  topic. Prevent creation from scheduled, proactive, ephemeral, and delegated
  work. Use one durable create key for the same source run, normalized task and
  instant so a model repair cannot create two reminders.
- Add a **Schedule follow-up** action to the topic. It preselects the topic,
  then uses the same validation with a date/time picker. Route an authenticated
  request through `POST /api/v1/jobs` using an additive follow-up request shape
  and support `Idempotency-Key` for this create path. Use the existing occurrence
  and delivery state for due, paused, completed and failed feedback. The ordinary
  job rules handle overdue work, quiet hours, archiving and cancellation.

## Acceptance

Continue the right child after another parent turn and reject a child from a
different topic, a busy child, and a worker whose grants were revoked. For timed
follow-ups, test ambiguity around DST, retries after a lost receipt, restart
before the due instant, archived topics, quiet-hour delivery, and attempted
autonomous successors. Check that the UI shows the accepted local time and that
one delivery appears in the originating conversation.
