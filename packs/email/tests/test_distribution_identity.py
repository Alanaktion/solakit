"""Namespace-pack metadata remains distinct when every workspace member is installed."""

from importlib import metadata


def test_workspace_tool_modules_report_their_own_distributions(pack_context):
    modules = ["journal", "letters", "email", "printer", "calendar", "image"]
    registry, ctx = pack_context(["sola_packs." + name for name in modules], [])
    assert registry.catalog()["distributions"] == [
        {
            "module": "sola_packs." + name,
            "distribution": "solakit-packs-" + name,
            "version": metadata.version("solakit-packs-" + name),
        }
        for name in modules
    ]
    assert ctx.config.profiles[ctx.profile].tools == []
