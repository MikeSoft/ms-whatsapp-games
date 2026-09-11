# Adding a new game

> How to write a game and hook it into the orchestrator. To understand first
> what lives in each module and which contracts you will be using, see the
> [architecture](architecture.md); the repository's engineering rules are in
> [CONTRIBUTING](../CONTRIBUTING.md).

🌍 **English** · [Español](es/juego-nuevo.md)

A game is a class with a `spec` and a `run()`. The orchestrator finds it,
launches it, routes messages to it and cleans up when it ends; you write the
mechanics and the text.

> [!NOTE]
> The snippets follow the repository convention: identifiers in English,
> comments and player-facing text in Spanish.

---

## Step 1: the package

```
app/games/my_game/
├── __init__.py
├── game.py       # the Game class (required)
├── state.py      # the state, if you use LangGraph
├── nodes.py      # the nodes, if you use LangGraph
├── parsing.py    # deterministic interpretation of messages
└── prompts.py    # prompts and fallback text
```

A simple game fits entirely in `game.py`. The split above is the one Werewolf
uses, because it has cyclic phases and shared state.

## Step 2: the minimal class

```python
# app/games/my_game/game.py
from app.games.base import Game, GameResult, GameSpec
from app.games.registry import register
from app.games.recruit import select_players


@register
class MyGame(Game):
    spec = GameSpec(
        key="mijuego",                       # canonical key
        title="Mi Juego",
        tagline="Una línea que explique de qué va.",
        aliases=("mj", "mi juego"),          # other ways people may write it
        min_players=3,
        max_players=20,
        how_to="Cómo funciona, para el menú de #juegos.",
        # Only if your game makes no sense without a model, like the quiz: it
        # then gets one without the master typing "ia". Note this switches
        # nothing on: with no LLM_API_KEY in the deployment, the client
        # arrives switched off.
        needs_llm=False,
    )

    async def run(self) -> GameResult:
        # 1. Call for players
        await self.ctx.transport.send_group("Escribe YO en 30 segundos.")
        mensajes = await self.ctx.inbox.collect(
            self.ctx.session_id, timeout=30, group=True
        )
        jugadores = await select_players(
            mensajes, llm=self.ctx.llm, max_players=self.spec.max_players
        )
        if len(jugadores) < self.spec.min_players:
            await self.ctx.transport.send_group("No hay suficiente gente.")
            return GameResult(status="aborted", summary="faltó gente")

        # 2. Play
        ...

        # 3. Close
        return GameResult(status="finished", winner="alguien", rounds=1)

    async def on_cancel(self) -> None:
        """El máster cortó la partida: deja el grupo utilizable."""
        await self.ctx.transport.set_group_locked(False)
        await self.ctx.transport.send_group("🛑 Partida cancelada.")
```

## Step 3: register it

Add the module to `BUILTIN_MODULES` in `app/games/registry.py`:

```python
BUILTIN_MODULES = (
    "app.games.werewolf.game",
    "app.games.kahoot.game",
    "app.games.my_game.game",
)
```

That is all: `#juegos` lists it and `#juego mijuego` launches it. Neither the
orchestrator nor the API needs touching.

## Step 4: wiring it to LangGraph

Only if your game has phases with shared state. The pattern is:

**a) The state** — a `TypedDict` of primitive structures, so the checkpointer
serialises it without surprises. Accumulating keys carry a reducer and nodes
return **only what is new**:

```python
# app/games/my_game/state.py
import operator
from typing import Annotated, TypedDict


class MyState(TypedDict, total=False):
    session_id: str
    ronda: int
    jugadores: list[dict]
    # Acumula: cada nodo devuelve los nuevos y LangGraph los concatena.
    narrativa: Annotated[list[str], operator.add]
    ganador: str | None
```

**b) The nodes** — methods of a class with the context injected. Each one takes
the state and returns **only the keys it changes**. Side effects (sending,
waiting) happen inside:

```python
# app/games/my_game/nodes.py
class MyNodes:
    def __init__(self, ctx, *, timers=None):
        self.ctx = ctx
        self.timers = timers or Timers.from_settings(ctx.settings)

    async def turno(self, state: MyState) -> dict:
        await self.ctx.transport.send_group("¡Tu turno!")
        recogidos = await self.ctx.inbox.collect(
            state["session_id"], timeout=self.timers.turno, group=True
        )
        return {"ronda": state["ronda"] + 1, "narrativa": ["turno jugado"]}

    def ruta(self, state: MyState) -> str:
        """Router: sólo lee el estado, no puede escribirlo."""
        return "final" if state.get("ganador") else "turno"
```

**Put the timings in a `Timers`**, do not read the seconds straight off
`settings` in every node. That is what lets the tests play a complete game in
milliseconds.

**c) The graph** — one node per phase, and conditional edges for the cycles:

```python
# app/games/my_game/game.py
from langgraph.graph import END, START, StateGraph


def build_graph(nodes, *, checkpointer=None):
    graph = StateGraph(MyState)
    graph.add_node("turno", nodes.turno)
    graph.add_node("final", nodes.final)
    graph.add_edge(START, "turno")
    graph.add_conditional_edges(
        "turno", nodes.ruta, {"turno": "turno", "final": "final"}
    )
    graph.add_edge("final", END)
    return graph.compile(checkpointer=checkpointer)
```

And in `run()`:

```python
async def run(self) -> GameResult:
    limite = max(
        self.ctx.settings.graph_recursion_limit,
        NODES_PER_ROUND * (self.ctx.settings.max_rounds + 1) + 10,
    )
    final = await self._graph.ainvoke(
        initial_state(self.ctx.session_id),
        config={
            "configurable": {"thread_id": self.ctx.session_id},
            "recursion_limit": limite,
        },
    )
    return GameResult(...)
```

The `thread_id` is the `session_id`: that way two simultaneous games share the
same checkpointer without mixing. The recursion cap is derived from
`MAX_ROUNDS` so that a long game ends in a draw instead of dying with a
`GraphRecursionError`.

**A node that leads into a router cannot pick the path on its own.** If the
same node is reached from two places and continues differently from each (like
`evaluar` in Werewolf), store the destination in the state — there it is
`resume_to` — and let the router read it.
