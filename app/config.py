"""Configuración centralizada del microservicio.

Todos los valores se leen de variables de entorno (o de un fichero .env) para
que el despliegue en Docker no requiera tocar el código.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
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
    environment: Literal["local", "dev", "prod"] = "local"
    log_level: str = "INFO"
    log_json: bool = False

    # ----------------------------------------------------------------- waha
    waha_base_url: str = "http://waha:3000"
    waha_api_key: str | None = None
    waha_session: str = "default"
    waha_timeout_seconds: float = Field(default=15.0, gt=0)
    waha_max_retries: int = Field(default=3, ge=1)
    # Los envíos reintentan menos que las consultas: un privado perdido sólo
    # significa que ese jugador no actúa, mientras que insistir bloquea el
    # nodo (los mensajes se serializan para no disparar el rate limit).
    waha_send_max_retries: int = Field(default=2, ge=1)
    # Intervalo mínimo entre envíos: WhatsApp penaliza las ráfagas.
    waha_min_send_interval: float = Field(default=0.4, ge=0)
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
    # Etiquetar a los contactos en los mensajes del grupo (@número). Deja
    # inequívoco de quién se habla y a quién mataron. Con false se escriben
    # nombres planos, para motores de WAHA que no resuelvan menciones.
    use_mentions: bool = True

    # ---------------------------------------------------------------- redis
    redis_url: str = "redis://redis:6379/0"
    # Tope de conexiones simultáneas a Redis. El pool por defecto de redis-py
    # lanza "Too many connections" al agotarse; aquí se usa uno que espera,
    # porque perder un voto por una ráfaga del webhook es inaceptable.
    redis_max_connections: int = Field(default=32, ge=1)
    # Cuánto espera un comando por una conexión libre antes de rendirse.
    redis_pool_timeout: float = Field(default=10.0, gt=0)
    # TTL de los buzones efímeros de mensajes (segundos).
    inbox_ttl_seconds: int = Field(default=900, ge=60)
    # Borrar el buzón de Redis al terminar la partida.
    purge_inbox_on_finish: bool = True

    # ------------------------------------------------------------- database
    database_url: str = "sqlite+aiosqlite:///./data/games.db"
    # Conexiones a SQLite. Sólo hay un escritor posible, así que un pool
    # pequeño va más rápido y gasta menos hilos que el de serie.
    db_pool_size: int = Field(default=2, ge=1)
    # Segundos que espera una escritura bloqueada antes de rendirse.
    db_busy_timeout: float = Field(default=30.0, gt=0)
    # Checkpointer de LangGraph: sqlite (persistente) o memory.
    checkpointer: Literal["sqlite", "memory"] = "sqlite"
    checkpointer_path: str = "./data/checkpoints.db"
    message_retention_days: int = Field(default=30, ge=0)

    # ------------------------------------------------------------------ llm
    llm_provider: Literal["deepseek", "openai", "none"] = "deepseek"
    llm_model: str = "deepseek-chat"
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str | None = None
    llm_temperature: float = Field(default=0.9, ge=0, le=2)
    llm_max_tokens: int = Field(default=700, ge=64)
    llm_timeout_seconds: float = Field(default=30.0, gt=0)
    llm_max_retries: int = Field(default=2, ge=0)

    # --------------------------------------------------------------- timers
    recruit_seconds: int = Field(default=30, ge=1)
    night_action_seconds: int = Field(default=60, ge=1)
    # Ventana de la bruja: se abre después de la de los lobos, porque necesita
    # saber a quién atacaron.
    witch_action_seconds: int = Field(default=45, ge=1)
    hunter_action_seconds: int = Field(default=40, ge=1)
    debate_seconds: int = Field(default=180, ge=1)
    vote_seconds: int = Field(default=30, ge=1)
    # Segundos entre mensajes de ambientación mientras se espera.
    filler_interval_seconds: int = Field(default=25, ge=0)
    # Tope de rondas para que una partida abandonada no viva para siempre.
    max_rounds: int = Field(default=20, ge=1)
    graph_recursion_limit: int = Field(default=250, ge=25)

    # -------------------------------------------------------------- werewolf
    # El reparto de roles no tiene sentido con menos de tres jugadores.
    werewolf_min_players: int = Field(default=4, ge=3)
    werewolf_max_players: int = Field(default=24, ge=3)
    # Qué hacer cuando hay empate en la votación del día.
    werewolf_tie_break: Literal["none", "random"] = "none"
    # Revelar el rol de quien muere de noche (el del linchado siempre se revela).
    werewolf_reveal_role_on_death: bool = True

    # ------------------------------------------------------------- kahoot
    kahoot_questions: int = Field(default=10, ge=1)
    # Diez segundos: entre el envío, el rate limit de WhatsApp y la vuelta
    # del webhook, con menos no llega a votar quien lea despacio. Se ajusta
    # por comando ("20 segundos").
    kahoot_seconds_per_question: int = Field(default=10, ge=3)
    # Las encuestas de WhatsApp admiten hasta 12 opciones.
    kahoot_options: int = Field(default=5, ge=2, le=12)
    # Topes de lo que puede pedir el máster en el comando.
    kahoot_max_questions: int = Field(default=30, ge=1)
    kahoot_max_seconds: int = Field(default=120, ge=3)
    # Silenciar el grupo mientras se juega. Si al cerrar la primera pregunta
    # no ha votado nadie, se reabre solo: puede que el silencio esté
    # impidiendo votar y es preferible jugar con ruido que no jugar.
    kahoot_lock_group: bool = True

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

    @model_validator(mode="after")
    def _coherencia(self) -> Settings:
        """Comprobaciones que cruzan varios campos.

        Una configuración incoherente tiene que romper el arranque, no salir
        como una partida rara que nadie sabe explicar.
        """
        if self.werewolf_max_players < self.werewolf_min_players:
            raise ValueError(
                "WEREWOLF_MAX_PLAYERS no puede ser menor que WEREWOLF_MIN_PLAYERS"
            )
        return self

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
