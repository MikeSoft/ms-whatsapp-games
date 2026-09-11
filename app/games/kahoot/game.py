"""El concurso de preguntas, de principio a fin.

No usa LangGraph: no hay fases cíclicas ni estado compartido que justifique
un grafo, sólo una tanda de preguntas una detrás de otra. El contrato de
:class:`~app.games.base.Game` da todo lo que hace falta.

    anuncio -> silencio -> [encuesta -> ventana -> retirar] x N -> resultados

Cada pregunta se retira al cerrarse su ventana para que no quede votable
después, y el buzón del grupo se vacía antes de abrir la siguiente para que
un voto que llegue tarde no cuente en la que viene.
"""

from __future__ import annotations

import asyncio
import random
from collections import defaultdict
from dataclasses import dataclass, field

from app.games.base import Game, GameResult, GameSpec
from app.games.kahoot.brief import Brief, parse_brief
from app.games.kahoot.questions import Question, generate
from app.games.mentions import GroupText
from app.games.registry import register
from app.logging_conf import get_logger
from app.waha.models import InboundMessage

log = get_logger("kahoot")

#: Respiro entre el cierre de una pregunta y la siguiente, para que el grupo
#: vea desaparecer la encuesta antes de que aparezca otra.
BREATHER_SECONDS = 1.5

#: Tope de líneas del resumen final en un solo mensaje. Con más preguntas se
#: parte, porque WhatsApp corta los mensajes muy largos.
SUMMARY_MAX_QUESTIONS = 15


@dataclass
class Scoreboard:
    """Quién acertó qué. Es la memoria de la partida."""

    #: JID -> número de aciertos.
    hits: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    #: JID -> nombre con el que mostrarlo. Vale ``None`` mientras no se sepa:
    #: un voto de encuesta no trae nombre y hay que ir a buscarlo.
    names: dict[str, str | None] = field(default_factory=dict)
    #: JID -> suma de posiciones de llegada en las preguntas que acertó.
    #:
    #: Es el desempate: quien acierta antes acumula menos. Se mide por el
    #: orden en que llegan los votos y no por reloj, porque el sello de
    #: tiempo de un voto lo pone el teléfono que vota y no hay forma de
    #: fiarse de que todos vayan en hora.
    speed: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    answered: set[str] = field(default_factory=set)

    def register(
        self, jid: str, name: str | None, *, correct: bool, position: int
    ) -> None:
        if self.names.get(jid) is None:
            self.names[jid] = name
        self.answered.add(jid)
        if correct:
            self.hits[jid] += 1
            self.speed[jid] += position

    def ranking(self) -> list[tuple[str, str, int, float]]:
        """(jid, nombre, aciertos, rapidez) de más a menos acertadas.

        A igualdad de aciertos gana quien los consiguió antes. Sólo cuentan
        las preguntas acertadas: comparar también las falladas premiaría a
        quien responde rápido cualquier cosa.
        """
        filas = [
            (jid, self.display(jid), self.hits.get(jid, 0), self.speed[jid])
            for jid in self.answered
        ]
        return sorted(filas, key=lambda fila: (-fila[2], fila[3], fila[1].casefold()))

    def display(self, jid: str) -> str:
        return self.names.get(jid) or jid.split("@", 1)[0]


