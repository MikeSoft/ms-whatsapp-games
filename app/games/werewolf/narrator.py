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


class Narrator:
    """Produce el texto de ambientación de cada escena.

    Mantiene un historial rodante para que las narraciones no se repitan entre
    rondas. Si el LLM no está disponible o falla, devuelve el texto estático
    que se le pase como respaldo.
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
            text = await self._llm.complete(prompts.SYSTEM, user)
            if text:
                text = _tidy(text)

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


#: Asteriscos dobles o más: markdown que WhatsApp no interpreta y muestra tal
#: cual. Su negrita es ``*palabra*``, con un solo asterisco.
_DOUBLE_STARS = re.compile(r"\*{2,}")


def _tidy(text: str) -> str:
    """Limpia adornos que los modelos añaden por su cuenta."""
    cleaned = _DOUBLE_STARS.sub("", text).strip()
    # Comillas envolventes.
    for quote in ('"', "'", "«", "“"):
        if cleaned.startswith(quote):
            cleaned = cleaned[1:].strip()
    for quote in ('"', "'", "»", "”"):
        if cleaned.endswith(quote):
            cleaned = cleaned[:-1].strip()
    # Encabezados tipo "Narrador:" o "**Escena**". Se limpia el adorno, luego
    # el encabezado, y otra vez el adorno: un "**Narrador:**" deja los
    # asteriscos de cierre al quitar el prefijo del medio.
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
    if len(cleaned) > MAX_CHARS:
        cut = cleaned[:MAX_CHARS]
        # Corta en la última frase completa para no dejar la idea a medias.
        for sep in (". ", "… ", "! ", "? "):
            index = cut.rfind(sep)
            if index > MAX_CHARS // 2:
                return cut[: index + 1].strip()
        cleaned = cut.rstrip() + "…"
    return cleaned
