"""Aplicación FastAPI: ensambla las piezas y gobierna su ciclo de vida."""

from __future__ import annotations

from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI

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


async def _build_inbox(settings: Settings, app: FastAPI) -> Inbox:
    """Buzón sobre Redis; si Redis no responde, se degrada a memoria.

    Con el buzón en memoria el servicio sigue jugando, pero pierde las colas
    si se reinicia y no puede repartirse entre varios procesos.
    """
    try:
        from redis.asyncio import Redis

        client = Redis.from_url(settings.redis_url, decode_responses=True)
        await client.ping()
        app.state.redis = client
        log.info("inbox.redis", url=settings.redis_url)
        return RedisInbox(client, ttl_seconds=settings.inbox_ttl_seconds)
    except Exception as exc:  # noqa: BLE001
        app.state.redis = None
        log.error("inbox.redis_failed", error=str(exc), fallback="memoria")
        return MemoryInbox()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    configure_logging(settings.log_level, settings.log_json)
    registry.load_builtin_games()

    async with AsyncExitStack() as stack:
        store = Store(settings.database_url)
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
