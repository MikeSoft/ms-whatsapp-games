# Arquitectura y organización del código

Este documento explica **qué hay en cada módulo**, **dónde escribir código
nuevo** y **cómo integrar un juego con el agente** para que use tu lógica.

Si sólo quieres añadir un juego, ve directo a
[Añadir un juego nuevo](#añadir-un-juego-nuevo).

---

## 1. La idea en una frase

El webhook **encola** mensajes; el grafo del juego los **recoge** dentro de una
ventana de tiempo. Nada más espera a nada.

```
   WhatsApp
      │
      ▼
   ┌────────┐   webhook    ┌───────────────────────────────────────┐
   │  WAHA  │─────────────►│ POST /webhooks/waha                   │
   │        │              │  1. verifica firma (HMAC / secreto)   │
   │        │◄─────────────│  2. normaliza el payload              │
   └────────┘  send/poll   │  3. Orchestrator.handle(mensaje)      │
      ▲                    └──────────────────┬────────────────────┘
      │                                       ▼
      │                              ┌──────────────────┐
      │                              │   Orchestrator   │
      │                              └────────┬─────────┘
      │              ¿comando del máster?     │     ¿mensaje de jugador?
      │               ┌──────────────────────┴───────────────────┐
      │               ▼                                          ▼
      │       lanzar / cancelar / informar              ┌─────────────────┐
      │               │                                 │  Buzón (Redis)  │
      │               ▼                                 │  listas + TTL   │
      │      ┌─────────────────┐   recoge con ventana    └────────┬────────┘
      └──────│  Juego (Game)   │◄─────────────────────────────────┘
     Transport│  LangGraph      │
              └────────┬────────┘
                       ▼
          SQLite: histórico + checkpoints
```

**Por qué así.** Una partida por turnos dura minutos y depende de que la gente
conteste. Si el webhook esperara, WAHA daría timeout y reintentaría, duplicando
mensajes. Al desacoplar con un buzón, WAHA recibe su `200` en milisegundos
aunque el pueblo tarde tres minutos en votar.

---

## 2. Mapa del código

### Bordes: hablar con el mundo

| Módulo | Responsabilidad | Cuándo tocarlo |
|---|---|---|
| `app/api/routes.py` | Webhook, `/health`, `/health/ready`, `/games`, `/status` | Un endpoint nuevo |
| `app/api/security.py` | Verificación HMAC y de secreto compartido | Otro esquema de firma |
| `app/waha/client.py` | **Único** sitio que hace HTTP contra WAHA | Una capacidad nueva de WAHA |
| `app/waha/normalize.py` | Payload de WAHA → `InboundMessage` | Un campo o evento nuevo de WAHA |
| `app/waha/models.py` | Tipos de entrada y salida | Un tipo de mensaje nuevo |

Regla: **el resto del código no conoce WAHA**. Sólo ve `InboundMessage` y el
protocolo `Transport`. Si una versión de WAHA cambia una ruta o un campo, se
tocan sólo esos dos ficheros.

### Infraestructura: recordar y esperar

| Módulo | Responsabilidad | Cuándo tocarlo |
|---|---|---|
| `app/core/inbox.py` | Buzones efímeros: `RedisInbox` (producción) y `MemoryInbox` (tests) | Otro backend de colas |
| `app/core/db.py` | Histórico de mensajes y traza de partidas en SQLite | Una tabla nueva |
| `app/core/llm.py` | Acceso al modelo con degradación elegante | Otro proveedor |
| `app/core/checkpointer.py` | Checkpointer de LangGraph | Cambiar la persistencia del grafo |
| `app/config.py` | Toda la configuración por entorno | Un ajuste nuevo (**y `.env.example`**) |
| `app/logging_conf.py` | Logging estructurado | — |

### Orquestación: decidir qué corre

| Módulo | Responsabilidad | Cuándo tocarlo |
|---|---|---|
| `app/orchestrator/manager.py` | Encamina mensajes, lanza/cancela partidas, supervisa | Cambiar el ciclo de vida de una partida |
| `app/orchestrator/commands.py` | Parseo de `!juego`, `!cancelar`… | Un comando nuevo del máster |

### Juegos: la lógica que te interesa

| Módulo | Responsabilidad | Cuándo tocarlo |
|---|---|---|
| `app/games/base.py` | Contrato: `Game`, `GameSpec`, `GameContext`, `GameResult`, `Transport` | Ampliar lo que un juego puede pedir |
| `app/games/registry.py` | Registro y resolución de nombres y alias | Registrar un juego nuevo |
| `app/games/transport.py` | `Transport` sobre WAHA, fijado a un grupo | — |
| `app/games/mentions.py` | Compone mensajes etiquetando contactos | — |
| `app/games/recruit.py` | Reclutamiento en lenguaje natural (reutilizable) | Mejorar la detección de inscripciones |
| `app/games/<juego>/` | **Tu juego** | Aquí escribes |

---

## 3. Los cuatro contratos que tiene que conocer un juego

Todo lo que un juego necesita llega en un `GameContext`. No hay estado global
ni imports cruzados entre juegos.

### `transport` — hablar

```python
await ctx.transport.send_group("🌙 Cae la noche")           # al grupo
await ctx.transport.send_group(texto, mentions=[jid, ...])  # etiquetando
await ctx.transport.send_direct(jid, "Tu rol es…")          # privado
ok = await ctx.transport.send_poll("¿Quién?", ["1. Ana", "2. Beto"])
await ctx.transport.set_group_locked(True)                  # silenciar
```

Ningún envío lanza excepción por un fallo de WhatsApp: `send_poll` y
`set_group_locked` devuelven `False` y el juego debe seguir. **Un privado que
no llega significa que ese jugador no actúa, no que la partida se rompe.**

Para etiquetar, compón el texto con `GroupText` y pásale la lista acumulada:

```python
from app.games.mentions import GroupText

texto = GroupText(enabled=ctx.settings.use_mentions)   # uno por mensaje
cuerpo = f"☠️ {texto.tag(jid, nombre)} ha caído"
await ctx.transport.send_group(cuerpo, mentions=texto.mentions)
```

WhatsApp sólo resuelve los tokens que vienen en el array, así que el compositor
se crea **por mensaje**: sus menciones tienen que corresponder con ese texto.

### `inbox` — escuchar con plazo

```python
mensajes = await ctx.inbox.collect(
    ctx.session_id,
    timeout=30,                 # la ventana, en segundos
    group=True,                 # escuchar el grupo
    direct=[jid1, jid2],        # y/o los privados de estos jugadores
    stop_when=lambda recogidos: ...,   # cortar antes si ya está todo
)
await ctx.inbox.clear(ctx.session_id, keys=["group"])   # descartar lo viejo
```

`collect` **siempre** vuelve al expirar el plazo, con lo que haya recogido. Es
lo que hace que una partida no se cuelgue porque alguien se fue a cenar.

`stop_when` debe exigir una respuesta **interpretable**, no la mera presencia
de un mensaje: si alguien escribe "ok" y luego su elección, cortar en el "ok"
perdería la respuesta de verdad. Ver `WerewolfNodes._stop_when_resolved`.

Limpia el buzón **antes** de pedir algo nuevo, nunca después de un comando: los
mensajes que lleguen entre el comando y tu nodo son válidos.

### `llm` — narrar, nunca decidir

```python
texto = await ctx.llm.complete(sistema, usuario)       # None si no hay LLM
datos = await ctx.llm.complete_json(sistema, usuario)  # None si falla
```

**Nunca es crítico.** Devuelve `None` si no hay clave, si se agota el tiempo o
si el proveedor falla. Todo lo que llame al LLM tiene que funcionar con
`LLM_PROVIDER=none`.

Y una regla de seguridad estructural: **no le pases secretos al modelo**. En El
Hombre Lobo el narrador nunca recibe el reparto de roles, así que ni una
inyección desde el nombre de WhatsApp de alguien podría filtrarlo — no está en
su contexto. Pásale sólo los hechos públicos que necesite.

### `store` y `record` — dejar traza

```python
await ctx.record("noche.acciones", round_no=2, phase="noche",
                 detail={...}, is_secret=True)
```

Opcional (`ctx.store` puede ser `None` en tests). `is_secret=True` marca lo que
no debería aparecer en un resumen público.

---

## 4. Añadir un juego nuevo

### Paso 1: el paquete

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

### Paso 2: la clase mínima

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
        how_to="Cómo funciona, para el menú de !juegos.",
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

### Paso 3: registrarlo

Añade el módulo a `BUILTIN_MODULES` en `app/games/registry.py`:

```python
BUILTIN_MODULES = (
    "app.games.werewolf.game",
    "app.games.mi_juego.game",
)
```

Ya está: `!juegos` lo lista y `!juego mijuego` lo lanza. No hay que tocar el
orquestador ni la API.

### Paso 4: integrarlo con el agente de LangGraph

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

---

## 5. Reglas que no se negocian

Estas nacen de bugs reales que se encontraron probando el sistema.

1. **El LLM no decide la mecánica.** Quién muere, quién vota a quién y quién
   gana se resuelve con reglas en tu `parsing.py`. El modelo narra y desambigua
   lenguaje coloquial. Todo debe funcionar con `LLM_PROVIDER=none`.

2. **Ningún camino puede dejar el grupo silenciado.** Si abres una rama nueva,
   asegúrate de que todas sus salidas —errores y cancelaciones incluidos—
   acaban en `set_group_locked(False)`.

3. **Nada de secretos en el grupo ni en el prompt.** Antes de añadir un mensaje
   al grupo, pregúntate qué información filtra. Y no pases el reparto al LLM.

4. **Los objetivos se resuelven contra los jugadores vivos.** Los números de
   lista no se reciclan, así que resolver contra la lista completa deja que
   alguien señale a un cadáver y pierda su turno.

5. **Ante la duda, no se gasta el recurso.** Un mensaje ambiguo no consume una
   poción, un disparo ni un voto.

6. **Toda espera tiene plazo.** Ningún `collect` sin `timeout`, ningún envío
   masivo sin presupuesto.

7. **Una tarea de fondo no puede tumbar la partida.** El relleno narrativo y
   los recordatorios capturan sus errores y siguen.

8. **El webhook no bloquea.** Encola y devuelve. Si necesitas esperar, se
   espera dentro de la tarea de la partida.

---

## 6. Concurrencia y rendimiento

Medido en este entorno (500 operaciones, SQLite en fichero con WAL):

| Camino | Rendimiento |
|---|---|
| `Store.log_inbound` secuencial | ~920 ops/s |
| `Store.log_inbound` concurrente | ~430 ops/s |
| `MemoryInbox.push` | ~76 000 ops/s |
| `RedisInbox.push` (fakeredis) | ~2 300 ops/s |
| `parse_player_reference` (24 jugadores) | ~168 000 ops/s |

Una partida frenética genera del orden de 10 mensajes por segundo, así que hay
un margen de dos órdenes de magnitud. Lo que importa no es el techo sino que
nada se serialice donde no debe:

- **SQLite en WAL** (`journal_mode=WAL`, `busy_timeout`, `synchronous=NORMAL`):
  el webhook registra mensajes mientras la partida escribe su traza. Sin WAL
  esa mezcla da `database is locked`.
- **Pool de SQLite de 2 conexiones sin desborde.** SQLite sólo admite un
  escritor: con el pool por defecto (5 + 10) las conexiones pelean por el lock
  y cada una añade un hilo de aiosqlite. Medido, 2 rinde ~25% más con la mitad
  de hilos.
- **Encolar va antes que registrar.** `Orchestrator.handle` empuja el mensaje
  al buzón *antes* de escribir el histórico: un voto tiene una ventana de
  segundos y una escritura en contención puede tardar hasta `busy_timeout`.
- **Pool de Redis bloqueante y acotado.** El pool por defecto de redis-py
  *lanza* `"Too many connections"` al agotarse, y eso aquí es un voto perdido.
  `BlockingConnectionPool` encola hasta tener conexión libre.
- **`BLPOP` con segundos enteros.** Los timeouts fraccionarios exigen Redis ≥ 6;
  el último segundo de cada ventana se sondea.
- **Los envíos se serializan** con un intervalo mínimo (WhatsApp penaliza las
  ráfagas), y por eso reintentan menos que las consultas: insistir en un
  mensaje retrasa a todos los demás.

---

## 7. Cómo se prueba

Los tests corren **sin WAHA, sin Redis y sin LLM**.

| Fichero | Qué cubre |
|---|---|
| `tests/conftest.py` | Dobles: `FakeTransport`, `ScriptedPlayers`, `render_mentions` |
| `tests/test_werewolf_rules.py` | Reglas deterministas: reparto, cadenas de muerte, victoria |
| `tests/test_werewolf_flow.py` | Partidas completas del grafo |
| `tests/test_adversarial.py` | Entradas hostiles: basura, muertos que actúan, inyección |
| `tests/test_concurrencia.py` | Acciones simultáneas, dos partidas a la vez, pools |
| `tests/test_resiliencia.py` | WAHA caído, transporte lento, Redis que se va |
| `tests/test_soak.py` | Muchas partidas con jugadores caóticos + invariantes |
| `tests/test_inbox.py` | Memoria y Redis con los **mismos** casos |
| `tests/test_mentions.py` | Etiquetado de contactos |
| `tests/test_api.py` | Endpoints, firma, encaminamiento |

Para un juego nuevo, el patrón que mejor funciona es **jugar una partida de
verdad** con jugadores automáticos en vez de simular el grafo:

```python
async def test_mi_juego_termina(table):
    ctx, transport, inbox, script = table(6)
    game = MiJuego(ctx, timers=fast_timers())
    result = await game.run()
    assert result.status == "finished"
    assert transport.locked is False        # nunca dejar el grupo mudo
```

`ScriptedPlayers` lee los privados del bot y responde como una persona; sólo
sabe lo que se le ha dicho, igual que un jugador real. Si tu juego manda otros
privados, extiéndelo con las respuestas que toquen.

Y cuando arregles un bug, **añade el caso que lo pillaba antes de arreglarlo**.
