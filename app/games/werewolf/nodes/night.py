"""Fase 2: la noche.

Los lobos, la vidente y Cupido actúan a la vez; la bruja va después porque
necesita saber a quién atacaron. La resolución cruza ataque, curación y
veneno, y arrastra las cadenas de muerte (enamorados, cazador)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.games.werewolf.nodes.base import NodeBase
from app.games.werewolf.parsing import (
    is_abstention,
    parse_player_reference,
    parse_two_player_references,
    parse_witch_choice,
    tally_votes,
    witch_decided,
)
from app.games.werewolf.roles import Role
from app.games.werewolf.state import (
    Player,
    WerewolfState,
    alive,
    by_jid,
    kill,
    with_role,
    wolves,
)
from app.logging_conf import get_logger
from app.waha.models import InboundMessage

log = get_logger("werewolf")



class NightPhase(NodeBase):
    """La noche: los roles con poder actúan en privado y el grupo calla."""

    async def noche_inicio(self, state: WerewolfState) -> dict[str, Any]:
        """Narra la noche y recoge en paralelo lobos, vidente y Cupido.

        Las tres ventanas son una sola: se manda todo, se espera una vez y se
        resuelve cada rol con lo que haya llegado. Esperar por turnos
        multiplicaría por tres lo que la partida tarda en pasar de noche.
        """
        players = list(state["players"])
        round_no = state["round_no"]
        session_id = state["session_id"]

        await self.ctx.transport.set_group_locked(True)

        pack, seer, cupid = self._night_actors(state, players)
        actor_jids = [p["jid"] for p in pack + seer + cupid]
        # Se limpia antes de pedir para que no cuente lo que alguien escribió
        # de más en la ronda anterior.
        await self.ctx.inbox.clear(session_id, keys=[f"dm:{jid}" for jid in actor_jids])

        await self._announce_night(state, players, round_no)
        await self._dm_all(self._night_prompts(players, round_no, pack, seer, cupid))

        collected: list[InboundMessage] = []
        if actor_jids:
            async with self._fillers(self.timers.night, round_no=round_no):
                collected = await self.ctx.inbox.collect(
                    session_id,
                    timeout=self.timers.night,
                    direct=actor_jids,
                    stop_when=self._stop_when_resolved(
                        self._night_predicates(players, pack, seer, cupid)
                    ),
                )

        latest = self._last_per_sender(collected)
        night_actions: dict[str, Any] = {}
        updates: dict[str, Any] = {"night_actions": night_actions, "phase": "noche"}

        await self._resolve_wolves(players, pack, latest, night_actions, session_id, round_no)
        await self._resolve_seer(players, seer, latest, night_actions)
        await self._resolve_cupid(players, cupid, latest, night_actions, updates)

        await self.ctx.record(
            "noche.acciones",
            round_no=round_no,
            phase="noche",
            detail=night_actions,
            is_secret=True,
        )
        updates.update(self._flush_narrative())
        return updates

    # ------------------------------------------------- quién actúa y cómo
    @staticmethod
    def _night_actors(
        state: WerewolfState, players: list[Player]
    ) -> tuple[list[Player], list[Player], list[Player]]:
        """Los roles que actúan esta noche. Cupido, sólo la primera."""
        primera_noche = state["round_no"] == 1 and not state.get("lovers")
        return (
            wolves(players),
            with_role(players, Role.VIDENTE),
            with_role(players, Role.CUPIDO) if primera_noche else [],
        )

    async def _announce_night(
        self, state: WerewolfState, players: list[Player], round_no: int
    ) -> None:
        night = await self.narrator.flavour(
            "noche",
            {
                "ronda": round_no,
                "vivos": len(alive(players)),
                **self._voces(state),
            },
            fallback=self.respaldo("night"),
            max_words=70,
        )
        texto = self._texto()
        night = self._etiqueta_nombres(night, players, texto)
        await self._group(
            self.t(
                "night.open",
                round_no=round_no,
                flavour=night,
                time=self._seconds(self.timers.night),
            ),
            texto=texto,
        )

    def _night_prompts(
        self,
        players: list[Player],
        round_no: int,
        pack: list[Player],
        seer: list[Player],
        cupid: list[Player],
    ) -> list[tuple[str, str]]:
        """El privado que recibe cada rol con poder, ya redactado."""
        wolf_names = ", ".join(p["name"] for p in pack)
        wolf_exclude = {p["jid"] for p in pack}
        nota = (
            self.t("night.wolf_pack_note", names=wolf_names)
            if len(pack) > 1
            else self.t("night.wolf_alone_note")
        )

        mensajes = [
            (
                wolf["jid"],
                self.t(
                    "night.wolf_prompt",
                    round_no=round_no,
                    targets=self._targets_block(players, exclude=wolf_exclude),
                    note=nota,
                ),
            )
            for wolf in pack
        ]
        mensajes += [
            (
                oracle["jid"],
                self.t(
                    "night.seer_prompt",
                    round_no=round_no,
                    targets=self._targets_block(players, exclude={oracle["jid"]}),
                ),
            )
            for oracle in seer
        ]
        mensajes += [
            (
                love["jid"],
                self.t("night.cupid_prompt", targets=self._targets_block(players)),
            )
            for love in cupid
        ]
        return mensajes

    @staticmethod
    def _night_predicates(
        players: list[Player],
        pack: list[Player],
        seer: list[Player],
        cupid: list[Player],
    ) -> dict[str, Callable[[str], bool]]:
        """Cuándo se da por respondido cada rol.

        Es lo que permite cerrar la noche antes de tiempo cuando ya han
        contestado todos, y exige una respuesta *interpretable*: un "ok" suelto
        no cuenta como elegir.
        """
        # Los objetivos se resuelven contra los vivos, que es lo que se muestra
        # en el privado. Contra la lista completa, escribir el número de un
        # muerto (los números no se reciclan) desperdiciaría la acción.
        vivos = alive(players)
        wolf_exclude = [p["jid"] for p in pack]
        predicates: dict[str, Callable[[str], bool]] = {}
        for wolf in pack:
            predicates[wolf["jid"]] = (
                lambda text: parse_player_reference(text, vivos, exclude=wolf_exclude)
                is not None
            )
        for oracle in seer:
            jid = oracle["jid"]
            predicates[jid] = (
                lambda text, _jid=jid: parse_player_reference(text, vivos, exclude=[_jid])
                is not None
            )
        for love in cupid:
            predicates[love["jid"]] = (
                lambda text: parse_two_player_references(text, vivos) is not None
            )
        return predicates

    # --------------------------------------------- qué hizo cada uno
    async def _resolve_wolves(
        self,
        players: list[Player],
        pack: list[Player],
        latest: dict[str, InboundMessage],
        night_actions: dict[str, Any],
        session_id: str,
        round_no: int,
    ) -> None:
        """La manada muerde por mayoría; un empate lo rompe el instinto."""
        vivos = alive(players)
        wolf_exclude = [p["jid"] for p in pack]
        wolf_votes: dict[str, str] = {}
        for wolf in pack:
            message = latest.get(wolf["jid"])
            if message is None:
                continue
            target = parse_player_reference(message.text, vivos, exclude=wolf_exclude)
            if target is not None:
                wolf_votes[wolf["jid"]] = target["jid"]

        if not wolf_votes:
            # Sin respuesta no hay ataque: mejor una noche en calma que una
            # muerte decidida por el sistema.
            night_actions["wolf_target"] = None
            log.info("werewolf.wolves_silent", session_id=session_id, round_no=round_no)
            return

        top, _ = tally_votes(wolf_votes)
        chosen = self.rng.choice(sorted(top)) if len(top) > 1 else top[0]
        night_actions["wolf_target"] = chosen
        night_actions["wolf_votes"] = wolf_votes
        if len(pack) > 1:
            victim = by_jid(players, chosen)
            clave = "night.wolves_decided" if len(top) == 1 else "night.wolves_tie"
            aviso = self.t(clave, name=victim["name"] if victim else chosen)
            await self._dm_all([(w["jid"], aviso) for w in pack])

    async def _resolve_seer(
        self,
        players: list[Player],
        seer: list[Player],
        latest: dict[str, InboundMessage],
        night_actions: dict[str, Any],
    ) -> None:
        """A la vidente se le responde en el acto, en su privado."""
        vivos = alive(players)
        for oracle in seer:
            message = latest.get(oracle["jid"])
            if message is None:
                await self._dm(oracle["jid"], self.t("night.seer_timeout"))
                continue
            target = parse_player_reference(message.text, vivos, exclude=[oracle["jid"]])
            if target is None:
                await self._dm(oracle["jid"], self.t("night.seer_unclear"))
                continue
            es_lobo = target["role"] == str(Role.LOBO)
            night_actions["seer_query"] = target["jid"]
            night_actions["seer_result"] = es_lobo
            await self._dm(
                oracle["jid"],
                self.t(
                    "night.seer_result",
                    name=target["name"],
                    verdict=self.t(
                        "night.seer_is_wolf" if es_lobo else "night.seer_not_wolf"
                    ),
                ),
            )

    async def _resolve_cupid(
        self,
        players: list[Player],
        cupid: list[Player],
        latest: dict[str, InboundMessage],
        night_actions: dict[str, Any],
        updates: dict[str, Any],
    ) -> None:
        """Cupido enamora a dos, y a cada uno se le dice con quién."""
        vivos = alive(players)
        for love in cupid:
            message = latest.get(love["jid"])
            pair = parse_two_player_references(message.text, vivos) if message else None
            if pair is None:
                if message is not None:
                    await self._dm(love["jid"], self.t("night.cupid_unclear"))
                continue
            first, second = pair
            updates["lovers"] = [first["jid"], second["jid"]]
            night_actions["lovers"] = [first["jid"], second["jid"]]
            await self._dm(
                love["jid"],
                self.t("night.cupid_done", first=first["name"], second=second["name"]),
            )
            await self._dm_all(
                [
                    (first["jid"], self.t("night.lovers_note", name=second["name"])),
                    (second["jid"], self.t("night.lovers_note", name=first["name"])),
                ]
            )

    def necesita_bruja(self, state: WerewolfState) -> str:
        """Router: la bruja sólo se consulta si está viva y le quedan pociones."""
        witch = with_role(list(state["players"]), Role.BRUJA)
        potions = state.get("witch_potions") or {}
        if witch and any(potions.values()):
            return "noche_bruja"
        return "resolucion"

    async def noche_bruja(self, state: WerewolfState) -> dict[str, Any]:
        """Informa a la bruja de la víctima y recoge su decisión.

        Su ventana va después de la de los lobos porque necesita saber a quién
        atacaron: es el único rol cuya decisión depende de otro.
        """
        players = list(state["players"])
        session_id = state["session_id"]
        night_actions = dict(state.get("night_actions") or {})
        potions = dict(state.get("witch_potions") or {"vida": True, "muerte": True})

        brujas = with_role(players, Role.BRUJA)
        if not brujas:
            # El router `necesita_bruja` ya lo comprueba; esto es un cinturón
            # de seguridad: una excepción aquí dejaría el grupo silenciado.
            log.warning(
                "werewolf.no_witch", session_id=session_id, round_no=state["round_no"]
            )
            return {"night_actions": night_actions, "witch_potions": potions}

        witch = brujas[0]
        target_jid = night_actions.get("wolf_target")
        victim = by_jid(players, target_jid) if target_jid else None

        message = await self._ask_witch(state, players, witch, victim, potions)
        if message is None:
            await self._dm(witch["jid"], self.t("witch.timeout"))
            return {
                "night_actions": night_actions,
                "witch_potions": potions,
                **self._flush_narrative(),
            }

        choice = parse_witch_choice(message.text)
        await self._apply_witch_choice(
            choice, message, players, witch, victim, potions, night_actions
        )

        await self.ctx.record(
            "noche.bruja",
            round_no=state["round_no"],
            phase="noche",
            detail={"eleccion": choice, "pociones": potions},
            is_secret=True,
        )
        return {
            "night_actions": night_actions,
            "witch_potions": potions,
            **self._flush_narrative(),
        }

    async def _ask_witch(
        self,
        state: WerewolfState,
        players: list[Player],
        witch: Player,
        victim: Player | None,
        potions: dict[str, bool],
    ) -> InboundMessage | None:
        """Le cuenta lo que pasó y espera su decisión. ``None`` si no contesta."""
        session_id = state["session_id"]
        await self.ctx.inbox.clear(session_id, keys=[f"dm:{witch['jid']}"])

        disponibles = []
        if potions.get("vida"):
            disponibles.append(self.t("witch.potion_life"))
        if potions.get("muerte"):
            disponibles.append(self.t("witch.potion_death"))

        cabecera = (
            self.t("witch.attacked", name=victim["name"])
            if victim is not None
            else self.t("witch.quiet")
        )

        # La lista que se muestra excluye a la propia bruja, igual que el
        # parser: ofrecerle su nombre y luego no aceptarlo sería una trampa.
        await self._dm(
            witch["jid"],
            self.t(
                "witch.prompt",
                header=cabecera,
                potions=", ".join(disponibles),
                targets=self._targets_block(players, exclude={witch["jid"]}),
                time=self._seconds(self.timers.witch),
            ),
        )

        async with self._fillers(self.timers.witch, round_no=state["round_no"]):
            collected = await self.ctx.inbox.collect(
                session_id,
                timeout=self.timers.witch,
                direct=[witch["jid"]],
                stop_when=self._stop_when_resolved({witch["jid"]: witch_decided}),
            )
        return self._last_per_sender(collected).get(witch["jid"])

    async def _apply_witch_choice(
        self,
        choice: str,
        message: InboundMessage,
        players: list[Player],
        witch: Player,
        victim: Player | None,
        potions: dict[str, bool],
        night_actions: dict[str, Any],
    ) -> None:
        """Gasta la poción que corresponda, o ninguna.

        Ante la duda no se gasta nada: una poción es de un solo uso en toda la
        partida y perderla por un mensaje ambiguo es peor que no usarla.
        """
        if choice == "vida" and potions.get("vida") and victim is not None:
            potions["vida"] = False
            night_actions["witch_heal"] = victim["jid"]
            await self._dm(witch["jid"], self.t("witch.healed", name=victim["name"]))
            return

        if choice == "vida":
            motivo = self.t(
                "witch.reason_used" if not potions.get("vida") else "witch.reason_nobody"
            )
            await self._dm(witch["jid"], self.t("witch.cannot_heal", reason=motivo))
            return

        if choice == "muerte" and potions.get("muerte"):
            objetivo = parse_player_reference(
                message.text, alive(players), exclude=[witch["jid"]]
            )
            if objetivo is None:
                await self._dm(witch["jid"], self.t("witch.poison_unclear"))
                return
            potions["muerte"] = False
            night_actions["witch_poison"] = objetivo["jid"]
            await self._dm(witch["jid"], self.t("witch.poisoned", name=objetivo["name"]))
            return

        if choice == "muerte":
            await self._dm(witch["jid"], self.t("witch.death_used"))
            return

        await self._dm(witch["jid"], self.t("witch.abstain"))

    async def resolucion(self, state: WerewolfState) -> dict[str, Any]:
        """Cruza ataque, curación y veneno para saber quién muere de verdad."""
        players = list(state["players"])
        round_no = state["round_no"]
        night_actions = dict(state.get("night_actions") or {})

        pending: list[tuple[str, str]] = []
        target_jid = night_actions.get("wolf_target")
        healed = night_actions.get("witch_heal")
        if target_jid and target_jid != healed:
            pending.append((target_jid, "lobos"))
        poisoned = night_actions.get("witch_poison")
        if poisoned:
            pending.append((poisoned, "veneno"))

        players, deaths = await self._apply_deaths(
            players,
            pending,
            round_no=round_no,
            lovers=list(state.get("lovers") or []),
            session_id=state["session_id"],
        )

        night_actions["saved"] = bool(target_jid and target_jid == healed)
        await self.ctx.record(
            "noche.resolucion",
            round_no=round_no,
            phase="resolucion",
            detail={"muertes": deaths, "salvado": night_actions["saved"]},
        )
        return {
            "players": players,
            "deaths_last_night": [d["jid"] for d in deaths],
            "night_actions": night_actions,
            "phase": "amanecer",
            **self._flush_narrative(),
        }

    async def _apply_deaths(
        self,
        players: list[Player],
        pending: list[tuple[str, str]],
        *,
        round_no: int,
        lovers: list[str],
        session_id: str | None = None,
    ) -> tuple[list[Player], list[dict[str, Any]]]:
        """Mata en cascada: amor y cazador pueden arrastrar a más gente.

        Devuelve la lista de jugadores actualizada y las muertes en el orden
        en que ocurrieron, con su causa.
        """
        queue = list(pending)
        deaths: list[dict[str, Any]] = []
        guard = len(players) * 2 + 4

        while queue and guard > 0:
            guard -= 1
            jid, cause = queue.pop(0)
            player = by_jid(players, jid)
            if player is None or not player.get("alive", True):
                continue

            players, victim = kill(players, jid, round_no=round_no, cause=cause)
            if victim is None:
                continue
            deaths.append(
                {
                    "jid": victim["jid"],
                    "name": victim["name"],
                    "role": victim["role"],
                    "cause": cause,
                }
            )

            # Los enamorados mueren juntos.
            if len(lovers) == 2 and jid in lovers:
                partner = lovers[0] if lovers[1] == jid else lovers[1]
                partner_player = by_jid(players, partner)
                if partner_player is not None and partner_player.get("alive", True):
                    queue.append((partner, "amor"))

            # El cazador dispara en su último aliento.
            if victim["role"] == str(Role.CAZADOR):
                shot = await self._ask_hunter(victim, players, session_id=session_id)
                if shot:
                    queue.append((shot, "cazador"))

        if guard <= 0:  # pragma: no cover - salvaguarda
            log.warning("werewolf.death_chain_guard", pending=queue)
        return players, deaths

    async def _ask_hunter(
        self, hunter: Player, players: list[Player], *, session_id: str | None = None
    ) -> str | None:
        """Pregunta al cazador a quién se lleva a la tumba.

        La sesión llega desde el estado, como en el resto de los nodos: usar
        ``ctx.session_id`` aquí funcionaba por casualidad (coinciden) y era una
        trampa esperando a que un juego los separara.
        """
        session_id = session_id or self.ctx.session_id
        options = [p for p in alive(players) if p["jid"] != hunter["jid"]]
        if not options:
            return None

        await self.ctx.inbox.clear(session_id, keys=[f"dm:{hunter['jid']}"])
        await self._dm(
            hunter["jid"],
            self.t(
                "hunter.prompt",
                targets=self._targets_block(players, exclude={hunter["jid"]}),
                time=self._seconds(self.timers.hunter),
            ),
        )

        collected = await self.ctx.inbox.collect(
            session_id,
            timeout=self.timers.hunter,
            direct=[hunter["jid"]],
            stop_when=self._stop_when_resolved(
                {
                    hunter["jid"]: lambda text: is_abstention(text)
                    or parse_player_reference(text, options) is not None
                }
            ),
        )
        message = self._last_per_sender(collected).get(hunter["jid"])
        if message is None or is_abstention(message.text):
            await self._dm(hunter["jid"], self.t("hunter.lowered"))
            return None

        target = parse_player_reference(message.text, options)
        if target is None:
            await self._dm(hunter["jid"], self.t("hunter.missed"))
            return None
        await self._dm(hunter["jid"], self.t("hunter.takes", name=target["name"]))
        return target["jid"]
