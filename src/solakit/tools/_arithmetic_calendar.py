"""Deterministic arithmetic and calendar tools."""

import ast
from datetime import date, datetime, timedelta
from decimal import Decimal, localcontext
from zoneinfo import ZoneInfo


def evaluate(expression: str) -> dict:
    """Evaluate bounded arithmetic without executing Python code."""
    if len(expression) > 2000:
        raise ValueError("Expression is too long")
    tree = ast.parse(expression, mode="eval")
    if sum(1 for _ in ast.walk(tree)) > 200:
        raise ValueError("Expression is too complex")

    def visit(node):
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            value = Decimal(ast.get_source_segment(expression, node).replace("_", ""))
        elif isinstance(node, ast.UnaryOp) and isinstance(
            node.op, (ast.UAdd, ast.USub)
        ):
            value = visit(node.operand)
            if isinstance(node.op, ast.USub):
                value = -value
        elif isinstance(node, ast.BinOp):
            left, right = visit(node.left), visit(node.right)
            if isinstance(node.op, ast.Add):
                value = left + right
            elif isinstance(node.op, ast.Sub):
                value = left - right
            elif isinstance(node.op, ast.Mult):
                value = left * right
            elif isinstance(node.op, ast.Div):
                value = left / right
            elif isinstance(node.op, ast.FloorDiv):
                value = (left / right).to_integral_value(rounding="ROUND_FLOOR")
            elif isinstance(node.op, ast.Mod):
                quotient = (left / right).to_integral_value(rounding="ROUND_FLOOR")
                value = left - quotient * right
            elif isinstance(node.op, ast.Pow):
                if right != right.to_integral_value() or abs(right) > 1000:
                    raise ValueError(
                        "Exponent must be an integer between -1000 and 1000"
                    )
                value = left ** int(right)
            else:
                raise ValueError("Unsupported operator")
        else:
            raise ValueError(
                "Only numeric literals and arithmetic operators are allowed"
            )
        if not value.is_finite() or abs(value.adjusted()) > 10000:
            raise ValueError("Result exceeds arithmetic limits")
        return value

    with localcontext() as context:
        context.prec = 50
        return {"result": str(visit(tree.body)), "precision": 50}


def convert(
    value: str, to_zone: str, from_zone: str | None = None, fold: int | None = None
) -> dict:
    dt = datetime.fromisoformat(value)
    if fold not in (None, 0, 1):
        raise ValueError("fold must be 0 or 1")
    if dt.tzinfo is None:
        if not from_zone:
            raise ValueError("Naive timestamps require from_zone")
        zone = ZoneInfo(from_zone)
        candidates = [dt.replace(tzinfo=zone, fold=f) for f in (0, 1)]
        valid = [
            c
            for c in candidates
            if c.astimezone(ZoneInfo("UTC")).astimezone(zone).replace(tzinfo=None) == dt
        ]
        if not valid:
            raise ValueError("Local time does not exist due to DST")
        if candidates[0].utcoffset() != candidates[1].utcoffset() and fold is None:
            raise ValueError("Ambiguous local time; specify fold=0 or fold=1")
        dt = candidates[fold or 0]
    elif from_zone:
        raise ValueError("Use from_zone only with a naive timestamp")
    result = dt.astimezone(ZoneInfo(to_zone))
    return {
        "datetime": result.isoformat(),
        "zone": to_zone,
        "weekday": result.strftime("%A"),
        "fold": result.fold,
    }


def add_days(value: str, days: int) -> dict:
    if type(days) is not int:
        raise ValueError("days must be an integer")
    result = date.fromisoformat(value) + timedelta(days=days)
    return {"date": result.isoformat(), "weekday": result.strftime("%A")}


def weekday(value: str) -> dict:
    result = date.fromisoformat(value)
    return {"weekday": result.strftime("%A"), "iso_weekday": result.isoweekday()}
