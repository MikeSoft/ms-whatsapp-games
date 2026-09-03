"""Reclutamiento de jugadores a partir de los mensajes del grupo.

El manager abre la convocatoria y la gente responde en lenguaje natural:
"yo", "yo juego", "va", "me apunto", "yo no", "paso", "yo la próxima"... El
LLM interpreta esa mezcla, pero con dos redes de seguridad:

* un "yo" inequívoco entra aunque el modelo lo pase por alto;
* un "no" inequívoco queda fuera aunque el modelo lo incluya.

Así el modelo aporta criterio en los casos raros sin poder equivocarse en los
casos obvios.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from app.core.llm import LLMClient
from app.games.werewolf.parsing import looks_like_join, normalise
from app.logging_conf import get_logger
from app.waha.models import InboundMessage

log = get_logger("recruit")

_SYSTEM = """\
Eres el asistente de un moderador de juegos por WhatsApp. Se ha abierto una
convocatoria y la gente responde en lenguaje coloquial. Tu única tarea es
decidir quién quiere jugar de verdad.

Entra quien confirme participar ("yo", "yo juego", "va", "me apunto", "dale",
"listo", "🙋"). NO entra quien bromee, pregunte por las reglas, salude,
comente sin comprometerse, o se excluya ("yo no", "paso", "la próxima",
"ahora no puedo").

Responde SÓLO con este JSON, sin texto alrededor:
{"jugadores": [<números de los participantes que SÍ entran>]}"""


@dataclass(frozen=True)
class Joiner:
    jid: str
    name: str


_NEGATIVE_HINTS = (
    "yo no",
    "no juego",
    "no puedo",
    "no entro",
    "no voy",
    "paso",
    "abstengo",
    "la proxima",
    "proxima vez",
    "otra vez",
    "manana",
    "luego",
    "ahorita no",
    "ahora no",
)


def _display_name(messages: list[InboundMessage]) -> str:
    """Nombre con el que se muestra a alguien en el grupo.

    Sin nombre público de WhatsApp queda el número, que es lo único que hay
    para distinguirlo dentro de la partida.
    """
    for message in reversed(messages):
        if message.sender_name and message.sender_name.strip():
            return message.sender_name.strip()
    # Sin nombre público queda el número. Un JID raro (por ejemplo "@lid") no
    # deja nada usable, y un nombre vacío rompería las listas de la partida.
    numero = messages[0].sender_id.split("@", 1)[0].strip()
    return numero or "Jugador anónimo"


def _prompt_label(index: int, messages: list[InboundMessage]) -> str:
    """Etiqueta que se le manda al LLM, sin datos de contacto.

    El modelo elige por número de lista, así que no necesita el teléfono de
    nadie: cuando no hay nombre público se manda una etiqueta genérica en vez
    del número.
    """
    for message in reversed(messages):
        if message.sender_name and message.sender_name.strip():
            return message.sender_name.strip()
    return f"Participante {index}"


def _group_by_sender(messages: list[InboundMessage]) -> dict[str, list[InboundMessage]]:
    grouped: dict[str, list[InboundMessage]] = {}
    for message in messages:
        if message.from_me or not message.text.strip():
            continue
        grouped.setdefault(message.sender_id, []).append(message)
    return grouped


def _is_hard_no(texts: list[str]) -> bool:
    """¿El remitente se excluyó explícitamente y nunca dijo que sí?"""
    said_yes = any(looks_like_join(text) for text in texts)
    if said_yes:
        return False
    joined = " ".join(normalise(text) for text in texts)
    return any(hint in joined for hint in _NEGATIVE_HINTS)


async def select_players(
    messages: list[InboundMessage],
    *,
    llm: LLMClient,
    max_players: int = 30,
) -> list[Joiner]:
    """Devuelve los jugadores inscritos, en orden de llegada."""
    grouped = _group_by_sender(messages)
    if not grouped:
        return []

    # Orden estable: por el primer mensaje de cada persona.
    senders = sorted(grouped, key=lambda jid: grouped[jid][0].timestamp)

    regex_yes = {
        jid for jid in senders if any(looks_like_join(m.text) for m in grouped[jid])
    }
    hard_no = {
        jid for jid in senders if _is_hard_no([m.text for m in grouped[jid]])
    }

    llm_yes = await _ask_llm(senders, grouped, llm=llm)
    if llm_yes is None:
        log.info("recruit.deterministic_only", candidates=len(senders))
        chosen = regex_yes
    else:
        chosen = llm_yes | regex_yes

    final = [jid for jid in senders if jid in chosen and jid not in hard_no]
    if len(final) > max_players:
        log.info("recruit.capped", total=len(final), max_players=max_players)
        final = final[:max_players]

    return [Joiner(jid=jid, name=_display_name(grouped[jid])) for jid in final]


async def _ask_llm(
    senders: list[str],
    grouped: dict[str, list[InboundMessage]],
    *,
    llm: LLMClient,
) -> set[str] | None:
    """Pide al modelo la lista de participantes. ``None`` si no se pudo usar."""
    if not llm.available:
        return None

    lines = []
    for index, jid in enumerate(senders, start=1):
        said = " | ".join(m.text.strip() for m in grouped[jid] if m.text.strip())
        lines.append(f"{index}. {_prompt_label(index, grouped[jid])} dijo: {said}")

    user = (
        "Participantes posibles y lo que escribieron:\n"
        + "\n".join(lines)
        + f"\n\nDevuelve el JSON con los números (1 a {len(senders)}) de quienes SÍ entran."
    )

    data = await llm.complete_json(_SYSTEM, user)
    if not data:
        return None

    raw = data.get("jugadores")
    if raw is None:
        raw = data.get("items") or data.get("players")
    if not isinstance(raw, list):
        log.warning("recruit.llm_bad_shape", payload=json.dumps(data)[:200])
        return None

    selected: set[str] = set()
    for entry in raw:
        index = _coerce_index(entry)
        if index is not None and 1 <= index <= len(senders):
            selected.add(senders[index - 1])
    return selected


def _coerce_index(entry: object) -> int | None:
    """El modelo puede devolver ``3``, ``"3"`` o ``{"id": 3}``."""
    if isinstance(entry, bool):
        return None
    if isinstance(entry, int):
        return entry
    if isinstance(entry, str):
        digits = "".join(ch for ch in entry if ch.isdigit())
        return int(digits) if digits else None
    if isinstance(entry, dict):
        for key in ("id", "numero", "número", "number", "index"):
            if key in entry:
                return _coerce_index(entry[key])
    return None
