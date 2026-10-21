"""Restricted interpreter for extensible, multi-line Tradeboot strategy formulas.
Never uses eval/exec. Unknown variables and unavailable data fail closed.
"""
import ast
import math
import operator

MAX_FORMULA_LENGTH = 50000
MAX_AST_NODES = 12000
MAX_BLOCK_STATEMENTS = 1000
MAX_NESTING_DEPTH = 30
CATALOG = {
    "price": "Precio actual", "bid": "Precio bid", "ask": "Precio ask",
    "change_pct": "Cambio porcentual", "gap_pct": "Gap porcentual",
    "volume": "Volumen actual", "rvol": "Volumen relativo",
    "premarket_change_pct": "Cambio premarket; requiere fuente premarket",
    "float_shares": "Float; requiere proveedor de float",
    "ema20": "EMA20; requiere velas históricas",
    "macd": "MACD; requiere velas históricas",
    "macd_signal": "Señal MACD; requiere velas históricas",
    "bollinger_upper": "Banda Bollinger superior; requiere velas históricas",
    "bollinger_lower": "Banda Bollinger inferior; requiere velas históricas",
}
FUNCS = {"abs": abs, "min": min, "max": max, "round": round}
BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
       ast.Div: operator.truediv, ast.Mod: operator.mod, ast.Pow: operator.pow}
CMP = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt,
       ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge}


class StrategyFormulaError(ValueError):
    pass


def _parse(formula):
    if not isinstance(formula, str) or not formula.strip():
        raise StrategyFormulaError("Escribe una fórmula.")
    if len(formula) > MAX_FORMULA_LENGTH:
        raise StrategyFormulaError("La fórmula supera el máximo de 50000 caracteres.")
    try:
        tree = ast.parse(formula, mode="exec")
    except (SyntaxError, ValueError, TypeError, RecursionError):
        raise StrategyFormulaError("Sintaxis Python no válida; revisa la indentación.") from None
    if not tree.body:
        raise StrategyFormulaError("Fórmula vacía o demasiado compleja.")
    if sum(1 for _ in ast.walk(tree)) > MAX_AST_NODES:
        raise StrategyFormulaError("La fórmula supera el límite seguro de complejidad.")
    return tree


def _expr(n, env):
    if isinstance(n, ast.Constant):
        if isinstance(n.value, bool): return n.value
        if isinstance(n.value, (int, float)) and not isinstance(n.value, bool) and math.isfinite(n.value): return float(n.value)
        raise StrategyFormulaError("Solo se admiten números finitos y True/False.")
    if isinstance(n, ast.Name):
        if n.id in FUNCS: raise StrategyFormulaError("Usa paréntesis al llamar " + n.id + ".")
        if n.id not in CATALOG and n.id not in env: raise StrategyFormulaError("Unsupported variable: " + n.id)
        if n.id not in env or env[n.id] is None: raise StrategyFormulaError("Missing value for variable: " + n.id + ". " + CATALOG.get(n.id, "variable calculada") + "; no se inventarán valores.")
        v = env[n.id]
        if isinstance(v, bool): return v
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v):
            raise StrategyFormulaError("El valor de '" + n.id + "' debe ser numérico y finito.")
        return float(v)
    if isinstance(n, ast.BinOp) and type(n.op) in BIN:
        a, b = _expr(n.left, env), _expr(n.right, env)
        if isinstance(n.op, ast.Pow) and abs(b) > 8: raise StrategyFormulaError("Exponente demasiado grande.")
        if isinstance(n.op, (ast.Div, ast.Mod)) and b == 0: raise StrategyFormulaError("No se permite dividir entre cero.")
        try: v = BIN[type(n.op)](a, b)
        except (ArithmeticError, ValueError, OverflowError): raise StrategyFormulaError("Operación matemática fuera de límites.") from None
        if not isinstance(v, (int, float)) or not math.isfinite(v): raise StrategyFormulaError("Resultado no finito.")
        return v
    if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.UAdd, ast.USub, ast.Not)):
        v = _expr(n.operand, env)
        if isinstance(n.op, ast.Not):
            if not isinstance(v, bool): raise StrategyFormulaError("not requiere una condición booleana.")
            return not v
        return v if isinstance(n.op, ast.UAdd) else -v
    if isinstance(n, ast.BoolOp) and isinstance(n.op, (ast.And, ast.Or)):
        for child in n.values:
            value = _expr(child, env)
            if not isinstance(value, bool):
                raise StrategyFormulaError("AND/OR requiere comparaciones booleanas.")
            if isinstance(n.op, ast.And) and not value:
                return False
            if isinstance(n.op, ast.Or) and value:
                return True
        return isinstance(n.op, ast.And)
    if isinstance(n, ast.Compare):
        left = _expr(n.left, env)
        for op, child in zip(n.ops, n.comparators):
            if type(op) not in CMP: raise StrategyFormulaError("Comparador no permitido.")
            right = _expr(child, env)
            if not CMP[type(op)](left, right): return False
            left = right
        return True
    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in FUNCS and not n.keywords:
        args = [_expr(x, env) for x in n.args]
        if not args or any(isinstance(x, bool) for x in args): raise StrategyFormulaError("La función requiere argumentos numéricos.")
        try: v = FUNCS[n.func.id](*args)
        except (TypeError, ValueError, ArithmeticError): raise StrategyFormulaError("Argumentos no válidos para " + n.func.id + ".") from None
        if not isinstance(v, (int, float)) or not math.isfinite(v): raise StrategyFormulaError("Resultado no finito.")
        return v
    raise StrategyFormulaError("Sintaxis no permitida. Admite asignaciones, if/else, comparaciones y abs/min/max/round; no imports, bucles, archivos ni red.")


