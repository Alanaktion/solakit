"""FTS addresses evidence, never owns it; all query syntax is constructed locally."""

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from solakit.cli import main
from solakit.core.archival import archive_session
from solakit.core.compaction import prepare_rotation
from solakit.core.schemas import Message, Origin, Run, RunStatus, Session
from solakit.core.search import SearchUnavailable, search_index
from solakit.core.secrets import Redactor
from solakit.core.store import Store
from solakit.main import create_app


def append(store, content, *, session="s", role="user", **kwargs):
    store.create_session(Session(id=session))
    message = Message(
        role=role,
        content=content,
        origin=Origin(channel="discord"),
        run_id="run",
        **kwargs,
    )
    store.append_message(session, message)
    return message


def test_incremental_substrings_tool_names_filters_and_rank(config):
    store = Store(config.paths.data)
    first = datetime(2026, 1, 2, tzinfo=UTC)
    append(store, "A moonlit journal entry", ts=first)
    append(
        store,
        None,
        role="assistant",
        tool_calls=[
            {
                "id": "publish",
                "function": {"name": "journal.publish", "arguments": "{}"},
            }
        ],
        ts=first + timedelta(hours=1),
    )
    append(store, "A moonlit summary", role="assistant", kind="compaction")
    append(store, "A moonlit other session", session="other")
    hits = store.search("nal.pub role:assistant channel:discord")
    assert len(hits) == 1
    assert hits[0]["archive"] == "active" and hits[0]["line_no"] == 2
    assert hits[0]["run_id"] == "run" and hits[0]["rank"] < 0
    assert "journal.publish" in hits[0]["snippet"]
    assert len(store.search("moonlit", session_id="s", role="user")) == 1
    assert len(store.search("moonlit", kind="compaction")) == 1
    assert len(store.search("moonlit", limit=1)) == 1
    assert len(store.search('"moonlit journal"')) == 1
    assert store.search(
        "entry", after="2026-01-02T01:00:00+01:00", before="2026-01-02T00:01:00Z"
    )
    assert not store.search("entry", before=first)
    assert not store.search("mo")  # FTS5 trigram requires at least three characters.


def test_rebuild_numeric_archives_and_physical_recovered_lines(config):
    store = Store(config.paths.data)
    message = append(store, "active needle")
    for number in [10, 2, 1]:
        path = store.root / f"sessions/s.jsonl.{number}"
        path.write_text(
            message.model_copy(update={"content": "archive needle"}).model_dump_json()
            + "\n"
        )
    active = store.root / "sessions/s.jsonl"
    with active.open("ab") as stream:
        stream.write(b'{"torn"')
    append(store, "last needle")  # Seals the damaged physical line 2, appends line 3.
    store.rebuild()
    hits = store.search("needle")
    assert {(h["archive"], h["line_no"]) for h in hits} == {
        ("s.jsonl.1", 1),
        ("s.jsonl.2", 1),
        ("s.jsonl.10", 1),
        ("active", 1),
        ("active", 3),
    }
    with sqlite3.connect(store.root / "app.db") as db:
        assert [
            r[0] for r in db.execute("SELECT archive FROM messages_fts ORDER BY rowid")
        ] == ["s.jsonl.1", "s.jsonl.2", "s.jsonl.10", "active", "active"]
    before = {p.name: p.read_bytes() for p in (store.root / "sessions").iterdir()}
    (store.root / "app.db").unlink()
    restored = Store(store.root)
    assert restored.search("needle") == hits
    assert {
        p.name: p.read_bytes() for p in (store.root / "sessions").iterdir()
    } == before


def test_rotations_refresh_addresses_and_index_summary(config):
    import hashlib

    store = Store(config.paths.data)
    append(store, "needle before archive")
    archive_session(store, "s")
    append(store, "needle after archive")
    assert {(h["archive"], h["line_no"]) for h in store.search("needle")} == {
        ("s.jsonl.1", 1),
        ("active", 1),
    }
    active = store.root / "sessions/s.jsonl"
    summary = Message(
        role="assistant",
        content="needle summarized",
        kind="compaction",
        origin=Origin(channel="compaction"),
        run_id="compact",
    )
    prepare_rotation(
        store,
        "s",
        {
            "stage": "prepared",
            "archive": "s.jsonl.2",
            "source_sha256": hashlib.sha256(active.read_bytes()).hexdigest(),
            "boundary": summary.model_dump(mode="json"),
        },
    )
    hits = store.search("needle")
    assert {h["archive"] for h in hits} == {"s.jsonl.1", "s.jsonl.2", "active"}
    assert store.search("needle", kind="compaction")[0]["archive"] == "active"
    store.rebuild()
    assert store.search("needle") == hits


