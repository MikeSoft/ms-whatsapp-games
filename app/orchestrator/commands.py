"""Comandos del máster.

Sólo el número configurado en ``MANAGER_NUMBER`` puede usarlos. El prefijo es
configurable (``COMMAND_PREFIX``, por defecto ``!``).
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Command:
    """Un comando ya parseado.

    ``args`` trae sólo lo que no es un modificador conocido, porque el nombre
    de un juego puede llevar varias palabras (``hombres lobo``) y hay que
    poder distinguirlo de un flag que va detrás (``hombreslobo ia``).
    """

    name: str
    args: list[str] = field(default_factory=list)
    raw: str = ""
    flags: frozenset[str] = frozenset()

    @property
    def argument(self) -> str:
        """Los argumentos como una sola cadena (``#juego hombres lobo``)."""
        return " ".join(self.args).strip()

    def has(self, flag: str) -> bool:
        return flag in self.flags


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

#: Nombre canónico del modificador -> formas aceptadas.
#:
#: ``ia`` enciende la narración con modelo para esa partida. Sin él se juega
#: con los textos estáticos, que es el comportamiento por defecto: una partida
#: no debería gastar API sin que el máster lo pida.
FLAGS: dict[str, tuple[str, ...]] = {
    "ia": ("ia", "ai", "llm", "narrador"),
}

_FLAG_LOOKUP: dict[str, str] = {
    form: canonical for canonical, forms in FLAGS.items() for form in forms
}


def _strip_accents(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def parse_command(text: str, *, prefix: str = "#") -> Command | None:
    """Extrae un comando de un mensaje. ``None`` si no lo es.

    Es tolerante con mayúsculas y acentos: ``#Juego``, ``#catálogo`` y
    ``#CANCELAR`` funcionan igual.
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

    args: list[str] = []
    flags: set[str] = set()
    for word in parts[1:]:
        flag = _FLAG_LOOKUP.get(_strip_accents(word).lower())
        if flag is None:
            args.append(word)
        else:
            flags.add(flag)
    return Command(name=canonical, args=args, raw=raw, flags=frozenset(flags))


def help_text(prefix: str = "#") -> str:
    return (
        "🎮 *Comandos del máster*\n\n"
        f"`{prefix}juegos` — lista los juegos disponibles\n"
        f"`{prefix}juego <nombre>` — inicia una partida\n"
        f"`{prefix}juego <nombre> ia` — con narración generada por el modelo\n"
        f"`{prefix}estado` — qué hay en marcha ahora\n"
        f"`{prefix}cancelar` — corta la partida en curso\n"
        f"`{prefix}ayuda` — este mensaje"
    )
