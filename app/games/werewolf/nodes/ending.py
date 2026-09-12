"""Fase 4: el veredicto y el final.

``evaluar`` es el único sitio donde se decide que la partida terminó, y se
alcanza desde dos caminos: el amanecer y el veredicto."""

from __future__ import annotations

from typing import Any

from app.games.mentions import GroupText
from app.games.werewolf import prompts
from app.games.werewolf.nodes.base import NodeBase
from app.games.werewolf.parsing import (
    tally_votes,
    votes_breakdown,
)
from app.games.werewolf.roles import Role, info
from app.games.werewolf.state import (
    Player,
    WerewolfState,
    alive,
    by_jid,
    public_summary,
    tagged_roster,
)
from app.logging_conf import get_logger

log = get_logger("werewolf")



class EndingPhase(NodeBase):
    """El cierre: quién gana, a quién se lincha y cómo termina la partida."""

    async def evaluar(self, state: WerewolfState) -> dict[str, Any]:
        """Decide si la partida terminó y quién ganó."""
        players = list(state["players"])
        vivos = alive(players)
        lobos = [p for p in vivos if p["role"] == str(Role.LOBO)]
        aldeanos = [p for p in vivos if p["role"] != str(Role.LOBO)]
        lovers = list(state.get("lovers") or [])

        winner: str | None = None
        reason: str | None = None

        if not vivos:
            winner = "nadie"
            reason = "no quedó nadie vivo"
        elif (
            len(lovers) == 2
            and len(vivos) == 2
            and {p["jid"] for p in vivos} == set(lovers)
            and len(lobos) == 1
        ):
            # Los enamorados de bandos opuestos ganan juntos.
            winner = "enamorados"
            reason = "sólo quedaron los dos enamorados"
        elif not lobos:
            winner = "pueblo"
            reason = "todos los lobos fueron eliminados"
        elif len(lobos) >= len(aldeanos):
            winner = "lobos"
            reason = "los lobos igualan o superan al pueblo"
        elif state["round_no"] > self.settings.max_rounds:
            winner = "nadie"
            reason = f"se alcanzó el tope de {self.settings.max_rounds} rondas"

        if winner:
            await self.ctx.record(
                "victoria",
                round_no=state["round_no"],
                phase="fin",
                detail={"ganador": winner, "motivo": reason},
            )
            return {
                "winner": winner,
                "finished": True,
                "abort_reason": None,
                "phase": "fin",
                **self._flush_narrative(),
            }
        return {"winner": None, "finished": False}

    def ruta_tras_evaluar(self, state: WerewolfState) -> str:
        """Router: al final, al debate, o de vuelta a la noche."""
        if state.get("winner"):
            return "final"
        return "debate" if state.get("resume_to") == "debate" else "noche_inicio"

    async def veredicto(self, state: WerewolfState) -> dict[str, Any]:
        """Lincha al más votado, revela su rol y cierra el día.

        El día siempre acaba aquí, se linche o no, y siempre devuelve la
        partida a la noche: es el único nodo que hace avanzar la ronda.
        """
        players = list(state["players"])
        votes = dict(state.get("votes") or {})
        top, count = tally_votes(votes)

        # El recuento y el anuncio comparten compositor: las menciones que
        # acumule el listado de votos tienen que viajar con el mismo mensaje.
        texto = self._texto()
        recuento = votes_breakdown(votes, players, texto, self.lang)

        lynched_jid = self._who_hangs(top)
        if lynched_jid is None:
            return await self._no_lynch(state, players, texto, recuento, empatados=len(top))
        return await self._lynch(state, players, texto, recuento, lynched_jid, count)

    def _who_hangs(self, top: list[str]) -> str | None:
        """A quién lincha la aldea, o ``None`` si a nadie.

        Un empate no lincha salvo que el despliegue lo pida: matar por azar a
        alguien a quien la mitad del pueblo defendía sienta peor que no matar.
        """
        if len(top) == 1:
            return top[0]
        if len(top) > 1 and self.settings.werewolf_tie_break == "random":
            return self.rng.choice(sorted(top))
        return None

    def _next_night(self, players: list[Player], round_no: int, **extra: Any) -> dict[str, Any]:
        """El estado con el que se vuelve a la noche."""
        return {
            "players": players,
            "votes": {},
            "round_no": round_no + 1,
            "phase": "noche",
            "resume_to": "noche",
            **extra,
            **self._flush_narrative(),
        }

    async def _no_lynch(
        self,
        state: WerewolfState,
        players: list[Player],
        texto: GroupText,
        recuento: str,
        *,
        empatados: int,
    ) -> dict[str, Any]:
        round_no = state["round_no"]
        flavour = await self.narrator.flavour(
            "sin_linchamiento",
            {"ronda": round_no, "empate": empatados > 1, **self._voces(state)},
            fallback=self.respaldo("no_lynch"),
            max_words=60,
        )
        flavour = self._etiqueta_nombres(flavour, players, texto)
        await self._group(
            self.t("verdict.no_lynch", tally=recuento, flavour=flavour), texto=texto
        )
        await self.ctx.record(
            "veredicto.sin_linchamiento",
            round_no=round_no,
            phase="veredicto",
            detail={"empatados": empatados},
        )
        return self._next_night(
            players, round_no, lynched=None, deaths_last_night=[]
        )

    def _lynch_line(
        self, death: dict[str, Any], players: list[Player], texto: GroupText
    ) -> str:
        """El parte de una muerte del día.

        Tolera no encontrar al jugador: es una función de presentación y un
        estado raro no puede dejar el veredicto sin publicar.
        """
        victim = by_jid(players, death["jid"])
        if victim is not None:
            return self._death_line(victim, texto)
        return self.t(
            "day.death_line",
            tag=death["name"],
            cause=self._cause_text(death["cause"]),
            role=death["role"],
        )

    async def _lynch(
        self,
        state: WerewolfState,
        players: list[Player],
        texto: GroupText,
        recuento: str,
        lynched_jid: str,
        votos: int,
    ) -> dict[str, Any]:
        round_no = state["round_no"]
        condenado = by_jid(players, lynched_jid)
        players, deaths = await self._apply_deaths(
            players,
            [(lynched_jid, "linchamiento")],
            round_no=round_no,
            lovers=list(state.get("lovers") or []),
            session_id=state["session_id"],
        )

        flavour = await self.narrator.flavour(
            "veredicto",
            {
                "ronda": round_no,
                "linchado": condenado["name"] if condenado else lynched_jid,
                "votos": votos,
                "rol_revelado": (
                    info(condenado["role"], self.lang).title if condenado else None
                ),
                "se_dijo": list(state.get("debate_log") or []),
            },
            fallback=self.respaldo("verdict"),
            max_words=80,
        )
        flavour = self._etiqueta_nombres(flavour, players, texto)

        vivos = alive(players)
        partes = [
            self.t("verdict.header", round_no=round_no), "", recuento, "", flavour, "",
            *(self._lynch_line(death, players, texto) for death in deaths),
            "",
            self.t("day.ignore_the_fallen"),
            "",
            self.t("verdict.still_alive", count=len(vivos)),
            tagged_roster(players, texto),
        ]
        await self._group("\n".join(partes), texto=texto)
        await self.ctx.record(
            "veredicto",
            round_no=round_no,
            phase="veredicto",
            detail={
                "linchado": condenado["name"] if condenado else lynched_jid,
                "muertes": [d["name"] for d in deaths],
            },
        )
        return self._next_night(
            players,
            round_no,
            lynched=lynched_jid,
            deaths_last_night=[d["jid"] for d in deaths],
        )

    async def final(self, state: WerewolfState) -> dict[str, Any]:
        """Narra el desenlace, revela todos los roles y reabre el grupo."""
        players = list(state["players"])
        winner = state.get("winner")
        abort_reason = state.get("abort_reason")

        await self.ctx.transport.set_group_locked(False)

        if abort_reason and not players:
            # Reclutamiento fallido: el mensaje ya se envió en su nodo.
            return {"phase": "fin", "finished": True, **self._flush_narrative()}

        respaldos = prompts.fallbacks(self.lang)
        textos = {
            "lobos": (respaldos["wolves_win"], self.t("final.wolves_win")),
            "pueblo": (respaldos["village_win"], self.t("final.village_win")),
            "enamorados": (respaldos["lovers_win"], self.t("final.lovers_win")),
            "nadie": (respaldos["aborted"], self.t("final.draw")),
        }
        fallback, titular = textos.get(winner or "nadie", textos["nadie"])

        texto = self._texto()
        # Si la partida se acabó en el amanecer, el juicio no llegó a
        # celebrarse y las muertes de anoche siguen sin contarse: van aquí,
        # antes del desenlace, o no se cuentan nunca.
        cuerpo, hechos_noche = (
            self._dawn_block(state, players, texto)
            if state.get("dawn_pending")
            else ("", {})
        )

        supervivientes = [p["name"] for p in alive(players)]
        flavour = await self.narrator.flavour(
            f"victoria_{winner or 'nadie'}",
            {
                "ganador": winner,
                "supervivientes": supervivientes,
                "rondas": state["round_no"],
                **hechos_noche,
                **self._voces(state),
            },
            fallback=fallback,
            max_words=100,
        )

        flavour = self._etiqueta_nombres(flavour, players, texto)
        partes = [titular, "", flavour]
        if cuerpo:
            partes += ["", cuerpo]
        partes += [
            "",
            self.t(
                "final.all_roles",
                summary=public_summary(players, texto, self.lang),
            ),
            "",
            self.t("final.rounds", rounds=max(1, state["round_no"] - 1)),
            self.t("final.thanks"),
        ]
        await self._group("\n".join(partes), texto=texto)
        return {
            "phase": "fin",
            "finished": True,
            "dawn_pending": False,
            **self._flush_narrative(),
        }
