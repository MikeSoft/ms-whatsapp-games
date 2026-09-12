"""Lo que el concurso le escribe a la gente, en los dos idiomas.

El cuestionario lo escribe el modelo y sale en el idioma que se le pida (ver
:mod:`app.games.kahoot.questions`); esto es el marco: anuncios, avisos,
respuestas correctas y clasificación.
"""

from __future__ import annotations

from app.i18n import Catalogue

TEXTS: Catalogue = {
    "es": {
        "topic.default": "cultura general",
        "announce": (
            "🧠 *CONCURSO DE PREGUNTAS*\n\n"
            "Tema: *{topic}*\n"
            "{questions} preguntas · {options} opciones · {seconds:g} segundos cada una"
        ),
        "no_questions": (
            "😕 No he podido preparar las preguntas. Volvé a intentarlo, o "
            "pedime otro tema."
        ),
        "fallback_bank": (
            "⚠️ No pude generar preguntas del tema pedido, así que van "
            "preguntas de cultura general del repertorio de siempre."
        ),
        "poll_failed": "⚠️ No pude publicar la pregunta {number}. Sigo con la siguiente.",
        "unlocked_after_silence": (
            "🔊 Nadie pudo responder la primera pregunta, así que reabro el chat "
            "por si el silencio lo estaba impidiendo. Seguimos."
        ),
        "answers.header": "📖 *RESPUESTAS CORRECTAS*",
        "answers.continued": "📖 *(sigue)*",
        "ranking.empty": "🏁 *RESULTADOS*\n\nNo respondió nadie. Otra vez será.",
        "ranking.header": "🏆 *CLASIFICACIÓN*",
        "ranking.row": "{mark} {name} — {hits}/{total}",
        "ranking.winner": "\n👑 Gana {name}.",
        "ranking.tie": "\n👑 Empate en lo más alto: {names}.",
        "cancelled": "🛑 *Concurso cancelado por el máster.* El chat queda abierto.",
        "summary": "{questions} preguntas, {players} participantes.",
        "summary.winner": " Ganó {name}.",
        "summary.no_hits": " Sin aciertos.",
    },
    "en": {
        "topic.default": "general knowledge",
        "announce": (
            "🧠 *QUIZ*\n\n"
            "Topic: *{topic}*\n"
            "{questions} questions · {options} options · {seconds:g} seconds each"
        ),
        "no_questions": (
            "😕 I could not put the questions together. Try again, or ask me for "
            "another topic."
        ),
        "fallback_bank": (
            "⚠️ I could not generate questions on the topic you asked for, so here "
            "come general knowledge ones from the usual set."
        ),
        "poll_failed": "⚠️ I could not publish question {number}. Moving on to the next.",
        "unlocked_after_silence": (
            "🔊 Nobody could answer the first question, so I am reopening the chat "
            "in case the silence was getting in the way. Carrying on."
        ),
        "answers.header": "📖 *CORRECT ANSWERS*",
        "answers.continued": "📖 *(continued)*",
        "ranking.empty": "🏁 *RESULTS*\n\nNobody answered. Next time.",
        "ranking.header": "🏆 *LEADERBOARD*",
        "ranking.row": "{mark} {name} — {hits}/{total}",
        "ranking.winner": "\n👑 {name} wins.",
        "ranking.tie": "\n👑 A tie at the top: {names}.",
        "cancelled": "🛑 *Quiz cancelled by the master.* The chat stays open.",
        "summary": "{questions} questions, {players} players.",
        "summary.winner": " {name} won.",
        "summary.no_hits": " No correct answers.",
    },
}
