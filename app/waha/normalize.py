"""Normalización de los webhooks de WAHA a :class:`InboundMessage`.

WAHA ha cambiado la forma de sus payloads entre versiones y entre motores
(WEBJS / NOWEB / GOWS), así que aquí se leen varias claves alternativas en
lugar de asumir una sola. Si aparece una versión con otra forma, este es el
único fichero que hay que tocar.
"""

from __future__ import annotations

import time
from typing import Any

from app.waha.models import InboundMessage, Scope, WahaEvent

#: Eventos de WAHA que transportan un mensaje de texto.
TEXT_EVENTS = {"message", "message.any", "message.text"}
#: Eventos de WAHA que transportan un voto de encuesta.
POLL_EVENTS = {"poll.vote", "poll.vote.failed", "message.poll.vote"}

SUPPORTED_EVENTS = TEXT_EVENTS | POLL_EVENTS

_NAME_KEYS = (
    "notifyName",
    "pushName",
    "senderName",
    "verifiedBizName",
    "chatName",
)


def _first_str(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _dig(data: Any, *path: str) -> Any:
    current = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def is_group(chat_id: str) -> bool:
    return chat_id.endswith("@g.us")


def scope_for(chat_id: str) -> Scope:
    return Scope.GROUP if is_group(chat_id) else Scope.DIRECT


def _extract_name(payload: dict[str, Any]) -> str | None:
    raw = payload.get("_data")
    data: dict[str, Any] = raw if isinstance(raw, dict) else {}

    candidates: list[Any] = []
    for key in _NAME_KEYS:
        candidates.append(payload.get(key))
        candidates.append(data.get(key))
    candidates.append(_dig(payload, "contact", "pushname"))

    return _first_str(*candidates) or None


def _extract_text(payload: dict[str, Any]) -> str:
    return _first_str(
        payload.get("body"),
        payload.get("text"),
        payload.get("caption"),
        _dig(payload, "_data", "body"),
        _dig(payload, "message", "conversation"),
        _dig(payload, "message", "extendedTextMessage", "text"),
    )


def _normalise_text_event(event: WahaEvent) -> InboundMessage | None:
    payload = event.payload
    chat_id = _first_str(payload.get("from"), payload.get("chatId"), payload.get("to"))
    if not chat_id:
        return None

    from_me = bool(payload.get("fromMe"))
    if is_group(chat_id):
        sender_id = _first_str(
            payload.get("participant"),
            _dig(payload, "_data", "participant"),
            _dig(payload, "_data", "author"),
            payload.get("author"),
        )
        # En mensajes propios enviados al grupo WAHA no siempre incluye el
        # participante; se marca como el propio bot.
        if not sender_id and from_me:
            sender_id = _first_str(payload.get("to")) or chat_id
    else:
        sender_id = _first_str(payload.get("to")) if from_me else chat_id

    if not sender_id:
        return None

    message_id = _first_str(
        payload.get("id"),
        _dig(payload, "_data", "id", "_serialized"),
    ) or f"synthetic-{time.time_ns()}"

    return InboundMessage(
        message_id=message_id,
        chat_id=chat_id,
        sender_id=sender_id,
        sender_name=_extract_name(payload),
        text=_extract_text(payload),
        scope=scope_for(chat_id),
        from_me=from_me,
        timestamp=_coerce_timestamp(payload.get("timestamp")),
        kind="text",
        raw=payload,
    )


def _normalise_poll_event(event: WahaEvent) -> InboundMessage | None:
    payload = event.payload
    vote = payload.get("vote") if isinstance(payload.get("vote"), dict) else payload
    poll = payload.get("poll") if isinstance(payload.get("poll"), dict) else {}

    voter = _first_str(vote.get("from"), vote.get("participant"), vote.get("author"))
    if not voter:
        return None

    # El chat de la encuesta: el grupo si la encuesta se publicó en un grupo.
    chat_candidates = [
        poll.get("to"),
        poll.get("from"),
        vote.get("to"),
        vote.get("chatId"),
    ]
    chat_id = _first_str(*[c for c in chat_candidates if isinstance(c, str) and is_group(c)])
    if not chat_id:
        chat_id = _first_str(*chat_candidates) or voter

    raw_options = (
        vote.get("selectedOptions")
        or vote.get("selectedOptionsLocalIds")
        or vote.get("options")
        or []
    )
    options: list[str] = []
    for option in raw_options if isinstance(raw_options, list) else []:
        if isinstance(option, str):
            options.append(option.strip())
        elif isinstance(option, dict):
            label = _first_str(option.get("name"), option.get("localId"), option.get("text"))
            if label:
                options.append(label)
        elif option is not None:
            options.append(str(option))

    message_id = _first_str(vote.get("id"), poll.get("id")) or f"vote-{time.time_ns()}"

    return InboundMessage(
        message_id=message_id,
        chat_id=chat_id,
        sender_id=voter,
        sender_name=_extract_name(vote) or _extract_name(payload),
        text=", ".join(options),
        scope=scope_for(chat_id),
        from_me=bool(vote.get("fromMe")),
        timestamp=_coerce_timestamp(vote.get("timestamp")),
        kind="poll_vote",
        poll_options=options,
        raw=payload,
    )


def _coerce_timestamp(value: Any) -> float:
    if isinstance(value, (int, float)) and value > 0:
        # WAHA envía segundos; algunos motores envían milisegundos.
        return float(value) / 1000.0 if value > 1e11 else float(value)
    return time.time()


def normalise(event: WahaEvent) -> InboundMessage | None:
    """Convierte un evento de WAHA en un mensaje normalizado.

    Devuelve ``None`` para eventos que no interesan al orquestador (acks,
    presencia, cambios de estado de sesión, etc.).
    """
    name = (event.event or "").strip().lower()
    if name in TEXT_EVENTS:
        return _normalise_text_event(event)
    if name in POLL_EVENTS:
        return _normalise_poll_event(event)
    return None
