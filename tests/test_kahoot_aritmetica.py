"""Verificación de las preguntas de cálculo."""

from __future__ import annotations

import pytest

from app.games.kahoot.arithmetic import check, evaluate, find_expression
from app.games.kahoot.questions import parse_questions


# ======================================================== evaluar sin eval
@pytest.mark.parametrize(
    ("expresion", "valor"),
    [
        # El orden de las operaciones, que es lo que se pone a prueba.
        ("6+6*6+6/1", 48.0),
        ("6+6*6", 42.0),
        ("(8-3)*2+4", 14.0),
        ("10-2^3", 2.0),
        ("100/4/5", 5.0),
        ("-3*-3", 9.0),
        ("2+3*4-6/2", 11.0),
        # Signos tipográficos: el modelo escribe los bonitos, y por punto de
        # código porque a la vista no se distinguen de los de teclado.
        ("8 \u00f7 2 \u00d7 (2+2)", 16.0),
        ("7 \u2212 2", 5.0),
    ],
)
def test_se_evaluan_las_operaciones_respetando_la_precedencia(expresion, valor):
    assert evaluate(expresion) == pytest.approx(valor)


@pytest.mark.parametrize(
    "texto",
    [
        # Nada que no sea aritmética se evalúa. El texto viene de un modelo,
        # que a su vez repite lo que escribe la gente.
        '__import__("os").system("echo pwned")',
        'open("/etc/passwd").read()',
        "1 + x",
        "print(1)",
        "[].__class__",
        # Un exponente enorme colgaría el proceso calculándolo.
        "9**999999",
        "",
        "no es una cuenta",
    ],
)
def test_no_se_evalua_nada_que_no_sea_una_cuenta(texto):
    assert evaluate(texto) is None


# =================================================== encontrar la operación
def test_se_encuentra_la_operacion_dentro_del_enunciado():
    assert find_expression("¿Cuál es el resultado de 6+6*6+6/1?") == "6+6*6+6/1"
    assert find_expression("¿Cuánto vale (8-3)*2+4 exactamente?") == "(8-3)*2+4"


@pytest.mark.parametrize(
    "texto",
    [
        # Una cifra suelta no es una operación que haya que verificar.
        "¿En qué año nació Einstein?",
        "¿Cuántos huesos tiene el cuerpo humano adulto?",
        "¿Cuál es la capital de Francia?",
        "¿Qué ocurrió entre 1914 y 1918?",
    ],
)
def test_una_cifra_suelta_no_es_una_operacion(texto):
    assert find_expression(texto) is None


# ============================================================ la comprobación
def test_una_cuenta_bien_pasa_y_una_mal_no():
    assert check("¿Cuál es el resultado de 6+6*6+6/1?", "48") is True
    # 72 es lo que sale resolviendo de izquierda a derecha: es la falsa que
    # el ejercicio pretende detectar, no la respuesta.
    assert check("¿Cuál es el resultado de 6+6*6+6/1?", "72") is False


def test_sin_nada_que_verificar_no_se_opina():
    """Ni bien ni mal: descartar por mencionar una cifra sería absurdo."""
    assert check("¿En qué año nació Einstein?", "1879") is None
    assert check("¿Cuánto vale 2+2*2?", "seis") is None


def test_la_opcion_puede_traer_adorno_alrededor():
    """La escribe un modelo: "= 48" y "48 unidades" son el mismo 48."""
    assert check("¿Cuánto es 6+6*6+6/1?", "= 48") is True
    assert check("¿Cuánto es 6+6*6+6/1?", "48 en total") is True


def test_las_divisiones_con_decimales_no_fallan_por_redondeo():
    assert check("¿Cuánto es 10/4?", "2.5") is True


# =================================== integrado en la validación de la tanda
def test_se_descarta_la_pregunta_de_calculo_mal_resuelta():
    crudas = [
        {"pregunta": "¿Cuál es el resultado de 6+6*6+6/1?",
         "opciones": ["48", "72", "42"], "correcta": 0},
        {"pregunta": "¿Cuánto vale (8-3)*2+4?",
         "opciones": ["18", "14", "20"], "correcta": 0},
    ]
    salida = parse_questions({"preguntas": crudas}, options=3)

    # La primera está bien (48); la segunda marca 18 y vale 14.
    assert len(salida) == 1
    assert salida[0].answer == "48"


def test_una_cuenta_no_se_descarta_por_repetir_sus_propias_cifras():
    """En "10-4*2+8/4" el resultado es 4, y el 4 está en la expresión.

    El filtro de "el enunciado regala la respuesta" tumbaba estas preguntas.
    En una cuenta eso no significa nada: saber el resultado es precisamente
    lo que se pregunta.
    """
    crudas = [{"pregunta": "10-4*2+8/4", "opciones": ["4", "6", "9"], "correcta": 0}]
    assert len(parse_questions({"preguntas": crudas}, options=3)) == 1


def test_dos_cuentas_distintas_conviven_aunque_compartan_cifras():
    """"8+2*5-4" y "(8+2)*5-4" son la pareja que se quiere preguntar.

    Comparadas por vocabulario son idénticas —las mismas cifras y los mismos
    signos—, y es el contraste entre las dos lo que enseña la regla.
    """
    crudas = [
        {"pregunta": "8+2*5-4", "opciones": ["14", "46", "18"], "correcta": 0},
        {"pregunta": "(8+2)*5-4", "opciones": ["46", "14", "50"], "correcta": 0},
    ]
    assert len(parse_questions({"preguntas": crudas}, options=3)) == 2


def test_la_misma_cuenta_dos_veces_si_se_descarta():
    crudas = [
        {"pregunta": "¿Cuánto es 8+2*5-4?", "opciones": ["14", "46", "18"], "correcta": 0},
        {"pregunta": "Calcula 8+2*5-4", "opciones": ["14", "46", "50"], "correcta": 0},
    ]
    assert len(parse_questions({"preguntas": crudas}, options=3)) == 1


def test_dos_cuentas_con_el_mismo_resultado_no_son_la_misma_pregunta():
    crudas = [
        {"pregunta": "2+3*4", "opciones": ["14", "20", "9"], "correcta": 0},
        {"pregunta": "20-3*2", "opciones": ["14", "34", "16"], "correcta": 0},
    ]
    assert len(parse_questions({"preguntas": crudas}, options=3)) == 2
