"""Fase 1: quién juega y con qué rol.

Se convoca en el grupo, se interpreta quién se apunta de verdad (ver
``app/games/recruit.py``) y se reparte en privado."""

from __future__ import annotations

from typing import Any

from app.games.recruit import select_players
from app.games.werewolf.nodes.base import NodeBase
from app.games.werewolf.roles import Role, distribute_roles, info, roster_summary
from app.games.werewolf.state import (
    Player,
    WerewolfState,
    roster_lines,
    tagged_roster,
)
from app.logging_conf import get_logger

log = get_logger("werewolf")



class RecruitmentPhase(NodeBase):
    """Convocatoria y reparto: de un grupo de gente a una partida con roles."""

    async def reclutamiento(self, state: WerewolfState) -> dict[str, Any]:
        """Abre la convocatoria, escucha el grupo y registra a los jugadores.

        No se vacía el buzón aquí: cada partida usa un ``session_id`` nuevo, así
        que no hay nada viejo que limpiar, y borrar en este punto tiraría los
        "Yo" que hayan llegado entre el comando del máster y este nodo.
        """
        session_id = state["session_id"]

        flavour = await self.narrator.flavour(
            "apertura",
            {"juego": "El Hombre Lobo", "aldea": "Castronegro"},
            fallback=self.respaldo("opening"),
            max_words=60,
        )
        seconds = self.timers.recruit
        await self._group(
            f"{flavour}\n\n"
            + self.t(
                "recruit.open",
                time=self._seconds(seconds),
                minimum=self.settings.werewolf_min_players,
                maximum=self.settings.werewolf_max_players,
            )
        )

        reminder = None
        if seconds >= 20:
            reminder = await self._reminder(
                seconds * 0.6,
                self.t("recruit.reminder", time=self._seconds(seconds * 0.4)),
            )

        try:
            messages = await self.ctx.inbox.collect(session_id, timeout=seconds, group=True)
        finally:
            if reminder is not None:
                await self._stop_task(reminder)

        joiners = await select_players(
            messages,
            llm=self.ctx.llm,
            max_players=self.settings.werewolf_max_players,
            language=self.lang,
        )
        log.info(
            "werewolf.recruited",
            session_id=session_id,
            candidates=len({m.sender_id for m in messages}),
            joined=len(joiners),
        )

        minimum = max(self.settings.werewolf_min_players, 3)
        if len(joiners) < minimum:
            texto = self._texto()
            nombres = ", ".join(
                texto.tag(j.jid, j.name) for j in joiners
            ) or self.t("recruit.nobody")
            await self._group(
                self.t("recruit.not_enough", names=nombres, minimum=minimum),
                texto=texto,
            )
            await self.ctx.record(
                "reclutamiento.insuficiente",
                phase="reclutamiento",
                detail={"inscritos": [j.name for j in joiners], "minimo": minimum},
            )
            return {
                "phase": "fin",
                "abort_reason": "no se alcanzó el mínimo de jugadores",
                "winner": None,
                "finished": True,
                "players": [],
                **self._flush_narrative(),
            }

        players: list[Player] = [
            Player(
                jid=joiner.jid,
                name=joiner.name,
                number=index,
                role=str(Role.ALDEANO),
                alive=True,
                death_round=None,
                death_cause=None,
            )
            for index, joiner in enumerate(joiners, start=1)
        ]

        await self.ctx.record(
            "reclutamiento.cerrado",
            phase="reclutamiento",
            detail={"jugadores": [p["name"] for p in players]},
        )
        return {"players": players, "phase": "reparto", **self._flush_narrative()}

    async def reparto(self, state: WerewolfState) -> dict[str, Any]:
        """Silencia el grupo, reparte los roles y los manda por privado."""
        players = list(state["players"])
        roles = distribute_roles(len(players), rng=self.rng)
        assigned: list[Player] = [
            {**player, "role": str(role)} for player, role in zip(players, roles, strict=True)
        ]

        await self.ctx.transport.set_group_locked(True)

        intro = await self.narrator.flavour(
            "intro",
            {
                "jugadores": [p["name"] for p in assigned],
                "total": len(assigned),
            },
            fallback=self.respaldo("intro"),
            max_words=80,
        )
        texto = self._texto()
        await self._group(
            f"{intro}\n\n"
            + self.t(
                "deal.announce",
                count=len(assigned),
                roster=tagged_roster(assigned, texto),
                summary=roster_summary(roles, self.lang),
            ),
            texto=texto,
        )

        # Privados con el rol de cada uno.
        pack = list(assigned)
        wolf_pack = [p for p in pack if p["role"] == str(Role.LOBO)]
        messages: list[tuple[str, str]] = []
        for player in pack:
            details = info(player["role"], self.lang)
            body = self.t(
                "deal.dm_role",
                emoji=details.emoji,
                title=details.title,
                briefing=details.briefing,
                roster=roster_lines(pack),
            )
            if player["role"] == str(Role.LOBO):
                partners = [p for p in wolf_pack if p["jid"] != player["jid"]]
                if partners:
                    nombres = ", ".join(p["name"] for p in partners)
                    body += self.t("deal.pack", names=nombres)
                else:
                    body += self.t("deal.lone_wolf")
            messages.append((player["jid"], body))

        await self._dm_all(messages)
        await self.ctx.record(
            "reparto",
            phase="reparto",
            detail={p["name"]: p["role"] for p in assigned},
            is_secret=True,
        )
        return {
            "players": assigned,
            "phase": "noche",
            "round_no": 1,
            **self._flush_narrative(),
        }

    def ruta_tras_reclutamiento(self, state: WerewolfState) -> str:
        return "final" if state.get("finished") else "reparto"
