"""Aplicación FastAPI: ensambla las piezas y gobierna su ciclo de vida."""

from __future__ import annotations

import contextlib
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import BlockingConnectionPool, Redis

from app.api.routes import router
from app.api.security import is_unprotected
from app.config import Settings, get_settings
from app.core.checkpointer import open_checkpointer
from app.core.db import Store
from app.core.inbox import Inbox, MemoryInbox, RedisInbox
from app.core.llm import LLMClient
from app.games import registry
from app.logging_conf import configure_logging, get_logger
from app.orchestrator.manager import Orchestrator
from app.waha.client import WahaClient

log = get_logger("main")


def build_redis_client(settings: Settings) -> Redis:
    """Cliente de Redis con el pool acotado y bloqueante.

    El ``ConnectionPool`` que usa ``Redis.from_url`` por defecto lanza
    ``"Too many connections"`` cuando se agota, y eso aquí significa perder un
    voto o un "Yo" en plena ráfaga del webhook. ``BlockingConnectionPool``
    encola la petición hasta que haya conexión libre, que es lo que quiere una
    cola de mensajes.
    """
    pool = BlockingConnectionPool.from_url(
        settings.redis_url,
        max_connections=settings.redis_max_connections,
        timeout=settings.redis_pool_timeout,
        decode_responses=True,
    )
    return Redis(connection_pool=pool)


async def _build_inbox(settings: Settings, app: FastAPI) -> Inbox:
    """Buzón sobre Redis; si Redis no responde, se degrada a memoria.

    Con el buzón en memoria el servicio sigue jugando, pero pierde las colas
    si se reinicia y no puede repartirse entre varios procesos.
    """
    client = build_redis_client(settings)
    try:
        await client.ping()
    except Exception as exc:  # noqa: BLE001
        # Se cierra el cliente fallido: si no, deja sockets a medio abrir.
        with contextlib.suppress(Exception):
            await client.aclose()
        app.state.redis = None
        log.error("inbox.redis_failed", error=str(exc), fallback="memoria")
        return MemoryInbox()

    app.state.redis = client
    log.info("inbox.redis", url=settings.redis_url)
    return RedisInbox(client, ttl_seconds=settings.inbox_ttl_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # El logging ya quedó configurado en create_app, que corre antes.
    settings: Settings = app.state.settings
    registry.load_builtin_games()

    async with AsyncExitStack() as stack:
        store = Store(
            settings.database_url,
            busy_timeout=settings.db_busy_timeout,
            pool_size=settings.db_pool_size,
        )
        await store.init()
        stale = await store.mark_stale_sessions()
        if stale:
            log.info("main.stale_sessions_closed", count=stale)
        purged = await store.purge_old_messages(settings.message_retention_days)
        if purged:
            log.info("main.messages_purged", count=purged)
        stack.push_async_callback(store.aclose)
        app.state.store = store

        waha = WahaClient(settings)
        stack.push_async_callback(waha.aclose)
        app.state.waha = waha

        inbox = await _build_inbox(settings, app)
        # El pool de Redis hay que cerrarlo: se desmonta después de que el
        # orquestador haya cancelado las partidas, que todavía lo usan.
        stack.push_async_callback(inbox.aclose)
        app.state.inbox = inbox

        checkpointer = await open_checkpointer(settings, stack)
        app.state.checkpointer = checkpointer

        orchestrator = Orchestrator(
            settings=settings,
            waha=waha,
            inbox=inbox,
            store=store,
            llm=LLMClient(settings),
            checkpointer=checkpointer,
        )
        stack.push_async_callback(orchestrator.shutdown)
        app.state.orchestrator = orchestrator

        if not settings.manager_jid:
            log.warning(
                "main.no_manager",
                hint="define MANAGER_NUMBER o nadie podrá lanzar partidas",
            )
        if is_unprotected(settings):
            log.warning(
                "main.webhook_unprotected",
                hint="define WAHA_WEBHOOK_HMAC_SECRET o WEBHOOK_SHARED_SECRET",
            )

        log.info(
            "main.ready",
            environment=settings.environment,
            games=registry.keys(),
            manager=settings.manager_jid or "(sin configurar)",
        )
        yield

    log.info("main.stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or get_settings()
    configure_logging(resolved.log_level, resolved.log_json)

    app = FastAPI(
        title="ms-whatsapp-games",
        description=(
            "Máster de juegos de texto para WhatsApp. Recibe los eventos de WAHA "
            "y dirige la partida con un agente de LangGraph."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.settings = resolved
    app.include_router(router)
    return app


app = create_app()
