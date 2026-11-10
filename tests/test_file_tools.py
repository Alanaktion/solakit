"""Windowed reads, whitespace-tolerant edits, overwrite, find and grep."""

import asyncio
import json

import pytest

from solakit.core.config import Profile
from solakit.core.schemas import Session
from solakit.core.store import Store
from solakit.services.fs import whitespace_replace
from solakit.tools import Registry, ToolContext, ToolError


@pytest.fixture
def files(config, tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    config.enabled_tools = ["files"]
    config.profiles["interactive"] = Profile(
        tools=["files.*"],
        write="workspace",
        workspace=[f"path:{root}"],
        write_workspace=[f"path:{root}"],
        tool_output_max_chars=600,
    )
    registry = Registry(config)
    store = Store(config.paths.data)
    store.create_session(Session(id="s"))
    ctx = ToolContext(store.session("s"), "interactive", config, store, "r")

    def call(name, **arguments):
        return asyncio.run(registry.dispatch(name, json.dumps(arguments), ctx))

    return root, call


def test_read_windows_large_files(files):
    root, call = files
    path = root / "big.txt"
    path.write_text("".join(f"line {n}\n" for n in range(1, 201)))
    first = call("files.read", path=str(path))
    assert first.content.startswith("     1\tline 1\n")
    footer = first.content.rsplit("\n", 1)[1]
    assert footer.startswith("[lines 1-") and "of 200; next offset=" in footer
    following = int(footer.rsplit("=", 1)[1].rstrip("]"))
    second = call("files.read", path=str(path), offset=following, limit=3)
    assert second.content.splitlines()[0].endswith(f"line {following}")
    assert second.content.endswith(
        f"[lines {following}-{following + 2} of 200; next offset={following + 3}]"
    )
    last = call("files.read", path=str(path), offset=199)
    assert last.content.endswith("[lines 199-200 of 200; end of file]")
    past = call("files.read", path=str(path), offset=500)
    assert past.error and "past the end" in past.content
    small = root / "small.txt"
    small.write_text("tiny\r\nfile")
    assert call("files.read", path=str(small)).content == "tiny\r\nfile"
    binary = root / "bin.dat"
    binary.write_bytes(b"\xff\xfe\x00")
    assert call("files.read", path=str(binary)).error


def test_whitespace_tolerant_edit_and_crlf():
    content = "a = 1   \r\nb = 2\r\nc = 3\r\n"
    updated, start, end = whitespace_replace(content, "a = 1\nb = 2", "a = 9\nb = 8")
    assert updated == "a = 9\r\nb = 8\r\nc = 3\r\n"
    assert (start, end) == (1, 2)
    with pytest.raises(ToolError, match="exactly once"):
        whitespace_replace("x\nx\n", "x", "y")
    with pytest.raises(ToolError):
        whitespace_replace("abc\n", "zzz", "y")


def test_edit_reports_mode_and_lines(files):
    root, call = files
    path = root / "conf.txt"
    path.write_text("one\ntwo  \nthree\n")
    result = call("files.edit", path=str(path), old="two\nthree", new="2\n3")
    assert result.content.endswith("replaced lines 2-3 (whitespace-insensitive)")
    assert path.read_text() == "one\n2\n3\n"
    result = call("files.edit", path=str(path), old="one", new="1")
    assert result.content.endswith("replaced lines 1-1")


def test_write_overwrite(files):
    root, call = files
    path = root / "note.txt"
    assert call("files.write", path=str(path), content="a").content.startswith(
        "Created"
    )
    refused = call("files.write", path=str(path), content="b")
    assert refused.error and "overwrite=true" in refused.content
    replaced = call("files.write", path=str(path), content="bb", overwrite=True)
    assert replaced.content == f"Replaced {path} (2 bytes)"
    assert path.read_text() == "bb"


def test_find_and_grep(files, tmp_path):
    root, call = files
    (root / "config").mkdir()
    (root / "config/server.toml").write_text('host = "x"\nport = 9123\n')
    (root / "config/app.toml").write_text('name = "demo"\n')
    (root / ".hidden").mkdir()
    (root / ".hidden/secret.toml").write_text("port = 1\n")
    (root / "blob.bin").write_bytes(b"port\0\0")
    outside = tmp_path / "outside.toml"
    outside.write_text("port = 2\n")
    (root / "link.toml").symlink_to(outside)
    found = json.loads(call("files.find", pattern="*.toml", path=str(root)).content)
    assert [m["path"] for m in found["matches"]] == [
        "config/app.toml",
        "config/server.toml",
    ]
    deep = json.loads(
        call("files.find", pattern="config/**/*.toml", path=str(root)).content
    )
    assert [m["path"] for m in deep["matches"]] == [
        "config/app.toml",
        "config/server.toml",
    ]
    grep = call("files.grep", query="PORT", path=str(root), context=1)
    assert grep.content.splitlines() == [
        'config/server.toml-1- host = "x"',
        "config/server.toml:2: port = 9123",
    ]
    assert call("files.grep", query="nothing", path=str(root)).content == "No matches."
    assert call("files.grep", query="(", regex=True, path=str(root)).error
    limited = call("files.grep", query="=", path=str(root), context=0, limit=1)
    assert limited.content.endswith(
        "[search incomplete; narrow the search or inspect skipped long lines]"
    )
    assert call("files.find", pattern="*.toml", path=str(tmp_path)).error
