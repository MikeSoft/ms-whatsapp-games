# Architecture and code layout

> This document is the reference for **writing code**. To play, see the
> [README](../README.md); for each game's rules, [Werewolf](werewolf.md) and
> [the quiz](quiz.md).

🌍 **English** · [Español](es/arquitectura.md)

It explains **what lives in each module**, **where to write new code** and
**how to wire a game to the agent** so that it runs your logic.

If all you want is to add a game, go straight to
[Adding a new game](new-game.md).

---

## 1. The idea in one sentence

The webhook **enqueues** messages; the game's graph **collects** them inside a
time window. Nothing else waits for anything.

```
   WhatsApp
      │
      ▼
   ┌────────┐   webhook    ┌───────────────────────────────────────┐
   │  WAHA  │─────────────►│ POST /webhooks/waha                   │
   │        │              │  1. verify signature (HMAC / secret)  │
   │        │◄─────────────│  2. normalise the payload             │
   └────────┘  send/poll   │  3. Orchestrator.handle(message)      │
      ▲                    └──────────────────┬────────────────────┘
      │                                       ▼
      │                              ┌──────────────────┐
      │                              │   Orchestrator   │
      │                              └────────┬─────────┘
      │              master command?          │        player message?
      │               ┌──────────────────────┴───────────────────┐
      │               ▼                                          ▼
      │       launch / cancel / report                  ┌─────────────────┐
      │               │                                 │ Mailbox (Redis) │
      │               ▼                                 │  lists + TTL    │
      │      ┌─────────────────┐  collects with window   └────────┬────────┘
      └──────│  Game           │◄─────────────────────────────────┘
     Transport│  LangGraph      │
              └────────┬────────┘
                       ▼
          SQLite: history + checkpoints
```

**Why this way.** A turn-based game lasts minutes and depends on people
answering. If the webhook waited, WAHA would time out and retry, duplicating
messages. Decoupling through a mailbox gets WAHA its `200` in milliseconds even
when the village takes three minutes to vote.

---

## 2. Code map

### Edges: talking to the world

| Module | Responsibility | When to touch it |
|---|---|---|
| `app/api/routes.py` | Webhook, `/health`, `/health/ready`, `/games`, `/status` | A new endpoint |
| `app/api/security.py` | HMAC and shared-secret verification | Another signature scheme |
| `app/waha/client.py` | The **only** place that speaks HTTP to WAHA | A new WAHA capability |
| `app/waha/normalize.py` | WAHA payload → `InboundMessage` | A new WAHA field or event |
| `app/waha/models.py` | Inbound and outbound types | A new kind of message |

The rule: **the rest of the code knows nothing about WAHA**. It only sees
`InboundMessage` and the `Transport` protocol. If a WAHA version changes a
route or a field, those two files are the only ones touched.

### Infrastructure: remembering and waiting

| Module | Responsibility | When to touch it |
|---|---|---|
| `app/core/inbox.py` | Ephemeral mailboxes: `RedisInbox` (production) and `MemoryInbox` (tests) | Another queue backend |
| `app/core/db.py` | Message history and game trace in SQLite | A new table |
| `app/core/llm.py` | Model access with graceful degradation | Another provider |
| `app/core/checkpointer.py` | LangGraph checkpointer | Changing graph persistence |
| `app/config.py` | All environment configuration | A new setting (**and `.env.example`**) |
| `app/i18n.py` | The language catalogues: what the games say, in each language | A new language, or the shape of a catalogue |
| `app/logging_conf.py` | Structured logging | — |

### Orchestration: deciding what runs

| Module | Responsibility | When to touch it |
|---|---|---|
| `app/orchestrator/manager.py` | Routes messages, launches/cancels games, supervises | Changing a game's lifecycle |
| `app/orchestrator/commands.py` | Parsing `#juego`, `#cancelar`… | A new master command |

### Games: the logic you care about

