"""Persistencia en SQLite (histórico de mensajes y de partidas).

Redis guarda lo efímero (los buzones de la ronda en curso, con TTL). SQLite
guarda lo que interesa conservar: el registro de mensajes y la traza de cada
partida, útil para depurar una ronda rara o rehacer un resumen.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    delete,
    select,
)
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.logging_conf import get_logger
from app.waha.models import InboundMessage

log = get_logger("db")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class MessageRow(Base):
    """Un mensaje entrante o saliente."""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message_id: Mapped[str] = mapped_column(String(200), index=True)
    chat_id: Mapped[str] = mapped_column(String(120), index=True)
    sender_id: Mapped[str] = mapped_column(String(120), index=True)
    sender_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    body: Mapped[str] = mapped_column(Text, default="")
    scope: Mapped[str] = mapped_column(String(16), default="group")
    kind: Mapped[str] = mapped_column(String(24), default="text")
    direction: Mapped[str] = mapped_column(String(8), default="in")
    game_session_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    ts: Mapped[float] = mapped_column(Float, default=time.time)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class GameSessionRow(Base):
    """Una partida. El ``id`` es el ``thread_id`` del grafo de LangGraph."""

    __tablename__ = "game_sessions"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    game_key: Mapped[str] = mapped_column(String(60), index=True)
    group_id: Mapped[str] = mapped_column(String(120), index=True)
    started_by: Mapped[str] = mapped_column(String(120), default="")
    status: Mapped[str] = mapped_column(String(24), default="running", index=True)
    winner: Mapped[str | None] = mapped_column(String(40), nullable=True)
    rounds: Mapped[int] = mapped_column(Integer, default=0)
    players: Mapped[str] = mapped_column(Text, default="[]")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class GameEventRow(Base):
    """Traza de lo que ocurrió dentro de una partida."""

    __tablename__ = "game_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        String(80), ForeignKey("game_sessions.id", ondelete="CASCADE"), index=True
    )
    round_no: Mapped[int] = mapped_column(Integer, default=0)
    phase: Mapped[str] = mapped_column(String(40), default="")
    kind: Mapped[str] = mapped_column(String(60), default="")
    detail: Mapped[str] = mapped_column(Text, default="{}")
    is_secret: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Store:
    """Fachada de acceso a datos.

    Los errores de escritura se registran pero no se propagan: perder una
    línea de histórico no debe tumbar una partida en curso.
    """

    def __init__(self, database_url: str) -> None:
        self._url = database_url
        self._ensure_parent_dir(database_url)
        self._engine: AsyncEngine = create_async_engine(
            database_url, echo=False, future=True
        )
        self._sessionmaker = async_sessionmaker(self._engine, expire_on_commit=False)

    @staticmethod
    def _ensure_parent_dir(database_url: str) -> None:
        """Crea el directorio del fichero .db si hace falta."""
        marker = "sqlite+aiosqlite:///"
        if not database_url.startswith(marker):
            return
        path = database_url[len(marker) :]
        if path and path != ":memory:":
            Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)

    async def init(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        log.info("db.ready", url=self._url)

    async def aclose(self) -> None:
        await self._engine.dispose()

    def session(self) -> AsyncSession:
        return self._sessionmaker()

    # -------------------------------------------------------------- mensajes
    async def log_inbound(
        self, message: InboundMessage, *, game_session_id: str | None = None
    ) -> None:
        row = MessageRow(
            message_id=message.message_id,
            chat_id=message.chat_id,
            sender_id=message.sender_id,
            sender_name=message.sender_name,
            body=message.text,
            scope=str(message.scope),
            kind=message.kind,
            direction="out" if message.from_me else "in",
            game_session_id=game_session_id,
            ts=message.timestamp,
        )
        await self._add(row)

    async def log_outbound(
        self,
        chat_id: str,
        text: str,
        *,
        message_id: str | None = None,
        game_session_id: str | None = None,
    ) -> None:
        row = MessageRow(
            message_id=message_id or f"out-{time.time_ns()}",
            chat_id=chat_id,
            sender_id="bot",
            sender_name=None,
            body=text,
            scope="group" if chat_id.endswith("@g.us") else "direct",
            kind="text",
            direction="out",
            game_session_id=game_session_id,
            ts=time.time(),
        )
        await self._add(row)

    # -------------------------------------------------------------- partidas
    async def create_game_session(
        self,
        session_id: str,
        *,
        game_key: str,
        group_id: str,
        started_by: str,
    ) -> None:
        row = GameSessionRow(
            id=session_id,
            game_key=game_key,
            group_id=group_id,
            started_by=started_by,
            status="running",
        )
        await self._add(row)

    async def finish_game_session(
        self,
        session_id: str,
        *,
        status: str,
        winner: str | None = None,
        rounds: int = 0,
        players: list[dict[str, Any]] | None = None,
        error: str | None = None,
    ) -> None:
        try:
            async with self.session() as session:
                row = await session.get(GameSessionRow, session_id)
                if row is None:
                    return
                row.status = status
                row.winner = winner
                row.rounds = rounds
                row.error = error
                if players is not None:
                    row.players = json.dumps(players, ensure_ascii=False)
                row.finished_at = _utcnow()
                await session.commit()
        except Exception as exc:  # noqa: BLE001
            log.warning("db.finish_session_failed", session_id=session_id, error=str(exc))

    async def log_game_event(
        self,
        session_id: str,
        *,
        round_no: int,
        phase: str,
        kind: str,
        detail: dict[str, Any] | None = None,
        is_secret: bool = False,
    ) -> None:
        row = GameEventRow(
            session_id=session_id,
            round_no=round_no,
            phase=phase,
            kind=kind,
            detail=json.dumps(detail or {}, ensure_ascii=False, default=str),
            is_secret=is_secret,
        )
        await self._add(row)

    async def active_game_session(self, group_id: str) -> GameSessionRow | None:
        async with self.session() as session:
            result = await session.execute(
                select(GameSessionRow)
                .where(
                    GameSessionRow.group_id == group_id,
                    GameSessionRow.status == "running",
                )
                .order_by(GameSessionRow.started_at.desc())
                .limit(1)
            )
            return result.scalar_one_or_none()

    async def recent_game_sessions(self, limit: int = 5) -> list[GameSessionRow]:
        async with self.session() as session:
            result = await session.execute(
                select(GameSessionRow).order_by(GameSessionRow.started_at.desc()).limit(limit)
            )
            return list(result.scalars())

    async def mark_stale_sessions(self) -> int:
        """Cierra partidas que quedaron 'running' tras un reinicio del servicio.

        El proceso que las ejecutaba ya no existe, así que su estado en base
        de datos es mentira: se marcan como interrumpidas al arrancar.
        """
        try:
            async with self.session() as session:
                result = await session.execute(
                    select(GameSessionRow).where(GameSessionRow.status == "running")
                )
                rows = list(result.scalars())
                for row in rows:
                    row.status = "interrupted"
                    row.finished_at = _utcnow()
                    row.error = "el servicio se reinició durante la partida"
                await session.commit()
                return len(rows)
        except Exception as exc:  # noqa: BLE001
            log.warning("db.mark_stale_failed", error=str(exc))
            return 0

    # ------------------------------------------------------------ retención
    async def purge_old_messages(self, retention_days: int) -> int:
        """Borra mensajes más antiguos que la ventana de retención."""
        if retention_days <= 0:
            return 0
        cutoff = _utcnow() - timedelta(days=retention_days)
        try:
            async with self.session() as session:
                result = await session.execute(
                    delete(MessageRow).where(MessageRow.created_at < cutoff)
                )
                await session.commit()
                return int(result.rowcount or 0)
        except Exception as exc:  # noqa: BLE001
            log.warning("db.purge_failed", error=str(exc))
            return 0

    # -------------------------------------------------------------- interno
    async def _add(self, row: Base) -> None:
        try:
            async with self.session() as session:
                session.add(row)
                await session.commit()
        except Exception as exc:  # noqa: BLE001
            log.warning("db.write_failed", table=type(row).__tablename__, error=str(exc))
