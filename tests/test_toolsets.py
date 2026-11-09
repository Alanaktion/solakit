import pytest
from pydantic import ValidationError

from solakit.core.config import Config, Profile
from solakit.tools import Registry


def build(**values):
    return Config(enabled_tools=["files", "math", "datetime"], **values)


def test_toolsets_expand_like_explicit_grants():
    config = build(
        toolsets={
            "reader": ["files.read", "files.list"],
            "calc": ["math.*", "@reader"],
        },
        profiles={
            "interactive": Profile(tools=["@calc", "datetime.weekday"]),
            "explicit": Profile(
                tools=["math.eval", "files.read", "files.list", "datetime.weekday"]
            ),
        },
    )
    registry = Registry(config)
    assert registry.tool_names("interactive") == [
        "math.eval",
        "files.read",
        "files.list",
        "datetime.weekday",
    ]
    assert set(registry.tool_names("interactive")) == set(
        registry.tool_names("explicit")
    )
    # Run evidence carries expanded names; tasks may narrow with toolsets too.
    assert registry.effective_tools("interactive", ["@reader"]) == [
        "files.read",
        "files.list",
    ]
    assert registry.catalog()["toolsets"]["calc"] == [
        "math.*",
        "files.read",
        "files.list",
    ]


def test_toolset_validation():
    with pytest.raises(ValidationError, match="unknown toolset"):
        build(toolsets={"a": ["@missing"]})
    with pytest.raises(ValidationError, match="invalid toolset name"):
        build(toolsets={"bad name": []})
    config = build(
        toolsets={"a": ["@b"], "b": ["@a"]},
        profiles={"interactive": Profile(tools=["@a"])},
    )
    with pytest.raises(ValueError, match="cycle"):
        Registry(config)
    config = build(profiles={"interactive": Profile(tools=["@nope"])})
    with pytest.raises(ValueError, match="unknown toolset"):
        Registry(config)
