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

#: Cada patrón captura un número con la palabra que lo cualifica, y se traga
#: además el andamiaje de alrededor ("que duren", "de a", "y con"). Si sólo
#: se quitara la cifra, el tema quedaría con los restos de la frase: de
#: "que duren 8 segundos" sobraría un "que duren" que no es ningún tema.
_LEAD = r"(?:\b(?:y|e)\s+)?"
_PATTERNS: dict[str, re.Pattern[str]] = {
    "questions": re.compile(
        _LEAD
        + r"(?:\b(?:has|haz|hazme|hagas|dame|genera|generame|quiero|pon|poneme)\s+)?"
        r"(?:(\d{1,3})\s*preguntas?|preguntas?\s*[:=]?\s*(\d{1,3}))",
        re.IGNORECASE,
    ),
    "seconds": re.compile(
        _LEAD
        + r"(?:\b(?:que\s+)?(?:se\s+)?"
        r"(?:duren|dure|duran|dura|durando"
        r"|demoren|demore|demoran|demora|demorando"
        r"|tarden|tarde|tardan|tarda|tardando)\s+)?"
        r"(?:\b(?:de|con|cada\s+una\s+de|cada\s+una)\s+)?"
        r"(?:(\d{1,3})\s*(?:segundos?|segs?\b|s\b)"
        r"|segundos?\s*[:=]?\s*(\d{1,3}))",
        re.IGNORECASE,
    ),
    "options": re.compile(
        _LEAD
        + r"(?:\b(?:de\s+a|con|de|cada\s+una\s+con)\s+)?"
        r"(?:(\d{1,2})\s*(?:opciones?|respuestas?|alternativas?)"
        r"|(?:opciones?|respuestas?|alternativas?)\s*[:=]?\s*(\d{1,2}))"
        r"(?:\s+por\s+pregunta)?",
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
    "que",
    "relacionadas",
    "relacionado",
    "relacionados",
    "relacionada",
    "a",
)


#: Nivel de exigencia -> cómo se le dice al modelo y con qué palabras se pide.
LEVELS: dict[str, tuple[str, tuple[str, ...]]] = {
    "facil": (
        "Nivel fácil: que casi cualquiera que conozca el tema por encima "
        "pueda acertar la mayoría.",
        ("facil", "faciles", "sencillas", "sencillo", "basicas", "basico"),
    ),
    "dificil": (
        "Nivel difícil: preguntas para quien domine el tema. La media no "
        "debería acertar ni la mitad.",
        ("dificil", "dificiles", "duras", "duro", "expertos", "experto",
         "avanzadas", "avanzado", "imposibles"),
    ),
}

_LEVEL_LOOKUP: dict[str, str] = {
    forma: nivel for nivel, (_, formas) in LEVELS.items() for forma in formas
}


@dataclass(frozen=True)
class Brief:
    """Lo que el máster pidió, ya acotado a lo que el juego admite."""

    topic: str
    questions: int
    #: En segundos. Es un flotante porque además de lo que pida el máster
    #: —siempre entero— los tests juegan tandas completas en milisegundos.
    seconds: float
    options: int
    #: ``None`` deja la calibración por defecto del prompt.
    level: str | None = None
    #: Segunda pasada de revisión para endurecer las preguntas.
    harden: bool = True
    #: Cuánto dejar pensar al modelo. Vacío no manda el parámetro.
    reasoning_effort: str = ""

    @property
    def topic_or_default(self) -> str:
        return self.topic or "cultura general"

    @property
    def level_note(self) -> str:
        """La línea que se le añade al prompt, o vacía si no se pidió nivel."""
        if self.level is None:
            return ""
        return LEVELS[self.level][0]


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
    # Sacar un patrón de en medio deja el hueco y su puntuación huérfana:
    # de "sobre cine y que dure 3 segundos, y que..." queda "cine , y que...".
    limpio = re.sub(r"\s+([,.;:])", r"\1", " ".join(words))
    # Los guiones largos van por punto de código: ruff los marca como
    # ambiguos si se escriben literales.
    return limpio.strip(" ,.:;-\u2013\u2014").strip()


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

    # El nivel se detecta antes de limpiar el tema: "preguntas difíciles de
    # anime" pide nivel y tema en la misma frase.
    nivel: str | None = None
    palabras: list[str] = []
    for palabra in resto.split():
        clave = _strip_accents(palabra).lower().strip(",.:;")
        if clave in _LEVEL_LOOKUP and nivel is None:
            nivel = _LEVEL_LOOKUP[clave]
            continue
        palabras.append(palabra)
    resto = " ".join(palabras)

    return Brief(
        topic=_clean_topic(resto),
        level=nivel,
        harden=settings.kahoot_harden,
        reasoning_effort=(settings.kahoot_llm_reasoning_effort or "").strip(),
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
