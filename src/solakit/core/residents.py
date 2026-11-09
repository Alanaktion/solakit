"""Advisory resident-tool suggestions from historical tool logs.

Residents stay operator-owned configuration: nothing here changes runtime
behaviour. Suggestions use coarse tiers with hysteresis (add at >= 25% of runs,
drop below 10%) so repeated runs propose the same set.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

ADD_SHARE, DROP_SHARE = 0.25, 0.10


def _last_line(path: Path) -> dict | None:
    try:
        lines = path.read_bytes().splitlines()
        return json.loads(lines[-1]) if lines else None
    except (OSError, ValueError, IndexError):
        return None


def usage(data: Path, profile: str, days: int, now: datetime | None = None):
    """(runs in window, {tool: runs that called it successfully})."""
    since = (now or datetime.now(UTC)) - timedelta(days=days)
    runs, counts = 0, {}
    for events in sorted((data / "runs").glob("*.events.jsonl")):
        record = _last_line(events)
        if not record or record.get("profile") != profile:
            continue
        try:
            created = datetime.fromisoformat(record["created_at"])
        except (KeyError, ValueError, TypeError):
            continue
        if created < since:
            continue
        runs += 1
        log = events.with_name(events.name.replace(".events.jsonl", ".tools.jsonl"))
        used = set()
        try:
            for line in log.read_bytes().splitlines():
                item = json.loads(line)
                if not item.get("result", {}).get("error"):
                    used.add(item.get("name"))
        except (OSError, ValueError):
            pass
        for name in used:
            counts[name] = counts.get(name, 0) + 1
    return runs, counts


def suggest(config, registry, profile: str, *, days=30, min_runs=100, now=None):
    granted = registry.tool_names(profile)
    current = list(config.profiles[profile].tool_resident)
    runs, counts = usage(config.paths.data, profile, days, now)
    shares = {name: counts.get(name, 0) / runs if runs else 0.0 for name in granted}
    proposed = [n for n in current if n in granted and shares[n] >= DROP_SHARE]
    proposed += sorted(
        (n for n in granted if n not in proposed and shares[n] >= ADD_SHARE),
        key=lambda n: (-shares[n], n),
    )
    size = len(json.dumps([registry.tools[n].schema() for n in proposed]).encode())
    return {
        "profile": profile,
        "runs": runs,
        "enough_evidence": runs >= min_runs,
        "shares": {n: round(s, 3) for n, s in sorted(shares.items()) if s},
        "current": current,
        "proposed": proposed,
        "added": [n for n in proposed if n not in current],
        "removed": [n for n in current if n not in proposed],
        "schema_bytes": size,
    }
