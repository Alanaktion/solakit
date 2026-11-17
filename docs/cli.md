# Operator CLI

The public executable has exactly four commands. Developer probes, evaluations,
index rebuild and migrations remain scripts.

- `sola serve --config config.toml --profile default` composes one app/worker and
  owns the data directory. Configure `[http]` binding/origins and the operator
  credential environment variable; see the [API deployment contract](contracts/api.md).
- `sola check --config config.toml [--profile default] [--explain]` validates the
  installation/configuration offline. Explain includes narrowed effective roots,
  tools, memory, budgets and provenance, with credential references redacted.
- `sola check --config config.toml --live [--explain]` explicitly contacts model
  health/model IDs and enabled memory health/version. It performs no inference,
  opens no state database and starts no worker, scheduler, extraction or recall.
  Disabled memory is reported without contact. Failure returns status 2 with safe
  diagnostics; clients close even after partial start or a failed check. This is
  service availability/API identity verification, not a model quality benchmark.
- `sola prompt 'Hello' --config config.toml [--hashes]` inspects private prompt
  content offline. `--frozen` selects exact input/time/history/recall;
  `--request-body` and `--manifest` inspect recorded artifacts. No live clients or
  background tasks are constructed. See `sola prompt --help` for overrides.
- `sola search 'exact phrase' --data-dir /absolute/data [--limit 20] [--session ID]`
  reads independent offline evidence search. It does not construct the app or
  start any services. Stale/missing search reports unavailable with status 2;
  rebuild explicitly with `python scripts/storage.py rebuild --data-dir …`.

The two `check` variants are options of the same command. A missing default
`~/.config/sola/config.toml` reports an unconfigured installation; an explicitly
invalid config or an unconfigured live check exits with status 2. Relative paths
resolve beside the selected TOML. `serve` disables access logs and proxy-header
trust and always uses one worker; a second data owner fails to start. Default
checks and prompt/search do not read login/model credentials.
