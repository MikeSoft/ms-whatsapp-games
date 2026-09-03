"""Checkpointer de LangGraph.

Con ``CHECKPOINTER=sqlite`` el estado de cada partida se persiste en disco, lo
que permite inspeccionar qué pasó en una ronda concreta después de jugarla.
Con ``memory`` vive sólo en el proceso, que es lo que quieren los tests.
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from app.config import Settings
from app.logging_conf import get_logger

log = get_logger("checkpointer")


async def open_checkpointer(settings: Settings, stack: AsyncExitStack) -> Any:
    """Abre el checkpointer y lo ata al ciclo de vida de ``stack``."""
    if settings.checkpointer == "memory":
        from langgraph.checkpoint.memory import MemorySaver

        log.info("checkpointer.memory")
        return MemorySaver()

    try:
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        path = Path(settings.checkpointer_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        saver = await stack.enter_async_context(
            AsyncSqliteSaver.from_conn_string(str(path))
        )
        await saver.setup()
        log.info("checkpointer.sqlite", path=str(path))
        return saver
    except Exception as exc:  # noqa: BLE001
        # Perder la persistencia del grafo degrada la depuración, no el juego.
        from langgraph.checkpoint.memory import MemorySaver

        log.error("checkpointer.sqlite_failed", error=str(exc))
        return MemorySaver()
