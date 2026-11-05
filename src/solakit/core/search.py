"""Queries over the disposable message FTS index; no transcript writes or recovery."""

import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from solakit.core.secrets import Redactor

FTS_SCHEMA = """CREATE VIRTUAL TABLE messages_fts USING fts5(
    content, role, kind, channel,
    session_id UNINDEXED, archive UNINDEXED, line_no UNINDEXED,
    ts UNINDEXED, run_id UNINDEXED, tokenize = 'trigram'
)"""
FTS_INSERT = "INSERT INTO messages_fts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"


class SearchUnavailable(RuntimeError):
    """The disposable search index needs rebuilding."""


def timestamp(value: str | datetime) -> str:
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(UTC).isoformat(timespec="microseconds")
    except (ValueError, AttributeError, TypeError):
        raise ValueError("Search timestamps require ISO 8601 with a timezone") from None


def match_query(query: str) -> str:
    if (
        not isinstance(query, str)
        or not query.strip()
        or len(query) > 2000
        or "\x00" in query
    ):
        raise ValueError("Search query must contain 1–2000 characters")
    clauses = []
    position = 0
    while position < len(query):
        if query[position].isspace():
            position += 1
            continue
        scope = re.match(r"(content|role|kind|channel):", query[position:])
        column = scope[1] if scope else ""
        position += len(scope[0]) if scope else 0
        text = ""
        if position < len(query) and query[position] == '"':
            position += 1
            while position < len(query):
                character = query[position]
                if query[position : position + 2] in {'""', '\\"'}:
                    text += '"'
                    position += 2
                elif character == '"':
                    position += 1
                    break
                else:
                    text += character
                    position += 1
            else:
                raise ValueError("Search query contains an unclosed quote")
            if position < len(query) and not query[position].isspace():
                raise ValueError("Search phrases must be separated by whitespace")
        else:
            start = position
            while position < len(query) and not query[position].isspace():
                position += 1
            text = query[start:position]
        if not text:
            raise ValueError("Search terms cannot be empty")
        quoted = '"' + text.replace('"', '""') + '"'
        clauses.append((column + ":" if column else "") + quoted)
        if len(clauses) > 32:
            raise ValueError("Search query must contain 1–32 terms or quoted phrases")

    return " AND ".join(clauses)


def search_index(
    root: Path,
    query: str,
    *,
    session_id: str | None = None,
    role: str | None = None,
    kind: str | None = None,
    after: str | datetime | None = None,
    before: str | datetime | None = None,
    limit: int = 50,
    redactor: Redactor | None = None,
) -> list[dict]:
    match = match_query(query)
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("Search limit must be between 1 and 200")
    if role is not None and role not in {"system", "user", "assistant", "tool"}:
        raise ValueError("Invalid search role")
    if kind is not None and kind not in {"message", "compaction"}:
        raise ValueError("Invalid search kind")
    start, end = (
        timestamp(after) if after is not None else None,
        timestamp(before) if before is not None else None,
    )
    if start and end and start >= end:
        raise ValueError("Search after must precede before")
    clauses, parameters = ["messages_fts MATCH ?"], [match]
    for column, value, comparison in (
        ("session_id", session_id, "="),
        ("role", role, "="),
        ("kind", kind, "="),
        ("ts", start, ">="),
        ("ts", end, "<"),
    ):
        if value is not None:
            clauses.append(f"{column} {comparison} ?")
            parameters.append(value)
    parameters.append(limit)
    sql = (
        "SELECT session_id, archive, line_no, ts, run_id, role, kind, channel, "
        "bm25(messages_fts) AS rank, "
        "snippet(messages_fts, 0, '', '', '', 64) AS snippet, content "
        "FROM messages_fts WHERE "
        + " AND ".join(clauses)
        + " ORDER BY rank, rowid LIMIT ?"
    )
    try:
        # Offline callers never create a database or recover/finalize runs.
        uri = (root.expanduser().resolve() / "app.db").as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(sql, parameters).fetchall()
    except sqlite3.Error:
        raise SearchUnavailable(
            "Search index unavailable; rebuild app.db from evidence"
        ) from None
    masker = redactor or Redactor()
    results = []
    for row in rows:
        hit = dict(row)
        content = hit.pop("content")
        hit["snippet"] = masker.excerpt(content, hit["snippet"])
        results.append(masker.redact(hit))
    return results
