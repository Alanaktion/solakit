"""The web UI is an HTTP client of the authenticated API, not another runtime."""

from solakit.ui.routes import build_ui_router

__all__ = ["build_ui_router"]
