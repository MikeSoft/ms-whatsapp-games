"""Los nodos del grafo de El Hombre Lobo, una fase por módulo.

Cada método público de :class:`WerewolfNodes` es un nodo de LangGraph: recibe
el estado y devuelve **sólo las claves que cambia**. Los efectos —mandar
mensajes, silenciar el grupo, esperar respuestas— ocurren dentro del nodo; el
estado guarda el resultado.

La clase se compone de una fase por módulo, que es como se lee la partida:

==================  ========================================================
`base.py`           Lo compartido: tiempos, envíos, ambientación de la espera
`recruitment.py`    Convocatoria y reparto de roles
`night.py`          Lobos, vidente, Cupido, bruja y la resolución de muertes
`day.py`            Amanecer, juicio y votación
`ending.py`         Evaluación de la victoria, veredicto y cierre
==================  ========================================================

Los tiempos viven en :class:`Timers` para que los tests puedan ejecutar una
partida entera en milisegundos.
"""

from __future__ import annotations

from app.games.werewolf.nodes.base import (
    DEBATE_BUDGET_CHARS,
    DEBATE_GAP_RATIO,
    DEBATE_LINES_PER_COMMENT,
    DEBATE_MAX_CHARS,
    DEBATE_SLICE_SECONDS,
    DM_BUDGET_MAX,
    DM_BUDGET_MIN,
    DM_BUDGET_PER_MESSAGE,
    NodeBase,
    Timers,
    dm_budget,
    trim_to_budget,
)
from app.games.werewolf.nodes.day import DayPhase
from app.games.werewolf.nodes.ending import EndingPhase
from app.games.werewolf.nodes.night import NightPhase
from app.games.werewolf.nodes.recruitment import RecruitmentPhase


class WerewolfNodes(RecruitmentPhase, NightPhase, DayPhase, EndingPhase):
    """Todos los nodos del grafo, con el contexto de la partida inyectado.

    No añade comportamiento: reúne las cuatro fases en el objeto que el grafo
    necesita. Si buscas un nodo concreto, está en el módulo de su fase.
    """


__all__ = [
    "DEBATE_BUDGET_CHARS",
    "DEBATE_GAP_RATIO",
    "DEBATE_LINES_PER_COMMENT",
    "DEBATE_MAX_CHARS",
    "DEBATE_SLICE_SECONDS",
    "DM_BUDGET_MAX",
    "DM_BUDGET_MIN",
    "DM_BUDGET_PER_MESSAGE",
    "DayPhase",
    "EndingPhase",
    "NightPhase",
    "NodeBase",
    "RecruitmentPhase",
    "Timers",
    "WerewolfNodes",
    "dm_budget",
    "trim_to_budget",
]