| Module | Responsibility | When to touch it |
|---|---|---|
| `app/games/base.py` | The contract: `Game`, `GameSpec`, `GameContext`, `GameResult`, `Transport` | Widening what a game may ask for |
| `app/games/registry.py` | Registration, name and alias resolution | Registering a new game |
| `app/games/transport.py` | `Transport` over WAHA, bound to one group | — |
| `app/games/mentions.py` | Composes messages that tag contacts | — |
| `app/games/recruit.py` | Natural-language sign-ups (reusable) | Improving sign-up detection |
| `app/games/werewolf/` | [Werewolf](werewolf.md): LangGraph graph, roles, narrator | — |
| `app/games/kahoot/` | [The quiz](quiz.md): instruction, generation, arithmetic | — |
| `app/games/<game>/` | **Your game** | This is where you write |

The two bundled games are deliberately different inside: Werewolf uses
LangGraph because it has cyclic phases and shared state, and the quiz is a
plain loop, because a batch of questions does not justify a graph. Using
LangGraph is optional.

---

## 3. The four contracts a game has to know

Everything a game needs arrives in a `GameContext`. There is no global state
and no cross-imports between games.

### `transport` — speaking

```python
await ctx.transport.send_group("🌙 Cae la noche")           # to the group
await ctx.transport.send_group(texto, mentions=[jid, ...])  # tagging
await ctx.transport.send_direct(jid, "Tu rol es…")          # private
poll_id = await ctx.transport.send_poll("¿Quién?", ["1. Ana", "2. Beto"])
await ctx.transport.delete_group_message(poll_id)           # withdraw it
nombre = await ctx.transport.contact_name(jid)              # None if unknown
await ctx.transport.set_group_locked(True)                  # mute
```

No send raises on a WhatsApp failure: the game must carry on with whatever it
gets back. **A DM that does not arrive means that player does not act, not that
the game breaks.**

Mind what each one returns:

| Method | Returns | The odd case |
|---|---|---|
| `send_poll` | the poll id, or `None` | It can be an **empty string**: the poll went out but WAHA gave no id, so it cannot be withdrawn later |
| `delete_group_message` | `bool` | — |
| `set_group_locked` | `bool` | `False` also when the bot is not an administrator |
| `contact_name` | the name, or `None` | A poll vote carries no name, and in newer groups the voter arrives as an `@lid` |

To tag, compose the text with `GroupText` and pass it the accumulated list:

```python
from app.games.mentions import GroupText

texto = GroupText(enabled=ctx.settings.use_mentions)   # one per message
cuerpo = f"☠️ {texto.tag(jid, nombre)} ha caído"
await ctx.transport.send_group(cuerpo, mentions=texto.mentions)
```

WhatsApp only resolves the tokens that come in the array, so the composer is
created **per message**: its mentions have to match that text.

### `inbox` — listening with a deadline

```python
mensajes = await ctx.inbox.collect(
    ctx.session_id,
    timeout=30,                 # the window, in seconds
    group=True,                 # listen to the group
    direct=[jid1, jid2],        # and/or these players' private chats
    stop_when=lambda recogidos: ...,   # cut short once everything is in
)
await ctx.inbox.clear(ctx.session_id, keys=["group"])   # drop the old stuff
```

`collect` **always** returns when the deadline expires, with whatever it
gathered. That is what keeps a game from hanging because someone went for
dinner.

`stop_when` must require an **interpretable** answer, not the mere presence of
a message: if someone writes "ok" and then their choice, cutting at the "ok"
would lose the real answer. See `WerewolfNodes._stop_when_resolved`.

Clear the mailbox **before** asking for something new, never after a command:
messages that arrive between the command and your node are valid.

### `llm` — narrating, never deciding

```python
texto = await ctx.llm.complete(sistema, usuario)       # None if there is no LLM
datos = await ctx.llm.complete_json(sistema, usuario)  # None if it fails
```

