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
