# Arquitectura y organización del código

> Este documento es la referencia para **escribir código**. Para jugar, el
> [README](../../README.es.md); para las reglas de cada juego,
> [El Hombre Lobo](hombreslobo.md) y [el concurso](kahoot.md).

[English](../architecture.md) · 🌍 **Español**

Este documento explica **qué hay en cada módulo**, **dónde escribir código
nuevo** y **cómo integrar un juego con el agente** para que use tu lógica.

Si sólo quieres añadir un juego, ve directo a
[Añadir un juego nuevo](juego-nuevo.md).

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
| `app/i18n.py` | Catálogos de idioma: lo que dice cada juego, en cada idioma | Un idioma nuevo, o la forma de un catálogo |
| `app/logging_conf.py` | Logging estructurado | — |

### Orquestación: decidir qué corre

| Módulo | Responsabilidad | Cuándo tocarlo |
|---|---|---|
| `app/orchestrator/manager.py` | Encamina mensajes, lanza/cancela partidas, supervisa | Cambiar el ciclo de vida de una partida |
| `app/orchestrator/commands.py` | Parseo de `#juego`, `#cancelar`… | Un comando nuevo del máster |

### Juegos: la lógica que te interesa

| Módulo | Responsabilidad | Cuándo tocarlo |
|---|---|---|
| `app/games/base.py` | Contrato: `Game`, `GameSpec`, `GameContext`, `GameResult`, `Transport` | Ampliar lo que un juego puede pedir |
| `app/games/registry.py` | Registro y resolución de nombres y alias | Registrar un juego nuevo |
| `app/games/transport.py` | `Transport` sobre WAHA, fijado a un grupo | — |
| `app/games/mentions.py` | Compone mensajes etiquetando contactos | — |
| `app/games/recruit.py` | Reclutamiento en lenguaje natural (reutilizable) | Mejorar la detección de inscripciones |
| `app/games/werewolf/` | [El Hombre Lobo](hombreslobo.md): grafo de LangGraph, roles, narrador | — |
| `app/games/werewolf/nodes/` | Un módulo por fase: `recruitment`, `night`, `day`, `ending`, y la `base` común | El comportamiento de una fase |
| `app/games/kahoot/` | [El concurso](kahoot.md): instrucción, generación, aritmética | — |
| `app/games/<juego>/` | **Tu juego** | Aquí escribes |

Los dos juegos incluidos son deliberadamente distintos por dentro: El Hombre
Lobo usa LangGraph porque tiene fases cíclicas y estado compartido, y el
concurso es un bucle, porque una tanda de preguntas no justifica un grafo.
Usar LangGraph es opcional.

---

## 3. Los cuatro contratos que tiene que conocer un juego

Todo lo que un juego necesita llega en un `GameContext`. No hay estado global
ni imports cruzados entre juegos.

### `transport` — hablar

```python
await ctx.transport.send_group("🌙 Cae la noche")           # al grupo
await ctx.transport.send_group(texto, mentions=[jid, ...])  # etiquetando
await ctx.transport.send_direct(jid, "Tu rol es…")          # privado
poll_id = await ctx.transport.send_poll("¿Quién?", ["1. Ana", "2. Beto"])
await ctx.transport.delete_group_message(poll_id)           # retirarla
nombre = await ctx.transport.contact_name(jid)              # None si no se sabe
await ctx.transport.set_group_locked(True)                  # silenciar
```

Ningún envío lanza excepción por un fallo de WhatsApp: el juego debe seguir con
lo que le devuelvan. **Un privado que no llega significa que ese jugador no
actúa, no que la partida se rompe.**

Cuidado con lo que devuelve cada uno:

| Método | Devuelve | El caso raro |
|---|---|---|
| `send_poll` | el id de la encuesta, o `None` | Puede venir **cadena vacía**: la encuesta se publicó pero WAHA no dio id, así que luego no se podrá retirar |
| `delete_group_message` | `bool` | — |
| `set_group_locked` | `bool` | `False` también cuando el bot no es administrador |
| `contact_name` | el nombre, o `None` | Un voto de encuesta no trae nombre, y en grupos nuevos el votante llega como `@lid` |

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

Tiene documento propio: [Añadir un juego nuevo](juego-nuevo.md). El paquete,
la clase mínima, el registro y cómo integrarlo con el agente de LangGraph.

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

Los dobles de prueba, qué cubre cada fichero de la suite y el patrón de jugar
una partida de verdad están en [Desarrollo](desarrollo.md), junto con el
entorno y la depuración contra WAHA.
