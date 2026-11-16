"""Installed external tools using only the supported Sola SDK."""

from html import escape

from solakit.sdk import Tool, ToolArguments, ToolContext, ToolResult

# The pack declares the SDK major it was authored against.
SDK_VERSION = 1
__all__ = ["SDK_VERSION", "TOOLS"]


class PathArguments(ToolArguments):
    path: str


class WriteArguments(PathArguments):
    content: str
    overwrite: bool = False


class ImageArguments(PathArguments):
    prompt: str


class FakeImageAdapter:
    async def generate(self, prompt: str) -> str:
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256">'
            '<rect width="256" height="256" fill="#64748b"/>'
            f'<text x="16" y="128">Fake: {escape(prompt)}</text></svg>'
        )


async def read(context: ToolContext, args: PathArguments) -> ToolResult:
    return ToolResult(data=context.files.read_text(args.path))


async def write(context: ToolContext, args: WriteArguments) -> ToolResult:
    return ToolResult(
        data=context.files.write_text(args.path, args.content, overwrite=args.overwrite)
    )


async def image(context: ToolContext, args: ImageArguments) -> ToolResult:
    content = await FakeImageAdapter().generate(args.prompt)
    return ToolResult(data=context.files.write_text(args.path, content))


TOOLS = (
    Tool("example.read", "Read a workspace text file.", PathArguments, read),
    Tool(
        "example.write", "Write a workspace text file.", WriteArguments, write, consequential=True
    ),
    Tool(
        "example.image",
        "Save a fake SVG image; no real image service.",
        ImageArguments,
        image,
        consequential=True,
    ),
)
