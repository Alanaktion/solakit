"""Named directory resources; layout conventions belong to external packs."""

from solakit.services.git import Git
from solakit.tools import ToolError, scope_root


class Collection:
    def __init__(self, ctx, alias: str):
        self.ctx, self.alias = ctx, alias
        self.config = ctx.config.collections.get(alias)
        if self.config is None:
            raise ToolError("Unknown collection")
        self.root = scope_root(f"collection:{alias}", ctx.config)
        ctx.path(str(self.root))

    def path(self, filename: str, *, write: bool = False):
        candidate = self.ctx.path(str(self.root / filename), write=write)
        if not candidate.is_relative_to(self.root) or candidate == self.root:
            raise ToolError("File is outside the collection")
        return candidate

    def git(self):
        return Git(self.ctx, self.config.repository, collection=self.alias)
