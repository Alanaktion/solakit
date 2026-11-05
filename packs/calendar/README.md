# Calendar read pack

`solakit-packs-calendar` reads a named, operator-exported UTF-8 ICS file. It has
no network access or calendar write API. Refresh the export through your existing
calendar client. Install with `uv sync --frozen --all-packages`, set
`SOLA_PACKS_CONFIG`, load `sola_packs.calendar`, and grant `calendar.events` plus
`calendar:personal:read` and a filesystem read scope containing the export.

```toml
[calendars.personal]
file = "/absolute/path/to/calendar.ics"
timezone = "America/Denver" # Floating and all-day event interpretation.
max_bytes = 1000000
```

`calendar.events(calendar, after, before, limit=50)` requires timezone-aware
ISO timestamps and an exclusive end, with a maximum 31-day range and 200 results.
Results include UTC start/end, all-day flag, UID, summary, location, description
and `has_more`. External text is marked as data. All-day end dates are exclusive.
Daily/weekly/monthly/yearly recurrence, exceptions and overridden instances use
the ICS library; sub-daily or BY* rules fail explicitly. Export size and component
count are bounded. A filesystem grant is checked on each read, including symlinks.
