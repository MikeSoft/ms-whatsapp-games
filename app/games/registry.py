"""Registro de juegos disponibles.

Resuelve el nombre que escribe el manager (``!juego hombres lobo``) contra la
clave y los alias de cada juego, tolerando acentos, guiones y mayúsculas.
"""

from __future__ import annotations

import importlib
import unicodedata
from typing import TYPE_CHECKING

from app.logging_conf import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from app.games.base import Game, GameSpec

log = get_logger("games")

#: Paquetes de juegos incluidos de serie.
BUILTIN_MODULES = ("app.games.werewolf.game",)

_REGISTRY: dict[str, type[Game]] = {}
_ALIASES: dict[str, str] = {}
_loaded = False


def slugify(value: str) -> str:
    """``"Hombres-Lobo"`` -> ``"hombreslobo"``."""
    decomposed = unicodedata.normalize("NFKD", value or "")
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return "".join(ch for ch in stripped.lower() if ch.isalnum())


def register(game_cls: type[Game]) -> type[Game]:
    """Decorador que añade un juego al registro."""
    spec = game_cls.spec
    key = slugify(spec.key)
    if key in _REGISTRY and _REGISTRY[key] is not game_cls:
        raise ValueError(f"la clave de juego '{spec.key}' ya está registrada")

    _REGISTRY[key] = game_cls
    _ALIASES[key] = key
    for alias in spec.aliases:
        alias_key = slugify(alias)
        if not alias_key:
            continue
        existing = _ALIASES.get(alias_key)
        if existing and existing != key:
            raise ValueError(f"el alias '{alias}' ya apunta a '{existing}'")
        _ALIASES[alias_key] = key
    return game_cls


def load_builtin_games() -> None:
    """Importa los módulos de juegos para que se auto-registren."""
    global _loaded
    if _loaded:
        return
    for module in BUILTIN_MODULES:
        try:
            importlib.import_module(module)
        except Exception as exc:  # noqa: BLE001
            log.error("games.load_failed", module=module, error=str(exc))
    _loaded = True
    log.info("games.loaded", games=sorted(_REGISTRY))


def resolve(name: str) -> type[Game] | None:
    """Busca un juego por clave o alias. ``None`` si no existe."""
    load_builtin_games()
    key = _ALIASES.get(slugify(name))
    return _REGISTRY.get(key) if key else None


def specs() -> list[GameSpec]:
    """Especificaciones de todos los juegos registrados, por título."""
    load_builtin_games()
    return sorted((cls.spec for cls in _REGISTRY.values()), key=lambda s: s.title)


def keys() -> list[str]:
    load_builtin_games()
    return sorted(_REGISTRY)
