# Sub-agent delegation

Let Sola hand one bounded research or analysis task to an isolated child topic.
The parent receives a short report and a link to the child's evidence. This is
a follow-up to the initial delegation deferral in the [decision register](../decisions.md).

## Tool call

`delegate.run(task)` takes a self-contained brief with the goal, permitted source
references, and expected deliverable. Return `session_id`, `run_id`, `status` and,
on completion, `report`. A failed or cancelled child returns its status and
evidence IDs in the tool result so that the parent can report what happened.
Use the existing [follow-up design](NEXT-AGENT-FOLLOW-UPS.md) for later turns.

## Authority and execution

- Enable delegation by configuring one worker profile and granting
  `delegate.run` on the parent profile. The worker receives its configured
  prompt fragments and its own brief and transcript. Use isolated mode so the
  child keeps local context for later follow-ups while remote memory remains
  off. Parent conversation text reaches the child only through the brief.
- Intersect the worker's tools and roots with the parent's frozen and current
  authority. Limit workers to read-only tools in the first slice. Deny nested
  delegation, scheduling, and external effects by service checks. Permit calls
  only from interactive parent runs. Carry trusted child lineage through every
  admission and dispatch so holding a raw child ID or relabelling an origin
  cannot bypass those checks.
- Use the existing run coordinator, one inference semaphore, and ordinary prompt,
  run and receipt records. The parent waits outside the inference permit. Admit
  and start a child only when the coordinator can reserve a free active slot,
  honoring existing eligible queued work first. If there is no slot, return busy
  before creating the child. This prevents parents from occupying every slot while
  waiting for queued children. Allow one child call at a time per parent and
  charge child work to the parent's remaining model, tool and time budgets.
  Reserve a model call for the parent to report the outcome.
- Commit the child topic, run and parent tool-call lineage in one durable
  admission. Derive the admission key from the parent call ID. Use ordered run
  evidence to link the report and the child. On parent cancellation or timeout,
  cancel and settle the child before the tool returns. Child cancellation through
  the existing run API returns a cancelled result to the parent. On restart,
  settle unfinished children as interrupted, including admitted children whose
  parent will not resume.
- Surface child links in the parent turn and in its run evidence. Use the
  existing operator session/run views for the child transcript. Ordinary
  `POST /api/v1/runs` and job target selection reject delegated child topics.
  Show a bounded child report as tool data with the original evidence available
  to the authenticated operator.

## Acceptance

Run the parent and child end to end with one inference permit. Fill the active
slots and verify bounded refusal and queue progress. Test worker grant narrowing,
nested delegation denial, private context, cancellation in both directions,
restart between admission and execution, and linked evidence in the browser.
With the pinned text model, ask for a bounded research task and verify the final
answer distinguishes the child's result from an incomplete attempt.
