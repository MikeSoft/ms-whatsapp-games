"""Contrato común de los juegos.

Un juego recibe un :class:`GameContext` (por dónde hablar, de dónde leer, con
qué LLM narrar) y devuelve un :class:`GameResult`. Nada más. Cómo lo haga por
dentro es asunto suyo: El Hombre Lobo usa un grafo de LangGraph, pero otro
juego podría ser un simple bucle.

Para añadir un juego nuevo:

1. Crear ``app/games/<mi_juego>/game.py`` con una subclase de :class:`Game`.
2. Definir su ``spec`` (clave, alias, mínimo y máximo de jugadores).
3. Registrarla con ``@register`` e importar el paquete en
   ``app/games/registry.py:load_builtin_games``.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from app.config import Settings
from app.core.inbox import Inbox
from app.core.llm import LLMClient


class GameSpec(BaseModel):
    """Metadatos de un juego, usados por el menú y las validaciones."""

    key: str
    title: str
    tagline: str
    aliases: tuple[str, ...] = ()
    min_players: int = 2
    max_players: int = 30
    how_to: str = ""
    #: El juego no tiene sentido sin modelo (por ejemplo, si genera su propio
    #: contenido). Recibe el LLM aunque el máster no escriba ``ia``, que en
    #: los demás juegos es lo que decide gastar API.
    needs_llm: bool = False

    def rango_jugadores(self) -> str:
        return f"{self.min_players}-{self.max_players} jugadores"


class GameResult(BaseModel):
    """Desenlace de una partida."""

    status: Literal["finished", "aborted", "cancelled", "error"] = "finished"
    winner: str | None = None
    rounds: int = 0
    players: list[dict[str, Any]] = Field(default_factory=list)
    summary: str = ""
    error: str | None = None


@runtime_checkable
class Transport(Protocol):
    """Salida del juego hacia WhatsApp, ya fijada a un grupo concreto.

    Los tests inyectan un doble que sólo apunta lo enviado, así que una
    partida completa se puede ejecutar sin WAHA ni red.
    """

    async def send_group(self, text: str, *, mentions: list[str] | None = None) -> None:
        """Publica un mensaje en el grupo de la partida.

        ``mentions`` son los JID a etiquetar; el texto debe contener ya sus
        tokens ``@<número>`` (ver :mod:`app.games.mentions`).
        """
        ...

    async def send_direct(self, jid: str, text: str) -> None:
        """Envía un privado a un jugador."""
        ...

    async def send_poll(self, question: str, options: list[str]) -> str | None:
        """Publica una encuesta y devuelve su identificador.

        ``None`` si no se pudo crear. La cadena puede venir vacía cuando el
        envío salió bien pero WAHA no devolvió identificador: la encuesta está
        publicada, pero no habrá forma de retirarla luego.
        """
        ...

    async def delete_group_message(self, message_id: str) -> bool:
        """Retira un mensaje del grupo. ``False`` si no se pudo."""
        ...

    async def contact_name(self, jid: str) -> str | None:
        """Nombre con el que mostrar a alguien, o ``None`` si no se sabe.

        Hay eventos que no lo traen —un voto de encuesta llega sólo con el
        identificador— y en los grupos nuevos ese identificador es un
        ``@lid``, que en crudo no permite reconocer a nadie.
        """
        ...

    async def set_group_locked(self, locked: bool) -> bool:
        """Silencia (``True``) o reabre (``False``) el grupo."""
        ...


@dataclass
class GameContext:
    """Todo lo que un juego necesita del entorno."""

    session_id: str
    group_id: str
    started_by: str
    settings: Settings
    transport: Transport
    inbox: Inbox
    llm: LLMClient
    #: Persistencia opcional; los tests pueden pasar ``None``.
    store: Any | None = None
    #: Checkpointer de LangGraph, si el juego usa uno.
    checkpointer: Any | None = None
    #: Argumentos libres del comando (``#juego hombreslobo rapido``).
    args: list[str] = field(default_factory=list)
    #: Modificadores reconocidos del comando (``#juego hombreslobo ia``).
    #: El orquestador ya los ha aplicado donde tocaba —``ia`` decide qué
    #: cliente de LLM llega en ``llm``—, pero el juego puede consultarlos.
    flags: frozenset[str] = frozenset()

    async def record(
        self,
        kind: str,
        *,
        round_no: int = 0,
        phase: str = "",
        detail: dict[str, Any] | None = None,
        is_secret: bool = False,
    ) -> None:
        """Guarda un evento de la partida si hay persistencia configurada."""
        if self.store is None:
            return
        await self.store.log_game_event(
            self.session_id,
            round_no=round_no,
            phase=phase,
            kind=kind,
            detail=detail,
            is_secret=is_secret,
        )


class Game(abc.ABC):
    """Clase base de todos los juegos."""

    spec: ClassVar[GameSpec]

    def __init__(self, ctx: GameContext) -> None:
        self.ctx = ctx

    @abc.abstractmethod
    async def run(self) -> GameResult:
        """Ejecuta la partida de principio a fin."""

    async def on_cancel(self) -> None:
        """Gancho invocado cuando el manager cancela la partida.

        Se ejecuta después de cancelar la tarea, así que sirve para dejar el
        grupo en un estado usable (por ejemplo, reabrir el chat).
        """
        return None
