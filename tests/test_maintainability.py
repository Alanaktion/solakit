"""Safety nets for refactors: route table, SDK surface, generated docs and
architecture references (MODULARITY-MAINTAINABILITY-PLAN)."""

import ast
import importlib
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
FIXTURES = Path(__file__).parent / "fixtures"


def route_table(config, mock_model):
    from solakit.main import create_app

    app = create_app(config=config, model=mock_model)
    table = set()

    def walk(routes, prefix=""):
        for route in routes:
            included = getattr(route, "original_router", None)
            if included is not None:
                context = getattr(route, "include_context", None)
                walk(included.routes, prefix + (getattr(context, "prefix", "") or ""))
                continue
            path = prefix + getattr(route, "path", "")
            methods = sorted(getattr(route, "methods", None) or ["MOUNT"])
            table.add(f"{','.join(methods)} {path}")

    walk(app.routes)
    return sorted(table)


def test_route_table_snapshot(config, mock_model):
    """Splitting ui/routes or main must keep every route (update deliberately)."""
    current = route_table(config, mock_model)
    expected = json.loads((FIXTURES / "route-table.json").read_text())
    assert current == expected


def test_sdk_surface_and_version_gate():
    import solakit.sdk as sdk

    assert sdk.SDK_VERSION == "1.0"
    for name in sdk.__all__:
        assert hasattr(sdk, name), name
    assert sdk.satisfies(">=1,<2") and not sdk.satisfies(">=2")
    assert sdk.satisfies("==1.0") and not sdk.satisfies("<1")
    with pytest.raises(ValueError):
        sdk.satisfies("~1")


def test_registry_rejects_incompatible_pack(config, monkeypatch):
    import sys
    from types import ModuleType

    from solakit.tools import Registry

    module = ModuleType("solakit.tools.future_pack")
    module.SOLA_SDK = ">=2"
    monkeypatch.setitem(sys.modules, module.__name__, module)
    config.enabled_tools = ["future_pack"]
    with pytest.raises(ValueError, match="requires solakit SDK >=2"):
        Registry(config)


PACK_SOURCES = [
    *ROOT.glob("packs/*/src/sola_packs/*.py"),
    ROOT / "packs/common/src/sola_packs/support/__init__.py",
    ROOT / "packs/common/src/sola_packs/support/evidence.py",
]


@pytest.mark.parametrize("path", PACK_SOURCES, ids=lambda p: p.name)
def test_packs_use_only_the_sdk(path):
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
            "solakit"
        ):
            assert node.module == "solakit.sdk", (path.name, node.module)
    module = path.stem if path.parent.name == "sola_packs" else None
    if module:
        importlib.import_module(f"sola_packs.{module}")


def test_generated_tool_reference_is_current():
    from solakit.core.toolref import render

    assert (ROOT / "docs/tools.md").read_text() == render()


def test_architecture_doc_module_paths_exist():
    text = (ROOT / "docs/architecture.md").read_text()
    paths = set(re.findall(r"`((?:src|packs)/[\w/.-]+\.py)`", text))
    assert len(paths) > 15
    for path in paths:
        assert (ROOT / path).exists(), path


def test_every_operations_doc_is_linked():
    linked = "".join(
        (ROOT / name).read_text()
        for name in ("docs/operations.md", "README.md", "docs/setup.md")
    )
    for path in (ROOT / "docs/operations").glob("*.md"):
        assert path.name in linked, path.name
