"""Lectura de la instrucción que escribe el máster.

``!juego kahoot 15 preguntas de 10 segundos sobre cine de los ochenta``

Los números se sacan con expresiones regulares y lo que sobra es el tema. Se
hace así a propósito: cuántas preguntas hay, cuánto duran y cuántas opciones
tienen son decisiones de la mecánica, y la mecánica no la decide el modelo.
Al modelo se le da el tema, que es lenguaje, y para eso sí sirve.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from app.config import Settings

#: Cada patrón captura un número seguido de la palabra que lo cualifica. Se
#: aceptan las dos órdenes ("10 preguntas" y "preguntas: 10") porque la gente
#: escribe de las dos formas.
_PATTERNS: dict[str, re.Pattern[str]] = {
    "questions": re.compile(
        r"(?:(\d{1,3})\s*(?:preguntas?)|(?:preguntas?)\s*[:=]?\s*(\d{1,3}))", re.IGNORECASE
    ),
    "seconds": re.compile(
        r"(?:(\d{1,3})\s*(?:segundos?|segs?\b|s\b)|(?:segundos?)\s*[:=]?\s*(\d{1,3}))",
        re.IGNORECASE,
    ),
    "options": re.compile(
        r"(?:(\d{1,2})\s*(?:opciones?|respuestas?|alternativas?)"
        r"|(?:opciones?|respuestas?|alternativas?)\s*[:=]?\s*(\d{1,2}))",
        re.IGNORECASE,
    ),
}

#: Palabras de relleno con las que suele empezar la orden. Quitarlas deja un
#: tema más limpio para el prompt ("historia de Roma" en vez de "genera
#: preguntas sobre historia de Roma").
_PREFIXES = (
    "generame",
    "generar",
    "genera",
    "hazme",
    "haz",
    "hacer",
    "poneme",
    "pon",
    "dame",
    "quiero",
    "preguntas",
    "pregunta",
    "sobre",
    "de",
    "del",
    "acerca",
    "tema",
    "temas",
    "con",
    "y",
)


@dataclass(frozen=True)
class Brief:
    """Lo que el máster pidió, ya acotado a lo que el juego admite."""

    topic: str
    questions: int
    #: En segundos. Es un flotante porque además de lo que pida el máster
    #: —siempre entero— los tests juegan tandas completas en milisegundos.
    seconds: float
    options: int

    @property
    def topic_or_default(self) -> str:
        return self.topic or "cultura general"


def _strip_accents(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _number(match: re.Match[str] | None) -> int | None:
    if match is None:
        return None
    for group in match.groups():
        if group:
            return int(group)
    return None


def _clamp(value: int | None, default: int, low: int, high: int) -> int:
    if value is None:
        return default
    return max(low, min(high, value))


def _clean_topic(text: str) -> str:
    """Lo que queda tras quitar las cifras y el andamiaje de la orden.

    Se limpia por los dos extremos: al sacar "4 opciones" del medio de "sobre
    biología con 4 opciones", la preposición se queda colgando al final.
    """
    words = text.split()

    def _es_relleno(palabra: str) -> bool:
        return _strip_accents(palabra).lower().strip(",.:;") in _PREFIXES

    while words and _es_relleno(words[0]):
        words.pop(0)
    while words and _es_relleno(words[-1]):
        words.pop()
    # Los guiones largos van por punto de código: ruff los marca como
    # ambiguos si se escriben literales.
    return " ".join(words).strip(" ,.:;-\u2013\u2014").strip()


def parse_brief(text: str, settings: Settings) -> Brief:
    """Interpreta la instrucción del máster; los topes mandan sobre lo pedido."""
    raw = (text or "").strip()

    found: dict[str, int | None] = {}
    resto = raw
    for name, pattern in _PATTERNS.items():
        match = pattern.search(resto)
        found[name] = _number(match)
        if match is not None:
            resto = resto[: match.start()] + " " + resto[match.end() :]

    return Brief(
        topic=_clean_topic(resto),
        questions=_clamp(
            found["questions"],
            settings.kahoot_questions,
            1,
            settings.kahoot_max_questions,
        ),
        seconds=_clamp(
            found["seconds"],
            settings.kahoot_seconds_per_question,
            3,
            settings.kahoot_max_seconds,
        ),
        # El tope de 12 es de WhatsApp, no nuestro.
        options=_clamp(found["options"], settings.kahoot_options, 2, 12),
    )
