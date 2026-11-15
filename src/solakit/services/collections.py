"""Named directory resources; layout conventions belong to external packs."""

from pathlib import Path

from solakit.services.git import Git
from solakit.tools import ToolContext, ToolError, choices_hint, scope_root


class Collection:
    def __init__(self, ctx: ToolContext, alias: str) -> None:
        self.ctx, self.alias = ctx, alias
        config = ctx.config.collections.get(alias)
        if config is None:
            granted = ctx.config.profiles[ctx.profile].resources
            raise ToolError(
                "Unknown collection alias."
                + choices_hint(
                    alias,
                    [
                        r.removeprefix("collection:")
                        for r in granted
                        if r.startswith("collection:")
                    ],
                )
            )
        self.config = config
        root = scope_root(f"collection:{alias}", ctx.config)
        if root is None:
            raise ToolError("Unknown collection root")
        self.root = root
        ctx.path(str(self.root))

    def path(self, filename: str, *, write: bool = False) -> Path:
        candidate = self.ctx.path(str(self.root / filename), write=write)
        if not candidate.is_relative_to(self.root) or candidate == self.root:
            raise ToolError("File is outside the collection")
        return candidate

    def git(self) -> Git:
        return Git(self.ctx, self.config.repository, collection=self.alias)
