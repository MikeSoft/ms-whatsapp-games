"""Implementación de :class:`~app.games.base.Transport` sobre WAHA."""

from __future__ import annotations

from app.core.db import Store
from app.logging_conf import get_logger
from app.waha.client import WahaClient

log = get_logger("transport")


class WahaTransport:
    """Salida del juego fijada a un grupo, con registro de lo enviado.

    Los fallos de envío se registran pero no se propagan: si un privado no
    llega, la partida sigue y ese jugador simplemente no actúa esa noche.
    """

    def __init__(
        self,
        client: WahaClient,
        group_id: str,
        *,
        store: Store | None = None,
        session_id: str | None = None,
    ) -> None:
        self._client = client
        self._group_id = group_id
        self._store = store
        self._session_id = session_id

    async def send_group(self, text: str) -> None:
        await self._send(self._group_id, text)

    async def send_direct(self, jid: str, text: str) -> None:
        await self._send(jid, text)

    async def _send(self, chat_id: str, text: str) -> None:
        if not text or not text.strip():
            return
        result = await self._client.send_text(chat_id, text)
        if not result.ok:
            log.warning("transport.send_failed", chat_id=chat_id, error=result.error)
            return
        if self._store is not None:
            await self._store.log_outbound(
                chat_id,
                text,
                message_id=result.message_id,
                game_session_id=self._session_id,
            )

    async def send_poll(self, question: str, options: list[str]) -> bool:
        result = await self._client.send_poll(self._group_id, question, options)
        if not result.ok:
            log.warning("transport.poll_failed", error=result.error)
            return False
        if self._store is not None:
            await self._store.log_outbound(
                self._group_id,
                f"[encuesta] {question} :: {' | '.join(options)}",
                message_id=result.message_id,
                game_session_id=self._session_id,
            )
        return True

    async def set_group_locked(self, locked: bool) -> bool:
        return await self._client.set_admins_only(self._group_id, locked)
