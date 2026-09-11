"""Verificación de las preguntas de cálculo.

Una pregunta de aritmética es el único caso en que el resultado se puede
comprobar sin preguntarle a nadie, así que se comprueba: si el enunciado
contiene una operación y la opción marcada como correcta no es su resultado,
la pregunta se descarta.

Vale la pena porque es justo donde un error se nota y se discute. En el resto
de temas hay que confiar en el modelo; aquí no hace falta.

La expresión se evalúa recorriendo el árbol de sintaxis y aceptando sólo
números y operadores aritméticos. Nunca se usa ``eval``: el texto viene de un
modelo de lenguaje, que a su vez repite lo que escribe la gente.
"""

from __future__ import annotations

import ast
import re

#: Signos que la gente y los modelos escriben en vez de los de teclado. Van
#: por punto de código a propósito: escritos tal cual son indistinguibles del
#: guion y de la equis normales, y ruff avisa de ello con razón.
_ALIASES = {
    "\u00d7": "*",   # signo de multiplicar
    "\u00b7": "*",   # punto medio
    "\u00f7": "/",   # signo de dividir
    "\u2215": "/",   # barra de división
    "\u2212": "-",   # signo menos
    "\u2013": "-",   # raya corta
    "\u2014": "-",   # raya larga
    "^": "**",
}

#: Un candidato a expresión: cifras, signos y paréntesis seguidos.
_CANDIDATE = re.compile(
    "[0-9+\\-*/^().\u00b7\u00d7\u00f7\u2215\u2212\u2013\u2014\\s]{5,}"
)

#: Operadores permitidos en el árbol.
_BINARY = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow)
_UNARY = (ast.UAdd, ast.USub)

#: Tolerancia al comparar. Las divisiones dan decimales y el modelo redondea.
_TOLERANCE = 1e-6


def normalise(expression: str) -> str:
    for signo, reemplazo in _ALIASES.items():
        expression = expression.replace(signo, reemplazo)
    return " ".join(expression.split())


def evaluate(expression: str) -> float | None:
    """El valor de una expresión aritmética, o ``None`` si no lo es.

    Se rechaza todo lo que no sea un número o una operación entre números:
    nombres, llamadas, atributos, índices. Así una cadena inesperada no puede
    ejecutar nada, sólo dejar de validar.
    """
    limpio = normalise(expression)
    if not limpio:
        return None
    try:
        arbol = ast.parse(limpio, mode="eval")
    except (SyntaxError, ValueError, MemoryError):
        return None
    try:
        return _walk(arbol.body)
    except (_Rejected, ArithmeticError, TypeError, ValueError):
        return None


class _Rejected(Exception):
    """El árbol contiene algo que no es aritmética."""


def _walk(node: ast.AST) -> float:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, int | float):
            raise _Rejected(str(node.value))
        return float(node.value)
    if isinstance(node, ast.BinOp) and isinstance(node.op, _BINARY):
        izquierda, derecha = _walk(node.left), _walk(node.right)
        # Un exponente grande cuelga el proceso calculando un número que no
        # cabe en memoria; una pregunta de concurso no lo necesita.
        if isinstance(node.op, ast.Pow) and abs(derecha) > 64:
            raise _Rejected("exponente desmedido")
        return _OPS[type(node.op)](izquierda, derecha)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, _UNARY):
        valor = _walk(node.operand)
        return valor if isinstance(node.op, ast.UAdd) else -valor
    raise _Rejected(type(node).__name__)


_OPS = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.FloorDiv: lambda a, b: a // b,
    ast.Mod: lambda a, b: a % b,
    ast.Pow: lambda a, b: a**b,
}


def find_expression(text: str) -> str | None:
    """La operación que contiene un enunciado, si contiene alguna.

    Se busca el candidato más largo y se exige que tenga al menos dos números
    y un operador: así un enunciado que sólo menciona un año o una cifra no se
    confunde con una operación que haya que verificar.
    """
    mejor: str | None = None
    for bruto in _CANDIDATE.findall(text):
        limpio = normalise(bruto).strip(" .,;:")
        if not _looks_like_operation(limpio):
            continue
        if mejor is None or len(limpio) > len(mejor):
            mejor = limpio
    return mejor


def _looks_like_operation(expression: str) -> bool:
    numeros = re.findall(r"\d+(?:\.\d+)?", expression)
    if len(numeros) < 2:
        return False
    # Un operador entre dos números, no un signo colgando de un extremo.
    return re.search(r"\d\s*(?:\*\*|[+\-*/%])\s*[(\d-]", expression) is not None


def parse_number(text: str) -> float | None:
    """El número que expresa una opción, si expresa uno.

    Se toleran el punto decimal, el signo y algo de adorno alrededor ("= 48",
    "48 unidades"), porque la opción la escribe un modelo.
    """
    match = re.search(r"-?\d+(?:\.\d+)?", (text or "").replace(",", ""))
    return float(match.group(0)) if match else None


def check(question_text: str, answer: str) -> bool | None:
    """Si la respuesta marcada cuadra con la operación del enunciado.

    ``None`` cuando no hay nada que verificar: sin operación en el enunciado o
    sin número en la respuesta, no se puede decir ni que esté bien ni mal, y
    descartarla sería tirar cualquier pregunta que mencione una cifra.
    """
    expresion = find_expression(question_text)
    if expresion is None:
        return None
    esperado = evaluate(expresion)
    if esperado is None:
        return None
    dado = parse_number(answer)
    if dado is None:
        return None
    return abs(esperado - dado) <= _TOLERANCE * max(1.0, abs(esperado))
