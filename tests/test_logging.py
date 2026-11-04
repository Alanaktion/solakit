"""Exercise the actual formatters that consume redacted logging records."""

import logging

import pytest
from uvicorn.logging import AccessFormatter, DefaultFormatter

from solakit.core.secrets import RedactingFilter, Redactor


@pytest.mark.parametrize("colors", [False, True])
def test_uvicorn_access_formatter_preserves_arguments_and_redacts(colors):
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1234", "GET", "/?key=private-value", "1.1", 303),
        None,
    )
    redaction = RedactingFilter(Redactor(["private-value"]))
    # Multiple handlers may filter the same record: redaction is repeatable.
    assert redaction.filter(record)
    assert redaction.filter(record)
    assert len(record.args) == 5
    assert record.args[-1] == 303
    output = AccessFormatter(
        '%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s',
        use_colors=colors,
    ).format(record)
    assert "127.0.0.1:1234" in output
    assert "GET /?key=[redacted] HTTP/1.1" in output
    assert "303 See Other" in output
    assert "private-value" not in output


@pytest.mark.parametrize("colors", [False, True])
def test_uvicorn_startup_color_message_keeps_interpolation(colors):
    record = logging.LogRecord(
        "uvicorn.error",
        logging.INFO,
        "",
        1,
        "Uvicorn running on %s://%s:%d (private-value)",
        ("http", "private-host", 8382),
        None,
    )
    record.color_message = "Uvicorn running on \x1b[1m%s://%s:%d\x1b[0m (private-value)"
    RedactingFilter(Redactor(["private-value", "private-host"])).filter(record)
    output = DefaultFormatter("%(levelprefix)s %(message)s", use_colors=colors).format(
        record
    )
    assert "http://[redacted]:8382" in output
    assert "private-value" not in output
    assert "private-host" not in output
    assert "%s" not in output and "%d" not in output


def test_structured_arguments_and_exception_are_redacted():
    try:
        raise ValueError("private-value")
    except ValueError:
        import sys

        info = sys.exc_info()
    record = logging.LogRecord(
        "solakit",
        logging.ERROR,
        "",
        1,
        "details %s",
        ({"API_KEY": "unknown-secret", "detail": "private-value"},),
        info,
    )
    RedactingFilter(Redactor(["private-value"])).filter(record)
    output = logging.Formatter("%(message)s").format(record)
    assert "ValueError: [redacted]" in output
    assert "private-value" not in output
    assert "unknown-secret" not in output
