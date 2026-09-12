# Contributing

Engineering rules for this repository. They are authoritative here and win over
any general habit.

🌍 **English** · [Español](CONTRIBUTING.es.md)

## Environment

```bash
make setup      # venv, dependencies and a .env to edit
```

By hand, if you prefer: `python3 -m venv .venv`,
`.venv/bin/pip install -r requirements-dev.txt`, `cp .env.example .env`.

Python 3.11 or later. Never commit a `.env`: `.gitignore` already covers it,
but check before `git add -A`.

## Before opening a pull request

```bash
make check      # ruff + the whole suite
```

The suite runs in parallel (`-n auto` in `pytest.ini`) and takes about twenty
seconds. `-n0` runs it serially, which is what you want to debug with pdb. A
new test may not wait real seconds: use `fast_timers()` and, if you measure a
duration, measure it to scale.

Both must pass clean. If a test fails, fix it or explain in the PR why it stays
red; do not skip it and do not delete it.

## Style

- 95-column lines, `ruff` with the configuration in `pyproject.toml`.
- **Comments and docstrings in Spanish. Code in English, with one exception:
  the game's own vocabulary.** The phases and roles of Werewolf keep their
  Spanish names (`noche_bruja`, `veredicto`, `Role.LOBO`) because that is
  what the graph, the docs and the players call them; renaming them would
  split the vocabulary in two. Inside those modules a local may follow suit
  (`vivos`, `recuento`). Everything outside `app/games/` is English.
- Player-facing text is not written in the code at all: it lives in the
  catalogues, in both languages (see *Where each thing goes*).
- Broad `except Exception` is legitimate **only at the edges** (WAHA, LLM,
  Redis, database) and always with `# noqa: BLE001` and a comment saying what
  degrades. Not in game logic.
- Type hints on public signatures. `from __future__ import annotations` in
  every module.
- Docstrings explain *why*, not *what*. If you need to describe what a function
  does, it probably needs a better name.

## Where each thing goes

- **A new game rule** → `app/games/werewolf/`, with its test in
  `tests/test_werewolf_rules.py`.
- **A new game** → `app/games/<game>/`, registered with `@register` and added
  to `BUILTIN_MODULES`. The walkthrough is in [`docs/new-game.md`](docs/new-game.md),
  and every game carries its own document in `docs/`.
- **A WAHA route** → `app/waha/client.py`. HTTP against WAHA happens nowhere
  else.
- **A field of a WAHA payload** → `app/waha/normalize.py`. The rest of the code
  only knows `InboundMessage`.
- **A configurable setting** → `app/config.py` **and** `.env.example`. Both, or
  nobody will know it exists.
- **Text a player reads** → the game's catalogue (`texts.py`), in **both**
  languages. Never a literal in a node: the suite checks that the Spanish and
  English catalogues have the same keys, and a hardcoded string bypasses that.
  What players write is different — the parsers accept both languages at all
  times, so new keywords go in with their counterpart.

## Non-negotiable rules

1. **The LLM never decides mechanics.** Who dies, who voted for whom and who
   wins is settled by deterministic rules. The model narrates and disambiguates
   colloquial language; nothing else. Everything that calls the LLM must work
   with `LLM_PROVIDER=none`.
2. **No node may leave the group muted.** If you open a new path in the graph,
   make sure all of its exits — errors and cancellations included — end up
   reopening the chat.
3. **No secrets in the group.** Roles go by private chat. If you add a group
   message, ask yourself what it leaks.
4. **The webhook does not block.** It enqueues and returns. If you need to wait
   for someone, wait inside the game's task, not in the HTTP handler.

## Tests

Tests run without WAHA, without Redis and without an LLM. `tests/conftest.py`
provides:

- `FakeTransport` — records what was sent to the group and to private chats.
- `ScriptedPlayers` — automatic players that read the bot's DMs and answer like
  people.
- `fast_timers()` — timings in milliseconds, to play complete games.

Prefer a test that **plays a game** over one that simulates the graph. When you
fix a bug, add the case that caught it before fixing it.

## Commits and branches

- A branch per change, never straight to the main branch.
- Messages in the imperative, and in Spanish: `añade el rol del Cazador`, not
  `añadido el rol del Cazador`.
- Subject of 72 characters at most; the body explains the *why* when it is not
  obvious.
- No tool, assistant or vendor attribution in commit messages, PRs or code
  comments.
- No emojis in pull request titles or descriptions.

## Documentation

In Markdown, referencing files by their repository-relative path
(`app/games/werewolf/nodes.py`), never by a path containing a home directory.

Documentation is bilingual: English under `docs/`, Spanish under `docs/es/`.
A change that touches one should touch its counterpart — a stale translation is
worse than no translation.
