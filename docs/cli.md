# Operator CLI

Run `sola --help` for the command list.

- `sola serve --config config.toml --profile default` composes one app/worker and
  owns the data directory. Configure `[http]` binding/origins and the operator
  credential environment variable; see the [API deployment contract](contracts/api.md).
- `sola service create|start|restart|stop|status [--config PATH]` manages a macOS LaunchAgent.
  See [Run Sola at login](#run-sola-at-login).
- `sola check --config config.toml [--profile default] [--explain]` validates the
  installation/configuration offline. Explain includes narrowed effective roots,
  tools, memory, budgets and provenance, with credential references redacted.
- `sola check --config config.toml --live [--explain]` checks the configured text and optional
  vision model IDs, plus enabled memory health/version. It performs no inference,
  opens no state database and starts no worker, scheduler, extraction or recall.
  Disabled memory is reported without contact. Failure returns status 2 with safe
  diagnostics; clients close even after partial start or a failed check. This is
  service availability/API identity verification, not a model quality benchmark.
- `sola doctor --config config.toml [--profile default] [--quick] [--json]` checks
  service availability, streams a synthetic reply, and asks the model to call a
  dummy locker lookup with an integer argument. The next completion must include
  the fresh code from the tool result. Full mode also checks separate reasoning
  output and retrieval of a fresh code from a longer prompt. It times two identical
  requests so the operator can inspect repeated-prefix latency. Both modes report
  visible tool schema size and per-probe outcomes. Doctor
  uses temporary storage, excludes identity and memory fragments, and discards
  request evidence on exit. Failed diagnostics exit 1. Invalid configuration exits 2.
- `sola config explain [PROFILE] --config config.toml` prints redacted effective
  settings, provenance, visible interactive tool names and total schema bytes as JSON.
- `sola config diff old.toml new.toml` shows before/after grants, visible tools,
  and resolved workspace roots for each changed interactive profile. Each TOML
  resolves paths relative to its own directory. An empty JSON object means the
  compared fields match.
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

Ctrl+C or SIGTERM starts graceful shutdown. `serve` uses `shutdown_timeout_seconds`
as the deadline for draining active HTTP connections before runtime cleanup
begins. Runtime cleanup has its own deadline of the same length. A second Ctrl+C
or SIGTERM terminates the process immediately, skipping any remaining cleanup.

## Run Sola at login

Run these commands as the macOS user who will run the agent, while signed into
the desktop. `create` installs a disabled LaunchAgent in `~/Library/LaunchAgents`.
`start` launches it now and enables automatic launch at future logins.
`restart` shuts down a running instance gracefully and launches a fresh process
to pick up code, config, or environment changes. It also starts a stopped
instance. launchd restarts the runtime if it exits while enabled. `stop` unloads
it and disables future launches until the next `start` or `restart`. `status`
reports the launchd process state, PID when running, and the installed agent's
log paths under `~/Library/Logs/sola`.

Each command defaults to `~/.config/sola/config.toml`. Pass the same `--config`
path to manage a second instance. Each concurrently running instance needs a
separate `data_dir` and a distinct HTTP host/port binding. Set `http.allowed_origins`
to match its URL. Relative paths within a config resolve beside that TOML.

```sh
sola service create --env-file ~/.config/sola/.env
sola service start
sola service status
sola service restart
sola service create --config ~/.config/sola/second.toml --env-file ~/.config/sola/second.env
sola service start --config ~/.config/sola/second.toml
sola service stop --config ~/.config/sola/second.toml
```

`--env-file` accepts the same dotenv format as `uv run --env-file .env`.
Have `uv` on your `PATH` when running `create` with this option. Give the file
mode `600` and keep it out of version control. Use the variable names specified
in the runtime config, including `http.password_env` for operator login.

```dotenv
SOLA_OPERATOR_PASSWORD = "replace-with-your-password"
```

The plist stores the credential file and uv executable paths. At launch, uv
loads the file and runs Sola with the existing Python installation. The service
suppresses uv's dotenv parse warnings because they can include credential values.
Restart the service after editing the file. Terminal exports belong to
the shell session. Supply any runtime credentials through the file or the user's
launchd environment. If you move the file, Python installation, or uv binary,
stop the service, remove the plist shown by `create`, then recreate and start it.
