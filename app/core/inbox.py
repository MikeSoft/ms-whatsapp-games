"""Buzones efímeros de mensajes.

El webhook y la partida corren desacoplados: el webhook sólo *encola*
mensajes y los nodos del grafo los *recogen* con una ventana de tiempo. Esa
cola es el buzón (inbox).

Hay dos implementaciones:

* :class:`RedisInbox` — la de producción. Usa listas con TTL, así que los
  mensajes de una partida caducan solos y se pueden borrar al terminar.
* :class:`MemoryInbox` — la de los tests, sin dependencias externas.

Ambas comparten la interfaz :class:`Inbox`, de modo que el juego no sabe (ni
necesita saber) dónde se guardan los mensajes.
"""

from __future__ import annotations

import abc
import asyncio
import json
from collections.abc import Callable, Iterable, Sequence
from typing import TYPE_CHECKING

from app.logging_conf import get_logger
from app.waha.models import InboundMessage

if TYPE_CHECKING:  # pragma: no cover
    from redis.asyncio import Redis

log = get_logger("inbox")

GROUP_KEY = "group"


def _direct_key(jid: str) -> str:
    return f"dm:{jid}"


def key_for(message: InboundMessage) -> str:
    """Clave de buzón a la que pertenece un mensaje entrante."""
    if message.scope == "group":
        return GROUP_KEY
    return _direct_key(message.sender_id)


#: Firma del predicado de parada temprana.
StopWhen = Callable[[list[InboundMessage]], bool]


class Inbox(abc.ABC):
    """Cola de mensajes por partida y por origen."""

    @abc.abstractmethod
    async def push(self, session_id: str, message: InboundMessage) -> None:
        """Encola un mensaje entrante."""

    @abc.abstractmethod
    async def collect(
        self,
        session_id: str,
        *,
        timeout: float,
        group: bool = False,
        direct: Sequence[str] = (),
        stop_when: StopWhen | None = None,
    ) -> list[InboundMessage]:
        """Recoge mensajes durante ``timeout`` segundos.

        Escucha el buzón del grupo (si ``group``) y los privados de los JID
        de ``direct``. Devuelve los mensajes en orden de llegada. Si
        ``stop_when`` devuelve ``True`` para lo recogido hasta el momento, se
        corta la espera antes del timeout.
        """

    @abc.abstractmethod
    async def clear(self, session_id: str, *, keys: Iterable[str] | None = None) -> None:
        """Vacía los buzones de la partida (todos, o sólo los indicados)."""

    @abc.abstractmethod
    async def mark_seen(self, message_id: str, *, ttl: int = 3600) -> bool:
        """Registra un ``message_id``; devuelve ``False`` si ya se había visto.

        WAHA puede reintentar un webhook, y procesar dos veces un "Yo" o un
        voto falsearía la partida.
        """

    async def aclose(self) -> None:  # pragma: no cover - opcional
        return None


def _keys(group: bool, direct: Sequence[str]) -> list[str]:
    keys: list[str] = []
    if group:
        keys.append(GROUP_KEY)
    keys.extend(_direct_key(jid) for jid in direct)
    return keys


class MemoryInbox(Inbox):
    """Buzón en memoria para tests y para ejecutar sin Redis."""

    def __init__(self) -> None:
        self._buffers: dict[str, dict[str, list[InboundMessage]]] = {}
        self._events: dict[str, asyncio.Event] = {}
        self._seen: set[str] = set()

    def _event(self, session_id: str) -> asyncio.Event:
        return self._events.setdefault(session_id, asyncio.Event())

    async def push(self, session_id: str, message: InboundMessage) -> None:
        session = self._buffers.setdefault(session_id, {})
        session.setdefault(key_for(message), []).append(message)
        self._event(session_id).set()

    async def collect(
        self,
        session_id: str,
        *,
        timeout: float,
        group: bool = False,
        direct: Sequence[str] = (),
        stop_when: StopWhen | None = None,
    ) -> list[InboundMessage]:
        wanted = _keys(group, direct)
        if not wanted:
            return []

        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout)
        collected: list[InboundMessage] = []

        while True:
            session = self._buffers.setdefault(session_id, {})
            drained = False
            for key in wanted:
                queue = session.get(key)
                while queue:
                    collected.append(queue.pop(0))
                    drained = True

            if drained and stop_when and stop_when(collected):
                return collected

            remaining = deadline - loop.time()
            if remaining <= 0:
                return collected

            event = self._event(session_id)
            event.clear()
            try:
                await asyncio.wait_for(event.wait(), timeout=remaining)
            except TimeoutError:
                # Última pasada para no perder un mensaje que llegó justo al
                # expirar la ventana.
                session = self._buffers.setdefault(session_id, {})
                for key in wanted:
                    queue = session.get(key)
                    while queue:
                        collected.append(queue.pop(0))
                return collected

    async def clear(self, session_id: str, *, keys: Iterable[str] | None = None) -> None:
        session = self._buffers.get(session_id)
        if session is None:
            return
        if keys is None:
            session.clear()
        else:
            for key in keys:
                session.pop(key, None)

    async def mark_seen(self, message_id: str, *, ttl: int = 3600) -> bool:
        if message_id in self._seen:
            return False
        self._seen.add(message_id)
        return True


