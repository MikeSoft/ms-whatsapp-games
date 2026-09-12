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


def _local_part(value: Any) -> str:
    """Los dígitos de un JID, sin dominio ni sufijo de dispositivo.

    Acepta la cadena o la forma ``{"_serialized": ...}`` que devuelve WEBJS.
    """
    if isinstance(value, dict):
        value = value.get("_serialized") or value.get("user")
    if not isinstance(value, str):
        return ""
    local = value.split("@", 1)[0].split(":", 1)[0]
    return "".join(ch for ch in local if ch.isdigit())


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
        #: Nombres ya resueltos. Un nombre no cambia a media partida y a la
        #: misma gente se le pregunta en cada ronda.
        self._contact_names: dict[str, str | None] = {}
        #: Las formas con las que esta sesión se identifica.
        self._own_jids: frozenset[str] | None = None
        #: Grupos en los que ya se comprobó si es administradora.
        self._admin_in: dict[str, bool] = {}

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
        params: dict[str, Any] | None = None,
        attempts: int | None = None,
    ) -> Any:
        """Ejecuta una petición con reintentos exponenciales.

        Devuelve el cuerpo decodificado (``None`` si la respuesta vino vacía)
        y lanza :class:`WahaError` cuando la petición no se puede completar.
        """
        attempts = max(1, attempts or self._settings.waha_max_retries)
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                response = await self._client.request(
                    method, path, json=json, params=params
                )
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
                base = self._settings.waha_retry_backoff
                if base > 0:
                    delay = min(8.0, base * 2 ** (attempt - 1))
                    await asyncio.sleep(delay + random.uniform(0, 0.3))

        raise WahaError(f"WAHA {method} {path} falló: {last_error}")

    async def _try_request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        attempts: int | None = None,
    ) -> tuple[bool, Any]:
        """Variante tolerante para operaciones que no deben abortar la partida.

        Se usa con endpoints opcionales (por ejemplo los exclusivos de WAHA
        Plus): devuelve ``(False, None)`` en lugar de propagar el error.
        """
        try:
            return True, await self._request(
                method, path, json=json, params=params, attempts=attempts
            )
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
    async def send_text(
        self,
        chat_id: str,
        text: str,
        *,
        reply_to: str | None = None,
        mentions: list[str] | None = None,
    ) -> SentMessage:
        """Envía un mensaje de texto a un chat (grupo o privado).

        ``mentions`` son los JID a etiquetar. WhatsApp sólo los resuelve si el
        texto contiene además su ``@<número>``, de lo que se encarga
        :class:`~app.games.mentions.GroupText`.
        """
        if self._settings.waha_dry_run:
            log.info("waha.dry_run.send_text", chat_id=chat_id, text=text, mentions=mentions)
            return SentMessage(ok=True, chat_id=chat_id, message_id="dry-run")

        payload: dict[str, Any] = {
            "session": self._settings.waha_session,
            "chatId": chat_id,
            "text": text,
        }
        if reply_to:
            payload["reply_to"] = reply_to
        if mentions:
            payload["mentions"] = mentions

        async with self._send_lock:
            await self._throttle()
            try:
                data = await self._request(
                    "POST",
                    "/api/sendText",
                    json=payload,
                    attempts=self._settings.waha_send_max_retries,
                )
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
                data = await self._request(
                    "POST",
                    "/api/sendPoll",
                    json=payload,
                    attempts=self._settings.waha_send_max_retries,
                )
            except WahaError as exc:
                log.error("waha.send_poll_failed", chat_id=chat_id, error=str(exc))
                return SentMessage(ok=False, chat_id=chat_id, error=str(exc))

        message_id = None
        if isinstance(data, dict):
            message_id = data.get("id")
            if isinstance(message_id, dict):
                message_id = message_id.get("_serialized")
        return SentMessage(ok=True, chat_id=chat_id, message_id=message_id)

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

        # Silenciar un grupo es cosa de administradores. Si no lo es, no se
        # intenta: no es un fallo del que haya que avisar, es una capacidad
        # que no está, y lo que depende de ella se puede jugar sin.
        if not await self.is_group_admin(group_id):
            log.info(
                "waha.admins_only_skipped",
                group_id=group_id,
                reason="la sesión no es administradora del grupo",
            )
            return False

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

    async def own_jids(self) -> frozenset[str]:
        """Las partes locales con las que se identifica la propia sesión.

        Un mismo número se direcciona como ``@c.us`` o como ``@lid`` según la
        antigüedad del grupo, y la sesión conoce las dos. Se guardan sin
        dominio para poder compararlas con lo que traiga cada grupo.
        """
        if self._own_jids is not None:
            return self._own_jids

        datos = await self.session_status()
        me = datos.get("me") if isinstance(datos, dict) else None
        forms = set()
        if isinstance(me, dict):
            for clave in ("id", "lid"):
                local = _local_part(me.get(clave))
                if local:
                    forms.add(local)
        # Sin respuesta de WAHA no se cachea: puede ser un fallo pasajero y
        # cachear un conjunto vacío dejaría el bot sin identidad toda la vida
        # del proceso.
        if forms:
            self._own_jids = frozenset(forms)
            return self._own_jids
        return frozenset()

    async def is_group_admin(self, group_id: str) -> bool:
        """Si esta sesión es administradora del grupo.

        Se consulta para no intentar operaciones que van a fallar. Ante la
        duda —WAHA no contesta, el grupo no trae participantes— se responde
        ``False``: lo que depende de esto es prescindible, y es mejor no
        hacerlo que llenar el log de errores.
        """
        if group_id in self._admin_in:
            return self._admin_in[group_id]

        propias = await self.own_jids()
        if not propias:
            return False

        session = self._settings.waha_session
        # Un solo intento: es una comprobación de capacidad, no una operación
        # de la partida. Insistir contra un grupo que no responde sólo gasta
        # segundos para acabar en el mismo "no" con el que se degrada igual.
        ok, data = await self._try_request(
            "GET", f"/api/{session}/groups/{group_id}", attempts=1
        )
        if not ok or not isinstance(data, dict):
            return False

        metadata = data.get("groupMetadata")
        if not isinstance(metadata, dict):
            metadata = data
        participantes = metadata.get("participants")
        if not isinstance(participantes, list):
            return False

        for participante in participantes:
            if not isinstance(participante, dict):
                continue
            if _local_part(participante.get("id")) not in propias:
                continue
            es_admin = bool(
                participante.get("isAdmin") or participante.get("isSuperAdmin")
            )
            self._admin_in[group_id] = es_admin
            return es_admin

        # No figura entre los participantes: no está en el grupo.
        self._admin_in[group_id] = False
        return False

    async def contact_name(self, jid: str) -> str | None:
        """Nombre con el que mostrar a alguien, o ``None`` si no se sabe.

        Hace falta porque hay eventos que no traen nombre: un ``poll.vote``
        llega con el identificador del votante y nada más, y en los grupos
        nuevos ese identificador es un ``@lid``, que mostrado en crudo no
        permite reconocer a nadie.

        Se cachea en memoria: dentro de una partida se pregunta por la misma
        gente en cada ronda y el nombre no cambia a media partida.
        """
        if not jid:
            return None
        if jid in self._contact_names:
            return self._contact_names[jid]

        ok, data = await self._try_request(
            "GET",
            "/api/contacts",
            params={"contactId": jid, "session": self._settings.waha_session},
        )
        nombre: str | None = None
        if ok and isinstance(data, dict):
            for key in ("pushname", "name", "shortName"):
                valor = data.get(key)
                if isinstance(valor, str) and valor.strip():
                    nombre = valor.strip()
                    break
        self._contact_names[jid] = nombre
        return nombre

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        """Borra un mensaje ya enviado ("eliminar para todos").

        Se usa para retirar una encuesta cuando se acaba su tiempo, de modo
        que no quede votable después. Es cosmético: si falla, se registra y
        el juego sigue, porque los votos que cuentan ya se recogieron dentro
        de la ventana.
        """
        if self._settings.waha_dry_run:
            log.info("waha.dry_run.delete", chat_id=chat_id, message_id=message_id)
            return True

        session = self._settings.waha_session
        path = f"/api/{session}/chats/{chat_id}/messages/{message_id}"
        ok, _ = await self._try_request("DELETE", path)
        if not ok:
            log.warning("waha.delete_failed", chat_id=chat_id, message_id=message_id)
        return ok

    # ----------------------------------------------------------------- salud
    async def session_status(self) -> dict[str, Any]:
        """Estado de la sesión de WhatsApp en WAHA."""
        _, data = await self._try_request(
            "GET", f"/api/sessions/{self._settings.waha_session}"
        )
        if isinstance(data, dict):
            return data
        return {"status": "UNKNOWN"}
