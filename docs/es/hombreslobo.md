# El Hombre Lobo

Los Hombres Lobo de Castronegro, dirigido por el bot. Reparte los roles por
privado, silencia el grupo de noche, recoge las acciones ocultas, narra el
amanecer y gestiona la votación del día.

[English](../werewolf.md) · 🌍 **Español**

- **Comando**: `#juego hombreslobo` · alias `hombres lobo`, `hombre lobo`,
  `loboso`, `lobos`, `lobo`, `werewolf`, `castronegro`, `hl`
- **No necesita el sufijo `ia`**: la narración del modelo es parte de
  cómo se juega, así que lo declara en su ficha (`needs_llm=True`) y el
  orquestador le entrega el modelo sin que el máster lo pida. Eso sí,
  **tiene que haber modelo**: sin `LLM_API_KEY` el cliente nace apagado y
  la partida cae a los textos estáticos. Ver [La narración](#la-narración)
- **Jugadores**: 4 a 24

---

## Cómo se ve una partida

```
Máster  ›  #juego hombreslobo

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
           La niebla se retira y deja ver lo que pasó de noche…
           ☠️ @Caro fue devorado por los lobos — era 🧑‍🌾 Aldeano
           Quien haya caído ya no participa: ignorad lo que escriba.

           ⚖️ EL JUICIO — día 1
           Tenéis 1 min 30 s para acusaros.
           Sospechosos (5): …
           🔊 El chat está abierto.

Bot     ›  🗳️ [encuesta] ¿A quién linchamos?
Bot     ›  ⚖️ VEREDICTO — ☠️ Beto fue linchado — era 🐺 Hombre Lobo
Bot     ›  🎉 GANA EL PUEBLO
```

---

## Roles

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

### Condiciones de victoria

- **Pueblo**: no queda ningún lobo vivo.
- **Lobos**: los lobos igualan o superan en número al resto.
- **Enamorados**: sobreviven sólo los dos enamorados y son de bandos opuestos
  (se comprueba antes que la de los lobos, que si no se la comería).
- **Tablas**: no queda nadie, o se alcanza `MAX_ROUNDS`.

---

## El grafo

Es el único juego que usa LangGraph, y lo usa porque tiene lo que justifica un
grafo: fases cíclicas y estado compartido. Cada fase es un nodo; el ciclo se
rompe cuando `evaluar` encuentra una condición de victoria.

| Nodo | Fase |
|---|---|
| `reclutamiento` | Abre la convocatoria y registra a quien se apunta |
| `reparto` | Silencia el grupo, asigna roles y los manda por privado |
| `noche_inicio` | Narra la noche y recoge lobos, vidente y Cupido en paralelo |
| `noche_bruja` | Le dice a quién atacaron y recoge su decisión |
| `resolucion` | Cruza ataque, curación y veneno; resuelve cadenas de muerte |
| `amanecer` | Resuelve la noche y reabre el grupo; no publica todavía |
| `evaluar` | Comprueba la victoria y enruta |
| `debate` | Cuenta el amanecer, abre el juicio, escucha y lo comenta |
| `votacion` | Publica la encuesta y recoge los votos |
| `veredicto` | Lincha al más votado y revela su rol |
| `final` | Narra el desenlace y revela todos los roles |

Los nodos viven una fase por módulo en `app/games/werewolf/nodes/`:
`recruitment.py` (los dos primeros), `night.py` (los cuatro de la noche),
`day.py` (amanecer, juicio y votación) y `ending.py` (evaluación, veredicto
y cierre). `base.py` guarda lo que todos comparten —tiempos, envíos,
ambientación de la espera— y `WerewolfNodes` es sólo la composición de las
cuatro fases.

La bruja tiene su propio nodo porque **necesita saber a quién atacaron los
lobos**: su ventana se abre después de la de ellos, no en paralelo.

El amanecer no publica nada: lo que la aldea encuentra al despertar y lo que
hace a continuación son la misma escena y salen en **un solo mensaje**, el
que abre el juicio. Así se narra una vez, se etiqueta a cada persona una vez
y se lista a los vivos una vez. Entre los dos nodos la deuda queda marcada en
`dawn_pending`, y si la partida termina en ese amanecer la paga `final`, que
publica las muertes antes del desenlace.

---

## La narración

La escribe el modelo siempre que haya uno configurado; si no lo hay, salen
los textos estáticos de `app/games/werewolf/prompts.py`. La partida es
idéntica en las dos: **el modelo pone ambientación, el código pone la
mecánica**. Quién muere, quién vota a quién y quién gana no pasa nunca por
el modelo.

### El narrador escucha el juicio

Durante el debate el bot no espera a ciegas: recoge lo que se habla en el
grupo y se lo pasa al narrador, que comenta las acusaciones reales en vez de
rellenar con niebla genérica.

```
Ana señala a Beto con un dedo tembloroso, y Beto escupe el nombre de
Caro como quien arroja una piedra al pozo. Caro retrocede.
```

Lo hablado no muere ahí: la noche siguiente, la apertura del juicio siguiente,
el día sin linchamiento y el cierre de la partida reciben también las últimas
voces, así que la partida arrastra de qué se venía hablando.

Tres cautelas, porque el narrador pasa a leer lo que escribe la gente:

- **Sólo mensajes del grupo.** Los privados llevan roles y acciones de noche.
- **Sólo de jugadores vivos.** Al pueblo se le pide ignorar a quien cayó, y
  escucharlo filtraría que sigue jugando.
- **Acotado por presupuesto de caracteres.** Un debate normal entra completo;
  el tope está para que una avalancha en un grupo grande no dispare coste y
  latencia justo cuando la partida tiene que responder rápido.

El comentario entra por actividad y no sólo por reloj: con un par de
intervenciones nuevas ya se comenta, con un suelo entre comentarios para que
un grupo muy hablador no acabe leyendo más bot que vecinos.

### Lo que el modelo deja a medias no se publica

Un modelo que razona escribe a veces en el mismo campo que la respuesta: su
repaso en inglés, la numeración de las palabras que lleva escritas, o se
queda sin cupo de tokens a media palabra porque pensar gasta del mismo
presupuesto. Eso llegó a salir al grupo.

Ahora hay tres defensas, de fuera adentro:

- `LLM_REASONING_EFFORT` acota lo que el modelo piensa antes de escribir, que
  es lo que dejaba la escena sin cupo.
- Cada escena pide un cupo de salida holgado, proporcional a su límite de
  palabras.
- `app/games/werewolf/narrator.py` descarta lo impublicable —andamiaje,
  arranques a media frase, finales cortados— y devuelve `None`. El narrador
  reintenta una vez y, si tampoco, publica el texto estático. Una escena de
  respaldo se lee bien; media escena, no.

Al modelo se le dice explícitamente que eso son **rumores**: puede recoger el
tono y quién señala a quién, nunca confirmarlo. El prompt además le prohíbe
obedecer instrucciones que vengan dentro de esos mensajes, y los HECHOS de esa
escena no contienen ningún rol — la protección que vale no es pedirle que se
resista, es no entregarle lo que no debe salir.

### El cierre nocturno

Silenciar el grupo de noche requiere que el bot sea **administrador**. Antes de
intentarlo se comprueba; si no lo es, no se intenta y la partida sigue: el
silencio pasa a ser una convención social en vez de una restricción técnica.
Con `MANAGE_GROUP_PERMISSIONS=false` ni se comprueba.

---

## Ajustes

| Variable | Por defecto | Para qué |
|---|---|---|
| `RECRUIT_SECONDS` | `30` | Ventana de inscripciones |
| `NIGHT_ACTION_SECONDS` | `60` | Ventana de las acciones nocturnas |
| `WITCH_ACTION_SECONDS` | `45` | Ventana de la bruja, que va después |
| `HUNTER_ACTION_SECONDS` | `40` | Ventana del cazador al morir |
| `DEBATE_SECONDS` | `90` | Duración del juicio |
| `VOTE_SECONDS` | `30` | Duración de la votación |
| `FILLER_INTERVAL_SECONDS` | `25` | Cadencia de la ambientación de espera |
| `MAX_ROUNDS` | `20` | Cierra una partida abandonada |
| `WEREWOLF_MIN_PLAYERS` | `4` | Mínimo para arrancar |
| `WEREWOLF_MAX_PLAYERS` | `24` | Máximo que entra al reparto |
| `WEREWOLF_TIE_BREAK` | `none` | `none` = un empate no lincha; `random` = decide el azar |
| `WEREWOLF_REVEAL_ROLE_ON_DEATH` | `true` | Revelar el rol de quien muere de noche |
| `MANAGE_GROUP_PERMISSIONS` | `true` | Silenciar el grupo de noche |
| `GAME_LANGUAGE` | `es` | Idioma en que se escribe todo: `es` o `en` |

---

## Límites conocidos

- **Si los lobos no responden, no hay ataque.** Se narra como que no se
  pusieron de acuerdo. Se prefirió eso a matar a alguien al azar: el día
  siempre avanza por linchamiento, así que la partida no se estanca.
- **Las encuestas de WhatsApp admiten 12 opciones.** Con mesas más grandes se
  recorta la encuesta, pero los votos por texto siguen aceptando a cualquiera.
- **Una partida por grupo a la vez.** `#cancelar` la corta.
- **Reiniciar el servicio corta las partidas en curso.** Los checkpoints del
  grafo quedan en disco para inspección, pero no se reanuda: las ventanas de
  tiempo ya habrían expirado. Las partidas a medias se marcan `interrupted`.

---

Para el código: [`docs/es/arquitectura.md`](arquitectura.md).
