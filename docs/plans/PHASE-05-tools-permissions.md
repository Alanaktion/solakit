# Phase 5 — Tool authoring and capability enforcement

Status: planned. Depends on [Phase 2](PHASE-02-records-configuration.md), [Phase 3](PHASE-03-evidence-search.md), and request/schema contracts in [Phase 4](PHASE-04-model-prompts.md). Unlocks safe tool execution.

## Outcome

A pack author can add an ordinary async Python tool through a small SDK. Authority is enforced at dispatch and guarded services, independently of anything the model says.

## Implementation slices

1. **Define the SDK and registry.** Support explicit tool declaration, argument schema, ToolContext, ToolResult, and recoverable ToolError. Choose SDK version compatibility rules and a contract test. Register only configured modules; reject duplicate names, invalid schemas, unsupported SDK requirements, and missing dependencies with useful errors. No automatic module discovery. Keep registry implementation separate from `tools/`.
2. **Resolve effective authority.** Compute loaded tools intersected with profile grants and run/job/child restrictions. Tool grants and workspace roots control access; there are no separate resource/effect grants. Session isolation, origin restrictions, budgets, and protected-path checks remain service invariants. Distinguish hidden, visible, and executable tools; loading deferred schemas never grants execution. Freeze the admitted ceiling and revalidate current grants at dispatch so revocation can narrow a queued/running task. New grants require a new run.
3. **Guard filesystem access.** Canonicalize roots and targets, check path components and symlinks, handle nonexistent write targets using verified parents, and use descriptor-based operations where needed to close check/use races on the supported OS. Deny writes through any `.git` component, symlink alias, or Git metadata/worktree indirection. Define overwrite/atomic-write behavior and managed runtime/code roots. No Git publication or general process/shell service is required for v1; domain packs and shell are deferred. Preserve `.git` write protection even without Git tools.
4. **Bound and classify execution.** Enforce argument/output sizes, timeout, call budget, cancellation, and structured error categories. Truncate model-facing results with explicit markers while retaining bounded referenced artifacts when needed. A timeout after an external send is potentially uncertain, not necessarily failed-with-no-effect. Register credentials with the redactor before network access. Validate types without guessing recipients, paths, or amounts.
5. **Ship the first tools.** Implement file read/write, transcript search, runtime status, and a memory port with unavailable behavior until Phase 7. Tools call application services rather than HTTP routes. Add an example extension outside core demonstrating one read and one guarded write; use a fake image adapter here and validate the real workflow in Phase 12. Specify intent/receipt data required before consequential calls.

## Deliverables, verification, and exit gate

Deliver SDK reference, registry, permission evaluator, guarded services, initial tools, example pack, and a tool/workspace access matrix. Test wildcard expansion, excluded tools guessed by the model, deferred visibility, empty tool set, stale grants, job narrowing, symlink escape, nonexistent parents, `.git` aliases, path traversal, schema failures, timeout, and output bounds.

Use disposable workspaces for writes. A minimal installed external pack must run without importing private core modules or modifying the loop. Deterministic permission tests must prove denial before an effect occurs.

Exit when the first end-to-end tool dispatch writes durable intent and receipt and all denial cases fail at enforcement boundaries. Trusted Python packs execute as the operator; these checks are not an OS sandbox against malicious pack code. Keep that boundary explicit in authoring documentation.
