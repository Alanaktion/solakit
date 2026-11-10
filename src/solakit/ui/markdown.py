"""Presentation-only Markdown; source evidence is never rewritten."""

import re

import nh3
from markdown_it import MarkdownIt
from markupsafe import Markup

PARSER = MarkdownIt("commonmark", {"html": False}).enable("table").disable("image")
TAGS = {
    "p",
    "br",
    "hr",
    "em",
    "strong",
    "s",
    "blockquote",
    "pre",
    "code",
    "ul",
    "ol",
    "li",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "a",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
}


def preview_text(value: str) -> str:
    """Hold unfinished trailing blocks; completion always renders exact evidence."""
    lines = value.splitlines(keepends=True)
    roots = [t for t in PARSER.parse(value) if t.level == 0 and t.map]
    if not roots:
        return ""
    tail = roots[-1]
    assert tail.map is not None
    start = tail.map[0]
    if tail.type == "fence":
        closing = re.compile(
            r"^ {0,3}"
            + re.escape(tail.markup[0])
            + "{"
            + str(len(tail.markup))
            + r",}\s*$"
        )
        if not any(closing.match(line) for line in lines[start + 1 :]):
            return "".join(lines[:start])
    elif tail.type in {
        "bullet_list_open",
        "ordered_list_open",
        "blockquote_open",
        "table_open",
        "code_block",
    }:
        if not value.endswith("\n\n"):
            return "".join(lines[:start])
    elif lines:
        # Incomplete inline constructs should not flash literal Markdown syntax.
        last = lines[-1]
        if (
            any(last.count(mark) % 2 for mark in ("`", "**", "__"))
            or last.count("[") > last.count("]")
            or re.search(r"\]\([^)]*$", last)
        ):
            return "".join(lines[:-1])
    return value


def markdown(value: str) -> Markup:
    # Disable raw HTML/images at the parser, then sanitize the generated HTML too.
    # No remote image requests, inline styles, event attributes or unsafe schemes.
    return Markup(
        nh3.clean(
            PARSER.render(value),
            tags=TAGS,
            attributes={"a": {"href", "title"}, "ol": {"start"}},
            url_schemes={"http", "https", "mailto"},
            link_rel="nofollow noopener noreferrer",
        )
    )
