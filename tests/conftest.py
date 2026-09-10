"""Dobles de prueba: permiten jugar una partida entera sin WAHA, Redis ni LLM."""

from __future__ import annotations

import itertools
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import pytest

from app.config import Settings
from app.core.inbox import MemoryInbox
from app.core.llm import LLMClient
from app.games.base import GameContext
from app.games.werewolf.nodes import Timers
from app.waha.models import InboundMessage, Scope

GROUP_ID = "120363000000000000@g.us"
MANAGER = "573000000000@c.us"

# La suite tiene que dar el mismo resultado en cualquier máquina. Dos cosas
# del entorno se colaban y la ponían roja sin que nadie tocara código:
#
# 1. El ``.env`` del desarrollador. ``Settings`` lo lee por defecto, así que
#    un despliegue local con otra sesión de WAHA u otro prefijo de comandos
#    cambiaba lo que los tests daban por sentado.
# 2. Un Redis escuchando en el 6379. El arranque hace ping y, si responde,
#    usa el buzón de Redis en vez del de memoria; los marcadores de "visto"
#    sobreviven entre ejecuciones y el webhook descarta como duplicados los
#    mensajes de la siguiente.
#
# Se cortan las dos aquí: sin fichero de entorno y contra un puerto cerrado.
Settings.model_config["env_file"] = None

#: Puerto reservado por la IANA como "sin uso": el ping falla al instante y el
#: buzón cae a memoria, que es lo que los tests asumen.
UNREACHABLE_REDIS = "redis://127.0.0.1:9/15"

_counter = itertools.count(1)


def make_jid(index: int) -> str:
    return f"5730011111{index:02d}@c.us"


@dataclass
class FakeTransport:
    """Registra todo lo enviado y permite reaccionar a cada mensaje."""

    group_messages: list[str] = field(default_factory=list)
    direct_messages: list[tuple[str, str]] = field(default_factory=list)
    polls: list[tuple[str, list[str]]] = field(default_factory=list)
    lock_history: list[bool] = field(default_factory=list)
    #: Los JID etiquetados en cada mensaje de grupo, en el mismo orden.
    group_mentions: list[list[str]] = field(default_factory=list)
    poll_supported: bool = True

    on_group: Callable[[str], Awaitable[None]] | None = None
    on_direct: Callable[[str, str], Awaitable[None]] | None = None

    async def send_group(self, text: str, *, mentions: list[str] | None = None) -> None:
        self.group_messages.append(text)
        self.group_mentions.append(list(mentions or []))
        if self.on_group is not None:
            await self.on_group(text)

    async def send_direct(self, jid: str, text: str) -> None:
        self.direct_messages.append((jid, text))
        if self.on_direct is not None:
            await self.on_direct(jid, text)

    async def send_poll(self, question: str, options: list[str]) -> bool:
        if not self.poll_supported:
            return False
        self.polls.append((question, options))
        return True

    async def set_group_locked(self, locked: bool) -> bool:
        self.lock_history.append(locked)
        return True

    # ------------------------------------------------------------- consultas
    @property
    def locked(self) -> bool:
        return self.lock_history[-1] if self.lock_history else False

    def group_text(self) -> str:
        return "\n".join(self.group_messages)

    def dms_to(self, jid: str) -> list[str]:
        return [text for target, text in self.direct_messages if target == jid]

    def dms_matching(self, pattern: str) -> list[tuple[str, str]]:
        return [(jid, text) for jid, text in self.direct_messages if pattern in text]

    def group_matching(self, pattern: str) -> list[tuple[str, list[str]]]:
        """Mensajes de grupo que contienen ``pattern``, con sus menciones."""
        return [
            (text, mentions)
            for text, mentions in zip(self.group_messages, self.group_mentions, strict=True)
            if pattern in text
        ]


def inbound(
    sender: str,
    text: str,
    *,
    scope: Scope = Scope.DIRECT,
    chat_id: str | None = None,
    name: str | None = None,
    poll_options: list[str] | None = None,
) -> InboundMessage:
    """Construye un mensaje entrante de prueba."""
    index = next(_counter)
    return InboundMessage(
        message_id=f"test-{index}",
        chat_id=chat_id or (GROUP_ID if scope == Scope.GROUP else sender),
        sender_id=sender,
        sender_name=name,
        text=text,
        scope=scope,
        timestamp=1_700_000_000 + index,
        kind="poll_vote" if poll_options else "text",
        poll_options=poll_options or [],
    )


def render_mentions(text: str, names: dict[str, str]) -> str:
    """Sustituye los tokens ``@<número>`` por el nombre del contacto.

    Es lo que hace el cliente de WhatsApp al mostrar una mención, así que los
    jugadores automáticos leen los mensajes del grupo igual que una persona.
    """
    rendered = text
    for jid, nombre in names.items():
        digits = "".join(ch for ch in jid.split("@", 1)[0] if ch.isdigit())
        if digits:
            rendered = rendered.replace(f"@{digits}", nombre)
    return rendered


def fast_timers() -> Timers:
    """Tiempos mínimos: una partida completa en milisegundos."""
    return Timers(
        recruit=0.05,
        night=0.30,
        witch=0.30,
        hunter=0.30,
        debate=0.02,
        vote=0.30,
        filler_interval=0.0,
    )


