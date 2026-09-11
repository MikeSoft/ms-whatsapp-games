"""Generación de la ambientación narrativa con el LLM."""

from __future__ import annotations

import re
from typing import Any

from app.core.llm import LLMClient
from app.games.werewolf import prompts
from app.logging_conf import get_logger

log = get_logger("narrator")

#: Cuántas narraciones anteriores se le recuerdan al modelo para que no repita.
CONTEXT_WINDOW = 3
#: Tope duro de longitud: WhatsApp permite mucho más, pero nadie lee un muro.
MAX_CHARS = 900
#: Por debajo de esto no queda escena que publicar y se usa el respaldo.
MIN_CHARS = 40
#: Una escena sin un solo punto puede ser una frase suelta legítima ("La niebla
#: baja") o el principio de una respuesta cortada. Se le concede lo primero
#: mientras sea corta; pasado esto, no hay narración sin puntuación que valga.
UNPUNCTUATED_MAX = 120
#: Intentos antes de rendirse al texto estático. Una escena descartada sale
#: cara en experiencia y barata en tokens: se reintenta.
ATTEMPTS = 2


class Narrator:
    """Produce el texto de ambientación de cada escena.

    Mantiene un historial rodante para que las narraciones no se repitan entre
    rondas. Si el LLM no está disponible, falla o devuelve algo impublicable,
    devuelve el texto estático que se le pase como respaldo.
    """

    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm
        self._history: list[str] = []

    @property
    def history(self) -> list[str]:
        return list(self._history)

    async def flavour(
        self,
        scene: str,
        facts: dict[str, Any],
        *,
        fallback: str,
        max_words: int = 70,
        remember: bool = True,
    ) -> str:
        """Devuelve la ambientación de una escena (nunca vacía).

        ``remember=False`` para textos de relleno: si entraran al historial,
        el modelo acabaría narrando la partida a base de pasos en la plaza.
        """
        text: str | None = None
        if self._llm.available:
            user = prompts.flavour_prompt(
                scene,
                facts,
                context=self._history[-CONTEXT_WINDOW:],
                max_words=max_words,
            )
            for intento in range(ATTEMPTS):
                raw = await self._llm.complete(
                    prompts.SYSTEM, user, max_tokens=token_budget(max_words)
                )
                text = _tidy(raw) if raw else None
                if text:
                    break
                # Media escena o el andamiaje del modelo no se publican: se
                # descarta entero y se vuelve a pedir. Lo que llegó queda en el
                # log, que es donde se ve si un modelo empezó a portarse mal.
                log.warning(
                    "narrator.discarded",
                    scene=scene,
                    intento=intento + 1,
                    preview=(raw or "")[:160],
                )

        result = text or fallback
        if remember:
            self.remember(result)
        return result

    def remember(self, text: str) -> None:
        """Añade al historial algo narrado por el código (no por el LLM)."""
        if not text:
            return
        self._history.append(text)
        if len(self._history) > 12:
            self._history = self._history[-12:]


def token_budget(max_words: int) -> int:
    """Cupo de salida para una escena de ``max_words`` palabras.

    Va holgado a propósito. El límite de longitud que importa es el de
    palabras del prompt; esto sólo evita que la respuesta se corte por cupo,
    y en los modelos que razonan el cupo lo comparten pensamiento y texto.
    """
    return max(400, max_words * 12)


#: Asteriscos dobles o más: markdown que WhatsApp no interpreta y muestra tal
#: cual. Su negrita es ``*palabra*``, con un solo asterisco.
_DOUBLE_STARS = re.compile(r"\*{2,}")

#: Numeración de palabras que algunos modelos escriben para no pasarse del
#: límite: "humea (37) bajo (38) el (39) pálido (40) sol".
_WORD_COUNTER = re.compile(r"\s*\(\s*\d{1,3}\s*\)")

#: Viñeta de repaso. La narración no lleva listas (regla 5 del sistema), así
#: que una línea que empieza así es andamiaje.
_META_BULLET = re.compile(r"^\s*[*\-•]\s+")

#: Vocabulario con el que los modelos que razonan se hablan a sí mismos. La
#: escena va en español; una línea con esto dentro no es la escena, es el
#: modelo comprobando su propio trabajo ("Survivors count? Matches 6").
_ENGLISH = re.compile(
    r"\b(the|and|without|word|words|count|counts|matches|survivors|"
    r"check|checking|draft|ensure|avoid|naming|mentioning|should|"
    r"let me|i will|we need|revised|final answer|output)\b",
    re.IGNORECASE,
)