@register
class KahootGame(Game):
    """Concurso de preguntas por encuestas de WhatsApp."""

    spec = GameSpec(
        key="kahoot",
        title="Kahoot",
        tagline="Preguntas contrarreloj: gana quien más acierte.",
        aliases=("preguntas", "trivia", "quiz", "concurso", "cultura general"),
        min_players=1,
        max_players=200,
        # Las preguntas las escribe el modelo, así que no se le pide al máster
        # que lo active con "ia": sin modelo el juego no es lo que promete.
        needs_llm=True,
        how_to=(
            "El máster pide un tema y el modelo escribe las preguntas. Se "
            "publican de una en una como encuesta, con unos segundos para "
            "responder; al cerrarse, la encuesta se retira. Al final se "
            "publican las respuestas correctas y la clasificación."
        ),
    )

    def __init__(
        self,
        ctx,
        *,
        rng: random.Random | None = None,
        brief: Brief | None = None,
        breather: float = BREATHER_SECONDS,
    ) -> None:
        super().__init__(ctx)
        self.rng = rng or random.Random()
        # Inyectables para los tests, que juegan tandas enteras en
        # milisegundos en vez de esperar los segundos de una partida real.
        self._brief = brief
        self._breather = breather
        self.board = Scoreboard()
        #: Disponible tras :meth:`run` para inspección y para los tests.
        self.questions: list[Question] = []

    # ------------------------------------------------------------------ run
    async def run(self) -> GameResult:
        brief = self._brief or parse_brief(" ".join(self.ctx.args), self.ctx.settings)
        await self._announce(brief)

        self.questions, generadas = await generate(self.ctx.llm, brief, rng=self.rng)
        if not self.questions:
            await self.ctx.transport.send_group(
                "😕 No he podido preparar las preguntas. Volvé a intentarlo, o "
                "pedime otro tema."
            )
            return GameResult(status="aborted", error="sin preguntas utilizables")

        if not generadas:
            await self.ctx.transport.send_group(
                "⚠️ No pude generar preguntas del tema pedido, así que van "
                "preguntas de cultura general del repertorio de siempre."
            )

        locked = await self._lock(brief)
        try:
            await self._run_questions(brief, locked)
        finally:
            # Pase lo que pase, el grupo se reabre: dejarlo mudo sería peor
            # que cualquier fallo del concurso.
            if locked:
                await self.ctx.transport.set_group_locked(False)

        await self._publish_results()
        return self._result()

    # ------------------------------------------------------------- fases
    async def _announce(self, brief: Brief) -> None:
        await self.ctx.transport.send_group(
            "🧠 *CONCURSO DE PREGUNTAS*\n\n"
            f"Tema: *{brief.topic_or_default}*\n"
            f"{brief.questions} preguntas · {brief.options} opciones · "
            f"{brief.seconds:g} segundos cada una\n\n"
            "Se responde tocando la encuesta. Cada una se cierra y desaparece "
            "al acabarse el tiempo.\n"
            "Preparando las preguntas…"
        )

    async def _lock(self, brief: Brief) -> bool:
        if not (self.ctx.settings.kahoot_lock_group and brief.questions):
            return False
        return await self.ctx.transport.set_group_locked(True)

    async def _run_questions(self, brief: Brief, locked: bool) -> None:
        total = len(self.questions)
        for numero, question in enumerate(self.questions, start=1):
            # El buzón se vacía antes de publicar: lo que se escribió entre
            # una pregunta y otra no es respuesta de la que viene.
            await self.ctx.inbox.clear(self.ctx.session_id, keys=["group"])

            poll_id = await self.ctx.transport.send_poll(
                f"{numero}/{total} · {question.text}", list(question.options)
            )
            if poll_id is None:
                log.warning("kahoot.poll_failed", numero=numero)
                await self.ctx.transport.send_group(
                    f"⚠️ No pude publicar la pregunta {numero}. Sigo con la siguiente."
                )
                continue

            votos = await self.ctx.inbox.collect(
                self.ctx.session_id, timeout=brief.seconds, group=True
            )
            respondieron = self._score(question, votos, poll_id=poll_id)

            if poll_id:
                await self.ctx.transport.delete_group_message(poll_id)

            await self.ctx.record(
                "kahoot.pregunta",
                round_no=numero,
                phase="pregunta",
                detail={"respuestas": respondieron, "correcta": question.answer},
            )

            if numero == 1 and locked and not respondieron:
                # Nadie pudo votar la primera. La sospecha razonable es que el
                # grupo en modo "sólo administradores" también bloquee las
                # encuestas, así que se reabre y se avisa en vez de jugar diez
                # preguntas en el vacío.
                locked = False
                await self.ctx.transport.set_group_locked(False)
                await self.ctx.transport.send_group(
                    "🔊 Nadie pudo responder la primera pregunta, así que "
                    "reabro el chat por si el silencio lo estaba impidiendo. "
                    "Seguimos."
                )
                log.warning("kahoot.unlocked_after_silence", session_id=self.ctx.session_id)

            if numero < total and self._breather > 0:
                await asyncio.sleep(self._breather)

    def _score(
        self,
        question: Question,
        votes: list[InboundMessage],
        *,
        poll_id: str,
    ) -> int:
        """Apunta las respuestas de una pregunta; devuelve cuántas hubo.

        Sólo cuenta el último voto de cada persona: WhatsApp deja cambiar la
        respuesta mientras la encuesta está abierta, y lo que vale es con qué
        se quedó.
        """
        # Se guarda con qué orden de llegada entró el voto que cuenta. Un
        # diccionario conserva el orden de la *primera* inserción de cada
        # clave, así que fiarse de él daría la rapidez del primer intento a
        # quien disparó mal y rectificó al final.
        ultimos: dict[str, tuple[int, InboundMessage]] = {}
        for llegada, vote in enumerate(votes):
            if vote.from_me or vote.kind != "poll_vote":
                continue
            # Si WAHA dice de qué encuesta es, se exige que sea la abierta.
            if vote.poll_id and poll_id and vote.poll_id != poll_id:
                continue
            elegidas = [o for o in vote.poll_options if o in question.options]
            if not elegidas:
                continue
            ultimos[vote.sender_id] = (llegada, vote)

        # El buzón conserva el orden de llegada, así que la posición dentro de
        # los aciertos de esta pregunta es quién respondió antes.
        posicion = 0
        en_orden = sorted(ultimos.items(), key=lambda par: par[1][0])
        for jid, (_llegada, vote) in en_orden:
            elegidas = [o for o in vote.poll_options if o in question.options]
            # Una sola opción y que sea la buena: marcar varias no es acertar.
            acierto = len(elegidas) == 1 and elegidas[0] == question.answer
            self.board.register(
                jid,
                # El nombre tal cual venga en el evento, que en un voto de
                # encuesta suele ser nada. Se resuelve antes de publicar.
                vote.sender_name,
                correct=acierto,
                position=posicion,
            )
            if acierto:
                posicion += 1
        return len(ultimos)

    # ---------------------------------------------------------- resultados
    async def _resolve_names(self) -> None:
        """Cambia identificadores por nombres antes de publicar nada.

        Un ``poll.vote`` llega sin nombre y, en los grupos nuevos, con el
        votante identificado por ``@lid``. Sin resolverlo la clasificación es
        una lista de números larguísimos en la que nadie se reconoce, que es
        justo lo contrario de lo que tiene que hacer una clasificación.
        """
        for jid in self.board.answered:
            if self.board.names.get(jid):
                continue
            try:
                self.board.names[jid] = await self.ctx.transport.contact_name(jid)
            except Exception as exc:  # noqa: BLE001
                # Sin nombre se muestra el identificador: feo, pero el
                # concurso no se cae por no saber cómo se llama alguien.
                log.warning("kahoot.name_failed", jid=jid, error=str(exc))

    async def _publish_results(self) -> None:
        await self._resolve_names()
        for bloque in self._answer_blocks():
            await self.ctx.transport.send_group(bloque)
        texto = self._texto()
        await self.ctx.transport.send_group(
            self._ranking_text(texto), mentions=texto.mentions
        )

    def _texto(self) -> GroupText:
        return GroupText(enabled=self.ctx.settings.use_mentions)

    def _answer_blocks(self) -> list[str]:
        """Las respuestas correctas, en un mensaje (o los menos posibles)."""
        lineas = [
            f"{numero}. {q.text}\n   ✅ *{q.answer}*"
            for numero, q in enumerate(self.questions, start=1)
        ]
        bloques: list[str] = []
        for inicio in range(0, len(lineas), SUMMARY_MAX_QUESTIONS):
            trozo = lineas[inicio : inicio + SUMMARY_MAX_QUESTIONS]
            cabecera = "📖 *RESPUESTAS CORRECTAS*" if inicio == 0 else "📖 *(sigue)*"
            bloques.append(cabecera + "\n\n" + "\n\n".join(trozo))
        return bloques

    def _ranking_text(self, texto: GroupText) -> str:
        filas = self.board.ranking()
        if not filas:
            return "🏁 *RESULTADOS*\n\nNo respondió nadie. Otra vez será."

        total = len(self.questions)
        medallas = ("🥇", "🥈", "🥉")
        lineas = ["🏆 *CLASIFICACIÓN*", ""]
        for puesto, (jid, nombre, aciertos, _) in enumerate(filas):
            marca = medallas[puesto] if puesto < len(medallas) else f"{puesto + 1}."
            lineas.append(f"{marca} {texto.tag(jid, nombre)} — {aciertos}/{total}")

        mejor = filas[0][2]
        campeones = [(jid, nombre) for jid, nombre, aciertos, _ in filas if aciertos == mejor]
        if mejor > 0:
            etiquetas = [texto.tag(jid, nombre) for jid, nombre in campeones]
            cierre = (
                f"\n👑 Gana {etiquetas[0]}."
                if len(etiquetas) == 1
                else "\n👑 Empate en lo más alto: " + ", ".join(etiquetas) + "."
            )
            lineas.append(cierre)
        return "\n".join(lineas)

    def _result(self) -> GameResult:
        filas = self.board.ranking()
        ganador = filas[0][1] if filas and filas[0][2] > 0 else None
        log.info(
            "kahoot.finished",
            session_id=self.ctx.session_id,
            preguntas=len(self.questions),
            participantes=len(filas),
            ganador=ganador,
        )
        return GameResult(
            status="finished",
            winner=ganador,
            rounds=len(self.questions),
            players=[
                {"nombre": nombre, "aciertos": aciertos} for _, nombre, aciertos, _ in filas
            ],
            summary=(
                f"{len(self.questions)} preguntas, {len(filas)} participantes."
                + (f" Ganó {ganador}." if ganador else " Sin aciertos.")
            ),
        )

    async def on_cancel(self) -> None:
        await self.ctx.transport.set_group_locked(False)
        await self.ctx.transport.send_group(
            "🛑 *Concurso cancelado por el máster.* El chat queda abierto."
        )
