# ms-whatsapp-games

> A game master for text games played in WhatsApp groups.

[![CI](https://github.com/MikeSoft/ms-whatsapp-games/actions/workflows/ci.yml/badge.svg)](https://github.com/MikeSoft/ms-whatsapp-games/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

🌍 **English** · [Español](README.es.md)

A FastAPI microservice that runs social games in a WhatsApp group. It takes
events from a [WAHA](https://waha.devlike.pro/) gateway, runs the game in the
group and talks to each player in their own private chat — secret roles go
where they belong, and the group only ever sees what everyone is allowed to
see.

The game rules are deterministic Python. A language model, when configured,
writes the atmosphere and reads colloquial answers; it never decides who dies,
who wins or whose vote counts. Everything plays fine with `LLM_PROVIDER=none`.

| Game | Command | What it is |
|---|---|---|
| [🐺 Werewolf](docs/werewolf.md) | `#juego hombreslobo` | Wolves feed at night, the village lynches by day. Roles by DM, group muted at night, public vote by day |
| [🧠 Quiz](docs/quiz.md) | `#juego kahoot <topic>` | A race-the-clock quiz. The model writes the questions and it is played through WhatsApp polls |

> [!NOTE]
> The games are played **in Spanish** — that is what the bot writes in the
> group, and the commands are Spanish words. The code, its identifiers and this
> documentation are in English.

```
Master  ›  #juego hombreslobo

Bot     ›  🌫️ A thick fog rolls down from the mountain…
           🐺 WEREWOLF — sign-ups are open.
           Type YO in the next 30 seconds to join.

Ana     ›  Yo
Beto    ›  me apunto

Bot     ›  🎭 6 players are in
           🔇 The group goes quiet. Check your private chat.

(DM to Beto)  🐺 You are a Werewolf. You hunt tonight…
(DM to Ana)   🔮 You are the Seer. Each night you may ask me…
```

---

## Table of contents

- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Using an existing WAHA](#using-an-existing-waha)
- [Commands](#commands)
- [How it fits together](#how-it-fits-together)
- [Configuration](#configuration)
- [Mentions, not names](#mentions-not-names)
- [Documentation](#documentation)
- [Known limits](#known-limits)
- [Contributing](#contributing)
- [License](#license)

---

## Requirements

- Docker and Docker Compose (or Python 3.11+ to run it directly)
- A WhatsApp number for the bot, bound to a [WAHA](https://waha.devlike.pro/)
  session — either the one in this stack or one you already run
- Optionally, an API key for a model that speaks the OpenAI dialect. Without
  one the games still run, with static narration and a built-in question bank

## Quick start

```bash
git clone https://github.com/MikeSoft/ms-whatsapp-games.git
cd ms-whatsapp-games
cp .env.example .env
```

Edit `.env` and set, at minimum, who is allowed to give orders:

```env
MANAGER_NUMBER=+573001234567     # several, comma separated
LLM_API_KEY=...                  # without a key: static narration, and the
                                 # quiz falls back to its question bank
```

Bring up the stack — the service, Redis for the ephemeral mailboxes and WAHA as
the gateway — and bind the bot's number:

```bash
docker compose --profile waha up -d --build --wait
docker compose logs -f api
```

`--wait` returns once the service reports healthy, not when the container
starts. A normal deploy — one that does not touch `requirements.txt` —
rebuilds nothing: dependencies live in their own layer and are only
reinstalled when they change, with the pip cache mounted so nothing already
downloaded is fetched again.

Without `--profile waha` only the service and Redis come up, which is what you
want if you already have a gateway — see [below](#using-an-existing-waha).

1. Open `http://localhost:3000` and start the WAHA session.
2. Scan the QR code with the phone that will act as the bot.
3. Add that number to the group you are going to play in. Make it an
   **administrator** if you want it to be able to mute the group; if it cannot,
   games are played just the same, without silence.

Check that it answers:

```bash
curl localhost:8000/health/ready
curl localhost:8000/games
```

And from a master's WhatsApp, inside the group:

```
#juegos
#juego hombreslobo
```

### Using an existing WAHA

Do not bring up the one in the stack: `docker compose up -d --build` leaves the
`waha` profile out. Point the application at yours, and its webhook back at the
application:

```env
WAHA_BASE_URL=http://192.168.0.250:3000
WAHA_SESSION=your-session-name
WAHA_WEBHOOK_HMAC_SECRET=<openssl rand -base64 32>
```

The webhook is configured **per session**. The environment variables of the
WAHA container only apply to the session this stack ships; on one that already
exists you register it with a `PUT`, which replaces the whole configuration —
include anything the session already had or you will lose it:

```bash
curl -X PUT http://192.168.0.250:3000/api/sessions/<session> \
  -H 'Content-Type: application/json' \
  -d '{"config": {"webhooks": [{
        "url": "http://<this-machine>:8000/webhooks/waha",
        "events": ["message", "poll.vote"],
        "hmac": {"key": "<the same secret as in .env>"},
        "retries": {"delaySeconds": 2, "attempts": 3, "policy": "linear"}
      }]}}'
```

Three things that cost an afternoon to find out:

- **WAHA resolves that URL, not you.** If WAHA runs in a container, `localhost`
  is its own container: use the host's IP, or the service name if they share a
  Docker network.
- The `PUT` restarts the session — back to `WORKING` in seconds and without
  scanning the QR again, but do not do it in the middle of a game.
- With the secret set, unsigned deliveries are rejected with a 401.
  `docker compose logs api | grep webhook` tells you whether they arrive signed.

---

## Commands

Only the numbers in `MANAGER_NUMBER` are obeyed.

| Command | What it does |
|---|---|
| `#juegos` | List the available games |
| `#juego <name>` | Start a game |
| `#juego <name> ia` | Same, with model-written narration |
| `#estado` | What is running right now |
| `#cancelar` | Cut the current game short and reopen the group |
| `#ayuda` | Remind you of the commands |

The prefix is configurable through `COMMAND_PREFIX`; `#` by default.

Case and accents are forgiving: `#Juego`, `#CATÁLOGO` and `#cancelar` all work.
Whatever is written **after the name** reaches the game as an instruction, so
`#juego kahoot 15 preguntas de cine` asks for fifteen questions about cinema.

**A game is played in the group it was asked for**, always. A private chat has
no group to infer, so only `#cancelar` is accepted there, and only when a
single game is alive.

Masters play too: if they type `Yo` during sign-ups, they join like anyone else.

---

## How it fits together

The webhook **enqueues** messages and returns; the game **collects** them
inside a time window, from a separate task. That is why WAHA gets its `200` in
milliseconds even when the village takes a minute to make up its mind, and why
a game does not hang because someone went for dinner.

Two rules explain the rest of the design:

- **The model never decides mechanics.** Who dies, who voted for whom and who
  wins is settled by deterministic rules. The model narrates and disambiguates
  colloquial language; nothing else. Everything that calls it must work with
  `LLM_PROVIDER=none`.
- **Every wait has a deadline**, so that no failure can leave the group muted,
  waiting for a night that never ends.

👉 The diagram, the module map, the contracts a game implements and the
concurrency numbers are in [`docs/architecture.md`](docs/architecture.md).
Failure paths are exercised in `tests/test_resiliencia.py`.

---

## Configuration

Every variable is documented in `.env.example`, and the per-game ones in each
game's document. The cross-cutting ones:

| Variable | Default | What for |
|---|---|---|
| `MANAGER_NUMBER` | *(empty)* | Numbers allowed to give orders, comma separated |
| `COMMAND_PREFIX` | `#` | Command prefix |
| `WAHA_BASE_URL` | `http://waha:3000` | Where WAHA lives |
| `WAHA_SESSION` | `default` | Which session to use |
| `WAHA_WEBHOOK_HMAC_SECRET` | *(empty)* | Webhook signature |
| `WAHA_DRY_RUN` | `false` | Log outbound messages instead of sending them |
| `USE_MENTIONS` | `true` | Tag contacts instead of just naming them |
| `LLM_PROVIDER` | `gemini` | `gemini`, `deepseek`, `openai` or `none` |
| `LLM_REASONING_EFFORT` | `low` | How much it thinks before writing; empty does not send it |
| `MESSAGE_RETENTION_DAYS` | `30` | History purge on startup |

### Redis and SQLite: what each is for

- **Redis** holds the ephemeral part: the mailboxes of the round in progress,
  with a TTL. If it does not answer, the service starts with an in-memory
  mailbox and says so in the log: you keep playing, but queues are lost on
  restart.
- **SQLite** holds what is worth keeping: the message history, the trace of
  each game and the graph checkpoints. It lives in the `./data` volume.

---

## Mentions, not names

Group messages **tag the contact** instead of writing their name: the body
carries the literal `@<id>` and the same contact travels as a JID in the
`mentions` array. WhatsApp crosses the two and shows a real, tappable mention —
the visible name is whatever each reader's device knows.

```
☠️ @Hugo is out of the game
No longer playing: ignore whatever they write.
```

It removes the ambiguity of namesakes and of people with no public name, and it
makes **who to ignore** unmistakable for the rest of the game. Private chats
keep using names, which read better one to one.

The `<id>` comes from the JID with the domain and the device suffix stripped,
and it is never converted between `@c.us` and `@lid`: everyone is mentioned in
the form the group addresses them by. With `USE_MENTIONS=false` it falls back
to plain names, for WAHA engines that do not resolve mentions.

---

## Documentation

| Document | What it covers |
|---|---|
| [`docs/werewolf.md`](docs/werewolf.md) | Roles, dealing, the phase graph, the narration and its limits |
| [`docs/quiz.md`](docs/quiz.md) | The command instruction, question generation and validation, scoring |
| [`docs/architecture.md`](docs/architecture.md) | Code map, the contracts a game implements, concurrency |
| [`docs/new-game.md`](docs/new-game.md) | Step by step for adding a game |
| [`docs/development.md`](docs/development.md) | Environment, the test suite and how to debug against WAHA |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | Branches, commits, style, where each thing goes |

The same documentation in Spanish lives in [`docs/es/`](docs/es/).

---

## Known limits

- **WAHA's routes change between versions and engines** (WEBJS / NOWEB / GOWS).
  They are concentrated in `app/waha/client.py`, and payload reading in
  `app/waha/normalize.py`: if a version differs, those two files are the only
  ones to touch.
- **Muting the group requires being an administrator**, and that is checked
  before trying. If the bot is not one, it does not try and the game goes on.
- **Restarting the service cuts games in progress.** Those left half-played are
  marked `interrupted` on startup.
- **The Redis mailbox uses `BLPOP` with whole seconds**, so as not to depend on
  Redis >= 6. If Redis goes down, the affected message is lost with an error in
  the log instead of taking the webhook with it.
- **SQLite runs in WAL mode** with a `busy_timeout`, so the webhook can record
  messages while the game writes its trace.

Per-game limits are in each game's document.

---

## Contributing

Pull requests are welcome. [`CONTRIBUTING.md`](CONTRIBUTING.md) is the
authoritative guide for this repository — branches, commit format, style, where
each kind of change belongs, and the two checks that must pass:

```bash
.venv/bin/ruff check app tests
.venv/bin/python -m pytest
```

Adding a game is a well-trodden path: [`docs/new-game.md`](docs/new-game.md)
walks through it end to end.

---

## License

[MIT](LICENSE).

Built on [WAHA](https://waha.devlike.pro/) for the WhatsApp gateway,
[FastAPI](https://fastapi.tiangolo.com/) for the service and
[LangGraph](https://langchain-ai.github.io/langgraph/) for the Werewolf state
machine.
