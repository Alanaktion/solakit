"""Bounded decimal arithmetic, with no Python code execution."""

import json
from typing import Annotated

from pydantic import Field

from solakit.tools import ToolContext, ToolError, ToolResult, tool
from solakit.tools._arithmetic_calendar import evaluate


@tool(
    name="math.eval",
    profiles=["*"],
    summary="Compute exact arithmetic instead of calculating mentally",
    keywords=("math", "arithmetic", "calculate", "money", "tokens", "bytes"),
)
def eval(
    ctx: ToolContext,
    expression: Annotated[
        str,
        Field(
            description=(
                "Arithmetic expression using numbers, parentheses and + - * / "
                "// % **; no functions or variables."
            )
        ),
    ],
) -> ToolResult:
    """Compute arithmetic exactly instead of mentally: totals, percentages, ratios.

    Literals, parentheses and + - * / // % **; returns a decimal string with 50
    significant digits. Exponents must be integers in -1000..1000. No functions,
    names, attributes or containers. No currency rounding policy."""
    try:
        result = evaluate(expression)
    except (ValueError, SyntaxError, ArithmeticError, RecursionError):
        raise ToolError(
            "Invalid arithmetic expression or arithmetic limit exceeded"
        ) from None
    return ToolResult(content=json.dumps(result))