def _block(statements, env, depth=0):
    if depth > MAX_NESTING_DEPTH or len(statements) > MAX_BLOCK_STATEMENTS: raise StrategyFormulaError("La fórmula supera el límite seguro de instrucciones o niveles if/else.")
    result = None
    for s in statements:
        if isinstance(s, ast.Assign) and len(s.targets) == 1 and isinstance(s.targets[0], ast.Name):
            name = s.targets[0].id
            if name.startswith("_") or name in FUNCS: raise StrategyFormulaError("Nombre no permitido: " + name)
            result = _expr(s.value, env); env[name] = result
        elif isinstance(s, ast.If):
            cond = _expr(s.test, env)
            if not isinstance(cond, bool): raise StrategyFormulaError("La condición if debe ser True/False.")
            branch = s.body if cond else s.orelse
            if branch: result = _block(branch, env, depth + 1)
        elif isinstance(s, ast.Expr):
            result = _expr(s.value, env)
        else:
            raise StrategyFormulaError("Instrucción no permitida: " + type(s).__name__ + ".")
    return result


def validate_formula(formula):
    tree = _parse(formula)
    allowed_nodes = (
        ast.Module, ast.Assign, ast.If, ast.Expr, ast.Name, ast.Load, ast.Store, ast.Constant,
        ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.Call,
        ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.Pow,
        ast.UAdd, ast.USub, ast.Not, ast.And, ast.Or,
        ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
    )
    if any(not isinstance(node, allowed_nodes) for node in ast.walk(tree)):
        raise StrategyFormulaError("Esta instrucción o sintaxis Python no está permitida.")
    def check_statements(statements, depth=0):
        if depth > MAX_NESTING_DEPTH:
            raise StrategyFormulaError("La fórmula supera el límite seguro de niveles if/else.")
        if len(statements) > MAX_BLOCK_STATEMENTS:
            raise StrategyFormulaError("La fórmula supera el límite seguro de instrucciones.")
        for statement in statements:
            if isinstance(statement, ast.Assign):
                if len(statement.targets) != 1 or not isinstance(statement.targets[0], ast.Name):
                    raise StrategyFormulaError("Solo se permite asignar a una variable por instrucción.")
                name = statement.targets[0].id
                if name.startswith("_") or name in FUNCS or name in CATALOG:
                    raise StrategyFormulaError("No se permite sobrescribir una variable de mercado o función: " + name)
            elif isinstance(statement, ast.If):
                check_statements(statement.body, depth + 1)
                check_statements(statement.orelse, depth + 1)
            elif not isinstance(statement, ast.Expr):
                raise StrategyFormulaError("Instrucción Python no permitida: " + type(statement).__name__ + ".")
    check_statements(tree.body)
    loads = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id not in FUNCS}
    stores = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    unknown = sorted(loads - set(CATALOG) - stores)
    if unknown: raise StrategyFormulaError("Unsupported variable(s): " + ", ".join(unknown) + ".")
    used = sorted(loads.intersection(CATALOG))
    return {"valid": True, "variables": used, "syntax": "safe-python-subset",
            "available_variables": sorted(CATALOG), "requires_data": used}


def evaluate_formula(formula, values=None):
    validation = validate_formula(formula)
    result = _block(_parse(formula).body, dict(values or {}))
    if not isinstance(result, bool):
        raise StrategyFormulaError("Formula must return TRUE/FALSE.")
    return {"valid": True, "matched": result, "variables": validation["variables"]}
