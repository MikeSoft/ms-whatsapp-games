# Werewolf

The Werewolves of Castronegro, refereed by the bot. It deals roles by private
chat, mutes the group at night, collects the hidden actions, narrates the dawn
and runs the daytime vote.

🌍 **English** · [Español](es/hombreslobo.md)

- **Command**: `#juego hombreslobo` · aliases `hombres lobo`, `hombre lobo`,
  `loboso`, `lobos`, `lobo`, `werewolf`, `castronegro`, `hl`
- **No `ia` suffix needed**: model narration is part of how this game plays, so
  it declares that in its spec (`needs_llm=True`) and the orchestrator hands it
  the model without the master asking. There does have to **be** a model,
  though: without `LLM_API_KEY` the client is born switched off and the game
  falls back to static text. See [Narration](#narration)
- **Players**: 4 to 24

> [!NOTE]
> The transcript below is a game in Spanish, which is the default. With
> `GAME_LANGUAGE=en` every message, briefing and narrated scene comes out in
> English instead; the answers players type are understood in both languages
> either way.

---

## What a game looks like

```
Master  ›  #juego hombreslobo

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

(DM to Beto)  🐺 Tu rol es Hombre Lobo. Eres el único lobo…
(DM to Ana)   🔮 Tu rol es Vidente. Cada noche puedes preguntarme…

Bot     ›  🌙 NOCHE 1 — Se apagan los candiles uno por uno…
(DM)      🐺 ¿A quién devoráis?  1. Ana  3. Caro  4. Dani …
(DM)      🔮 ¿De quién quieres conocer la identidad?
(DM)      🧪 Esta noche los lobos atacaron a Caro. ¿Curar, veneno o nada?

Bot     ›  🌅 AMANECE EL DÍA 1
           La niebla se retira y deja ver lo que pasó de noche…
           ☠️ @Caro fue devorado por los lobos — era 🧑‍🌾 Aldeano
           Quien haya caído ya no participa: ignorad lo que escriba.

           ⚖️ EL JUICIO — día 1
           Tenéis 1 min 30 s para acusaros.
           Sospechosos (5): …
           🔊 El chat está abierto.

Bot     ›  🗳️ [poll] ¿A quién linchamos?
Bot     ›  ⚖️ VEREDICTO — ☠️ Beto fue linchado — era 🐺 Hombre Lobo
Bot     ›  🎉 GANA EL PUEBLO
```

---

## Roles

| Role | Acts at night | What it does |
|---|---|---|
| 🐺 Werewolf | yes | Picks the victim. With several wolves, majority decides |
| 🔮 Seer | yes | Asks about one player and is told whether they are a wolf |
| 🧪 Witch | yes | Two single-use potions: life (revives the victim) and death |
| 🏹 Hunter | on death | Takes someone to the grave with them |
| 🏹💘 Cupid | 1st night | Makes two players lovers: if one dies, so does the other |
| 🧑‍🌾 Villager | no | Nothing but instinct and rhetoric |

### The deal, by player count

Wolves grow in bands (1 up to 6 players, 2 up to 11, 3 up to 15, 4 up to 19,
then one more per 5) and special roles are added at thresholds: Seer from 4
players, Witch from 6, Hunter from 8, Cupid from 10.

Two invariants are checked in the tests for every table size: **the village
always starts in a strict majority** and **there is always at least one plain
villager** (without one there is nobody to deduce about).

### Win conditions

- **Village**: no wolf is left alive.
- **Wolves**: wolves equal or outnumber everyone else.
- **Lovers**: only the two lovers survive and they are from opposite sides
  (checked before the wolves' condition, which would otherwise swallow it).
- **Draw**: nobody is left, or `MAX_ROUNDS` is reached.

---

## The graph

It is the only game that uses LangGraph, and it uses it because it has what
justifies a graph: cyclic phases and shared state. Each phase is a node; the
cycle breaks when `evaluar` finds a win condition.

| Node | Phase |
|---|---|
| `reclutamiento` | Opens sign-ups and registers whoever joins |
| `reparto` | Mutes the group, assigns roles and sends them by DM |
| `noche_inicio` | Narrates the night and collects wolves, seer and Cupid in parallel |
| `noche_bruja` | Tells the witch who was attacked and collects her decision |
| `resolucion` | Crosses attack, healing and poison; resolves death chains |
| `amanecer` | Resolves the night and reopens the group; publishes nothing yet |
| `evaluar` | Checks for victory and routes |
| `debate` | Tells the dawn, opens the trial, listens and comments on it |
| `votacion` | Publishes the poll and collects votes |
| `veredicto` | Lynches the most voted and reveals their role |
| `final` | Narrates the ending and reveals every role |

The nodes live one phase per module under `app/games/werewolf/nodes/`:
`recruitment.py` (the first two), `night.py` (the four night ones),
`day.py` (dawn, trial, vote) and `ending.py` (evaluation, verdict, close).
`base.py` holds what they all share — timers, sending, the waiting-room
atmosphere — and `WerewolfNodes` is just the composition of the four.

The witch has her own node because she **needs to know who the wolves
attacked**: her window opens after theirs, not in parallel.

The dawn publishes nothing: what the village finds on waking and what it does
next are the same scene, and they go out in **a single message** — the one that
opens the trial. That way the story is told once, each person is tagged once
and the living are listed once. Between the two nodes the debt is flagged in
`dawn_pending`, and if the game ends on that dawn it is `final` that pays it,
publishing the deaths before the ending.

---

## Narration

The model writes it whenever one is configured; when there is none, the static
text in `app/games/werewolf/prompts.py` comes out instead. The game is
identical either way: **the model supplies atmosphere, the code supplies
mechanics**. Who dies, who voted for whom and who wins never passes through the
model.

### The narrator listens to the trial

During the debate the bot does not wait blindly: it collects what is said in
the group and passes it to the narrator, which comments on the real accusations
instead of filling the air with generic fog.

```
Ana señala a Beto con un dedo tembloroso, y Beto escupe el nombre de
Caro como quien arroja una piedra al pozo. Caro retrocede.
```

What was said does not die there: the following night, the opening of the next
trial, a day without a lynching and the end of the game all receive the latest
voices too, so the game carries forward what people were talking about.

Three cautions, since the narrator now reads what people write:

- **Group messages only.** Private chats carry roles and night actions.
- **Living players only.** The village is asked to ignore whoever fell, and
  listening to them would leak that they are still playing.
- **Bounded by a character budget.** A normal debate fits whole; the cap is
  there so that a flood in a big group cannot blow up cost and latency exactly
  when the game has to respond quickly.

Commenting is triggered by activity, not just by the clock: a couple of new
lines are enough, with a floor between comments so that a very chatty group
does not end up reading more bot than neighbours.

### What the model leaves half-finished is not published

A reasoning model sometimes writes into the same field as the answer: its
review in English, a running count of the words it has written, or it runs out
of token budget mid-word because thinking spends from the same allowance. That
did reach the group once.

There are now three defences, outermost first:

- `LLM_REASONING_EFFORT` caps how much the model thinks before writing, which
  is what left the scene without budget.
- Every scene asks for a generous output budget, proportional to its word
  limit.
- `app/games/werewolf/narrator.py` discards the unpublishable — scaffolding,
  openings that start mid-sentence, cut-off endings — and returns `None`. The
  narrator retries once and, failing that, publishes the static text. A fallback
  scene reads fine; half a scene does not.

The model is told explicitly that those lines are **rumours**: it may pick up
the tone and who points at whom, never confirm it. The prompt also forbids it
from obeying instructions that arrive inside those messages, and the FACTS for
that scene contain no roles at all — the protection that works is not asking it
to resist, it is not handing it what must not get out.

### Locking down the night

Muting the group at night requires the bot to be an **administrator**. That is
checked before trying; if it is not one, no attempt is made and the game goes
on: silence becomes a social convention rather than a technical restriction.
With `MANAGE_GROUP_PERMISSIONS=false` it is not even checked.

---

## Settings

| Variable | Default | What for |
|---|---|---|
| `RECRUIT_SECONDS` | `30` | Sign-up window |
| `NIGHT_ACTION_SECONDS` | `60` | Window for the night actions |
| `WITCH_ACTION_SECONDS` | `45` | The witch's window, which comes after |
| `HUNTER_ACTION_SECONDS` | `40` | The hunter's window on death |
| `DEBATE_SECONDS` | `90` | Length of the trial |
| `VOTE_SECONDS` | `30` | Length of the vote |
| `FILLER_INTERVAL_SECONDS` | `25` | Cadence of the waiting-room atmosphere |
| `MAX_ROUNDS` | `20` | Closes an abandoned game |
| `WEREWOLF_MIN_PLAYERS` | `4` | Minimum to start |
| `WEREWOLF_MAX_PLAYERS` | `24` | Maximum dealt into a game |
| `WEREWOLF_TIE_BREAK` | `none` | `none` = a tie lynches nobody; `random` = chance decides |
| `WEREWOLF_REVEAL_ROLE_ON_DEATH` | `true` | Reveal the role of whoever dies at night |
| `MANAGE_GROUP_PERMISSIONS` | `true` | Mute the group at night |
| `GAME_LANGUAGE` | `es` | Language everything is written in: `es` or `en` |

---

## Known limits

- **If the wolves do not answer, there is no attack.** It is narrated as them
  failing to agree. That was preferred over killing someone at random: the day
  always advances through a lynching, so the game does not stall.
- **WhatsApp polls take 12 options.** With bigger tables the poll is trimmed,
  but text votes still accept anyone.
- **One game per group at a time.** `#cancelar` cuts it short.
- **Restarting the service cuts games in progress.** The graph checkpoints stay
  on disk for inspection, but no game is resumed: its time windows would have
  expired anyway. Half-played games are marked `interrupted`.

---

For the code: [`docs/architecture.md`](architecture.md).
