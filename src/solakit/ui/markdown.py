"""Presentation-only Markdown; source evidence is never rewritten."""

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
