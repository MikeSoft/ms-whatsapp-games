"""Fase 3: el día.

El amanecer no publica —lo cuenta el juicio, en el mismo mensaje— y el
juicio escucha lo que se dice para que el narrador lo comente."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from app.games.werewolf.nodes.base import (
    DEBATE_GAP_RATIO,
    DEBATE_LINES_PER_COMMENT,
    DEBATE_MAX_CHARS,
    DEBATE_SLICE_SECONDS,
    NodeBase,
    trim_to_budget,
)
from app.games.werewolf.parsing import (
    is_abstention,
    parse_player_reference,
)
from app.games.werewolf.state import (
    Player,
    WerewolfState,
    alive,
    by_jid,
    poll_options,
    tagged_roster,
)
from app.logging_conf import get_logger
from app.waha.models import InboundMessage

log = get_logger("werewolf")



class DayPhase(NodeBase):
    """El día: amanecer, juicio y votación."""

    async def amanecer(self, state: WerewolfState) -> dict[str, Any]:
        """Cierra la noche y reabre el grupo, sin publicar todavía.

        Lo que la aldea encuentra al despertar y lo que hace a continuación son
        la misma escena, así que salen en un solo mensaje desde :meth:`debate`.
        Separarlos obligaba a etiquetar a todo el mundo dos veces y a contar la
        historia dos veces, con dos llamadas al modelo que no se conocían entre
        sí. Si la partida termina en este amanecer, es el cierre el que publica
        las muertes: ``dawn_pending`` dice que están sin contar.
        """
        players = list(state["players"])
        round_no = state["round_no"]
        deaths = list(state.get("deaths_last_night") or [])
        victims = [v for v in (by_jid(players, jid) for jid in deaths) if v is not None]

        await self.ctx.transport.set_group_locked(False)
        await self.ctx.record(
            "amanecer",
            round_no=round_no,
            phase="amanecer",
            detail={
                "muertes": [v["name"] for v in victims],
                "vivos": len(alive(players)),
            },
        )
        return {
            "phase": "debate",
            "resume_to": "debate",
            "deaths_last_night": deaths,
            "dawn_pending": True,
            **self._flush_narrative(),
        }

    async def debate(self, state: WerewolfState) -> dict[str, Any]:
        """Cuenta el amanecer, abre el juicio y escucha: todo en un mensaje.

        El amanecer y el juicio son la misma escena y se narran de una sola
        vez, con una sola lista de sospechosos. Así el pueblo se lee la
        historia entera seguida y a nadie se le etiqueta dos veces por lo
        mismo.
        """
        players = list(state["players"])
        round_no = state["round_no"]
        texto = self._texto()

        amanecio = bool(state.get("dawn_pending"))
        cuerpo, hechos_noche = (
            self._dawn_block(state, players, texto) if amanecio else ("", {})
        )

        flavour = await self.narrator.flavour(
            "amanecer_y_juicio" if amanecio else "juicio",
            {
                "ronda": round_no,
                "vivos": [p["name"] for p in alive(players)],
                **hechos_noche,
                # Lo de la ronda pasada: el juicio nuevo sabe de qué se venía
                # hablando en lugar de empezar de cero cada día.
                **self._voces(state),
            },
            fallback=(
                self._fallback_dia(hechos_noche)
                if amanecio
                else self.respaldo("trial")
            ),
            max_words=110 if amanecio else 70,
        )
        # El juicio empieza con este anuncio, no antes. El buzón del grupo
        # arrastra todo lo dicho desde la votación anterior —el veredicto, la
        # noche entera si el grupo no llegó a silenciarse, las reacciones al
        # amanecer— y nada de eso se dijo en el juicio. Sin vaciarlo, la
        # primera recogida se lo atribuiría al debate y el narrador comentaría
        # acusaciones que nadie hizo aquí.
        await self.ctx.inbox.clear(state["session_id"], keys=["group"])

        flavour = self._etiqueta_nombres(flavour, players, texto)
        vivos = alive(players)
        partes: list[str] = []
        if amanecio:
            cabecera = self.t("day.header", round_no=round_no)
            partes.append(f"{cabecera}\n\n{flavour}\n\n{cuerpo}")
        else:
            partes.append(flavour)
        partes.append(
            self.t(
                "day.trial",
                round_no=round_no,
                time=self._seconds(self.timers.debate),
                count=len(vivos),
                roster=tagged_roster(players, texto),
            )
        )
        await self._group("\n\n".join(partes), texto=texto)

        oido = await self._listen_to_debate(state["session_id"], round_no, players)

        return {
            "phase": "votacion",
            "debate_log": oido,
            "dawn_pending": False,
            **self._flush_narrative(),
        }

    def _debate_lines(
        self, messages: list[InboundMessage], players: list[Player]
    ) -> list[dict[str, str]]:
        """Lo dicho en el grupo, en la forma en que lo lee el narrador.

        Se descarta lo que manda el propio bot y lo que escriben los muertos:
        al pueblo ya se le dice que ignore a quien cayó, y el narrador tiene
        que ignorarlo igual o filtraría que ese muerto sigue jugando.
        """
        vivos = {p.get("jid", ""): p.get("name", "anónimo") for p in alive(players)}
        lineas: list[dict[str, str]] = []
        for message in messages:
            if message.from_me:
                continue
            nombre = vivos.get(message.sender_id)
            if nombre is None:
                continue
            if message.poll_options:
                # Un voto no es una intervención. Contarlo como tal pondría en
                # boca de alguien una acusación que nunca pronunció.
                continue
            dicho = message.text.strip()
            if not dicho:
                continue
            lineas.append({"quien": nombre, "dijo": dicho[:DEBATE_MAX_CHARS]})
        return trim_to_budget(lineas)

    async def _listen_to_debate(
        self, session_id: str, round_no: int, players: list[Player]
    ) -> list[dict[str, str]]:
        """Consume la ventana del juicio escuchando lo que se dice.

        Antes se dormía a ciegas y el buzón del grupo se tiraba entero en la
        votación. Recogerlo aquí cuesta lo mismo y da dos cosas: ambientación
        que reacciona a las acusaciones de verdad, y un resumen de lo hablado
        que arrastran las escenas siguientes.

        El narrador entra por actividad y no sólo por reloj: cuando se acumulan
        intervenciones nuevas se comenta antes, con un suelo entre comentarios
        para que un grupo grande no acabe leyendo más bot que vecinos.
        """
        interval = self.timers.filler_interval
        deadline = time.monotonic() + self.timers.debate
        # Sondeo corto para engancharse a lo recién dicho; margen al final para
        # no publicar encima de la encuesta; suelo entre comentarios.
        tramo_max = min(interval, DEBATE_SLICE_SECONDS) if interval > 0 else 0.0
        margen = tramo_max * 0.5
        hueco = interval * DEBATE_GAP_RATIO

        lineas: list[dict[str, str]] = []
        dichas = 0
        dichas_al_comentar = 0
        ultimo = time.monotonic()
        index = 0
        comentario: asyncio.Task[None] | None = None

        try:
            while True:
                restante = deadline - time.monotonic()
                if restante <= 0:
                    break
                tramo = restante if interval <= 0 else min(tramo_max, restante)
                recogido = await self.ctx.inbox.collect(
                    session_id, timeout=tramo, group=True
                )
                # Se reduce a líneas en cada tramo y se guarda sólo la cola: en
                # un grupo grande, retener los mensajes crudos del debate
                # entero es cargar con el payload de WAHA de cada uno.
                nuevas = self._debate_lines(recogido, players)
                dichas += len(nuevas)
                lineas = trim_to_budget(lineas + nuevas)

                if interval <= 0 or deadline - time.monotonic() <= margen:
                    continue
                # Sin encadenar llamadas: si la anterior sigue en vuelo, se
                # deja pasar este tramo.
                if comentario is not None and not comentario.done():
                    continue
                desde_el_ultimo = time.monotonic() - ultimo
                hay_novedad = dichas - dichas_al_comentar >= DEBATE_LINES_PER_COMMENT
                if not (
                    (hay_novedad and desde_el_ultimo >= hueco)
                    or desde_el_ultimo >= interval
                ):
                    continue

                comentario = asyncio.create_task(
                    self._ambience(
                        "debate",
                        round_no=round_no,
                        deadline=deadline,
                        index=index,
                        max_words=60,
                        # Cuanto más sepa, más cosas tiene que decir: quién
                        # sigue en pie y qué se acaba de gritar, no sólo el
                        # reloj.
                        extra={
                            "se_dijo": list(lineas),
                            "vivos": [p["name"] for p in alive(players)],
                        },
                        players=players,
                    )
                )
                dichas_al_comentar = dichas
                ultimo = time.monotonic()
                index += 1
        finally:
            if comentario is not None:
                await self._stop_task(comentario)

        return lineas

    async def votacion(self, state: WerewolfState) -> dict[str, Any]:
        """Publica la encuesta y recoge los votos (encuesta o texto)."""
        players = list(state["players"])
        round_no = state["round_no"]
        session_id = state["session_id"]
        vivos = alive(players)
        votantes = {p["jid"] for p in vivos}

        # Red de seguridad: el nodo del debate consume su buzón, pero entre
        # que cierra su ventana y se publica la encuesta pueden colarse
        # mensajes, y ninguno de ellos es un voto.
        await self.ctx.inbox.clear(session_id, keys=["group"])

        opciones = poll_options(players)
        encuesta_ok = False
        if len(opciones) >= 2:
            encuesta_ok = (
                await self.ctx.transport.send_poll(
                    self.t("vote.poll_question", round_no=round_no), opciones
                )
                is not None
            )

        instruccion = self.t("vote.use_poll" if encuesta_ok else "vote.use_text")
        texto = self._texto()
        await self._group(
            self.t(
                "vote.body",
                time=self._seconds(self.timers.vote),
                instruction=instruccion,
                roster=tagged_roster(players, texto),
            ),
            texto=texto,
        )

        def _voto_valido(text: str) -> bool:
            return is_abstention(text) or parse_player_reference(text, vivos) is not None

        collected = await self.ctx.inbox.collect(
            session_id,
            timeout=self.timers.vote,
            group=True,
            stop_when=self._stop_when_resolved(dict.fromkeys(votantes, _voto_valido)),
        )

        votes: dict[str, str] = {}
        for message in collected:
            if message.from_me or message.sender_id not in votantes:
                continue
            texto = self._text_of(message)
            if is_abstention(texto):
                votes.pop(message.sender_id, None)
                continue
            target = parse_player_reference(texto, vivos)
            if target is not None:
                # El último voto de cada persona es el que cuenta.
                votes[message.sender_id] = target["jid"]

        await self.ctx.record(
            "votacion",
            round_no=round_no,
            phase="votacion",
            detail={"votos": len(votes), "participantes": len(votantes)},
        )
        return {"votes": votes, "phase": "veredicto", **self._flush_narrative()}