def test_fts_failure_never_prevents_append_and_rebuild_repairs(config):
    store = Store(config.paths.data)
    append(store, "first needle")
    with sqlite3.connect(store.root / "app.db") as db:
        db.execute("DROP TABLE messages_fts")
    append(store, "second needle")
    assert len(store.transcript("s")) == 2
    assert store.storage_health()["issues"] == [
        {"file": "app.db", "kind": "fts_index_unavailable", "offset": None}
    ]
    with pytest.raises(SearchUnavailable):
        store.search("needle")
    store.rebuild()
    assert len(store.search("needle")) == 2
    assert store.storage_health()["status"] == "ok"


def test_fts_unavailable_at_startup_still_allows_evidence(config, monkeypatch):
    monkeypatch.setattr("solakit.core.store.FTS_SCHEMA", "invalid sql")
    store = Store(config.paths.data)
    append(store, "needle")
    assert len(store.transcript("s")) == 1
    assert store.storage_health()["status"] == "degraded"


@pytest.mark.parametrize(
    "query",
    [
        "needle OR absent",
        "needle NOT absent",
        "needle) OR (absent",
        "session_id:s",
        '"needle OR absent"',
    ],
)
def test_user_operators_are_literal_not_match_syntax(config, query):
    store = Store(config.paths.data)
    append(store, "needle")
    assert store.search(query) == []
    assert store.search("needle", session_id="s' OR 1=1 --") == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"query": '"unclosed'},
        {"query": ""},
        {"query": "x" * 2001},
        {"query": "needle\x00"},
        {"query": "needle", "limit": 0},
        {"query": "needle", "limit": 201},
        {"query": "needle", "role": "bad"},
        {"query": "needle", "kind": "bad"},
        {"query": "needle", "after": "yesterday"},
        {"query": "needle", "before": "2026-01-01"},
    ],
)
def test_query_validation(config, kwargs):
    with pytest.raises(ValueError):
        Store(config.paths.data).search(**kwargs)


def test_damaged_archive_not_indexed_other_sessions_remain_available(config):
    store = Store(config.paths.data)
    append(store, "needle damaged session")
    append(store, "needle healthy session", session="healthy")
    (store.root / "sessions/s.jsonl.1").write_text('{"role":"user"}\n')
    store.rebuild()
    assert [h["session_id"] for h in store.search("needle")] == ["healthy"]
    assert store.storage_health()["status"] == "degraded"


def test_api_auth_validation_and_masking_clipped_secrets(config, mock_model):
    with TestClient(create_app(config=config, model=mock_model)) as client:
        assert client.get("/api/search", params={"query": "needle"}).status_code == 401
        assert (
            client.post("/login", data={"token": "operator-test-token"}).status_code
            == 200
        )
        store = client.app.state.store
        secret = "private-" + "0123456789abcdef" * 20
        store.redactor.values += (secret,)
        append(store, "needle " + secret + " ending")
        response = client.get("/api/search", params={"query": "needle"})
        assert response.status_code == 200
        hit = response.json()[0]
        assert "[redacted]" in hit["snippet"]
        assert "0123456789abcdef" not in hit["snippet"]
        with sqlite3.connect(store.root / "app.db") as db:
            assert (
                secret in db.execute("SELECT content FROM messages_fts").fetchone()[0]
            )
        assert client.get("/api/search", params={"query": '"bad'}).status_code == 422
        with sqlite3.connect(store.root / "app.db") as db:
            db.execute("DROP TABLE messages_fts")
        assert client.get("/api/search", params={"query": "needle"}).status_code == 503


def test_offline_cli_never_initializes_store_recovers_runs_or_calls_api(
    config, monkeypatch, capsys
):
    store = Store(config.paths.data)
    append(store, "needle operator-test-token")
    store.save_run(
        Run(
            id="run",
            session="s",
            profile="interactive",
            origin=Origin(),
            status=RunStatus.running,
        )
    )
    evidence = {p: p.read_bytes() for p in store.root.rglob("*") if p.is_file()}
    monkeypatch.setattr("solakit.cli.load_config", lambda: config)
    monkeypatch.setattr("sys.argv", ["sola", "search", "needle", "--json"])

    def forbidden(*args, **kwargs):
        raise AssertionError("offline search must be a read-only index query")

    monkeypatch.setattr(Store, "__init__", forbidden)
    monkeypatch.setattr("httpx.get", forbidden)
    main()
    hit = json.loads(capsys.readouterr().out)[0]
    assert hit["snippet"] == "needle [redacted]"
    assert {p: p.read_bytes() for p in store.root.rglob("*") if p.is_file()} == evidence
    missing = store.root / "missing"
    with pytest.raises(SearchUnavailable):
        search_index(missing, "needle")
    assert not missing.exists()


