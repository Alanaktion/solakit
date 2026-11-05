"""Installed distribution identity for explicitly configured tool modules."""

import json
from importlib import metadata
from pathlib import Path
from urllib.parse import unquote, urlsplit

SYNC_HINT = "run uv sync --frozen --all-packages"


def distribution_name(module: str) -> str | None:
    if module == "solakit" or module.startswith("solakit."):
        return "solakit"
    if module.startswith("sola_packs."):
        return "solakit-packs-" + module.split(".")[1].replace("_", "-")
    return None


def installed(module: str):
    """Check known pack metadata before imports, including source-only installs."""
    name = distribution_name(module)
    if name is None:
        return None
    try:
        return metadata.distribution(name)
    except metadata.PackageNotFoundError:
        raise ValueError(
            f"enabled_tools references {module} but {name} is not installed; "
            f"{SYNC_HINT}"
        ) from None


def describes(dist, imported) -> bool:
    filename = getattr(imported, "__file__", None)
    if not filename:
        return False
    target = Path(filename).resolve()
    if any(Path(dist.locate_file(f)).resolve() == target for f in dist.files or ()):
        return True
    # Editable wheels may list only their .pth and metadata. PEP 610 identifies
    # the project directory without guessing namespace ownership from a prefix.
    try:
        direct = json.loads(dist.read_text("direct_url.json") or "{}")
        url = urlsplit(direct.get("url", ""))
        return (
            direct.get("dir_info", {}).get("editable") is True
            and url.scheme == "file"
            and target.is_relative_to(Path(unquote(url.path)).resolve())
        )
    except (ValueError, TypeError, AttributeError, OSError):
        return False


def identity(module: str, imported, dist=None) -> dict:
    if dist is None:
        candidates = metadata.packages_distributions().get(module.split(".")[0], [])
        for candidate in candidates:
            provider = metadata.distribution(candidate)
            if describes(provider, imported):
                dist = provider
                break
    return {
        "module": module,
        "distribution": dist.metadata["Name"] if dist is not None else None,
        "version": dist.version if dist is not None else None,
    }


def description(provider: dict) -> str:
    name = provider["distribution"] or "unpackaged"
    version = provider["version"] or "unknown version"
    return f"{provider['module']}: {name} {version}"
