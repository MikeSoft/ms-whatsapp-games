# Añadir un juego nuevo

> Cómo escribir un juego y engancharlo al orquestador. Para entender antes
> qué hay en cada módulo y qué contratos vas a usar, la
> [arquitectura](arquitectura.md); las reglas de ingeniería del repositorio
> están en [CONTRIBUTING](../../CONTRIBUTING.es.md).

[English](../new-game.md) · 🌍 **Español**

Un juego es una clase con un `spec` y un `run()`. El orquestador se encarga
de encontrarlo, lanzarlo, encaminarle los mensajes y limpiar cuando acaba;
tú escribes la mecánica y los textos.

---

## Paso 1: el paquete

```
app/games/mi_juego/
├── __init__.py
├── game.py       # la clase Game (obligatorio)
├── state.py      # el estado, si usas LangGraph
├── nodes.py      # los nodos, si usas LangGraph
├── parsing.py    # interpretación determinista de los mensajes
└── prompts.py    # prompts y textos de respaldo
```

Un juego sencillo cabe entero en `game.py`. La separación de arriba es la que
usa El Hombre Lobo porque tiene fases cíclicas y estado compartido.

## Paso 2: la clase mínima

```python
# app/games/mi_juego/game.py
from app.games.base import Game, GameResult, GameSpec
from app.games.registry import register
from app.games.recruit import select_players


@register
class MiJuego(Game):
    spec = GameSpec(
        key="mijuego",                       # clave canónica
        title="Mi Juego",
        tagline="Una línea que explique de qué va.",
        aliases=("mj", "mi juego"),          # cómo más lo pueden escribir
        min_players=3,
        max_players=20,
        how_to="Cómo funciona, para el menú de #juegos.",
        # Sólo si tu juego no tiene sentido sin modelo, como el concurso: lo
        # recibe sin que el máster escriba "ia". Ojo, esto no enciende nada:
        # si el despliegue no tiene LLM_API_KEY, el cliente llega apagado.
        needs_llm=False,
    )

    async def run(self) -> GameResult:
        # 1. Convocar
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

        # 2. Jugar
        ...

        # 3. Cerrar
        return GameResult(status="finished", winner="alguien", rounds=1)

    async def on_cancel(self) -> None:
        """El máster cortó la partida: deja el grupo utilizable."""
        await self.ctx.transport.set_group_locked(False)
        await self.ctx.transport.send_group("🛑 Partida cancelada.")
```

## Paso 3: registrarlo

Añade el módulo a `BUILTIN_MODULES` en `app/games/registry.py`:

```python
BUILTIN_MODULES = (
    "app.games.werewolf.game",
    "app.games.kahoot.game",
    "app.games.mi_juego.game",
)
```

Ya está: `#juegos` lo lista y `#juego mijuego` lo lanza. No hay que tocar el
orquestador ni la API.

## Paso 4: integrarlo con el agente de LangGraph

Sólo si tu juego tiene fases con estado compartido. El patrón es:

**a) El estado** — `TypedDict` con estructuras primitivas, para que el
checkpointer lo serialice sin sorpresas. Las claves acumulativas llevan un
reductor y los nodos devuelven **sólo lo nuevo**:

```python
# app/games/mi_juego/state.py
import operator
from typing import Annotated, TypedDict


class MiEstado(TypedDict, total=False):
    session_id: str
    ronda: int
    jugadores: list[dict]
    # Acumula: cada nodo devuelve los nuevos y LangGraph los concatena.
    narrativa: Annotated[list[str], operator.add]
    ganador: str | None
```

**b) Los nodos** — métodos de una clase con el contexto inyectado. Cada uno
recibe el estado y devuelve **sólo las claves que cambia**. Los efectos
(mandar, esperar) ocurren dentro:

```python
# app/games/mi_juego/nodes.py
class MisNodos:
    def __init__(self, ctx, *, timers=None):
        self.ctx = ctx
        self.timers = timers or Timers.from_settings(ctx.settings)

    async def turno(self, state: MiEstado) -> dict:
        await self.ctx.transport.send_group("¡Tu turno!")
        recogidos = await self.ctx.inbox.collect(
            state["session_id"], timeout=self.timers.turno, group=True
        )
        return {"ronda": state["ronda"] + 1, "narrativa": ["turno jugado"]}

    def ruta(self, state: MiEstado) -> str:
        """Router: sólo lee el estado, no puede escribirlo."""
        return "final" if state.get("ganador") else "turno"
```

**Mete los tiempos en un `Timers`**, no leas los segundos directamente de
`settings` en cada nodo. Es lo que permite que los tests jueguen una partida
completa en milisegundos.

**c) El grafo** — un nodo por fase, y bordes condicionales para los ciclos:

```python
# app/games/mi_juego/game.py
from langgraph.graph import END, START, StateGraph


def build_graph(nodes, *, checkpointer=None):
    graph = StateGraph(MiEstado)
    graph.add_node("turno", nodes.turno)
    graph.add_node("final", nodes.final)
    graph.add_edge(START, "turno")
    graph.add_conditional_edges(
        "turno", nodes.ruta, {"turno": "turno", "final": "final"}
    )
    graph.add_edge("final", END)
    return graph.compile(checkpointer=checkpointer)
```

Y en `run()`:

```python
async def run(self) -> GameResult:
    limite = max(
        self.ctx.settings.graph_recursion_limit,
        NODOS_POR_RONDA * (self.ctx.settings.max_rounds + 1) + 10,
    )
    final = await self._graph.ainvoke(
        estado_inicial(self.ctx.session_id),
        config={
            "configurable": {"thread_id": self.ctx.session_id},
            "recursion_limit": limite,
        },
    )
    return GameResult(...)
```

El `thread_id` es el `session_id`: así dos partidas simultáneas comparten el
mismo checkpointer sin mezclarse. El tope de recursión se deriva de
`MAX_ROUNDS` para que una partida larga se cierre por tablas y no muera con un
`GraphRecursionError`.

**Un nodo que llega a un router no puede decidir el camino por sí solo.** Si un
mismo nodo se alcanza desde dos sitios y de cada uno sigue distinto (como
`evaluar` en El Hombre Lobo), guarda el destino en el estado — ahí es
`resume_to`— y que el router lo lea.
