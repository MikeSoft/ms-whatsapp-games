"""Configuración centralizada del microservicio.

Todos los valores se leen de variables de entorno (o de un fichero .env) para
que el despliegue en Docker no requiera tocar el código.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def normalise_jid(value: str) -> str:
    """Normaliza un número o JID de WhatsApp a la forma ``<digits>@c.us``.

    Acepta ``+57 300 123 4567``, ``573001234567`` o ``573001234567@c.us``.
    Los JID de grupo (``...@g.us``) se devuelven intactos.
    """
    raw = (value or "").strip()
    if not raw:
        return ""
    if raw.endswith("@g.us") or raw.endswith("@newsletter"):
        return raw
    if "@" in raw:
        local, _, domain = raw.partition("@")
        digits = "".join(ch for ch in local if ch.isdigit())
        return f"{digits}@{domain}"
    digits = "".join(ch for ch in raw if ch.isdigit())
    return f"{digits}@c.us" if digits else ""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ------------------------------------------------------------------ app
    app_name: str = "ms-whatsapp-games"
    environment: Literal["local", "dev", "prod"] = "local"
    log_level: str = "INFO"
    log_json: bool = False

    # ----------------------------------------------------------------- waha
    waha_base_url: str = "http://waha:3000"
    waha_api_key: str | None = None
    waha_session: str = "default"
    waha_timeout_seconds: float = 20.0
    waha_max_retries: int = 3
    # Intervalo mínimo entre envíos: WhatsApp penaliza las ráfagas.
    waha_min_send_interval: float = 0.4
    # WAHA puede firmar el webhook con HMAC; si se define, se valida la firma.
    waha_webhook_hmac_secret: str | None = None
    # Cabecera de secreto compartido como alternativa simple al HMAC.
    webhook_shared_secret: str | None = None
    # Deshabilita el envío real (útil en desarrollo: sólo escribe en el log).
    waha_dry_run: bool = False

    # -------------------------------------------------------------- control
    # Único número autorizado a lanzar y administrar partidas.
    manager_number: str = ""
    # Grupo donde se juega. Si se deja vacío se usa el grupo desde el que el
    # manager envía el comando.
    game_group_id: str | None = None
    command_prefix: str = "!"
    # Silenciar el grupo (sólo administradores) requiere WAHA Plus.
    manage_group_permissions: bool = True

    # ---------------------------------------------------------------- redis
    redis_url: str = "redis://redis:6379/0"
    # TTL de los buzones efímeros de mensajes (segundos).
    inbox_ttl_seconds: int = 900
    # Borrar el buzón de Redis al terminar la partida.
    purge_inbox_on_finish: bool = True

    # ------------------------------------------------------------- database
    database_url: str = "sqlite+aiosqlite:///./data/games.db"
    # Checkpointer de LangGraph: sqlite (persistente) o memory.
    checkpointer: Literal["sqlite", "memory"] = "sqlite"
    checkpointer_path: str = "./data/checkpoints.db"
    message_retention_days: int = 30

    # ------------------------------------------------------------------ llm
    llm_provider: Literal["deepseek", "openai", "none"] = "deepseek"
    llm_model: str = "deepseek-chat"
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str | None = None
    llm_temperature: float = 0.9
    llm_max_tokens: int = 700
    llm_timeout_seconds: float = 30.0
    llm_max_retries: int = 2

    # --------------------------------------------------------------- timers
    recruit_seconds: int = 30
    night_action_seconds: int = 60
    # Ventana de la bruja: se abre después de la de los lobos, porque necesita
    # saber a quién atacaron.
    witch_action_seconds: int = 45
    hunter_action_seconds: int = 40
    debate_seconds: int = 180
    vote_seconds: int = 30
    # Segundos entre mensajes de ambientación mientras se espera.
    filler_interval_seconds: int = 25
    # Tope de rondas para que una partida abandonada no viva para siempre.
    max_rounds: int = 20
    graph_recursion_limit: int = 250

    # -------------------------------------------------------------- werewolf
    werewolf_min_players: int = 4
    werewolf_max_players: int = 24
    # Qué hacer cuando hay empate en la votación del día.
    werewolf_tie_break: Literal["none", "random"] = "none"
    # Revelar el rol de quien muere de noche (el del linchado siempre se revela).
    werewolf_reveal_role_on_death: bool = True

    @field_validator("manager_number", mode="after")
    @classmethod
    def _normalise_manager(cls, value: str) -> str:
        return normalise_jid(value)

    @field_validator("game_group_id", mode="after")
    @classmethod
    def _normalise_group(cls, value: str | None) -> str | None:
        if not value:
            return None
        raw = value.strip()
        return raw if "@" in raw else f"{raw}@g.us"

    @field_validator("waha_base_url", "llm_base_url", mode="after")
    @classmethod
    def _strip_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @property
    def manager_jid(self) -> str:
        return self.manager_number

    @property
    def llm_enabled(self) -> bool:
        return self.llm_provider != "none" and bool(self.llm_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
