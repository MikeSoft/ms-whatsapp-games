"""Generación y validación de las preguntas del concurso.

El modelo propone; este módulo decide qué se publica. Una pregunta que llegue
mal formada —sin respuesta correcta, con opciones repetidas, con más o menos
alternativas de las pedidas— se descarta en vez de salir al grupo, porque una
encuesta rota no se puede arreglar una vez enviada.

Si no queda nada utilizable se recurre al banco estático de abajo: el juego
tiene que poder jugarse con ``LLM_PROVIDER=none``, como todo lo demás.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

from app.core.llm import LLMClient
from app.games.kahoot.brief import Brief
from app.logging_conf import get_logger

log = get_logger("kahoot")

#: Límites de las encuestas de WhatsApp.
MAX_QUESTION_CHARS = 240
MAX_OPTION_CHARS = 90

SYSTEM = """\
Escribes preguntas de concurso tipo Kahoot para jugar por WhatsApp, en español.

REGLAS:
1. Devuelves únicamente un objeto JSON, sin texto alrededor ni vallas de código.
2. Formato exacto:
   {"preguntas": [{"pregunta": "...", "opciones": ["...", "..."], "correcta": 0}]}
3. "correcta" es el índice (empezando en 0) de la opción verdadera.
4. Cada pregunta tiene exactamente el número de opciones que se te pide, todas
   distintas entre sí y sólo una verdadera. Las falsas tienen que ser creíbles:
   nada de rellenar con disparates que delaten la respuesta.
5. Pregunta de una sola frase, máximo 200 caracteres. Opciones muy cortas,
   máximo 70 caracteres, sin numerarlas ni ponerles letras delante.
6. Hechos comprobables y sin ambigüedad. Nada de opiniones, nada que dependa
   de la fecha de hoy, nada que haya cambiado en los últimos dos años.
