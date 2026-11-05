"""Distribution identity must describe imports and fail clearly before missing packs."""

import logging
import sys
from importlib import metadata
from types import ModuleType

import pytest
from fastapi.testclient import TestClient

from solakit.cli import main
from solakit.main import create_app
from solakit.tools import Registry


def test_core_identity_shared_by_catalog_cli_and_startup(
    config, mock_model, monkeypatch, capsys, caplog
):
    config.enabled_tools = ["files"]
    expected = [
        {
            "module": "solakit.tools.files",
            "distribution": "solakit",
            "version": metadata.version("solakit"),
        }
    ]
    assert Registry(config).catalog()["distributions"] == expected
    monkeypatch.setattr("solakit.core.startup.load_config", lambda: config)
    monkeypatch.setattr("sys.argv", ["sola", "check"])
    main()
    output = capsys.readouterr().out
    assert f"solakit.tools.files: solakit {metadata.version('solakit')}" in output
    with (
        caplog.at_level(logging.INFO),
        TestClient(create_app(config=config, model=mock_model)) as client,
    ):
        assert (
            client.get(
                "/api/tools", headers={"Authorization": "Bearer operator-test-token"}
            ).json()["distributions"]
            == expected
        )
    assert (
        "Tool module solakit.tools.files: distribution=solakit; version=" in caplog.text
    )


def test_missing_pack_metadata_fails_before_import_even_with_source_module(
    config, monkeypatch, capsys
):
    config.enabled_tools = ["sola_packs.letters"]
    real_distribution = metadata.distribution

    def missing(name):
        if name == "solakit-packs-letters":
            raise metadata.PackageNotFoundError(name)
        return real_distribution(name)

    monkeypatch.setattr(metadata, "distribution", missing)
    monkeypatch.setitem(sys.modules, "sola_packs.letters", ModuleType("letters"))
    monkeypatch.setattr("solakit.core.startup.load_config", lambda: config)
    monkeypatch.setattr("sys.argv", ["sola", "check"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 1
    output = capsys.readouterr().err
    assert output == (
        "Configuration error: enabled_tools references sola_packs.letters but "
        "solakit-packs-letters is not installed; "
        "run uv sync --frozen --all-packages\n"
    )
    assert "Traceback" not in output


def test_missing_dependency_has_sync_guidance(config, monkeypatch):
    config.enabled_tools = ["files"]

    def broken_import(path):
        raise ModuleNotFoundError("private import details", name="dependency")

    monkeypatch.setattr("solakit.tools.importlib.import_module", broken_import)
    with pytest.raises(ValueError) as error:
        Registry(config)
    assert str(error.value) == (
        "enabled_tools references solakit.tools.files but required module "
        "dependency is unavailable; run uv sync --frozen --all-packages"
    )


def test_editable_namespace_identity_selects_actual_provider(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    from solakit.tools.distributions import identity

    providers = {}
    for name in ["first", "second"]:
        root = tmp_path / name
        providers[name] = SimpleNamespace(
            metadata={"Name": name},
            version="1.2",
            files=[],
            read_text=lambda _, root=root: json.dumps(
                {"url": root.as_uri(), "dir_info": {"editable": True}}
            ),
        )
    module = ModuleType("namespace.tool")
    module.__file__ = str(tmp_path / "second/src/namespace/tool.py")
    monkeypatch.setattr(
        metadata, "packages_distributions", lambda: {"namespace": ["first", "second"]}
    )
    monkeypatch.setattr(metadata, "distribution", providers.__getitem__)
    assert identity("namespace.tool", module) == {
        "module": "namespace.tool",
        "distribution": "second",
        "version": "1.2",
    }
