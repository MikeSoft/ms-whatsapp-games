"""Comandos del máster.

Sólo el número configurado en ``MANAGER_NUMBER`` puede usarlos. El prefijo es
configurable (``COMMAND_PREFIX``, por defecto ``!``).
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Command:
    """Un comando ya parseado."""

    name: str
    args: list[str] = field(default_factory=list)
    raw: str = ""

    @property
    def argument(self) -> str:
        """Los argumentos como una sola cadena (``!juego hombres lobo``)."""
        return " ".join(self.args).strip()


#: Nombre canónico -> formas aceptadas.
ALIASES: dict[str, tuple[str, ...]] = {
    "juego": ("juego", "jugar", "start", "game", "nuevo"),
    "juegos": ("juegos", "games", "lista", "list", "catalogo"),
    "cancelar": ("cancelar", "cancel", "abortar", "stop", "parar"),
    "estado": ("estado", "status", "info"),
    "ayuda": ("ayuda", "help", "comandos", "h"),
}

_LOOKUP: dict[str, str] = {
    alias: canonical for canonical, aliases in ALIASES.items() for alias in aliases
}


def _strip_accents(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def parse_command(text: str, *, prefix: str = "!") -> Command | None:
    """Extrae un comando de un mensaje. ``None`` si no lo es.

    Es tolerante con mayúsculas y acentos: ``!Juego``, ``!catálogo`` y
    ``!CANCELAR`` funcionan igual.
    """
    raw = (text or "").strip()
    if not raw or not prefix or not raw.startswith(prefix):
        return None

    body = raw[len(prefix) :].strip()
    if not body:
        return None

    parts = body.split()
    head = _strip_accents(parts[0]).lower()
    canonical = _LOOKUP.get(head)
    if canonical is None:
        return None
    return Command(name=canonical, args=parts[1:], raw=raw)


def help_text(prefix: str = "!") -> str:
    return (
        "🎮 *Comandos del máster*\n\n"
        f"`{prefix}juegos` — lista los juegos disponibles\n"
        f"`{prefix}juego <nombre>` — inicia una partida\n"
        f"`{prefix}estado` — qué hay en marcha ahora\n"
        f"`{prefix}cancelar` — corta la partida en curso\n"
        f"`{prefix}ayuda` — este mensaje"
    )
