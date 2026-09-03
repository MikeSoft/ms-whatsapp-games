"""El Hombre Lobo como máquina de estados de LangGraph.

El grafo es cíclico: noche → resolución → amanecer → evaluación → debate →
votación → veredicto → evaluación → noche… El bucle sólo se rompe cuando
``evaluar`` encuentra una condición de victoria y enruta a ``final``.

    START
      │
      ▼
    reclutamiento ──(sin gente)──────────────────────┐
      │                                              │
      ▼                                              │
    reparto                                          │
      │                                              │
      ▼                                              │
    noche_inicio ──(hay bruja)── noche_bruja ──┐      │
      │                                        │      │
      └────────────────────────────────────────┴─► resolucion
                                                      │
                                                      ▼
                                                   amanecer
                                                      │
                                                      ▼
    debate ◄──(sigue la partida)────────────────── evaluar ──► final ──► END
      │                                              ▲
      ▼                                              │
    votacion ──► veredicto ──────────────────────────┘
"""

from __future__ import annotations

import random
from typing import Any

from langgraph.graph import END, START, StateGraph

from app.games.base import Game, GameContext, GameResult, GameSpec
from app.games.registry import register
from app.games.werewolf.narrator import Narrator
from app.games.werewolf.nodes import Timers, WerewolfNodes
from app.games.werewolf.state import WerewolfState, initial_state
from app.logging_conf import get_logger

log = get_logger("werewolf.game")


def build_graph(nodes: WerewolfNodes, *, checkpointer: Any | None = None):
    """Construye y compila el grafo de la partida."""
    graph = StateGraph(WerewolfState)

    graph.add_node("reclutamiento", nodes.reclutamiento)
    graph.add_node("reparto", nodes.reparto)
    graph.add_node("noche_inicio", nodes.noche_inicio)
    graph.add_node("noche_bruja", nodes.noche_bruja)
    graph.add_node("resolucion", nodes.resolucion)
    graph.add_node("amanecer", nodes.amanecer)
    graph.add_node("evaluar", nodes.evaluar)
    graph.add_node("debate", nodes.debate)
    graph.add_node("votacion", nodes.votacion)
    graph.add_node("veredicto", nodes.veredicto)
    graph.add_node("final", nodes.final)

    graph.add_edge(START, "reclutamiento")
    graph.add_conditional_edges(
        "reclutamiento",
        nodes.ruta_tras_reclutamiento,
        {"reparto": "reparto", "final": "final"},
    )
    graph.add_edge("reparto", "noche_inicio")
    graph.add_conditional_edges(
        "noche_inicio",
        nodes.necesita_bruja,
        {"noche_bruja": "noche_bruja", "resolucion": "resolucion"},
    )
    graph.add_edge("noche_bruja", "resolucion")
    graph.add_edge("resolucion", "amanecer")
    graph.add_edge("amanecer", "evaluar")
    graph.add_conditional_edges(
        "evaluar",
        nodes.ruta_tras_evaluar,
        {"final": "final", "debate": "debate", "noche_inicio": "noche_inicio"},
    )
    graph.add_edge("debate", "votacion")
    graph.add_edge("votacion", "veredicto")
    graph.add_edge("veredicto", "evaluar")
    graph.add_edge("final", END)

    return graph.compile(checkpointer=checkpointer)


@register
class WerewolfGame(Game):
    """Máster de una partida de El Hombre Lobo por WhatsApp."""

    spec = GameSpec(
        key="hombreslobo",
        title="El Hombre Lobo",
        tagline="Los lobos devoran de noche; la aldea lincha de día.",
        aliases=(
            "hombres lobo",
            "hombre lobo",
            "loboso",
            "lobos",
            "lobo",
            "werewolf",
            "castronegro",
            "hl",
        ),
        min_players=4,
        max_players=24,
        how_to=(
            "Reparto de roles por privado (lobos, vidente, bruja, cazador, cupido). "
            "De noche el grupo se silencia y los roles actúan por privado; de día "
            "se debate y se vota a quién linchar."
        ),
    )

    def __init__(
        self,
        ctx: GameContext,
        *,
        timers: Timers | None = None,
        rng: random.Random | None = None,
    ) -> None:
        super().__init__(ctx)
        self.nodes = WerewolfNodes(
            ctx,
            narrator=Narrator(ctx.llm),
            timers=timers,
            rng=rng,
        )
        self._graph = build_graph(self.nodes, checkpointer=ctx.checkpointer)

    async def run(self) -> GameResult:
        state = initial_state(self.ctx.session_id, self.ctx.group_id)
        config: dict[str, Any] = {
            "configurable": {"thread_id": self.ctx.session_id},
            "recursion_limit": self.ctx.settings.graph_recursion_limit,
        }

        final_state: dict[str, Any] = await self._graph.ainvoke(state, config=config)

        players = [dict(player) for player in final_state.get("players", [])]
        winner = final_state.get("winner")
        abort_reason = final_state.get("abort_reason")
        rounds = max(0, int(final_state.get("round_no", 0)) - 1)

        status = "aborted" if abort_reason else "finished"
        log.info(
            "werewolf.finished",
            session_id=self.ctx.session_id,
            winner=winner,
            rounds=rounds,
            status=status,
        )
        return GameResult(
            status=status,
            winner=winner,
            rounds=rounds,
            players=players,
            summary=_summary(winner, abort_reason, rounds, players),
        )

    async def on_cancel(self) -> None:
        """Deja el grupo utilizable si el manager corta la partida a medias."""
        await self.ctx.transport.set_group_locked(False)
        await self.ctx.transport.send_group(
            "🛑 *Partida cancelada por el máster.* La niebla se disipa y el chat "
            "queda abierto."
        )


def _summary(
    winner: str | None,
    abort_reason: str | None,
    rounds: int,
    players: list[dict[str, Any]],
) -> str:
    if abort_reason:
        return f"Partida no iniciada: {abort_reason}."
    titulos = {
        "lobos": "Ganaron los Hombres Lobo",
        "pueblo": "Ganó el pueblo",
        "enamorados": "Ganaron los enamorados",
        "nadie": "Sin ganador",
    }
    cabeza = titulos.get(winner or "nadie", "Sin ganador")
    return f"{cabeza} en {rounds} ronda{'s' if rounds != 1 else ''} con {len(players)} jugadores."
