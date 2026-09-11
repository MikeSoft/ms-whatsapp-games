"""Modelos de los eventos entrantes de WAHA y su forma normalizada."""

from __future__ import annotations

import time
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class WahaEvent(BaseModel):
    """Envoltorio genérico de un webhook de WAHA.

    WAHA cambia la forma de ``payload`` según el evento, así que se acepta
    como diccionario libre y se normaliza aparte.
    """

    model_config = ConfigDict(extra="allow")

    event: str = ""
    session: str | None = None
    me: dict[str, Any] | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    engine: str | None = None
    environment: dict[str, Any] | None = None


class Scope(StrEnum):
    """Origen de un mensaje: el grupo de la partida o un privado."""

    GROUP = "group"
    DIRECT = "direct"


class InboundMessage(BaseModel):
    """Mensaje entrante ya normalizado, independiente de la versión de WAHA."""

    message_id: str
    chat_id: str
    sender_id: str
    sender_name: str | None = None
    text: str = ""
    scope: Scope
    from_me: bool = False
    timestamp: float = Field(default_factory=time.time)
    kind: Literal["text", "poll_vote"] = "text"
    # Opciones elegidas cuando ``kind == "poll_vote"``.
    poll_options: list[str] = Field(default_factory=list)
    #: Identificador de la encuesta votada, si WAHA lo trae. Sin él, un juego
    #: que encadena encuestas no puede distinguir el voto que llega tarde a la
    #: anterior del que corresponde a la que está abierta.
    poll_id: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def dedupe_key(self) -> str:
        """Clave para descartar reenvíos del webhook sin perder correcciones.

        El identificador de un voto de encuesta no identifica el voto: WhatsApp
        lo compone con la encuesta y el votante
        (``..._3EB0B34E7D16087FA8D743_265914461237419@lid``) y lo reutiliza
        cuando esa misma persona cambia su respuesta. Deduplicar sólo por él
        tira la corrección creyendo que es un reenvío, que es justo lo que
        pasaba: quien rectificaba se quedaba con su primera respuesta.

        Para los votos entra también la selección. Un reenvío trae la misma y
        se sigue descartando; un cambio trae otra y pasa. Queda fuera el caso
        de volver a una opción ya elegida antes en la misma pregunta: se ve
        idéntico a un reenvío y no hay en el evento nada que los separe.
        """
        if self.kind == "poll_vote":
            return f"{self.message_id}|{'|'.join(sorted(self.poll_options))}"
        return self.message_id

    @property
    def display_name(self) -> str:
        return self.sender_name or self.sender_id.split("@", 1)[0]

    def to_log(self) -> dict[str, Any]:
        """Proyección compacta para logs y persistencia."""
        return {
            "message_id": self.message_id,
            "chat_id": self.chat_id,
            "sender_id": self.sender_id,
            "sender_name": self.sender_name,
            "text": self.text,
            "scope": str(self.scope),
            "kind": self.kind,
            "timestamp": self.timestamp,
        }


class SentMessage(BaseModel):
    """Resultado del envío de un mensaje a través de WAHA."""

    ok: bool
    message_id: str | None = None
    chat_id: str = ""
    error: str | None = None
