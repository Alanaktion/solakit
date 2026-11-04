# Phase 4: Tool registry and enforced capabilities

Status: planned

Depends on: phase 3

- Add configured module discovery, the decorator API, Pydantic argument schemas, `ToolContext`, and structured results/errors.
- Validate profile membership and declared write scopes at startup and again before dispatch.
- Enforce actual filesystem/repository access inside generic tools; manifest validation alone cannot constrain arbitrary paths.
- Implement timeouts, explicit output truncation, call logging, and nonblocking execution.
- Start with fixture tools and narrowly scoped file/repository primitives.

**Exit check:** Forbidden calls never execute; invalid scope grants prevent startup; a fixture module becomes available through configuration alone.

## Completion record

Record implementation, validation, and remaining limitations here before marking complete.
