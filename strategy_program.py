"""Syntax validation for complete Python strategy source.

This module deliberately does not execute submitted source. Full strategy programs
must be ported to a reviewed, trusted engine before they can produce signals.
"""
import ast
from strategy_formula_v2 import StrategyFormulaError

MAX_PROGRAM_LENGTH = 50000

def validate_strategy_source(source):
    if not isinstance(source, str) or not source.strip():
        raise StrategyFormulaError("Pega el programa Python completo.")
    if len(source) > MAX_PROGRAM_LENGTH:
        raise StrategyFormulaError("El programa supera el máximo de 50000 caracteres.")
    try:
        tree = ast.parse(source, mode="exec")
    except SyntaxError as exc:
        line = exc.lineno or "desconocida"
        column = exc.offset or "desconocida"
        reason = (exc.msg or "error de sintaxis")[:160]
        raise StrategyFormulaError(f"Sintaxis Python no válida en línea {line}, columna {column}: {reason}.") from None
    except (ValueError, TypeError, RecursionError):
        raise StrategyFormulaError("No se pudo analizar el programa; revisa sintaxis, indentación y complejidad.") from None
    if len(list(ast.walk(tree))) > 20000:
        raise StrategyFormulaError("El programa supera el límite seguro de complejidad.")
    if not any(isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) for node in tree.body):
        raise StrategyFormulaError("No se detectó ninguna clase o función de estrategia. Selecciona Fórmula si solo son condiciones.")
    return {"valid": True, "syntax": "python-program", "line_count": len(source.splitlines()),
            "executable": False, "note": "Sintaxis válida. El código no se ejecuta; requiere integración en un motor específico revisado."}
