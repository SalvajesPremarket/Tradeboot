"""Safe expression evaluator for user-defined Tradeboot strategy conditions.

This module intentionally evaluates a small numeric expression language, not Python code.
It never executes arbitrary code and has no broker/order side effects.
"""
import ast
import math
import operator


ALLOWED_VARIABLES = frozenset({
    "price", "bid", "ask", "change_pct", "gap_pct", "volume", "rvol",
})
MAX_FORMULA_LENGTH = 1000
MAX_AST_NODES = 100
_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_CMPOPS = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}
_UNARYOPS = {ast.UAdd: operator.pos, ast.USub: operator.neg, ast.Not: operator.not_}


class StrategyFormulaError(ValueError):
    """A formula is outside the supported safe expression language."""


def _number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StrategyFormulaError("Variable values must be numeric: " + name)
    value = float(value)
    if not math.isfinite(value):
        raise StrategyFormulaError("Variable values must be finite: " + name)
    return value


def _evaluate(node, values):
    if isinstance(node, ast.Expression):
        return _evaluate(node.body, values)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool):
            return node.value
        if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            value = float(node.value)
            if math.isfinite(value):
                return value
        raise StrategyFormulaError("Only finite numeric constants and booleans are allowed.")
    if isinstance(node, ast.Name):
        if node.id not in ALLOWED_VARIABLES:
            raise StrategyFormulaError("Unsupported variable: " + node.id)
        if node.id not in values or values[node.id] is None:
            raise StrategyFormulaError("Missing value for variable: " + node.id)
        return _number(values[node.id], node.id)
    if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
        left, right = _evaluate(node.left, values), _evaluate(node.right, values)
        if isinstance(node.op, ast.Pow) and abs(right) > 8:
            raise StrategyFormulaError("Exponent magnitude cannot exceed 8.")
        if isinstance(node.op, (ast.Div, ast.Mod)) and right == 0:
            raise StrategyFormulaError("Division by zero is not allowed.")
        result = _BINOPS[type(node.op)](left, right)
        if not math.isfinite(result):
            raise StrategyFormulaError("Formula result is not finite.")
        return result
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARYOPS:
        return _UNARYOPS[type(node.op)](_evaluate(node.operand, values))
    if isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
        items = [_evaluate(item, values) for item in node.values]
        if not all(isinstance(item, bool) for item in items):
            raise StrategyFormulaError("Use comparisons to build AND/OR conditions.")
        return all(items) if isinstance(node.op, ast.And) else any(items)
    if isinstance(node, ast.Compare):
        left = _evaluate(node.left, values)
        for op, comparator in zip(node.ops, node.comparators):
            if type(op) not in _CMPOPS:
                raise StrategyFormulaError("Unsupported comparison operator.")
            right = _evaluate(comparator, values)
            if not _CMPOPS[type(op)](left, right):
                return False
            left = right
        return True
    raise StrategyFormulaError("Unsupported syntax. Use numeric variables, arithmetic, comparisons, and AND/OR/NOT.")


def validate_formula(formula):
    """Check syntax and supported variables without using real market data."""
    probe_values = {name: 1.0 for name in ALLOWED_VARIABLES}
    result = evaluate_formula(formula, probe_values)
    return {"valid": True, "variables": result["variables"]}


def evaluate_formula(formula, values=None):
    """Validate and evaluate a safe boolean strategy expression against numeric inputs."""
    if not isinstance(formula, str) or not formula.strip():
        raise StrategyFormulaError("Formula is required.")
    if len(formula) > MAX_FORMULA_LENGTH:
        raise StrategyFormulaError("Formula is too long (maximum 1000 characters).")
    try:
        tree = ast.parse(formula, mode="eval")
    except (SyntaxError, ValueError):
        raise StrategyFormulaError("Formula syntax is invalid.") from None
    if sum(1 for _ in ast.walk(tree)) > MAX_AST_NODES:
        raise StrategyFormulaError("Formula is too complex.")
    result = _evaluate(tree, values or {})
    if not isinstance(result, bool):
        raise StrategyFormulaError("The formula must return TRUE/FALSE, for example: price > 10 and change_pct > 1.")
    return {"valid": True, "matched": result, "variables": sorted({
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    })}