#: Arranques que delatan que falta texto por delante: un cierre o un signo de
#: puntuación suelto. La minúscula no vale como señal: hay escenas que empiezan
#: así de forma legítima cuando se les quita el encabezado.
_BAD_START = ")]}»”\u2019,;:."

#: Principio de frase creíble: mayúscula, apertura de interrogación o comilla.
_SENTENCE_START = re.compile(r"(?:^|[.!?…]\s+|\n)([\"“«¡¿A-ZÁÉÍÓÚÜÑ])")

_TERMINAL = ".!?…"


def _tidy(text: str) -> str | None:
    """Limpia el texto del modelo, o ``None`` si no hay escena publicable.

    Devolver ``None`` es parte del contrato: más vale el respaldo estático que
    una escena a medias o con el andamiaje del modelo dentro. Quien llama
    reintenta y, si tampoco, publica el respaldo.
    """
    cleaned = _WORD_COUNTER.sub("", text or "")
    cleaned = _DOUBLE_STARS.sub("", cleaned).strip()
    cleaned = _drop_meta_lines(cleaned)
    cleaned = _strip_wrapping(cleaned)
    cleaned = _drop_leading_fragment(cleaned)

    recortado = False
    if len(cleaned) > MAX_CHARS:
        cleaned = _cut_to_last_sentence(cleaned[:MAX_CHARS])
        recortado = True
    elif not _ends_complete(cleaned) and (
        any(ch in _TERMINAL for ch in cleaned) or len(cleaned) > UNPUNCTUATED_MAX
    ):
        # Cortado por cupo de tokens a media palabra: se retrocede a la última
        # frase entera, que es lo único que se puede publicar tal cual.
        cleaned = _cut_to_last_sentence(cleaned)
        recortado = True

    if not cleaned:
        return None
    # El mínimo sólo se exige a lo que hubo que recortar: una escena corta pero
    # entera es válida, y un muñón de lo que llegó cortado, no.
    if recortado and len(cleaned) < MIN_CHARS:
        return None
    return cleaned


def _drop_meta_lines(text: str) -> str:
    """Quita las líneas que son repaso del modelo y no narración."""
    lineas = [
        linea
        for linea in text.splitlines()
        if not _META_BULLET.match(linea) and not _ENGLISH.search(linea)
    ]
    return "\n".join(lineas).strip()


def _strip_wrapping(text: str) -> str:
    """Quita comillas envolventes y encabezados tipo "Narrador:"."""
    cleaned = text
    for quote in ('"', "'", "«", "“"):
        if cleaned.startswith(quote):
            cleaned = cleaned[1:].strip()
    for quote in ('"', "'", "»", "”"):
        if cleaned.endswith(quote):
            cleaned = cleaned[:-1].strip()
    # Se limpia el adorno, luego el encabezado, y otra vez el adorno: un
    # "**Narrador:**" deja los asteriscos de cierre al quitar el prefijo.
    for _ in range(2):
        # Sólo almohadillas de encabezado: un asterisco suelto es la negrita
        # válida de WhatsApp y no se toca.
        cleaned = cleaned.lstrip("# ").strip()
        for prefix in ("narrador:", "narración:", "narracion:", "escena:"):
            if cleaned.lower().startswith(prefix):
                cleaned = cleaned[len(prefix) :].strip()
                break
        else:
            break
    return cleaned


def _drop_leading_fragment(text: str) -> str:
    """Descarta lo que venga antes de la primera frase de verdad.

    Un modelo que razona en el mismo campo de texto empieza a veces por el
    final de su propio repaso: ") humea bajo el pálido sol". Eso no se
    publica, y lo que queda detrás casi siempre sí.
    """
    if not text or text[0] not in _BAD_START:
        return text
    for match in _SENTENCE_START.finditer(text):
        if match.start(1) > 0:
            return text[match.start(1) :].strip()
    return ""


def _ends_complete(text: str) -> bool:
    """¿Termina en frase cerrada? Los emojis y comillas del final no cuentan."""
    stripped = text.rstrip()
    while stripped and not stripped[-1].isalnum() and stripped[-1] not in _TERMINAL:
        stripped = stripped[:-1].rstrip()
    return bool(stripped) and stripped[-1] in _TERMINAL


def _cut_to_last_sentence(text: str) -> str:
    """Recorta hasta el último punto, que es donde la idea está entera."""
    for index in range(len(text) - 1, -1, -1):
        if text[index] in _TERMINAL:
            return text[: index + 1].strip()
    return ""