def test_excerpt_masks_leading_trailing_and_duplicate_secret_fragments():
    redactor = Redactor(["prefix-credential-suffix"])
    assert (
        redactor.excerpt("prefix-credential-suffix", "credential-suffix")
        == "…[redacted]"
    )
    assert (
        redactor.excerpt("prefix-credential-suffix", "prefix-credential")
        == "[redacted]…"
    )
    assert "credential" not in redactor.excerpt(
        "credential ordinary prefix-credential-suffix", "credential"
    )


def test_rotation_index_failure_does_not_fail_durable_archival(config):
    store = Store(config.paths.data)
    append(store, "needle")
    with sqlite3.connect(store.root / "app.db") as db:
        db.execute("DROP TABLE messages_fts")
    assert archive_session(store, "s")["archived"]
    append(store, "another needle")
    assert len(store.all_messages("s")) == 2
    store.rebuild()
    assert {h["archive"] for h in store.search("needle")} == {"active", "s.jsonl.1"}


def test_offline_search_does_not_echo_invalid_configuration(monkeypatch, capsys):
    def invalid():
        raise ValueError("private credential in invalid config")

    monkeypatch.setattr("solakit.cli.load_config", invalid)
    monkeypatch.setattr("sys.argv", ["sola", "search", "needle"])
    with pytest.raises(SystemExit):
        main()
    output = capsys.readouterr().err
    assert "cannot load configuration" in output
    assert "private credential" not in output


def test_rebuild_accepts_legacy_valid_numeric_timestamps_without_rewriting(config):
    store = Store(config.paths.data)
    message = append(store, "needle legacy")
    path = store.root / "sessions/s.jsonl.1"
    record = message.model_dump(mode="json")
    record["ts"] = 1700000000
    record["origin"] = {}
    path.write_text(json.dumps(record) + "\n")
    original = path.read_bytes()
    store.rebuild()
    assert len(store.search("needle")) == 2
    assert path.read_bytes() == original


def test_names_and_code_punctuation_remain_literal(config):
    store = Store(config.paths.data)
    append(store, r'''O'Neill C:\work\index.py print("moon") say "moon"''')
    for query in ["O'Neill", r"C:\work\index.py", 'print("moon")', '"say ""moon"""']:
        assert len(store.search(query)) == 1


def test_rebuild_does_not_invent_missing_legacy_timestamps(config):
    store = Store(config.paths.data)
    message = append(store, "needle")
    record = message.model_dump(mode="json")
    record.pop("ts")
    (store.root / "sessions/s.jsonl.1").write_text(json.dumps(record) + "\n")
    store.rebuild()
    hit = next(h for h in store.search("needle") if h["archive"] != "active")
    assert hit["ts"] is None


def test_web_search_names_ids_archived_text_links_and_errors(config, mock_model):
    app = create_app(config=config, model=mock_model)
    with TestClient(app) as client:
        assert (
            client.get("/search", follow_redirects=False).headers["location"]
            == "/login"
        )
        client.post("/login", data={"token": "operator-test-token"})
        store = app.state.store
        for i in range(55):
            store.create_session(Session(id=f"import-{i}", title=f"Imported note {i}"))
        append(
            store,
            "Moonstone <script>alert(1)</script> operator-test-token",
            session="old",
        )
        archive_session(store, "old")
        append(store, "Moonstone active evidence", session="old")
        store.set_session_archived("old", True)
        assert 'href="/search"' in client.get("/").text
        assert "Search sessions" in client.get("/search").text
        page = client.get("/search", params={"q": "Imported note"})
        assert page.status_code == 200
        assert page.text.count('class="panel search-result"') == 50
        assert "Next →" in page.text
        page = client.get("/search", params={"q": "Imported note", "offset": 50})
        assert page.text.count('class="panel search-result"') == 5
        assert "← Previous" in page.text
        page = client.get("/search", params={"q": "old"})
        assert "Archived" in page.text and 'href="/sessions/old"' in page.text
        page = client.get("/search", params={"mode": "text", "q": "Moonstone"})
        assert page.status_code == 200
        assert 'href="/sessions/old/archives/1"' in page.text
        assert 'href="/sessions/old"' in page.text
        assert "operator-test-token" not in page.text
        assert "<script>alert" not in page.text
        assert "&lt;script&gt;" in page.text
        assert "Moonstone" in client.get("/sessions/old/archives/1").text
        assert (
            "No matching sessions or messages"
            in client.get("/search", params={"q": "nonexistent"}).text
        )
        for params in (
            {"mode": "bad"},
            {"mode": "text", "q": '"unclosed'},
            {"q": "import", "offset": -1},
        ):
            response = client.get("/search", params=params)
            assert response.status_code == 422
            assert 'role="alert"' in response.text
        with sqlite3.connect(store.root / "app.db") as db:
            db.execute("DROP TABLE messages_fts")
        assert (
            client.get("/search", params={"mode": "text", "q": "Moonstone"}).status_code
            == 503
        )
