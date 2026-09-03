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
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable, Sequence
from typing import TYPE_CHECKING

from app.logging_conf import get_logger
from app.waha.models import InboundMessage

if TYPE_CHECKING:  # pragma: no cover
    from redis.asyncio import Redis

log = get_logger("inbox")

GROUP_KEY = "group"

#: Cada cuánto se sondea Redis en el último segundo de una ventana, donde no
#: se puede usar BLPOP con segundos enteros.
POLL_INTERVAL = 0.05


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


#: Tope de ``message_id`` recordados por :class:`MemoryInbox`. Sin él, el
#: conjunto de deduplicación crecería sin límite mientras el servicio viva.
SEEN_LIMIT = 20_000


class MemoryInbox(Inbox):
    """Buzón en memoria para tests y para ejecutar sin Redis."""

    def __init__(self, *, seen_limit: int = SEEN_LIMIT) -> None:
        self._buffers: dict[str, dict[str, deque[InboundMessage]]] = {}
        # Un evento por *colector*, no uno por sesión: con un evento
        # compartido, el `clear()` de un colector se comería el aviso de otro
        # que estuviera esperando en la misma sesión.
        self._waiters: dict[str, set[asyncio.Event]] = {}
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._seen_limit = max(1, seen_limit)

    async def push(self, session_id: str, message: InboundMessage) -> None:
        session = self._buffers.setdefault(session_id, {})
        session.setdefault(key_for(message), deque()).append(message)
        for event in self._waiters.get(session_id, ()):
            event.set()

    def _drain(self, session_id: str, wanted: Sequence[str]) -> list[InboundMessage]:
        session = self._buffers.get(session_id)
        if not session:
            return []
        drained: list[InboundMessage] = []
        for key in wanted:
            queue = session.get(key)
            while queue:
                drained.append(queue.popleft())
        return drained

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

        event = asyncio.Event()
        self._waiters.setdefault(session_id, set()).add(event)
        try:
            while True:
                nuevos = self._drain(session_id, wanted)
                collected.extend(nuevos)
                if nuevos and stop_when and stop_when(collected):
                    return collected

                remaining = deadline - loop.time()
                if remaining <= 0:
                    return collected

                event.clear()
                try:
                    await asyncio.wait_for(event.wait(), timeout=remaining)
                except TimeoutError:
                    # Última pasada: no perder un mensaje que llegó justo al
                    # expirar la ventana.
                    collected.extend(self._drain(session_id, wanted))
                    return collected
        finally:
            waiters = self._waiters.get(session_id)
            if waiters is not None:
                waiters.discard(event)
                if not waiters:
                    self._waiters.pop(session_id, None)

    async def clear(self, session_id: str, *, keys: Iterable[str] | None = None) -> None:
        session = self._buffers.get(session_id)
        if session is None:
            return
        if keys is None:
            # Purga total: se suelta también la entrada de la sesión, que si no
            # se acumularía una por partida jugada.
            self._buffers.pop(session_id, None)
        else:
            for key in keys:
                session.pop(key, None)

    async def mark_seen(self, message_id: str, *, ttl: int = 3600) -> bool:
        if message_id in self._seen:
            return False
        self._seen[message_id] = None
        while len(self._seen) > self._seen_limit:
            self._seen.popitem(last=False)
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
        try:
            await pipe.execute()
        except Exception as exc:  # noqa: BLE001
            # El mensaje ya está deduplicado, así que propagar no serviría de
            # nada: WAHA reintentaría y el reintento se descartaría. Se pierde
            # este mensaje y se sigue atendiendo el resto.
            log.error(
                "inbox.push_failed",
                session_id=session_id,
                key=key,
                error=str(exc),
            )

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
            nuevos = await self._drain(wanted)
            collected.extend(nuevos)
            if nuevos and stop_when and stop_when(collected):
                return collected

            remaining = deadline - loop.time()
            if remaining <= 0:
                return collected

            # BLPOP con timeout fraccionario exige Redis >= 6, así que sólo se
            # usa con segundos enteros. Para la cola de menos de un segundo se
            # sondea, que además es el camino que ejercitan los tests.
            if remaining < 1.0:
                await asyncio.sleep(min(POLL_INTERVAL, remaining))
                continue

            try:
                result = await self._redis.blpop(wanted, timeout=1)
            except Exception as exc:  # noqa: BLE001 - Redis caído no mata la partida
                log.warning("inbox.blpop_failed", error=str(exc))
                await asyncio.sleep(min(0.5, remaining))
                continue

            if not result:
                continue

            # BLPOP ya extrajo este mensaje de la lista: hay que quedárselo,
            # porque nadie más lo va a volver a ver.
            _, raw = result
            message = _decode(raw)
            if message is None:
                continue
            collected.append(message)
            if stop_when and stop_when(collected):
                return collected

    async def _drain(self, list_keys: Sequence[str]) -> list[InboundMessage]:
        """Vacía de golpe todas las listas indicadas.

        ``LRANGE`` + ``DELETE`` dentro de una transacción saca la lista entera
        en un solo viaje, en lugar de un ``BLPOP`` por mensaje. Se usa
        ``LRANGE`` en vez de ``LPOP key count`` porque este último exige
        Redis >= 6.2.
        """
        pipe = self._redis.pipeline()
        for key in list_keys:
            pipe.lrange(key, 0, -1)
            pipe.delete(key)
        try:
            results = await pipe.execute()
        except Exception as exc:  # noqa: BLE001 - Redis caído no mata la partida
            log.warning("inbox.drain_failed", error=str(exc))
            return []

        mensajes: list[InboundMessage] = []
        # Los resultados vienen en pares (lrange, delete) por cada clave.
        for raw_list in results[::2]:
            for raw in raw_list or ():
                message = _decode(raw)
                if message is not None:
                    mensajes.append(message)
        return mensajes

    async def clear(self, session_id: str, *, keys: Iterable[str] | None = None) -> None:
        index_key = self._index_key(session_id)
        if keys is None:
            known = await self._redis.smembers(index_key)
            names = [k.decode() if isinstance(k, bytes) else str(k) for k in known]
            list_keys = [self._list_key(session_id, name) for name in names]
            if list_keys:
                await self._redis.delete(*list_keys)
            await self._redis.delete(index_key)
            return

        # Purga parcial: se quitan sólo esas listas del índice. Borrar el
        # índice entero aquí dejaría huérfanas las demás claves de la partida,
        # que ya no se encontrarían en la purga final.
        names = list(keys)
        if not names:
            return
        list_keys = [self._list_key(session_id, name) for name in names]
        pipe = self._redis.pipeline()
        pipe.delete(*list_keys)
        pipe.srem(index_key, *names)
        await pipe.execute()

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