**It is never critical.** It returns `None` if there is no key, if it times out
or if the provider fails. Everything that calls the LLM has to work with
`LLM_PROVIDER=none`.

And a structural security rule: **do not hand secrets to the model**. In
Werewolf the narrator never receives the role assignments, so not even an
injection through someone's WhatsApp display name could leak them — they are
not in its context. Pass it only the public facts it needs.

### `store` and `record` — leaving a trace

```python
await ctx.record("noche.acciones", round_no=2, phase="noche",
                 detail={...}, is_secret=True)
```

Optional (`ctx.store` can be `None` in tests). `is_secret=True` marks what
should not show up in a public summary.

---

## 4. Adding a new game

It has its own document: [Adding a new game](new-game.md). The package, the
minimal class, registration and how to wire it to the LangGraph agent.

---

## 5. Non-negotiable rules

These come from real bugs found while testing the system.

1. **The LLM does not decide mechanics.** Who dies, who voted for whom and who
   wins is settled by rules in your `parsing.py`. The model narrates and
   disambiguates colloquial language. Everything must work with
   `LLM_PROVIDER=none`.

2. **No path may leave the group muted.** If you open a new branch, make sure
   all of its exits — errors and cancellations included — end in
   `set_group_locked(False)`.

3. **No secrets in the group or in the prompt.** Before adding a group message,
   ask yourself what it leaks. And do not pass the role assignment to the LLM.

4. **Targets are resolved against living players.** List numbers are not
   recycled, so resolving against the full list lets someone point at a corpse
   and waste their turn.

5. **When in doubt, the resource is not spent.** An ambiguous message consumes
   no potion, no shot and no vote.

6. **Every wait has a deadline.** No `collect` without a `timeout`, no mass
   send without a budget.

7. **A background task cannot take the game down.** Narrative filler and
   reminders catch their own errors and carry on.

8. **The webhook does not block.** It enqueues and returns. If you need to
   wait, wait inside the game's task.

---

## 6. Concurrency and performance

Measured in this environment (500 operations, file-backed SQLite with WAL):

| Path | Throughput |
|---|---|
| `Store.log_inbound` sequential | ~920 ops/s |
| `Store.log_inbound` concurrent | ~430 ops/s |
| `MemoryInbox.push` | ~76,000 ops/s |
| `RedisInbox.push` (fakeredis) | ~2,300 ops/s |
| `parse_player_reference` (24 players) | ~168,000 ops/s |

A frantic game generates on the order of 10 messages per second, so there are
two orders of magnitude of headroom. What matters is not the ceiling but that
nothing serialises where it should not:

- **SQLite in WAL** (`journal_mode=WAL`, `busy_timeout`, `synchronous=NORMAL`):
  the webhook records messages while the game writes its trace. Without WAL
  that mix gives `database is locked`.
- **A 2-connection SQLite pool with no overflow.** SQLite allows a single
  writer: with the default pool (5 + 10) connections fight over the lock and
  each one adds an aiosqlite thread. Measured, 2 performs ~25% better with half
  the threads.
- **Enqueueing comes before recording.** `Orchestrator.handle` pushes the
  message into the mailbox *before* writing history: a vote has a window of
  seconds, and a contended write can take up to `busy_timeout`.
- **A blocking, bounded Redis pool.** redis-py's default pool *raises*
  `"Too many connections"` when exhausted, and here that is a lost vote.
  `BlockingConnectionPool` queues until a connection frees up.
- **`BLPOP` with whole seconds.** Fractional timeouts require Redis ≥ 6; the
  last second of each window is polled.
- **Sends are serialised** with a minimum interval (WhatsApp punishes bursts),
  which is also why they retry less than queries do: insisting on one message
  delays every other one.

---

## 7. How it is tested

The test doubles, what each file of the suite covers and the pattern of playing
a real game are in [Development](development.md), along with the environment
and debugging against WAHA.