class RedisInbox(Inbox):
    """Buzón sobre listas de Redis con TTL.

    Se usa ``RPUSH`` + ``BLPOP`` para tener FIFO y poder esperar en varias
    claves a la vez (los privados de todos los roles nocturnos).
    """

    def __init__(self, redis: Redis, *, ttl_seconds: int = 900, namespace: str = "wag") -> None:
        self._redis = redis
        self._ttl = ttl_seconds
        self._ns = namespace

    def _list_key(self, session_id: str, key: str) -> str:
        return f"{self._ns}:inbox:{session_id}:{key}"

    def _index_key(self, session_id: str) -> str:
        return f"{self._ns}:inbox:{session_id}:keys"

    async def push(self, session_id: str, message: InboundMessage) -> None:
        key = key_for(message)
        list_key = self._list_key(session_id, key)
        index_key = self._index_key(session_id)
        payload = message.model_dump_json()

        pipe = self._redis.pipeline()
        pipe.rpush(list_key, payload)
        pipe.expire(list_key, self._ttl)
        pipe.sadd(index_key, key)
        pipe.expire(index_key, self._ttl)
        await pipe.execute()

    async def collect(
        self,
        session_id: str,
        *,
        timeout: float,
        group: bool = False,
        direct: Sequence[str] = (),
        stop_when: StopWhen | None = None,
    ) -> list[InboundMessage]:
        wanted = [self._list_key(session_id, key) for key in _keys(group, direct)]
        if not wanted:
            return []

        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout)
        collected: list[InboundMessage] = []

        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return collected

            # BLPOP sólo acepta timeouts con resolución de segundo; se limita
            # a 1s por iteración para no pasarse del deadline.
            block = min(1.0, max(0.05, remaining))
            try:
                result = await self._redis.blpop(wanted, timeout=block)
            except Exception as exc:  # noqa: BLE001 - Redis caído no mata la partida
                log.warning("inbox.blpop_failed", error=str(exc))
                await asyncio.sleep(0.5)
                continue

            if not result:
                continue

            _, raw = result
            message = _decode(raw)
            if message is None:
                continue
            collected.append(message)
            if stop_when and stop_when(collected):
                return collected

    async def clear(self, session_id: str, *, keys: Iterable[str] | None = None) -> None:
        index_key = self._index_key(session_id)
        if keys is None:
            known = await self._redis.smembers(index_key)
            keys = [k.decode() if isinstance(k, bytes) else str(k) for k in known]
        list_keys = [self._list_key(session_id, key) for key in keys]
        if list_keys:
            await self._redis.delete(*list_keys)
        await self._redis.delete(index_key)

    async def mark_seen(self, message_id: str, *, ttl: int = 3600) -> bool:
        key = f"{self._ns}:seen:{message_id}"
        stored = await self._redis.set(key, "1", nx=True, ex=ttl)
        return bool(stored)

    async def aclose(self) -> None:
        await self._redis.aclose()


def _decode(raw: bytes | str) -> InboundMessage | None:
    try:
        data = json.loads(raw)
        return InboundMessage.model_validate(data)
    except Exception as exc:  # noqa: BLE001
        log.warning("inbox.decode_failed", error=str(exc))
        return None
