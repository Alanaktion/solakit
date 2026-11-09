"""Local icons work in full pages and independently rendered fragments."""

import re
from pathlib import Path
from xml.etree import ElementTree

from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader

from solakit.main import create_app

UI = Path(__file__).resolve().parents[1] / "src/solakit/ui"


def test_template_icon_references_resolve_to_local_symbols():
    sprite = ElementTree.parse(UI / "static/vendor/lucide.svg").getroot()
    symbols = {symbol.attrib["id"] for symbol in sprite}
    referenced = set()
    for template in (UI / "templates").rglob("*.html"):
        source = template.read_text()
        names = re.findall(r'icon\("([a-z-]+)"\)', source)
        if names:
            assert 'from "macros/icons.html" import icon' in source, template
        referenced.update(names)
        assert not any(char in source for char in "←→↑↓↗⌄＋⌁☾↻✧"), template
    assert referenced == symbols
    assert all(symbol.attrib["viewBox"] == "0 0 24 24" for symbol in sprite)


def test_icon_macro_is_decorative_and_escapes_names():
    env = Environment(loader=FileSystemLoader(UI / "templates"), autoescape=True)
    icon = env.get_template("macros/icons.html").module.icon
    html = str(icon('arrow-left" onload="alert(1)'))
    assert 'aria-hidden="true"' in html
    assert 'focusable="false"' in html
    assert 'onload="' not in html
    assert "&#34;" in html


def test_local_sprite_and_icons_in_login_and_chat(config, mock_model):
    with TestClient(create_app(config=config, model=mock_model)) as client:
        login = client.get("/login")
        assert "/static/vendor/lucide.svg#arrow-right" in login.text
        sprite = client.get("/static/vendor/lucide.svg")
        assert sprite.status_code == 200
        assert sprite.headers["content-type"].startswith("image/svg+xml")
        client.post("/login", data={"token": "operator-test-token"})
        client.post(
            "/sessions",
            data={
                "id": "icons",
                "default_profile": "interactive",
                "memory_scope": "ephemeral",
            },
        )
        chat = client.get("/sessions/icons")
        assert chat.status_code == 200
        assert "/static/vendor/lucide.svg#arrow-up" in chat.text
        assert 'aria-label="Send message"' in chat.text
        assert "/static/vendor/lucide.svg#plus" in chat.text


def test_independent_message_fragment_keeps_evidence_literal():
    from solakit.ui.routes import TEMPLATES

    html = TEMPLATES.get_template("fragments/message_tail.html").render(
        session={"id": "icons"},
        turns=[
            {
                "boundary": True,
                "date": "2026-11-09",
                "content": "Literal evidence: ← → <script>not markup</script>",
                "archive_number": "1",
            }
        ],
    )
    assert "/static/vendor/lucide.svg#arrow-right" in html
    assert "Literal evidence: ← → &lt;script&gt;not markup&lt;/script&gt;" in html
    empty = TEMPLATES.get_template("fragments/message_tail.html").render(turns=[])
    assert "/static/vendor/lucide.svg#sparkles" in empty
