"""Lo que el orquestador le contesta al máster, en los dos idiomas.

Los comandos en sí no se traducen: `#juego`, `#cancelar` y compañía ya aceptan
formas en inglés como alias (ver :mod:`app.orchestrator.commands`), así que un
máster angloparlante escribe `#game werewolf` y funciona. Lo que cambia con el
idioma es la respuesta.
"""

from __future__ import annotations

from app.i18n import Catalogue

TEXTS: Catalogue = {
    "es": {
        "help": (
            "🎮 *Comandos del máster*\n\n"
            "`{prefix}juegos` — lista los juegos disponibles\n"
            "`{prefix}juego <nombre>` — inicia una partida\n"
            "`{prefix}juego <nombre> ia` — con narración generada por el modelo\n"
            "`{prefix}estado` — qué hay en marcha ahora\n"
            "`{prefix}cancelar` — corta la partida en curso\n"
            "`{prefix}ayuda` — este mensaje"
        ),
        "catalogue.empty": "No hay juegos registrados.",
        "catalogue.header": "🎲 *Juegos disponibles*",
        "catalogue.launch": "  Lanzar con: `{prefix}juego {key}`",
        "players.range": "{minimum}-{maximum} jugadores",
        "status.idle": (
            "💤 No hay ninguna partida en marcha.\nLanza una con `{prefix}juegos`."
        ),
        "status.header": "🎯 *Partidas en marcha*",
        "status.row": "*{title}* en {group}\n  sesión: `{session}`\n  lleva {seconds} s",
        "cancel.done": "🛑 Partida cancelada.",
        "cancel.nothing": "No había ninguna partida que cancelar.",
        "start.which": (
            "Dime qué juego. Por ejemplo:\n`{prefix}juego hombreslobo`\n\n"
            "Ver todos: `{prefix}juegos`"
        ),
        "start.unknown": "No conozco el juego «{name}».\nDisponibles: {available}",
        "start.no_group": (
            "No sé en qué grupo jugar: la partida se juega donde se pide. "
            "Manda el comando dentro del grupo."
        ),
        "start.busy": (
            "Ya hay una partida de *{title}* en marcha en ese grupo. "
            "Usa `{prefix}cancelar` primero."
        ),
        "start.launched": "✅ Lanzando *{title}* en el grupo.\n{narration}\nSesión: `{session}`",
        "narration.model": "🧠 narración generada",
        "narration.static": "📜 narración estática",
        "game.failed": (
            "⚠️ La partida `{session}` falló: {error}\n"
            "El grupo se ha reabierto por si quedó silenciado."
        ),
    },
    "en": {
        "help": (
            "🎮 *Master commands*\n\n"
            "`{prefix}games` — list the available games\n"
            "`{prefix}game <name>` — start a game\n"
            "`{prefix}game <name> ai` — with model-written narration\n"
            "`{prefix}status` — what is running right now\n"
            "`{prefix}cancel` — cut the current game short\n"
            "`{prefix}help` — this message"
        ),
        "catalogue.empty": "No games are registered.",
        "catalogue.header": "🎲 *Available games*",
        "catalogue.launch": "  Start with: `{prefix}game {key}`",
        "players.range": "{minimum}-{maximum} players",
        "status.idle": "💤 No game is running.\nStart one with `{prefix}games`.",
        "status.header": "🎯 *Games in progress*",
        "status.row": "*{title}* in {group}\n  session: `{session}`\n  running for {seconds} s",
        "cancel.done": "🛑 Game cancelled.",
        "cancel.nothing": "There was no game to cancel.",
        "start.which": (
            "Tell me which game. For example:\n`{prefix}game werewolf`\n\n"
            "See them all: `{prefix}games`"
        ),
        "start.unknown": "I do not know the game “{name}”.\nAvailable: {available}",
        "start.no_group": (
            "I do not know which group to play in: a game is played where it is "
            "asked for. Send the command inside the group."
        ),
        "start.busy": (
            "There is already a game of *{title}* running in that group. "
            "Use `{prefix}cancel` first."
        ),
        "start.launched": "✅ Starting *{title}* in the group.\n{narration}\nSession: `{session}`",
        "narration.model": "🧠 model narration",
        "narration.static": "📜 static narration",
        "game.failed": (
            "⚠️ Game `{session}` failed: {error}\n"
            "The group has been reopened in case it was left muted."
        ),
    },
}
