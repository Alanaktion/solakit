# Ideas

- [ ] Multiple assistants with OpenViking account isolation

- [ ] Expand functionality of wakes/time-based triggers:

> On your flexibility question: it's a partial yes, and the split matters. What's genuinely extensible:
> 
> New time-based triggers are cheap. The schedule grammar is just a string parsed in one place (once: / cron:), occurrence identity is a hash of job + generation + instant, and everything executes through the ordinary run loop. An interval schedule or another cron dialect is a small branch.
> New delivery targets slot in cleanly. Delivery is a separate state machine (pending → sending → delivered, with dedup, cooldown, expiry) — an external notification adapter would plug in there without touching execution.
> The tick loop is the right seam for polling. The 1-second APScheduler wake evaluates whatever's due; a condition evaluator could live there.
> 
> What would need redesign, not extension:
> 
> Event/condition triggers (my hooks). There's no condition store, no event bus, no evaluator. A hook-style "wake me when X changes" needs condition definitions on the Job record, an evaluator, and occurrence identity keyed on events rather than instants — a new subsystem, not a new schedule string.
> Inbound-message wakes. The only out-of-band entry is manual(), which needs an interactive caller and a request ID. There's no ingress path for mail, chat, or device events arriving.
> Chaining. This is the big one: Muse chains wakes constantly, but Phase 9 forbids it by design — deny_autonomous_successor() runs on every schedule mutation and tool call, via a contextvar that survives relabeling tricks, and there's a test proving even a relabeled interactive profile can't slip past it. Supporting chains would mean redesigning the autonomy model (e.g. successors only to a declared parent), not adding a trigger type.
> External delivery. Local topic publication only; the contract is explicit that no notification provider exists in v1.