7. No repitas pregunta ni tema dentro de la misma tanda."""


@dataclass(frozen=True)
class Question:
    """Una pregunta ya validada y lista para publicarse."""

    text: str
    options: tuple[str, ...]
    correct: int

    @property
    def answer(self) -> str:
        return self.options[self.correct]


def _prompt(brief: Brief) -> str:
    return (
        f"TEMA: {brief.topic_or_default}\n"
        f"CANTIDAD: {brief.questions} preguntas\n"
        f"OPCIONES POR PREGUNTA: {brief.options}\n\n"
        "Devuelve ahora el JSON."
    )


def _clean(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit].strip()


def parse_questions(data: Any, *, options: int) -> list[Question]:
    """Convierte la respuesta del modelo en preguntas utilizables.

    Tolerante con la envoltura (acepta la lista suelta o bajo varias claves) y
    estricta con el contenido: lo que no cumple, se cae.
    """
    crudas: Any = data
    if isinstance(data, dict):
        for key in ("preguntas", "questions", "items"):
            if isinstance(data.get(key), list):
                crudas = data[key]
                break
    if not isinstance(crudas, list):
        return []

    preguntas: list[Question] = []
    vistas: set[str] = set()
    for cruda in crudas:
        pregunta = _one(cruda, options=options)
        if pregunta is None:
            continue
        clave = pregunta.text.casefold()
        if clave in vistas:
            continue
        vistas.add(clave)
        preguntas.append(pregunta)
    return preguntas


def _one(cruda: Any, *, options: int) -> Question | None:
    if not isinstance(cruda, dict):
        return None

    texto = _clean(cruda.get("pregunta") or cruda.get("question"), MAX_QUESTION_CHARS)
    if not texto:
        return None

    brutas = cruda.get("opciones") or cruda.get("options")
    if not isinstance(brutas, list):
        return None

    # Se deduplica sin distinguir mayúsculas conservando el orden: WhatsApp no
    # admite dos opciones iguales, y el índice correcto se mueve con ellas.
    limpias: list[str] = []
    indices: dict[str, int] = {}
    for bruta in brutas:
        opcion = _clean(bruta, MAX_OPTION_CHARS)
        if not opcion or opcion.casefold() in indices:
            continue
        indices[opcion.casefold()] = len(limpias)
        limpias.append(opcion)

    if len(limpias) != options:
        return None

    correcta = cruda.get("correcta")
    if correcta is None:
        correcta = cruda.get("correct")
    if isinstance(correcta, str):
        # Algunos modelos devuelven el texto de la respuesta en vez del índice.
        correcta = indices.get(_clean(correcta, MAX_OPTION_CHARS).casefold())
    if not isinstance(correcta, int) or isinstance(correcta, bool):
        return None
    if not 0 <= correcta < len(limpias):
        return None

    return Question(text=texto, options=tuple(limpias), correct=correcta)


def shuffle_options(
    preguntas: list[Question], rng: random.Random | None = None
) -> list[Question]:
    """Baraja las opciones de cada pregunta y recoloca la correcta.

    Hace falta con las dos fuentes. El banco estático guarda la correcta en
    una posición fija, y los modelos tienden a poner la buena de primera; en
    los dos casos, sin barajar se aprende a ganar mirando el índice en vez de
    sabiendo la respuesta.
    """
    azar = rng or random.Random()
    barajadas: list[Question] = []
    for pregunta in preguntas:
        orden = list(pregunta.options)
        azar.shuffle(orden)
        barajadas.append(
            Question(
                text=pregunta.text,
                options=tuple(orden),
                correct=orden.index(pregunta.answer),
            )
        )
    return barajadas


async def generate(
    llm: LLMClient, brief: Brief, *, rng: random.Random | None = None
) -> tuple[list[Question], bool]:
    """Preguntas para la partida y si salieron del modelo.

    El segundo valor dice si se generaron (``True``) o si se tiró del banco
    estático (``False``), para poder avisarlo en el grupo en vez de fingir que
    el tema pedido se respetó.
    """
    if llm.available:
        data = await llm.complete_json(SYSTEM, _prompt(brief), temperature=0.8)
        preguntas = parse_questions(data, options=brief.options)
        if len(preguntas) >= brief.questions:
            return shuffle_options(preguntas[: brief.questions], rng), True
        log.warning(
            "kahoot.generation_short",
            pedidas=brief.questions,
            validas=len(preguntas),
            tema=brief.topic_or_default,
        )
        if preguntas:
            return shuffle_options(preguntas, rng), True

    return shuffle_options(fallback_questions(brief), rng), False


def fallback_questions(brief: Brief) -> list[Question]:
    """Banco estático, recortado a las opciones que pidió el máster.

    Devuelve la correcta en la primera posición; barajarlas es cosa de
    :func:`shuffle_options`, que corre para las dos fuentes.
    """
    preguntas: list[Question] = []
    for texto, opciones, correcta in _BANK:
        if len(opciones) < brief.options:
            continue
        # Se conserva siempre la correcta y se rellena con las primeras falsas.
        buena = opciones[correcta]
        malas = [o for i, o in enumerate(opciones) if i != correcta]
        elegidas = [buena, *malas[: brief.options - 1]]
        preguntas.append(
            Question(text=texto, options=tuple(elegidas), correct=0)
        )
        if len(preguntas) >= brief.questions:
            break
    return preguntas


#: Preguntas de respaldo: ``(enunciado, opciones, índice correcto)``.
#:
#: Existen para cumplir la regla de que nada dependa del modelo para
#: funcionar. Son de cultura general y deliberadamente estables: nada que
#: pueda cambiar de respuesta con el tiempo.
_BANK: tuple[tuple[str, tuple[str, ...], int], ...] = (
    ("¿Cuál es el río más largo de América del Sur?",
     ("Amazonas", "Orinoco", "Paraná", "Magdalena", "São Francisco", "Ucayali"), 0),
    ("¿Cuántos huesos tiene el cuerpo humano adulto?",
     ("206", "180", "232", "195", "250", "212"), 0),
    ("¿Quién pintó 'La persistencia de la memoria'?",
     ("Salvador Dalí", "Pablo Picasso", "Joan Miró", "René Magritte",
      "Frida Kahlo", "Marc Chagall"), 0),
    ("¿Cuál es el planeta más grande del sistema solar?",
     ("Júpiter", "Saturno", "Neptuno", "Urano", "La Tierra", "Venus"), 0),
    ("¿En qué país se encuentra Machu Picchu?",
     ("Perú", "Bolivia", "Ecuador", "Chile", "Colombia", "México"), 0),
    ("¿Cuál es el símbolo químico del oro?",
     ("Au", "Ag", "Or", "Go", "Fe", "Pb"), 0),
    ("¿Quién escribió 'Cien años de soledad'?",
     ("Gabriel García Márquez", "Mario Vargas Llosa", "Julio Cortázar",
      "Jorge Luis Borges", "Pablo Neruda", "Isabel Allende"), 0),
    ("¿Cuál es el océano más profundo del planeta?",
     ("Pacífico", "Atlántico", "Índico", "Ártico", "Antártico", "Caribe"), 0),
    ("¿Cuántos lados tiene un heptágono?",
     ("Siete", "Seis", "Ocho", "Nueve", "Cinco", "Diez"), 0),
    ("¿Cuál es la capital de Australia?",
     ("Camberra", "Sídney", "Melbourne", "Brisbane", "Perth", "Adelaida"), 0),
    ("¿Qué gas absorben las plantas para hacer la fotosíntesis?",
     ("Dióxido de carbono", "Oxígeno", "Nitrógeno", "Hidrógeno",
      "Metano", "Ozono"), 0),
    ("¿En qué continente está el desierto del Sahara?",
     ("África", "Asia", "Oceanía", "América", "Europa", "Antártida"), 0),
    ("¿Cuál es el metal líquido a temperatura ambiente?",
     ("Mercurio", "Plomo", "Estaño", "Zinc", "Aluminio", "Cobre"), 0),
    ("¿Quién formuló la teoría de la relatividad?",
     ("Albert Einstein", "Isaac Newton", "Niels Bohr", "Galileo Galilei",
      "Max Planck", "Stephen Hawking"), 0),
    ("¿Cuántos jugadores tiene un equipo de fútbol en el campo?",
     ("Once", "Diez", "Doce", "Nueve", "Trece", "Ocho"), 0),
    ("¿Cuál es el idioma más hablado del mundo como lengua materna?",
     ("Chino mandarín", "Inglés", "Español", "Hindi", "Árabe", "Portugués"), 0),
    ("¿Qué instrumento mide la presión atmosférica?",
     ("Barómetro", "Termómetro", "Higrómetro", "Anemómetro",
      "Altímetro", "Sismógrafo"), 0),
    ("¿Cuál es la montaña más alta de América?",
     ("Aconcagua", "Chimborazo", "Denali", "Huascarán",
      "Pico de Orizaba", "Illimani"), 0),
    ("¿Cuántos colores tiene el arcoíris según la convención clásica?",
     ("Siete", "Cinco", "Seis", "Ocho", "Nueve", "Cuatro"), 0),
    ("¿Qué órgano produce la insulina?",
     ("El páncreas", "El hígado", "El riñón", "El bazo",
      "La tiroides", "El estómago"), 0),
)
