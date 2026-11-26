# macOS service verification

Verified on 2026-11-25 with macOS 27.2 and Python 3.15.2 from checkout
`8067416` plus the service changes.

The live smoke check started two LaunchAgents with disposable configurations,
data directories, loopback ports, and credential files. Both returned a healthy
`/health` response and accepted a bearer login with their own password. The
passwords were absent from the generated plists. Stopping one agent left the
other healthy. `launchctl print-disabled` confirmed the disabled and re-enabled
states. An enabled agent received `SIGTERM` and returned to health with a new
PID. The smoke check stopped the agents and removed the disposable files.

The dotenv follow-up used uv 0.12.9 and a private `.env` file in a directory
with spaces. A live LaunchAgent accepted the password assembled through dotenv
variable expansion. Launching and stopping through uv preserved graceful shutdown
and automatic respawn. The plist and service logs omitted the fixture password.

The focused `tests/test_service.py` checks the CLI default, relative and symlink
aliases, per-config identity, repeated operations, launchctl errors, and
credential-file validation. The full suite passed all 739 tests. Locked Ruff lint
and format checks, strict mypy, and the clean wheel smoke test passed.

A full logout and login was not exercised. Login startup was checked through
launchd's persisted enable and disable settings.

The manual restart follow-up started two stopped, disposable agents with
`restart`, then restarted one while the other was running. The old process
completed shutdown before the replacement came up with a new PID and picked up
an updated `.env` password. The other agent kept serving with the same PID.
Focused service and CLI smoke tests, Ruff, and strict mypy passed.
