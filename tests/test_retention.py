import asyncio
import json
from pathlib import Path

import pytest

from solakit.backup import backup, restore
from solakit.evidence import digest
from solakit.legacy import import_fixture, retain_tree
from solakit.store import Store


def test_deferred_pack_retention_without_loading_or_replaying(tmp_path: Path) -> None:
    source = tmp_path / "copied-legacy"
    source.mkdir()
    (source / "config.toml").write_text(
        'tool_modules = ["uninstalled_letters", "uninstalled_mail"]\n'
        '[mail]\napi_key_env = "LEGACY_MAIL_KEY"\nunknown_future = "keep"\n'
    )
    (source / "letters").mkdir()
    (source / "letters" / "café.txt").write_bytes("保留\r\n".encode())
    receipt = source / "receipt.jsonl"
    receipt.write_text('{"state":"intent","outcome":"uncertain","future":{"keep":true}}\n')
    (source / "pack.py").write_text('raise RuntimeError("must never load")\n')
    (source / "image.bin").write_bytes(b"\x00\xff\x80")
    store = Store(tmp_path / "data")
    asyncio.run(store.start())
    try:
        result = retain_tree(store, source)
        assert result == retain_tree(store, source)
        manifest = json.loads((store.root / str(result["manifest"])).read_bytes())
        retained = (store.root / str(result["manifest"])).parent / "files"
        for name, metadata in manifest["files"].items():
            assert (retained / name).read_bytes() == (source / name).read_bytes()
            assert digest((retained / name).read_bytes()) == metadata["sha256"]
        assert import_fixture(store, retained / "receipt.jsonl") == 1
        assert store.db.execute("SELECT count(*) FROM entities").fetchone()[0] == 0
        assert json.loads((retained / "receipt.jsonl").read_text())["outcome"] == "uncertain"
        backup(store, tmp_path / "backup")
    finally:
        asyncio.run(store.stop())
    restore(tmp_path / "backup", tmp_path / "restored")
    assert (tmp_path / "restored" / str(result["manifest"])).read_bytes() == (
        tmp_path / "data" / str(result["manifest"])
    ).read_bytes()
    assert not (source / "state.db").exists()


def test_retention_rejects_alias_and_detects_changed_copy(tmp_path: Path) -> None:
    source = tmp_path / "copied"
    source.mkdir()
    (source / "file").write_text("source")
    store = Store(tmp_path / "data")
    asyncio.run(store.start())
    try:
        (source / "alias").symlink_to(source / "file")
        with pytest.raises(ValueError):
            retain_tree(store, source)
        (source / "alias").unlink()
        result = retain_tree(store, source)
        retained = (store.root / str(result["manifest"])).parent / "files" / "file"
        retained.write_text("changed")
        with pytest.raises(ValueError):
            retain_tree(store, source)
        with pytest.raises(ValueError):
            retain_tree(store, store.root)
    finally:
        asyncio.run(store.stop())
