import json

import pytest
from sola_packs import calendar


@pytest.fixture
def setup(tmp_path, monkeypatch, pack_context):
    export = tmp_path / "events.ics"
    export.write_text(
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Test//EN\r\n"
        "BEGIN:VEVENT\r\nUID:daily\r\nDTSTART:20261105T090000Z\r\n"
        "DTEND:20261105T100000Z\r\nRRULE:FREQ=DAILY;COUNT=3\r\n"
        "EXDATE:20261106T090000Z\r\nSUMMARY:Morning <meeting>\r\nEND:VEVENT\r\n"
        "BEGIN:VEVENT\r\nUID:day\r\nDTSTART;VALUE=DATE:20261105\r\n"
        "DTEND;VALUE=DATE:20261106\r\nSUMMARY:Holiday\r\nEND:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    sidecar = tmp_path / "packs.toml"
    sidecar.write_text(
        f'[calendars.personal]\nfile = "{export}"\ntimezone = "America/Denver"\n'
    )
    monkeypatch.setenv("SOLA_PACKS_CONFIG", str(sidecar))
    registry, ctx = pack_context(
        ["sola_packs.calendar"],
        ["calendar.events"],
        read=[f"path:{tmp_path}"],
        capabilities=["calendar:personal:read"],
    )
    return registry, ctx, export


def invoke(setup, **kwargs):
    import asyncio

    registry, ctx, _ = setup
    return asyncio.run(
        registry.dispatch(
            "calendar.events",
            json.dumps(
                {
                    "calendar": "personal",
                    "after": "2026-11-05T00:00:00Z",
                    "before": "2026-11-08T00:00:00Z",
                    **kwargs,
                }
            ),
            ctx,
        )
    )


def test_agenda_recurrence_exceptions_all_day_and_bounds(setup):
    result = invoke(setup)
    assert not result.error
    data = json.loads(result.content.split("\n")[1])
    assert len(data["events"]) == 3
    day = next(event for event in data["events"] if event["uid"] == "day")
    assert day["all_day"] and day["start"] == "2026-11-05T07:00:00+00:00"
    assert day["end"] == "2026-11-06T07:00:00+00:00"
    assert "\\u003cmeeting\\u003e" in result.content
    assert '"has_more": true' in invoke(setup, limit=1).content
    assert (
        '"events": []'
        in invoke(
            setup, after="2026-11-08T00:00:00Z", before="2026-11-09T00:00:00Z"
        ).content
    )


def test_grants_and_scopes_before_read(setup, monkeypatch):
    _, ctx, _ = setup
    ctx.config.profiles[ctx.profile].resources = []

    def forbidden(*args):
        raise AssertionError("must not read")

    monkeypatch.setattr(calendar, "query", forbidden)
    assert "Missing resource" in invoke(setup).content


def test_filesystem_scope_and_invalid_export(setup):
    _, ctx, export = setup
    ctx.config.profiles[ctx.profile].read = "none"
    ctx.config.profiles[ctx.profile].workspace = []
    assert "outside profile" in invoke(setup).content
    ctx.config.profiles[ctx.profile].read = "workspace"
    ctx.config.profiles[ctx.profile].workspace = [f"path:{export}"]
    original = export.read_bytes()
    export.write_text("invalid")
    assert invoke(setup).error
    export.write_bytes(original.replace(b"FREQ=DAILY", b"FREQ=SECONDLY"))
    assert "unsupported expansion" in invoke(setup).content


def test_symlink_escape_and_oversized_export(setup, tmp_path):
    _, ctx, export = setup
    elsewhere = tmp_path / "outside.ics"
    elsewhere.write_bytes(export.read_bytes())
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    export.unlink()
    export.symlink_to(elsewhere)
    ctx.config.profiles[ctx.profile].read = "workspace"
    ctx.config.profiles[ctx.profile].workspace = [f"path:{allowed}"]
    assert "outside profile" in invoke(setup).content
    ctx.config.profiles[ctx.profile].read = "workspace"
    ctx.config.profiles[ctx.profile].workspace = [f"path:{tmp_path}"]
    elsewhere.write_text("x" * 1_000_001)
    assert "byte limit" in invoke(setup).content


def test_cancelled_and_overridden_instances(setup):
    _, _, export = setup
    content = export.read_text().replace("END:VCALENDAR", "")
    content += (
        "BEGIN:VEVENT\nUID:daily\nRECURRENCE-ID:20261107T090000Z\n"
        "DTSTART:20261107T110000Z\nDTEND:20261107T120000Z\n"
        "SUMMARY:Moved\nEND:VEVENT\n"
        "BEGIN:VEVENT\nUID:cancelled\nDTSTART:20261107T090000Z\n"
        "DTEND:20261107T100000Z\nSTATUS:CANCELLED\nEND:VEVENT\n"
        "END:VCALENDAR\n"
    )
    export.write_text(content)
    result = invoke(setup)
    assert not result.error
    events = json.loads(result.content.split("\n")[1])["events"]
    assert len(events) == 3
    assert events[-1]["summary"] == "Moved"
    assert events[-1]["start"] == "2026-11-07T11:00:00+00:00"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"limit": 201},
        {"after": "2026-01-01"},
        {"before": "2027-01-01T00:00:00Z"},
        {"calendar": "../personal"},
    ],
)
def test_invalid_queries(setup, kwargs):
    assert invoke(setup, **kwargs).error
