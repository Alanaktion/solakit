# Runtime regression scenarios

Use disposable state, workspaces, and effect destinations. Exercise persistence
and authority with deterministic fixtures. Use the configured model, image, and
memory services to verify live capability. Review conversation continuity,
keyboard and mobile usability, and recovery controls in the browser.

| Scenario | Exercise | Expected behavior |
| --- | --- | --- |
| Conversation and identity | Ask an ordinary question in a shared topic. Repeat with OpenViking unavailable, and disconnect during generation. | Identity files match recorded request hashes, optional recall degradation is visible, and accepted work completes once and reappears on reconnect. |
| Files and images | Read a brief, write a text artifact, and generate an image in a granted workspace. Exercise a denied path and interrupted provider submission. | Receipts match output bytes and the image is available through an authorized reference. Denial precedes effects. Uncertain submissions retain a reconciliation reference and never replay automatically. |
| Memory and isolation | Extract a generated fact into an isolated OpenViking test account, then ask about it in a fresh shared topic. Repeat in isolated and ephemeral modes. | Shared recall records actual sources. The other modes make no OpenViking calls and cannot bypass isolation via transcript tools. Extraction can continue after reply delivery. |
| Scheduling and proactive work | Run a bound wake with and without pending work. Include quiet hours, an archived target, and a request to schedule a successor. | Each occurrence has one durable trace. No-action stays private. Notifications defer and coalesce, archived targets remain blocked, and autonomous successor creation is denied. |
| Restart and restore | Stop after admission, after tool intent, and after simulated provider acceptance. Rebuild search and restore a coherent backup into a new directory. | Acknowledged evidence, schedules, and consumed occurrences survive. Untouched queued work is recoverable and active work is interrupted. Search rebuild leaves operational state intact and uncertain effects remain visible without replay. |
| Legacy data preservation | Import synthetic archive, unknown-field, and torn-tail examples into fresh data. | Original bytes remain verifiable, interrupted history does not become a successful run, and imported jobs stay disabled. |

See the [evaluation guide](../../evals/README.md) for repeatable model and tool
reliability measurements.
