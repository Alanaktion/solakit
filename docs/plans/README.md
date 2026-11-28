# Sola follow-up plans

`main-v2` is in production and the v1 cutover is complete. Start with the
[product scope](../product.md) and [decision register](../decisions.md) for
current behavior and constraints. See the [operator guides](../../README.md#documentation)
for setup and recovery.

## Follow-up tool plans

| Plan | First tool surface |
| --- | --- |
| [Email](NEXT-TOOLS-EMAIL.md) | `email.search`, `email.read`, `email.reply`, `email.send` |
| [Wikipedia lookups](NEXT-TOOLS-WIKI.md) | `wiki.summary` |

Reuse the [tool registry and dispatcher](../contracts/tools.md), including typed
arguments, eager schemas, budgets, and durable receipts. Give each integration
an operator-owned configuration section. Apply enablement and grants to both
prompt disclosure and dispatch, freeze resource identity at admission, and recheck
current settings before I/O. Wildcards can include newly registered tools at new
admission. Keep credential values private to the adapter and redactor.

Treat provider results as external content. Use the dispatcher's read checks
before credentialed reads and effect markers for sends or mutations. Preserve
uncertain effects for operator reconciliation. Validate changes with disposable
fixtures and record live checks separately from deterministic tests. Update the
configuration example and tool contract when a plan ships.

The [RSS feed-lite plan](NEXT-TOOLS-FEED.md) shipped with
[operator-managed subscriptions and a local reader](../chat-ui.md#reading-feeds)
and [isolated digest jobs](../contracts/jobs.md#feed-digests).

## Agent and operator follow-up plans

| Plan | First usable workflow |
| --- | --- |
| [Operator diagnostics](NEXT-OPERATOR-DIAGNOSTICS.md) | Check deployment health in Settings and compare effective authority with a recorded run. |
| [Operator probes and evaluations](NEXT-OPERATOR-EVALUATIONS.md) | Run fixed synthetic checks and compare results in Settings and the authenticated API. |
