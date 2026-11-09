"""Progress uses active run state without exposing transcript tool arguments."""

from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader

from solakit.ui.routes import display_time
from solakit.ui.view import turns


@pytest.mark.parametrize(
    ("status", "result", "expected"),
    [
        ("running", False, "running"),
        ("running", True, "not recorded"),
        ("cancelled", False, "not recorded"),
        ("completed", True, "not recorded"),
    ],
)
def test_tool_progress(status, result, expected):
    messages = [
        {
            "role": "assistant",
            "run_id": "r",
            "ts": "2026-11-07T00:00:00Z",
            "tool_calls": [
                {
                    "id": "call",
                    "function": {
                        "name": "runtime.status",
                        "arguments": "PRIVATE ARGUMENTS",
                    },
                }
            ],
        }
    ]
    if result:
        messages.append(
            {
                "role": "tool",
                "run_id": "r",
                "ts": "2026-11-07T00:00:01Z",
                "tool_call_id": "call",
                "content": "PRIVATE RESULT",
            }
        )
    run = {
        "id": "r",
        "session": "s",
        "status": status,
        "profile": "interactive",
        "created_at": "2026-11-07T00:00:00Z",
    }
    history = turns(
        {"id": "s", "default_profile": "interactive", "messages": messages}, [run], {}
    )
    assert history[0]["items"][0]["state"] == expected
    assert history[0]["waiting_for_tool"] == (expected == "running")
    env = Environment(
        loader=FileSystemLoader(Path(__file__).parents[1] / "src/solakit/ui/templates"),
        autoescape=True,
    )
    env.filters["display_time"] = display_time
    env.filters["markdown"] = lambda text: text
    template = env.get_template("fragments/message_tail.html")
    html = template.render(turns=history)
    assert ('class="tool-spinner"' in html) == (expected == "running")
    assert ('class="typing-indicator"' in html) == (status == "running" and result)
    assert '<time datetime="2026-11-07T00:00:00Z" data-time>' in html
    assert "Nov 7, 2026 · 00:00:00 UTC</time>" in html
    assert "PRIVATE" not in html
    if status == "running" and result:
        history[0]["preview"] = "Partial response"
        assert "Sola is typing" in template.render(turns=history)
