"""Cliente HTTP asíncrono para la API de WAHA.

Las rutas de WAHA varían entre versiones y algunas (como silenciar un grupo)
sólo existen en WAHA Plus. Por eso todas las llamadas se concentran aquí y las
operaciones opcionales degradan con un warning en vez de romper la partida.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any

import httpx

from app.config import Settings
from app.logging_conf import get_logger
from app.waha.models import SentMessage

log = get_logger("waha")

#: Códigos que merecen un reintento (el resto son errores del cliente).
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class WahaError(RuntimeError):
    """Error no recuperable devuelto por WAHA."""


class WahaClient:
    """Envoltorio fino sobre la API REST de WAHA.

    Serializa los envíos con un intervalo mínimo entre mensajes: WhatsApp
    penaliza las ráfagas y el juego manda varios mensajes seguidos.
    """

    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=settings.waha_base_url,
            timeout=settings.waha_timeout_seconds,
            headers=self._default_headers(settings),
        )
        self._send_lock = asyncio.Lock()
        self._last_send_at = 0.0

    def _default_headers(self, settings: Settings) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if settings.waha_api_key:
            headers["X-Api-Key"] = settings.waha_api_key
        return headers

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------------ core
    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
    ) -> Any:
        """Ejecuta una petición con reintentos exponenciales.

        Devuelve el cuerpo decodificado (``None`` si la respuesta vino vacía)
        y lanza :class:`WahaError` cuando la petición no se puede completar.
        """
        attempts = max(1, self._settings.waha_max_retries)
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                response = await self._client.request(method, path, json=json)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_error = exc
                log.warning("waha.transport_error", path=path, attempt=attempt, error=str(exc))
            else:
                if response.status_code < 400:
                    if not response.content:
                        return None
                    try:
                        return response.json()
                    except ValueError:
                        return response.text
                body = response.text[:400]
                if response.status_code in RETRYABLE_STATUS and attempt < attempts:
                    last_error = WahaError(f"{response.status_code}: {body}")
                    log.warning(
                        "waha.retryable_status",
                        path=path,
                        status=response.status_code,
                        attempt=attempt,
                    )
                else:
                    raise WahaError(
                        f"WAHA {method} {path} -> {response.status_code}: {body}"
                    )

            if attempt < attempts:
                backoff = min(8.0, 0.5 * 2 ** (attempt - 1)) + random.uniform(0, 0.3)
                await asyncio.sleep(backoff)

        raise WahaError(f"WAHA {method} {path} falló: {last_error}")

    async def _try_request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
    ) -> tuple[bool, Any]:
        """Variante tolerante para operaciones que no deben abortar la partida.

        Se usa con endpoints opcionales (por ejemplo los exclusivos de WAHA
        Plus): devuelve ``(False, None)`` en lugar de propagar el error.
        """
        try:
            return True, await self._request(method, path, json=json)
        except WahaError as exc:
            log.warning("waha.optional_failed", path=path, error=str(exc))
            return False, None

    async def _throttle(self) -> None:
        interval = self._settings.waha_min_send_interval
        if interval <= 0:
            return
        loop = asyncio.get_running_loop()
        elapsed = loop.time() - self._last_send_at
        if elapsed < interval:
            await asyncio.sleep(interval - elapsed)
        self._last_send_at = loop.time()

    # ----------------------------------------------------------------- envío
    async def send_text(self, chat_id: str, text: str, *, reply_to: str | None = None) -> SentMessage:
        """Envía un mensaje de texto a un chat (grupo o privado)."""
        if self._settings.waha_dry_run:
            log.info("waha.dry_run.send_text", chat_id=chat_id, text=text)
            return SentMessage(ok=True, chat_id=chat_id, message_id="dry-run")

        payload: dict[str, Any] = {
            "session": self._settings.waha_session,
            "chatId": chat_id,
            "text": text,
        }
        if reply_to:
            payload["reply_to"] = reply_to

        async with self._send_lock:
            await self._throttle()
            try:
                data = await self._request("POST", "/api/sendText", json=payload)
            except WahaError as exc:
                log.error("waha.send_text_failed", chat_id=chat_id, error=str(exc))
                return SentMessage(ok=False, chat_id=chat_id, error=str(exc))

        message_id = None
        if isinstance(data, dict):
            message_id = data.get("id") or (data.get("_data") or {}).get("id")
            if isinstance(message_id, dict):
                message_id = message_id.get("_serialized")
        return SentMessage(ok=True, chat_id=chat_id, message_id=message_id)

    async def send_poll(
        self,
        chat_id: str,
        question: str,
        options: list[str],
        *,
        multiple_answers: bool = False,
    ) -> SentMessage:
        """Publica una encuesta. WhatsApp exige entre 2 y 12 opciones."""
        if len(options) < 2:
            return SentMessage(ok=False, chat_id=chat_id, error="una encuesta necesita 2 opciones")

        if self._settings.waha_dry_run:
            log.info("waha.dry_run.send_poll", chat_id=chat_id, question=question, options=options)
            return SentMessage(ok=True, chat_id=chat_id, message_id="dry-run")

        payload = {
            "session": self._settings.waha_session,
            "chatId": chat_id,
            "poll": {
                "name": question[:255],
                "options": options[:12],
                "multipleAnswers": multiple_answers,
            },
        }
        async with self._send_lock:
            await self._throttle()
            try:
                data = await self._request("POST", "/api/sendPoll", json=payload)
            except WahaError as exc:
                log.error("waha.send_poll_failed", chat_id=chat_id, error=str(exc))
                return SentMessage(ok=False, chat_id=chat_id, error=str(exc))

        message_id = None
        if isinstance(data, dict):
            message_id = data.get("id")
            if isinstance(message_id, dict):
                message_id = message_id.get("_serialized")
        return SentMessage(ok=True, chat_id=chat_id, message_id=message_id)

    # ------------------------------------------------------------- presencia
    async def start_typing(self, chat_id: str) -> None:
        if self._settings.waha_dry_run:
            return
        await self._try_request(
            "POST",
            "/api/startTyping",
            json={"session": self._settings.waha_session, "chatId": chat_id},
        )

    async def stop_typing(self, chat_id: str) -> None:
        if self._settings.waha_dry_run:
            return
        await self._try_request(
            "POST",
            "/api/stopTyping",
            json={"session": self._settings.waha_session, "chatId": chat_id},
        )

    # ---------------------------------------------------------------- grupos
    async def set_admins_only(self, group_id: str, admins_only: bool) -> bool:
        """Restringe el envío de mensajes del grupo a los administradores.

        Es la mecánica de "todos duermen". Requiere WAHA Plus; si el endpoint
        no está disponible se registra un warning y la partida continúa (el
        silencio pasa a ser una convención social en vez de técnica).
        """
        if not self._settings.manage_group_permissions:
            return False
        if self._settings.waha_dry_run:
            log.info("waha.dry_run.admins_only", group_id=group_id, admins_only=admins_only)
            return True

        session = self._settings.waha_session
        path = f"/api/{session}/groups/{group_id}/settings/security/messages-admin-only"
        ok, _ = await self._try_request("PUT", path, json={"adminsOnly": admins_only})
        if not ok:
            log.warning(
                "waha.admins_only_unavailable",
                group_id=group_id,
                hint="requiere WAHA Plus y que el bot sea admin del grupo",
            )
        return ok

    async def get_participants(self, group_id: str) -> list[dict[str, Any]]:
        """Devuelve los participantes del grupo (para resolver nombres)."""
        session = self._settings.waha_session
        _, data = await self._try_request(
            "GET", f"/api/{session}/groups/{group_id}/participants"
        )
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict) and isinstance(data.get("participants"), list):
            return [item for item in data["participants"] if isinstance(item, dict)]
        return []

    async def get_contact_name(self, chat_id: str) -> str | None:
        """Intenta resolver el nombre público de un contacto."""
        _, data = await self._try_request(
            "GET",
            f"/api/contacts?contactId={chat_id}&session={self._settings.waha_session}",
        )
        if isinstance(data, dict):
            for key in ("pushname", "name", "shortName", "verifiedName"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return None

    # ----------------------------------------------------------------- salud
    async def session_status(self) -> dict[str, Any]:
        """Estado de la sesión de WhatsApp en WAHA."""
        _, data = await self._try_request(
            "GET", f"/api/sessions/{self._settings.waha_session}"
        )
        if isinstance(data, dict):
            return data
        return {"status": "UNKNOWN"}
