"""Orquestador: encamina los mensajes entrantes y gobierna las partidas.

Este servicio atiende **un solo número** de WhatsApp: no hay multi-tenencia.
El máster (``MANAGER_NUMBER``) manda comandos; todo lo demás son mensajes de
jugadores que se encolan en el buzón para que los lea la partida en curso.

    webhook ──► Orchestrator.handle
                  │
                  ├── ¿comando del máster?  ──► lanzar / cancelar / informar
                  │
                  └── ¿mensaje de jugador?  ──► buzón (Redis) ──► nodos del grafo

La partida corre en una tarea de asyncio aparte: el webhook nunca se queda
esperando a que alguien vote.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.config import Settings
from app.core.db import Store
from app.core.inbox import Inbox
from app.core.llm import LLMClient
from app.games import registry
from app.games.base import Game, GameContext, GameResult
from app.games.transport import WahaTransport
from app.i18n import Texts
from app.logging_conf import get_logger
from app.orchestrator.commands import Command, parse_command
from app.orchestrator.texts import TEXTS
from app.waha.client import WahaClient
from app.waha.models import InboundMessage, Scope

log = get_logger("orchestrator")


@dataclass
class RunningGame:
    """Una partida viva y su tarea de fondo."""

    session_id: str
    game_key: str
    group_id: str
    started_by: str
    game: Game
    task: asyncio.Task[GameResult]
    started_at: float = field(default_factory=time.time)

    @property
    def elapsed(self) -> float:
        return time.time() - self.started_at


class Orchestrator:
    """Punto de entrada de todo lo que llega de WhatsApp."""

    def __init__(
        self,
        *,
        settings: Settings,
        waha: WahaClient,
        inbox: Inbox,
        store: Store | None = None,
        llm: LLMClient | None = None,
        checkpointer: Any | None = None,
    ) -> None:
        self.settings = settings
        self.lang = settings.game_language
        self.t = Texts(TEXTS, self.lang)
        self.waha = waha
        self.inbox = inbox
        self.store = store
        self.llm = llm or LLMClient(settings)
        # Cliente apagado para las partidas que no piden "ia". Se construye
        # una vez: no toca la red y evita decidir con ramas en cada nodo.
        self.llm_off = LLMClient(settings, enabled=False)
        self.checkpointer = checkpointer
        self._games: dict[str, RunningGame] = {}
        self._lock = asyncio.Lock()
        registry.load_builtin_games()

    # =============================================================== entrada
    async def handle(self, message: InboundMessage) -> None:
        """Procesa un mensaje ya normalizado."""
        # Descarta reenvíos del webhook: contar dos veces un "Yo" o un voto
        # falsearía la partida. La clave no siempre es el identificador del
        # mensaje; ver `InboundMessage.dedupe_key`.
        if not await self.inbox.mark_seen(message.dedupe_key):
            log.debug("orchestrator.duplicate", message_id=message.message_id)
            return

        is_manager = self.settings.is_manager(message.sender_id)
        command = parse_command(message.text, prefix=self.settings.command_prefix)
        session_id = self._session_for(message)

        # Encolar va primero y la escritura del histórico después: un voto o un
        # "Yo" tiene una ventana de segundos, mientras que el registro puede
        # esperar. Al revés, una escritura en contención (SQLite espera hasta
        # `busy_timeout`) retrasaría el mensaje hasta perder su turno.
        if command is None and not message.from_me:
            # Los mensajes que manda el propio bot no vuelven al juego: eso
            # sería un bucle de retroalimentación.
            await self._route_to_games(message)

        if self.store is not None:
            await self.store.log_inbound(message, game_session_id=session_id)

        if command is None:
            return

        if is_manager:
            await self._run_command(command, message)
        else:
            log.info(
                "orchestrator.command_rejected",
                sender=message.sender_id,
                command=command.name,
            )

    async def _route_to_games(self, message: InboundMessage) -> None:
        """Encola el mensaje en las partidas que puedan quererlo."""
        if not self._games:
            return

        if message.scope == Scope.GROUP:
            targets = [g for g in self._games.values() if g.group_id == message.chat_id]
        else:
            # Un privado se ofrece a todas las partidas vivas: cada una sólo
            # lee los buzones de los jugadores a los que ha preguntado, así
            # que ofrecerlo de más es inofensivo.
            targets = list(self._games.values())

        for game in targets:
            await self.inbox.push(game.session_id, message)

    def _session_for(self, message: InboundMessage) -> str | None:
        for game in self._games.values():
            if message.scope == Scope.GROUP and game.group_id == message.chat_id:
                return game.session_id
        if len(self._games) == 1:
            return next(iter(self._games.values())).session_id
        return None

    # ============================================================== comandos
    async def _run_command(self, command: Command, message: InboundMessage) -> None:
        log.info("orchestrator.command", name=command.name, args=command.args)
        handlers = {
            "juego": self._cmd_start,
            "juegos": self._cmd_list,
            "cancelar": self._cmd_cancel,
            "estado": self._cmd_status,
            "ayuda": self._cmd_help,
        }
        handler = handlers.get(command.name)
        if handler is not None:
            await handler(command, message)

    async def _reply(self, message: InboundMessage, text: str) -> None:
        """Contesta al máster por donde escribió."""
        await self.waha.send_text(message.chat_id, text)

    async def _cmd_help(self, command: Command, message: InboundMessage) -> None:
        await self._reply(message, self.t("help", prefix=self.settings.command_prefix))

    async def _cmd_list(self, command: Command, message: InboundMessage) -> None:
        specs = registry.specs()
        if not specs:
            await self._reply(message, self.t("catalogue.empty"))
            return
        prefix = self.settings.command_prefix
        lines = [self.t("catalogue.header"), ""]
        for spec in specs:
            lines.append(f"*{spec.title_in(self.lang)}* — {spec.tagline_in(self.lang)}")
            lines.append(
                "  "
                + self.t(
                    "players.range",
                    minimum=spec.min_players,
                    maximum=spec.max_players,
                )
            )
            lines.append(self.t("catalogue.launch", prefix=prefix, key=spec.key))
            lines.append("")
        await self._reply(message, "\n".join(lines).strip())

    async def _cmd_status(self, command: Command, message: InboundMessage) -> None:
        if not self._games:
            await self._reply(
                message,
                self.t("status.idle", prefix=self.settings.command_prefix),
            )
            return
        lines = [self.t("status.header"), ""]
        for game in self._games.values():
            spec = type(game.game).spec
            lines.append(
                self.t(
                    "status.row",
                    title=spec.title_in(self.lang),
                    group=game.group_id,
                    session=game.session_id,
                    seconds=int(game.elapsed),
                )
            )
        await self._reply(message, "\n".join(lines))

    async def _cmd_cancel(self, command: Command, message: InboundMessage) -> None:
        group_id = self._resolve_group(message)
        cancelled = await self.cancel(group_id)
        if cancelled:
            await self._reply(message, self.t("cancel.done"))
        else:
            await self._reply(message, self.t("cancel.nothing"))

    async def _cmd_start(self, command: Command, message: InboundMessage) -> None:
        name = command.argument
        if not name:
            await self._reply(
                message,
                self.t("start.which", prefix=self.settings.command_prefix),
            )
            return

        # El nombre del juego y lo que el máster le pide detrás comparten
        # línea: ``#juego kahoot preguntas de cine`` es un juego y una orden.
        game_cls, instruccion = registry.resolve_prefix(command.args)
        if game_cls is None:
            available = ", ".join(spec.key for spec in registry.specs())
            await self._reply(
                message, self.t("start.unknown", name=name, available=available)
            )
            return

        group_id = self._resolve_group(message)
        if not group_id:
            await self._reply(message, self.t("start.no_group"))
            return

        async with self._lock:
            if group_id in self._games:
                actual = type(self._games[group_id].game).spec.title_in(self.lang)
                await self._reply(
                    message,
                    self.t(
                        "start.busy",
                        title=actual,
                        prefix=self.settings.command_prefix,
                    ),
                )
                return

            running = await self._launch(
                game_cls, group_id, message, instruccion, command.flags
            )

        spec = type(running.game).spec
        narracion = self.t(
            "narration.model"
            if running.game.ctx.llm.available
            else "narration.static"
        )
        await self._reply(
            message,
            self.t(
                "start.launched",
                title=spec.title_in(self.lang),
                narration=narracion,
                session=running.session_id,
            ),
        )

    def _resolve_group(self, message: InboundMessage) -> str:
        """Se juega donde se pregunta, siempre.

        Antes había un grupo configurable con prioridad, y el resultado era
        que el máster pedía una partida en un grupo y arrancaba en otro. No
        hay caso en que eso sea lo que alguien quiere.

        Por privado no hay grupo del que deducirlo, así que sólo se resuelve
        para cancelar cuando hay una única partida viva: ahí no se elige
        dónde hablar, se señala qué cortar.
        """
        if message.scope == Scope.GROUP:
            return message.chat_id
        if len(self._games) == 1:
            return next(iter(self._games))
        return ""

    # =============================================================== partidas
    async def _launch(
        self,
        game_cls: type[Game],
        group_id: str,
        message: InboundMessage,
        args: list[str],
        flags: frozenset[str] = frozenset(),
    ) -> RunningGame:
        spec = game_cls.spec
        session_id = f"{spec.key}-{uuid.uuid4().hex[:10]}"

        transport = WahaTransport(
            self.waha, group_id, store=self.store, session_id=session_id
        )
        ctx = GameContext(
            session_id=session_id,
            group_id=group_id,
            started_by=message.sender_id,
            settings=self.settings,
            transport=transport,
            inbox=self.inbox,
            # Sin "ia" la partida corre con los textos estáticos aunque haya
            # clave configurada: gastar API es una decisión del máster. La
            # excepción son los juegos que sin modelo no existen, que lo
            # declaran en su ficha.
            llm=self.llm if ("ia" in flags or spec.needs_llm) else self.llm_off,
            store=self.store,
            checkpointer=self.checkpointer,
            args=args,
            flags=flags,
        )
        game = game_cls(ctx)

        if self.store is not None:
            await self.store.create_game_session(
                session_id,
                game_key=spec.key,
                group_id=group_id,
                started_by=message.sender_id,
            )

        task = asyncio.create_task(
            self._supervise(session_id, group_id, game), name=f"game:{session_id}"
        )
        running = RunningGame(
            session_id=session_id,
            game_key=spec.key,
            group_id=group_id,
            started_by=message.sender_id,
            game=game,
            task=task,
        )
        self._games[group_id] = running
        log.info(
            "orchestrator.game_started",
            session_id=session_id,
            game=spec.key,
            group_id=group_id,
            narracion="ia" if ctx.llm.available else "estatica",
        )
        return running

    async def _supervise(self, session_id: str, group_id: str, game: Game) -> GameResult:
        """Ejecuta la partida y limpia detrás de ella, pase lo que pase."""
        result = GameResult(status="error", error="no ejecutada")
        try:
            result = await game.run()
            return result
        except asyncio.CancelledError:
            result = GameResult(status="cancelled")
            log.info("orchestrator.game_cancelled", session_id=session_id)
            raise
        except Exception as exc:
            result = GameResult(status="error", error=str(exc))
            log.exception("orchestrator.game_failed", session_id=session_id, error=str(exc))
            # Un fallo a media noche dejaría el grupo silenciado para siempre.
            try:
                await game.ctx.transport.set_group_locked(False)
            except Exception:  # noqa: BLE001
                log.warning("orchestrator.unlock_failed", session_id=session_id)
            await self._notify(
                game.ctx.started_by,
                self.t("game.failed", session=session_id, error=exc),
            )
            return result
        finally:
            self._games.pop(group_id, None)
            await self._cleanup(session_id, result, starter=game.ctx.started_by)

    async def _cleanup(
        self, session_id: str, result: GameResult, *, starter: str = ""
    ) -> None:
        """Cierra el rastro de una partida terminada.

        Cada paso se protege por separado: esto corre dentro de un ``finally``
        que también se alcanza al cancelar, y ahí un ``await`` puede quedarse a
        medias. ``cancel()`` vuelve a cerrar la sesión después por ese motivo.

        ``starter`` es a quien se le cuenta el desenlace. Se recibe suelto y no
        como la partida entera porque es lo único que hace falta de ella.
        """
        if self.settings.purge_inbox_on_finish:
            try:
                await self.inbox.clear(session_id)
            except Exception as exc:  # noqa: BLE001
                log.warning("orchestrator.purge_failed", session_id=session_id, error=str(exc))

        if self.store is not None:
            try:
                await self.store.finish_game_session(
                    session_id,
                    status=result.status,
                    winner=result.winner,
                    rounds=result.rounds,
                    players=result.players,
                    error=result.error,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("orchestrator.finish_failed", session_id=session_id, error=str(exc))

        if result.status == "finished" and result.summary:
            try:
                await self._notify(starter, f"🏁 `{session_id}`: {result.summary}")
            except Exception as exc:  # noqa: BLE001
                log.warning("orchestrator.notify_failed", session_id=session_id, error=str(exc))

    async def cancel(self, group_id: str) -> bool:
        """Corta la partida de un grupo. ``True`` si había alguna."""
        running = self._games.get(group_id)
        if running is None:
            return False

        running.task.cancel()
        # `asyncio.wait` espera sin propagar ni el CancelledError ni la
        # excepción de la tarea. Un `await running.task` dentro de un suppress
        # se tragaría también una cancelación dirigida a *este* coroutine, que
        # es justo lo que no se debe silenciar durante un apagado.
        await asyncio.wait({running.task})

        # Después de cancelar, el juego deja el grupo en un estado usable:
        # si la partida murió de noche, el grupo estaba silenciado.
        try:
            await running.game.on_cancel()
        except Exception as exc:  # noqa: BLE001
            log.warning("orchestrator.on_cancel_failed", error=str(exc))

        self._games.pop(group_id, None)
        await self.inbox.clear(running.session_id)
        if self.store is not None:
            await self.store.finish_game_session(running.session_id, status="cancelled")
        return True

    async def _notify(self, jid: str, text: str) -> None:
        """Avisa por privado a una persona concreta.

        Los avisos de una partida van a quien la lanzó y a nadie más. Con
        varios másteres, avisar a todos convierte el problema de uno en el
        buzón de los demás, y quien puede hacer algo al respecto —relanzar,
        cancelar, mirar qué pasó— es quien la puso en marcha.
        """
        if jid:
            await self.waha.send_text(jid, text)

    # ================================================================= estado
    def snapshot(self) -> dict[str, Any]:
        """Resumen para el endpoint de estado."""
        return {
            "juegos_registrados": [spec.key for spec in registry.specs()],
            "partidas_activas": [
                {
                    "session_id": g.session_id,
                    "juego": g.game_key,
                    "grupo": g.group_id,
                    "segundos": int(g.elapsed),
                }
                for g in self._games.values()
            ],
            "llm": {
                "disponible": self.llm.available,
                "proveedor": self.settings.llm_provider,
                "modelo": self.settings.llm_model,
            },
        }

    async def shutdown(self) -> None:
        """Cancela todas las partidas al parar el servicio."""
        for group_id in list(self._games):
            await self.cancel(group_id)
