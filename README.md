# ms-whatsapp-games

Microservicio en FastAPI que hace de **máster de juegos de texto por WhatsApp**.
Recibe los eventos de una instancia de [WAHA](https://waha.devlike.pro/) y dirige
la partida con un agente de **LangGraph**: reparte roles por privado, silencia el
grupo de noche, recoge las acciones ocultas, narra el amanecer y gestiona la
votación del día.

Trae dos juegos: **El Hombre Lobo** (Los Hombres Lobo de Castronegro) y
**Kahoot**, un concurso de preguntas contrarreloj cuyo cuestionario escribe el
modelo. El módulo de juegos es extensible: añadir otro es escribir una clase y
registrarla.

> Atiende **un solo número** de WhatsApp. No es multi-tenant y no pretende serlo.

📐 **Para escribir código**, la referencia es
[`docs/ARQUITECTURA.md`](docs/ARQUITECTURA.md): qué hay en cada módulo, dónde
va lo nuevo, los contratos que usa un juego y cómo integrarlo con el agente.

---

## Cómo se ve una partida

```
Máster  ›  !juego hombreslobo

Bot     ›  🌫️ Una niebla densa baja de la montaña…
           🐺 EL HOMBRE LOBO — se abren las inscripciones.
           Escribe YO en los próximos 30 segundos para entrar.

Ana     ›  Yo
Beto    ›  me apunto
Caro    ›  yo juego
…

Bot     ›  🎭 6 jugadores entran a la partida
           El reparto de esta noche:
           🐺 1 Hombre Lobo · 🔮 1 Vidente · 🧪 1 Bruja · 🧑‍🌾 3 Aldeanos
           🔇 El grupo queda en silencio. Revisa tu chat privado.

(privado a Beto)  🐺 Tu rol es Hombre Lobo. Eres el único lobo…
(privado a Ana)   🔮 Tu rol es Vidente. Cada noche puedes preguntarme…

Bot     ›  🌙 NOCHE 1 — Se apagan los candiles uno por uno…
(privado) 🐺 ¿A quién devoráis?  1. Ana  3. Caro  4. Dani …
(privado) 🔮 ¿De quién quieres conocer la identidad?
(privado) 🧪 Esta noche los lobos atacaron a Caro. ¿Curar, veneno o nada?

Bot     ›  🌅 AMANECE EL DÍA 1
           ☠️ @Caro fue devorado por los lobos — era 🧑‍🌾 Aldeano
           Quien haya caído ya no participa: ignorad lo que escriba.
           🔊 El chat está abierto.

Bot     ›  ⚖️ EL JUICIO — tenéis 3 minutos para acusaros.
Bot     ›  🗳️ [encuesta] ¿A quién linchamos?
Bot     ›  ⚖️ VEREDICTO — ☠️ Beto fue linchado — era 🐺 Hombre Lobo
Bot     ›  🎉 GANA EL PUEBLO
```

---

## Arquitectura

```
   WhatsApp
      │
      ▼
   ┌────────┐   webhook    ┌──────────────────────────────────────┐
   │  WAHA  │─────────────►│  POST /webhooks/waha                 │
   │        │◄─────────────│  (verifica HMAC → normaliza evento)  │
   └────────┘  send/poll   └──────────────────┬───────────────────┘
                                              ▼
                                     ┌──────────────────┐
                                     │   Orchestrator   │
                                     └────────┬─────────┘
                        ¿comando del máster?  │  ¿mensaje de jugador?
                         ┌────────────────────┴──────────────────┐
                         ▼                                       ▼
                 lanzar / cancelar                       ┌───────────────┐
                         │                              │ Buzón (Redis) │
                         ▼                              │  listas + TTL │
                 ┌───────────────┐   recoge con ventana  └───────┬───────┘
                 │ Grafo del     │◄──────────────────────────────┘
                 │ juego         │
                 │ (LangGraph)   │──► WAHA (grupo y privados)
                 └───────┬───────┘
                         │
             ┌───────────┴───────────┐
             ▼                       ▼
      SQLite (histórico)      SQLite (checkpoints)
```

**El desacople es la idea central.** El webhook nunca espera: sólo *encola*
mensajes. Los nodos del grafo los *recogen* dentro de una ventana de tiempo
(30 s para inscribirse, 60 s para las acciones de noche, 3 min de debate).
La partida corre en una tarea de asyncio aparte, así que WAHA recibe su 200
en milisegundos aunque el pueblo tarde tres minutos en decidirse.

### Reparto de responsabilidades

| Ruta | Qué hace |
|---|---|
| `app/main.py` | Ensambla la aplicación y gobierna el ciclo de vida |
| `app/config.py` | Configuración por entorno (pydantic-settings) |
| `app/api/routes.py` | Webhook, healthchecks, `/games`, `/status` |
| `app/api/security.py` | Verificación HMAC y de secreto compartido |
| `app/waha/client.py` | Cliente HTTP de WAHA con reintentos |
| `app/waha/normalize.py` | Normaliza los payloads de WAHA a un tipo propio |
| `app/core/inbox.py` | Buzones efímeros (Redis con TTL, o memoria) |
| `app/core/db.py` | Histórico de mensajes y partidas en SQLite |
| `app/core/llm.py` | Acceso a DeepSeek con degradación elegante |
| `app/orchestrator/manager.py` | Encamina mensajes y gobierna las partidas |
| `app/games/base.py` | Contrato común de los juegos |
| `app/games/registry.py` | Registro y resolución de nombres |
| `app/games/werewolf/` | El Hombre Lobo (grafo, roles, narrador, parseo) |
| `app/games/kahoot/` | El concurso de preguntas (instrucción, generación, juego) |

---

## Arranque rápido

```bash
git clone <este-repo> && cd ms-whatsapp-games
cp .env.example .env
```

Edita `.env` y pon como mínimo tu número de máster:

```env
MANAGER_NUMBER=+573001234567
LLM_API_KEY=sk-...            # opcional: sin clave usa narrativa estática
```

Levanta el stack:

```bash
docker compose up -d --build
docker compose logs -f api
```

Vincula el número de WhatsApp del bot en WAHA:

1. Abre `http://localhost:3000` y arranca la sesión `default`.
2. Escanea el QR con el teléfono que hará de bot.
3. Añade ese número al grupo donde vais a jugar y **hazlo administrador**
   (hace falta para poder silenciar el grupo de noche).

Comprueba que todo responde:

```bash
curl localhost:8000/health/ready
curl localhost:8000/games
```

Y desde tu WhatsApp de máster, escribe en el grupo:

```
!juegos
!juego hombreslobo
```

---

## Comandos del máster

Sólo los acepta el número de `MANAGER_NUMBER`. El prefijo es configurable con
`COMMAND_PREFIX`.

| Comando | Qué hace |
|---|---|
| `!juegos` | Lista los juegos disponibles |
| `!juego <nombre>` | Inicia una partida (`hombreslobo`, `lobos`, `kahoot`, `trivia`…) |
| `!juego <nombre> ia` | Igual, pero narrada por el modelo (ver más abajo) |
| `!estado` | Qué partidas hay en marcha |
| `!cancelar` | Corta la partida y reabre el grupo |
| `!ayuda` | Recuerda los comandos |

Tolera mayúsculas y acentos: `!Juego`, `!CATÁLOGO` y `!cancelar` funcionan igual.

Lo que se escriba **detrás del nombre** llega al juego como instrucción, así
que `!juego kahoot 15 preguntas de cine` lanza el concurso pidiéndole quince
preguntas de cine. El nombre se separa probando primero el prefijo más largo,
para que `hombres lobo` siga funcionando como nombre de dos palabras.

El sufijo `ia` va en cualquier posición y admite `ai`, `llm` y `narrador`. Como
el nombre de un juego puede llevar varias palabras, el modificador se separa
del nombre al parsear: `!juego hombres lobo ia` lanza «hombres lobo» con
narración generada.

El máster también puede jugar: si escribe `Yo` durante las inscripciones, entra
como cualquier otro.

---

## El Hombre Lobo

### Roles

| Rol | Actúa de noche | Qué hace |
|---|---|---|
| 🐺 Hombre Lobo | sí | Elige la víctima. Si hay varios, deciden por mayoría |
| 🔮 Vidente | sí | Pregunta por un jugador y se le dice si es lobo |
| 🧪 Bruja | sí | Dos pociones de un solo uso: vida (revive a la víctima) y muerte |
| 🏹 Cazador | al morir | Se lleva a alguien a la tumba con él |
| 🏹💘 Cupido | 1.ª noche | Enamora a dos jugadores: si uno muere, el otro también |
| 🧑‍🌾 Aldeano | no | Sólo su intuición y su labia |

### Reparto por número de jugadores

Los lobos crecen por tramos (1 hasta 6 jugadores, 2 hasta 11, 3 hasta 15,
4 hasta 19, luego 1 por cada 5) y los roles especiales se añaden por umbrales:
Vidente desde 4 jugadores, Bruja desde 6, Cazador desde 8, Cupido desde 10.

Dos invariantes se comprueban en los tests para todo tamaño de mesa: **el
pueblo arranca siempre en mayoría estricta** y **siempre queda al menos un
aldeano raso** (si no, no hay a quién deducir).

### El grafo

Cada fase es un nodo; el ciclo se rompe cuando `evaluar` encuentra una
condición de victoria.

| Nodo | Fase |
|---|---|
| `reclutamiento` | Abre la convocatoria y registra a quien se apunta |
| `reparto` | Silencia el grupo, asigna roles y los manda por privado |
| `noche_inicio` | Narra la noche y recoge lobos, vidente y Cupido en paralelo |
| `noche_bruja` | Le dice a quién atacaron y recoge su decisión |
| `resolucion` | Cruza ataque, curación y veneno; resuelve cadenas de muerte |
| `amanecer` | Publica las víctimas y reabre el grupo |
| `evaluar` | Comprueba la victoria y enruta |
| `debate` | Abre el juicio público con temporizador |
| `votacion` | Publica la encuesta y recoge los votos |
| `veredicto` | Lincha al más votado y revela su rol |
| `final` | Narra el desenlace y revela todos los roles |

La bruja tiene su propio nodo porque **necesita saber a quién atacaron los
lobos**: su ventana se abre después de la de ellos, no en paralelo.

### Condiciones de victoria

- **Pueblo**: no queda ningún lobo vivo.
- **Lobos**: los lobos igualan o superan en número al resto.
- **Enamorados**: sobreviven sólo los dos enamorados y son de bandos opuestos
  (se comprueba antes que la de los lobos, que si no se la comería).
- **Tablas**: no queda nadie, o se alcanza `MAX_ROUNDS`.

---

## Configuración

Todas las variables están documentadas en `.env.example`. Las que más importan:

| Variable | Por defecto | Para qué |
|---|---|---|
| `MANAGER_NUMBER` | *(vacío)* | Único número que puede dar órdenes |
| `LLM_PROVIDER` | `deepseek` | `deepseek`, `openai` o `none` |
| `LLM_API_KEY` | *(vacío)* | Sin clave, la narrativa es estática (el juego funciona igual) |
| `MANAGE_GROUP_PERMISSIONS` | `true` | Silenciar el grupo de noche (requiere WAHA Plus) |
| `USE_MENTIONS` | `true` | Etiquetar contactos en el grupo en vez de sólo nombrarlos |
| `WAHA_WEBHOOK_HMAC_SECRET` | *(vacío)* | Firma de los webhooks |
| `WAHA_DRY_RUN` | `false` | Escribe los envíos en el log en vez de mandarlos |
| `WAHA_MAX_RETRIES` | `3` | Reintentos de las consultas a WAHA |
| `WAHA_SEND_MAX_RETRIES` | `2` | Reintentos de los envíos: menos a propósito (ver más abajo) |
| `RECRUIT_SECONDS` | `30` | Ventana de inscripciones |
| `NIGHT_ACTION_SECONDS` | `60` | Ventana de las acciones nocturnas |
| `DEBATE_SECONDS` | `180` | Duración del debate |
| `VOTE_SECONDS` | `30` | Duración de la votación |
| `WEREWOLF_MIN_PLAYERS` | `4` | Mínimo para arrancar |
| `WEREWOLF_TIE_BREAK` | `none` | `none` = un empate no lincha; `random` = decide el azar |
| `MESSAGE_RETENTION_DAYS` | `30` | Purga del histórico al arrancar |

### Redis y SQLite: para qué cada uno

- **Redis** guarda lo efímero: los buzones de la ronda en curso, con TTL
  (`INBOX_TTL_SECONDS`). Se borran al terminar la partida
  (`PURGE_INBOX_ON_FINISH`). Si Redis no responde, el servicio arranca con un
  buzón en memoria y lo dice en el log: se sigue jugando, pero se pierden las
  colas al reiniciar.
- **SQLite** guarda lo que interesa conservar: el histórico de mensajes, la
  traza de cada partida (`game_events`) y los checkpoints del grafo. Vive en el
  volumen `./data`.

---

## El LLM nunca es crítico

Es una decisión de diseño, no una casualidad: **el modelo pone ambientación, el
código pone la mecánica**.

**Se pide por partida, no se hereda del entorno.** Tener `LLM_API_KEY` puesta
sólo deja el modelo disponible; quien decide gastarlo es el máster, al lanzar
con `!juego hombreslobo ia`. Sin el sufijo la partida corre entera sin modelo
aunque haya clave: narración estática **y** reclutamiento determinista. El
flag no es sólo la narración, apaga o enciende el LLM para toda la partida.
Si se pide `ia` y no hay clave válida, la partida se lanza igual y avisa de
que narrará en estático: pedir el modelo nunca impide jugar.

- La narrativa la genera el LLM a partir de unos HECHOS acotados, y cada escena
  tiene un texto estático de respaldo en `app/games/werewolf/prompts.py`. Con
  `LLM_PROVIDER=none` la partida es perfectamente jugable.
- Quién muere, quién vota a quién y quién gana **no pasa nunca por el modelo**:
  se resuelve con reglas en `app/games/werewolf/parsing.py`. Un fallo de red no
  puede cambiar el resultado de una partida.
- **El narrador escucha el juicio.** Lo que se habla en el grupo durante el
  debate se recoge y se le pasa como HECHOS, así que la ambientación comenta
  las acusaciones reales en vez de rellenar con niebla genérica, y el veredicto
  se narra sabiendo de qué se discutió. Con tres cautelas: sólo mensajes
  **públicos** (los privados llevan roles), sólo de **jugadores vivos** (a los
  muertos ya se les ignora), y acotado a las últimas intervenciones para que el
  prompt no crezca con el tamaño de la mesa. Al modelo se le dice
  explícitamente que eso son rumores: puede recoger el tono, nunca confirmarlos.
- El reclutamiento usa el modelo para interpretar respuestas coloquiales
  **en las partidas lanzadas con `ia`**, con dos redes de seguridad: un "yo"
  inequívoco entra aunque el modelo lo omita, y un "yo no" inequívoco queda
  fuera aunque el modelo lo incluya. Sin el sufijo el reclutamiento es
  determinista, así que un "va, contá conmigo" puede quedarse fuera: es el
  precio de que una partida sin `ia` no gaste API por ningún lado.
- Al narrador se le prohíbe explícitamente revelar roles que no estén en los
  HECHOS. Cuando la bruja salva a alguien, se le pide insinuar una
  "intervención misteriosa" sin nombrar quién: un frasco vacío en el alféizar,
  arañazos en la puerta.

---

## Se etiqueta, no se nombra

Los mensajes al grupo **etiquetan al contacto** (`@número` más el array
`mentions` de WAHA) en vez de escribir su nombre. WhatsApp lo muestra como una
mención real, tocable:

```
☠️ @Hugo fue devorado por los lobos — era 🧪 Bruja
Quien haya caído ya no participa: ignorad lo que escriba.

Siguen vivos (7):
1. @Ana
2. @Beto
…
```

Quita la ambigüedad de los tocayos y de quien no tiene nombre público, y deja
claro **a quién ignorar** el resto de la partida. Los privados siguen usando
nombres: en un 1:1 son más legibles, y el jugador necesita reconocer a quién
señala en su lista de objetivos.

Con `USE_MENTIONS=false` se vuelve a nombres planos, para motores de WAHA que
no resuelvan menciones.

---

## Nada se cuelga sin límite

Un servicio que dirige una partida por turnos tiene un enemigo claro: quedarse
esperando para siempre y dejar el grupo silenciado. Los topes que lo evitan:

- **Cada ventana tiene deadline.** El buzón devuelve lo que haya recogido
  cuando expira el plazo; nunca espera a que alguien conteste.
- **Los envíos reintentan menos que las consultas** (`WAHA_SEND_MAX_RETRIES`).
  Van serializados por el rate limit de WhatsApp, así que insistir en un
  mensaje retrasa a todos los demás. Un privado perdido sólo significa que ese
  jugador no actúa esa noche; un nodo bloqueado rompe la partida entera.
- **Los envíos masivos tienen presupuesto con techo absoluto** (60 s). Reparte
  roles a 24 jugadores en unos 17 s con un WAHA sano, y corta a uno patológico
  en lugar de multiplicar su latencia por el número de jugadores.
- **La ambientación es una tarea de fondo que no puede propagar.** Si el
  narrador falla mientras se espera, se calla y la partida sigue su curso.
- **`MAX_ROUNDS` cierra una partida abandonada** y el tope de recursión del
  grafo se eleva solo para no chocar antes de tiempo.
- **Un fallo dentro de una partida reabre el grupo y avisa al máster**, en vez
  de dejar a la gente muda esperando una noche que no termina.

Todo esto está cubierto en `tests/test_resiliencia.py`, que ejecuta las rutas
de fallo: WAHA caído a media partida, transporte lento y serializado, tarea de
relleno que revienta, Redis que se va y apagado con partida a medias.

## Kahoot: preguntas contrarreloj

El segundo juego incluido. El máster pide un tema, el modelo escribe las
preguntas y se publican de una en una como encuesta de WhatsApp.

```
Máster  ›  !juego kahoot 10 preguntas de historia de Colombia

Bot     ›  🧠 CONCURSO DE PREGUNTAS
           Tema: historia de Colombia
           10 preguntas · 5 opciones · 7 segundos cada una
           🔇 (el grupo queda en silencio)

Bot     ›  [encuesta] 1/10 · ¿En qué año se proclamó la independencia?
           … 7 segundos … la encuesta desaparece

Bot     ›  📖 RESPUESTAS CORRECTAS
           1. ¿En qué año se proclamó la independencia?
              ✅ 1810
           …

Bot     ›  🏆 CLASIFICACIÓN
           🥇 Ana — 8/10
           🥈 Beto — 6/10
```

### Lo que se puede pedir en el comando

Todo es opcional y va en lenguaje corriente; lo que no se diga usa el valor
por defecto del entorno.

| Se escribe | Qué cambia | Por defecto |
|---|---|---|
| `de historia de Roma`, `sobre cine` | El tema | cultura general |
| `15 preguntas` | Cuántas | `KAHOOT_QUESTIONS` (10) |
| `10 segundos` | Cuánto dura cada una | `KAHOOT_SECONDS_PER_QUESTION` (7) |
| `4 opciones` | Alternativas por pregunta | `KAHOOT_OPTIONS` (5) |

Las cifras se sacan con expresiones regulares y lo que sobra es el tema: la
mecánica no la decide el modelo, sólo el contenido.

### Detalles que importan

- **Cada encuesta se retira al cerrarse su ventana**, para que no quede
  votable cuando ya no cuenta. Y el buzón del grupo se vacía antes de abrir
  la siguiente, de modo que un voto que llegue tarde no se cuele en la que
  viene. Cuando WAHA informa del identificador de la encuesta votada, se
  exige además que coincida con la abierta.
- **Sólo cuenta el último voto de cada persona**, porque WhatsApp deja
  cambiar la respuesta mientras la encuesta está abierta. Marcar varias
  opciones no es acertar.
- **Las opciones se barajan siempre**, vengan del modelo o del banco de
  respaldo. Sin barajar se gana mirando la posición en vez de sabiendo la
  respuesta.
- **Empate de aciertos lo desempata el tiempo**: sin eso, el orden de dos
  empatados dependería del azar y cambiaría entre partidas idénticas.
- **Sin modelo se juega igual**, con un banco estático de cultura general, y
  se avisa en el grupo de que el tema pedido no se respetó. Es la misma regla
  que el resto del servicio: nada depende del LLM para funcionar.
- **El grupo se silencia mientras se juega** (`KAHOOT_LOCK_GROUP`). Si al
  cerrarse la primera pregunta no ha votado nadie, se reabre solo y se avisa:
  no está confirmado que WhatsApp permita votar en un grupo restringido a
  administradores, así que ante la duda se prefiere jugar con ruido a no
  jugar. Ver *Límites conocidos*.

## Añadir un juego nuevo

El módulo de juegos es extensible: un juego es una clase con su ficha, y el
orquestador no hay que tocarlo.

```python
# app/games/mi_juego/game.py
from app.games.base import Game, GameResult, GameSpec
from app.games.registry import register


@register
class MiJuego(Game):
    spec = GameSpec(
        key="mijuego",
        title="Mi Juego",
        tagline="Una línea que explique de qué va.",
        aliases=("mj", "mi juego"),
        min_players=3,
        max_players=20,
    )

    async def run(self) -> GameResult:
        await self.ctx.transport.send_group("¡Empezamos!")
        mensajes = await self.ctx.inbox.collect(
            self.ctx.session_id, timeout=30, group=True
        )
        ...
        return GameResult(status="finished", winner="alguien")
```

Se añade el módulo a `BUILTIN_MODULES` en `app/games/registry.py` y ya está:
`!juegos` lo lista y `!juego mijuego` lo lanza.

El contexto (`self.ctx`) da `transport` para hablar, `inbox` para escuchar con
plazo, `llm` para narrar y `store` para dejar traza. Usar LangGraph es
opcional: El Hombre Lobo lo usa porque tiene fases cíclicas y estado
compartido, pero un juego sencillo puede ser un bucle.

👉 **El paso a paso completo** —estructura del paquete, los cuatro contratos,
cómo montar el grafo, las reglas que no se negocian y cómo probarlo— está en
[`docs/ARQUITECTURA.md`](docs/ARQUITECTURA.md).

Para reutilizar el reclutamiento en lenguaje natural, llama a
`app.games.recruit.select_players`.

---

## Desarrollo

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt

.venv/bin/python -m pytest        # 300 tests
.venv/bin/ruff check app tests
.venv/bin/uvicorn app.main:app --reload
```

Los tests corren **sin WAHA, sin Redis y sin LLM**: `tests/conftest.py` trae
un transporte que apunta lo enviado y una mesa de jugadores automáticos que
lee los privados del bot y responde como lo haría una persona. Una partida
completa de 11 jugadores tarda milisegundos, así que
`tests/test_werewolf_flow.py` juega partidas enteras de verdad en lugar de
simular el grafo.

El buzón se prueba contra las dos implementaciones (memoria y Redis, con
`fakeredis`) usando los mismos casos, para que la ruta de producción no se
desvíe de la que usan los demás tests.

---

## Límites conocidos

- **Silenciar el grupo requiere ser administrador.** Antes de intentarlo se
  comprueba si la sesión lo es, cotejando sus dos identidades (`@c.us` y
  `@lid`, porque cada grupo direcciona con una u otra) contra la lista de
  participantes. Si no lo es, no se intenta y la partida sigue: el silencio
  pasa a ser una convención social. La comprobación se hace una vez por
  grupo y con un solo intento, que para una capacidad opcional insistir sólo
  gasta segundos para llegar al mismo "no".
- **Silenciar el grupo requiere WAHA Plus.** El endpoint
  `PUT /api/{session}/groups/{id}/settings/security/messages-admin-only` no
  está en la imagen `core` gratuita. Si no está disponible, se registra un
  warning y la partida continúa: el silencio pasa a ser una convención social
  en vez de una restricción técnica. Con `MANAGE_GROUP_PERMISSIONS=false` ni se
  intenta.
- **Las rutas de WAHA cambian entre versiones y motores** (WEBJS / NOWEB /
  GOWS). Están todas concentradas en `app/waha/client.py` y la lectura de
  payloads en `app/waha/normalize.py`, que ya contempla varias formas
  alternativas. No se han verificado contra una instancia real en este
  entorno: si una versión difiere, esos dos ficheros son los únicos a tocar.
- **Si los lobos no responden, no hay ataque.** Se narra como que no se
  pusieron de acuerdo. Se prefirió eso a matar a alguien al azar: el día
  siempre avanza por linchamiento, así que la partida no se estanca.
- **No está verificado que se pueda votar en un grupo silenciado.** El
  concurso cierra el grupo mientras juega, y no hemos confirmado si WhatsApp
  deja que un participante no administrador vote una encuesta en un grupo en
  modo "sólo administradores". Si al cerrarse la primera pregunta no votó
  nadie, el juego reabre el grupo solo y lo avisa; con
  `KAHOOT_LOCK_GROUP=false` ni se intenta silenciarlo.
- **Siete segundos por pregunta es muy poco.** Es el valor que se pidió, pero
  entre el envío, el intervalo antiflood y la vuelta del webhook, quien lea
  despacio no llega. Se sube por comando (`20 segundos`) o por entorno.
- **La calidad de las preguntas es la del modelo.** Se valida la *forma* —una
  sola correcta, sin opciones repetidas, tantas alternativas como se pidió—
  pero no los hechos: una pregunta puede salir ambigua o con más de una
  respuesta defendible.
- **Las encuestas de WhatsApp admiten 12 opciones.** Con mesas más grandes se
  recorta la encuesta, pero los votos por texto siguen aceptando a cualquiera.
- **Una partida por grupo a la vez.** `!cancelar` la corta.
- **El buzón de Redis usa `BLPOP` con segundos enteros** para no depender de
  Redis >= 6 (el último segundo de cada ventana se sondea). Si Redis se cae, el
  mensaje afectado se pierde con un log de error en vez de tumbar el webhook:
  propagar no ayudaría, porque el `message_id` ya quedó deduplicado y el
  reintento de WAHA se descartaría igual.
- **SQLite corre en modo WAL** con `busy_timeout`, para que el webhook pueda
  registrar mensajes mientras la partida escribe su traza. Por eso el
  orquestador encola el mensaje *antes* de escribir el histórico: una escritura
  en contención no puede retrasar un voto hasta perder su turno.
- **Reiniciar el servicio corta las partidas en curso.** Los checkpoints del
  grafo quedan en disco para inspección, pero no se reanuda automáticamente:
  las ventanas de tiempo ya habrían expirado. Las partidas que quedaron a medias
  se marcan como `interrupted` al arrancar.