def make_settings(**overrides) -> Settings:
    base = {
        "manager_number": MANAGER,
        "game_group_id": GROUP_ID,
        "llm_provider": "none",
        "llm_api_key": None,
        "redis_url": UNREACHABLE_REDIS,
        "database_url": "sqlite+aiosqlite:///:memory:",
        "checkpointer": "memory",
        "werewolf_min_players": 4,
        "max_rounds": 12,
        "filler_interval_seconds": 0,
        "manage_group_permissions": True,
    }
    base.update(overrides)
    return Settings(**base)


def make_context(
    *,
    settings: Settings | None = None,
    transport: FakeTransport | None = None,
    inbox: MemoryInbox | None = None,
    session_id: str = "test-session",
) -> GameContext:
    resolved = settings or make_settings()
    return GameContext(
        session_id=session_id,
        group_id=GROUP_ID,
        started_by=MANAGER,
        settings=resolved,
        transport=transport or FakeTransport(),
        inbox=inbox or MemoryInbox(),
        llm=LLMClient(resolved),
        store=None,
        checkpointer=None,
    )


# --------------------------------------------------------------------------
# Jugadores automáticos
# --------------------------------------------------------------------------

_ROLE_LINE = re.compile(r"Tu rol es \*(.+?)\*")
_OPTION_LINE = re.compile(r"^(\d{1,2})\.\s+(.+)$", re.MULTILINE)


@dataclass
class ScriptedPlayers:
    """Mesa de jugadores automáticos.

    Reacciona a los mensajes del bot igual que lo harían personas: se apunta
    cuando se abre la convocatoria, responde los privados de su rol y vota en
    el juicio. Aprende su propio rol leyendo el privado del reparto, así que
    la estrategia es la de un jugador real: sólo sabe lo que le han dicho.
    """

    inbox: MemoryInbox
    session_id: str
    jids: list[str]
    names: dict[str, str]
    #: Respuesta de la bruja: "curar", "veneno", "nada".
    witch_reply: str = "nada"
    #: Respuesta del cazador: "nadie" o un número.
    hunter_reply: str = "nadie"
    #: Si es False, nadie contesta los privados de la noche.
    answer_night: bool = True

    roles: dict[str, str] = field(default_factory=dict)
    joined: bool = False

    def wolves(self) -> list[str]:
        return [jid for jid, role in self.roles.items() if role == "Hombre Lobo"]

    @staticmethod
    def _options(text: str) -> list[tuple[int, str]]:
        return [(int(num), name.strip()) for num, name in _OPTION_LINE.findall(text)]

    async def _say(self, jid: str, text: str) -> None:
        await self.inbox.push(self.session_id, inbound(jid, text, name=self.names.get(jid)))

    async def _say_group(self, jid: str, text: str) -> None:
        await self.inbox.push(
            self.session_id,
            inbound(jid, text, scope=Scope.GROUP, name=self.names.get(jid)),
        )

    async def on_group(self, raw: str) -> None:
        text = render_mentions(raw, self.names)
        if "se abren las inscripciones" in text and not self.joined:
            self.joined = True
            for jid in self.jids:
                await self._say_group(jid, "Yo")
            return

        if "*VOTACIÓN*" in text:
            options = self._options(text)
            if not options:
                return
            # Todos votan al primer vivo de la lista: converge y es estable.
            target = options[0][0]
            name_to_jid = {v: k for k, v in self.names.items()}
            for _, name in options:
                jid = name_to_jid.get(name)
                if jid is not None:
                    await self._say_group(jid, str(target))

    async def on_direct(self, jid: str, text: str) -> None:
        match = _ROLE_LINE.search(text)
        if match:
            self.roles[jid] = match.group(1)
            return

        if not self.answer_night:
            return

        if "¿A quién devoráis?" in text:
            options = self._options(text)
            if options:
                # Los lobos muerden al último de la lista.
                await self._say(jid, str(options[-1][0]))
        elif "¿De quién quieres conocer la identidad?" in text:
            options = self._options(text)
            if options:
                await self._say(jid, str(options[0][0]))
        elif "Elige a dos jugadores" in text:
            options = self._options(text)
            if len(options) >= 2:
                await self._say(jid, f"{options[0][0]} {options[1][0]}")
        elif "Pociones que te quedan" in text:
            await self._say(jid, self.witch_reply)
        elif "Acabas de morir" in text:
            await self._say(jid, self.hunter_reply)


@pytest.fixture
def table():
    """Fábrica de mesas: devuelve (ctx, transport, inbox, players)."""

    def _build(count: int = 6, *, settings: Settings | None = None, **script_kwargs):
        jids = [make_jid(i) for i in range(1, count + 1)]
        names = {jid: f"Jugador{i}" for i, jid in enumerate(jids, start=1)}
        inbox = MemoryInbox()
        transport = FakeTransport()
        ctx = make_context(settings=settings, transport=transport, inbox=inbox)
        script = ScriptedPlayers(
            inbox=inbox,
            session_id=ctx.session_id,
            jids=jids,
            names=names,
            **script_kwargs,
        )
        transport.on_group = script.on_group
        transport.on_direct = script.on_direct
        return ctx, transport, inbox, script

    return _build
