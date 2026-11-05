# Deliberate OpenViking first contact

Local configuration inspected on 2026-11-04:

- Endpoint on the Sola host: `http://127.0.0.1:1933`
- User: `default`
- Canonical root: `viking://user/default/memories`

Earlier operator settings named user `sola`, root `viking://user/sola/memories`
and credential reference `OPENVIKING_API_KEY`. Use the actual server's configured
identity and explicit credential reference when authentication is required.
The client was checked against `openviking-server 0.4.17.1`; the currently installed
server version was not reverified.

## Verification status

The operator has confirmed the OpenViking round-trip and reports that general
functionality looks good. The original live memory gate is satisfied on that
attestation. Probe run IDs/results were not supplied here; retain them privately
on the Sola host. The [local integration audit](../audits/local-integration.md)
records subsequent live API/model/tool/archival checks without repeating this probe.

The procedure below remains an operational reference for deliberate revalidation,
not an outstanding instruction to repeat writes. Live compaction and rollout
checks remain separate; see the [audit](../audits/implementation-status.md).

The client contract was checked against upstream tag `v0.4.17.1`, including session
creation/memory policy, async commit/task polling, completed-only archive reads,
and list-valued recall roots. That release's archive result lacks the newer
`status` field. The client requires a completed task and matching archive ID,
then accepts its completed-only overview. It sends only the supported
`keep_recent_count` commit option. Static source and mock checks are not live proof.

## On the Sola host

Use a checkout/package of this revision. Supply an operator token reference and
live SOUL/fragments in local `config.toml`, following `config.example.toml`.
Add these memory settings, keeping channels and jobs disabled:

```toml
[memory]
enabled = true
base_url = "http://127.0.0.1:1933"
roots = ["viking://user/default/memories"]
user = "default"
# If required by the server:
# api_key = { env = "OPENVIKING_API_KEY" }
trust_env = false
```

If using an environment credential reference, the process running uvicorn must
receive that environment variable. The explicit
`user` sends the version's `X-OpenViking-User` hint (useful for admin/root API keys)
and requires the returned session URI to match that user before adding messages.
A user-bound key must already belong to Sola; a mismatched identity fails closed. Provide
`fragments/recall-header.md` and every configured profile fragment, then run:

```sh
uv run sola check
uv run sola serve
```

Solakit's serve command uses port 8382; oMLX uses port 8000. Configure optional
`[model].api_key = { env = "OPENAI_API_KEY" }` if oMLX requires Bearer auth.
Use `uv run sola serve --log-level debug` for sanitized backend diagnostics.

In another shell using the same operator-token reference, choose a harmless,
unique verification code and deliberately run the scratch test:

```sh
uv run sola memory-test \
  --fact "The solakit scratch verification code is PROBE-UNIQUE-CODE." \
  --query "What is the solakit scratch verification code?" \
  --expected "PROBE-UNIQUE-CODE"
```

This creates a new shared scratch session and writes a real fact. It does not
alter Hermes, delete existing memory, or enable a bot/job. Keep the scratch
session and archives as evidence; do not automate or replay it as a recovery tool.

A passing response includes both run IDs. Inspect the first run's completed
extraction and the second run's recall and exact prompt. Success requires the
expected text in actual recall as well as that persisted prompt, not in a model's
claim or an unrelated persona fragment. Pending/failed extraction or missing
recall is a failed verification, not a reason to retry uncertain message writes.
Use `POST /api/runs/{id}/memory/refresh` to inspect acknowledged extraction.

For future revalidation, retain endpoint/version, probe result and run IDs in
private operator evidence. Phase 9 acceptance remains required independently.
Only after both gates pass should an operator enable a low-stakes pilot channel. Preserve SPEC §15's week-long pilot, shadow publishing,
and rollback stages. No such production activation is performed by this work.
