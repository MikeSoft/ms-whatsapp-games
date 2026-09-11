# Development

> How to set up the environment, run the suite and debug against a real WAHA.
> The engineering rules — branches, commits, style, where each thing goes — are
> in [CONTRIBUTING](../CONTRIBUTING.md), which overrides this document.

🌍 **English** · [Español](es/desarrollo.md)

---

## Environment

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env
```

Python 3.11 or later.

```bash
.venv/bin/python -m pytest
.venv/bin/ruff check app tests
.venv/bin/uvicorn app.main:app --reload
```

The first two must pass clean before opening a pull request.

### If `python3 -m venv` does not work

On Debian and Ubuntu the `venv` module ships in a separate package and
`ensurepip` fails. If you cannot install it, the suite runs just as well inside
a container, without touching the machine:

```bash
docker run --rm -v "$PWD":/w -w /w -u "$(id -u):$(id -g)" \
  -e HOME=/tmp python:3.11-slim \
  sh -c 'pip install -q -r requirements-dev.txt && \
         ruff check app tests && python -m pytest'
```

---

## How it is tested

Tests run **without WAHA, without Redis and without an LLM**, and without
reading `.env` or the machine's environment variables: `tests/conftest.py`
shuts both doors, because with only one of them shut an `export
COMMAND_PREFIX=/` would still get in and turn the suite red without anyone
having touched code.

| File | What it covers |
|---|---|
| `tests/conftest.py` | Doubles: `FakeTransport`, `ScriptedPlayers`, `render_mentions` |
| `tests/test_werewolf_rules.py` | Deterministic rules: dealing, death chains, victory |
| `tests/test_orchestrator.py` | Command parsing, a game's lifecycle, routing |
| `tests/test_recruit.py` | Who signs up: "yo", "me apunto" and what does not count |
| `tests/test_werewolf_flow.py` | Complete games through the graph |
| `tests/test_adversarial.py` | Hostile input: garbage, dead players acting, injection |
| `tests/test_concurrencia.py` | Simultaneous actions, two games at once, pools |
| `tests/test_resiliencia.py` | WAHA down, slow transport, Redis going away |
| `tests/test_soak.py` | Many games with chaotic players + invariants |
| `tests/test_inbox.py` | Memory and Redis against the **same** cases |
| `tests/test_mentions.py` | Tagging contacts, and names written in prose |
| `tests/test_kahoot.py` | The instruction, question validation, whole rounds |
| `tests/test_kahoot_aritmetica.py` | Evaluating expressions and what gets rejected |
| `tests/test_waha_client.py` | Retries, degradation, the shape of the requests |
| `tests/test_api.py` | Endpoints, signature, routing |

For a new game, the pattern that works best is **playing a real game** with
automatic players rather than simulating the graph:

```python
async def test_mi_juego_termina(table):
    ctx, transport, inbox, script = table(6)
    game = MiJuego(ctx, timers=fast_timers())
    result = await game.run()
    assert result.status == "finished"
    assert transport.locked is False        # never leave the group muted
```

`ScriptedPlayers` reads the bot's DMs and answers like a person; it only knows
what it has been told, just like a real player. If your game sends other DMs,
extend it with the answers that fit.

And when you fix a bug, **add the case that caught it before fixing it**.

### The suite runs in parallel

`pytest.ini` carries `-n auto`: tests are spread across the machine's cores.
It is not a speed whim, it is the shape of this suite — it plays whole games,
and nearly all of its time is spent waiting on windows and retries, not
computing. Serially it takes about two minutes; in parallel, about twenty
seconds.

```bash
.venv/bin/python -m pytest          # in parallel, the normal case
.venv/bin/python -m pytest -n0      # serially: pdb, prints, an odd failure
```

Hence the other rule: **a test does not wait real seconds**. Timings live in
`fast_timers()`, WAHA retries do not sleep in the suite
(`waha_retry_backoff=0`), and anything measured to scale is scaled whole — the
cut-off of a mass send is checked in tenths, with the same proportion as in
production, and the real values are checked separately without sleeping them.

---

## Debugging against WhatsApp

Bringing the service up does not force you to burn a real session:

- `WAHA_DRY_RUN=true` writes outbound messages to the log instead of sending
  them. Everything else runs the same, so you can follow a whole game without
  the group receiving anything.
- `LLM_PROVIDER=none` leaves the model out and uses the static text. It is also
  the configuration the suite assumes.
- `COMMAND_PREFIX` lets you use a prefix other than production's if the bot
  shares a group with another one.

Three endpoints say what is going on without entering the container:

```bash
curl localhost:8000/health/ready    # redis, database, WAHA session, LLM
curl localhost:8000/status          # games in progress
curl localhost:8000/games           # registered games
```

And to see whether WAHA's deliveries are arriving, and signed:

```bash
docker compose logs -f api | grep webhook
```

A `webhook.rejected` with `falta la cabecera HMAC` means the secret is set in
`.env` but not in the WAHA session, or the other way round: see
[the setup](../README.md#using-an-existing-waha).

---

## What lives where

| Document | What it covers |
|---|---|
| [`architecture.md`](architecture.md) | Code map, a game's contracts, concurrency |
| [`new-game.md`](new-game.md) | Step by step for adding a game |
| [`../CONTRIBUTING.md`](../CONTRIBUTING.md) | Branches, commits, style, review |
