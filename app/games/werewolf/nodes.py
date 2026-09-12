"""Nodos del grafo de El Hombre Lobo.

Cada método público es un nodo de LangGraph: recibe el estado y devuelve
**sólo las claves que cambia**. Los efectos secundarios (mandar mensajes,
silenciar el grupo, esperar respuestas) ocurren dentro del nodo; el estado
guarda el resultado.

Los tiempos viven en :class:`Timers` para que los tests puedan ejecutar una
partida entera en milisegundos.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.config import Settings
from app.games.base import GameContext
from app.games.mentions import GroupText, tag_names
from app.games.recruit import select_players
from app.games.werewolf import prompts
from app.games.werewolf.narrator import Narrator
from app.games.werewolf.parsing import (
    is_abstention,
    parse_player_reference,
    parse_two_player_references,
    parse_witch_choice,
    tally_votes,
    votes_breakdown,
    witch_decided,
)
from app.games.werewolf.roles import Role, distribute_roles, info, roster_summary
from app.games.werewolf.state import (
    Player,
    WerewolfState,
    alive,
    by_jid,
    kill,
    label,
    poll_options,
    public_summary,
    role_title,
    roster_lines,
    tag,
    tagged_roster,
    with_role,
    wolves,
)
from app.games.werewolf.texts import CAUSES, TEXTS
from app.i18n import Texts
from app.logging_conf import get_logger
from app.waha.models import InboundMessage

log = get_logger("werewolf")

#: Presupuesto de un envío masivo de privados, en segundos.
#:
#: Escala con el número de destinatarios porque los envíos van serializados,
#: pero con un techo absoluto: si escalara sin límite no acotaría nada, que es
#: justo el problema que se quiere evitar. Un WAHA sano tarda unos 0,7 s por
#: mensaje (intervalo antiflood incluido), así que 2 s deja margen de sobra y
#: el techo corta a un WAHA patológico.
DM_BUDGET_PER_MESSAGE = 2.0
DM_BUDGET_MIN = 20.0
DM_BUDGET_MAX = 60.0


def dm_budget(count: int) -> float:
    """Segundos que se le conceden a un envío masivo de ``count`` privados."""
    return min(DM_BUDGET_MAX, max(DM_BUDGET_MIN, DM_BUDGET_PER_MESSAGE * count))


#: Lo que se le pasa al narrador del juicio se acota por caracteres, no por
#: número de intervenciones.
#:
#: El contexto del modelo da de sobra para un debate entero, así que lo normal
#: es que quepa todo y el narrador tenga la conversación completa. El tope
#: existe para el caso patológico —una avalancha en un grupo grande— donde
#: crecer sin límite dispararía coste y latencia justo cuando la partida
#: necesita responder rápido. Al recortar se conserva la cola, que es la que
#: tiene el calor del momento.
DEBATE_BUDGET_CHARS = 20_000
#: Tope por intervención. Un mensaje de WhatsApp rara vez llega aquí; corta
#: los pegotes de texto sin tocar la conversación real.
DEBATE_MAX_CHARS = 400

#: Coste aproximado de envolver una intervención en el JSON de los HECHOS
#: (claves, comillas, comas y sangrado). Sin contarlo, el presupuesto se
#: quedaría corto justo con las mesas grandes, que es cuando importa.
_LINE_OVERHEAD_CHARS = 30


def trim_to_budget(lineas: list[dict[str, str]]) -> list[dict[str, str]]:
    """La cola de la conversación que cabe en :data:`DEBATE_BUDGET_CHARS`."""
    total = 0
    desde = len(lineas)
    for indice in range(len(lineas) - 1, -1, -1):
        linea = lineas[indice]
        coste = len(linea["quien"]) + len(linea["dijo"]) + _LINE_OVERHEAD_CHARS
        if total + coste > DEBATE_BUDGET_CHARS:
            break
        total += coste
        desde = indice
    return lineas[desde:]


#: Cada cuánto se sondea el buzón durante el juicio. Más corto que el
#: intervalo de relleno a propósito: aquí no se espera a ciegas, y cuanto
#: antes se lea lo que se acaba de decir, antes puede el narrador engancharse.
DEBATE_SLICE_SECONDS = 8.0

#: Intervenciones nuevas que justifican comentar antes de que toque por reloj.
#: Un juicio encendido da para varios comentarios; uno apagado se queda con la
#: cadencia del reloj y no molesta. Bajo a propósito: el comentario que recoge
#: acusaciones reales es lo que hace que el juicio se sienta arbitrado, y
#: llegar tarde a una acusación es llegar cuando ya se habla de otra cosa.
DEBATE_LINES_PER_COMMENT = 2

#: Suelo entre comentarios, como fracción del intervalo de relleno. Existe para
#: que un grupo muy hablador no acabe leyendo más bot que vecinos.
DEBATE_GAP_RATIO = 0.4


@dataclass(frozen=True)
class Timers:
    """Duración de cada ventana de espera, en segundos."""

    recruit: float = 30.0
    night: float = 60.0
    witch: float = 45.0
    hunter: float = 40.0
    debate: float = 90.0
    vote: float = 30.0
    filler_interval: float = 25.0

    @classmethod
    def from_settings(cls, settings: Settings) -> Timers:
        return cls(
            recruit=float(settings.recruit_seconds),
            night=float(settings.night_action_seconds),
            witch=float(settings.witch_action_seconds),
            hunter=float(settings.hunter_action_seconds),
            debate=float(settings.debate_seconds),
            vote=float(settings.vote_seconds),
            filler_interval=float(settings.filler_interval_seconds),
        )


class WerewolfNodes:
    """Colección de nodos, con el contexto de la partida inyectado."""

    def __init__(
        self,
        ctx: GameContext,
        *,
        narrator: Narrator | None = None,
        timers: Timers | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.ctx = ctx
        self.settings = ctx.settings
        self.lang = ctx.settings.game_language
        self.t = Texts(TEXTS, self.lang)
        self.narrator = narrator or Narrator(ctx.llm, language=self.lang)
        self.timers = timers or Timers.from_settings(ctx.settings)
        self.rng = rng or random.Random()
        # Lo narrado al grupo desde el último nodo. Los nodos corren en serie,
        # así que acumular en la instancia es seguro.
        self._narrated: list[str] = []

    # ================================================================ ayudas
    def _seconds(self, value: float) -> str:
        """Formatea una espera para anunciarla ("90 s" -> "1 min 30 s")."""
        total = round(value)
        if total < 60:
            return self.t("time.seconds", total=total)
        minutes, rest = divmod(total, 60)
        if rest:
            return self.t("time.min_sec", minutes=minutes, rest=rest)
        clave = "time.minute" if minutes == 1 else "time.minutes"
        return self.t(clave, minutes=minutes)

    def respaldo(self, key: str) -> str:
        """El texto estático de una escena, para cuando el modelo no está."""
        return prompts.fallbacks(self.lang)[key]

    def _fallback_dia(self, hechos_noche: dict[str, Any]) -> str:
        """Respaldo del mensaje que junta amanecer y juicio, sin modelo detrás."""
        if hechos_noche.get("victimas"):
            amanecer = self.respaldo("dawn_deaths")
        elif hechos_noche.get("intervencion_misteriosa"):
            amanecer = self.respaldo("dawn_quiet")
        else:
            amanecer = self.respaldo("dawn_calm")
        return f"{amanecer} {self.respaldo('trial')}"

    def _cause_text(self, cause: str | None) -> str:
        """Cómo se cuenta esta muerte, entre las formas de su causa."""
        formas = CAUSES[self.lang].get(cause or "") or CAUSES[self.lang][""]
        return self.rng.choice(formas)

    def _texto(self) -> GroupText:
        """Compositor de un mensaje de grupo, con las menciones que acumule.

        Uno por mensaje: la lista de etiquetados que se manda a WAHA tiene que
        corresponder exactamente con ese texto.
        """
        return GroupText(enabled=self.settings.use_mentions)

    async def _group(
        self,
        text: str,
        *,
        record: bool = True,
        texto: GroupText | None = None,
    ) -> None:
        """Publica en el grupo. ``record=False`` para el relleno de espera."""
        await self.ctx.transport.send_group(
            text, mentions=texto.mentions if texto is not None else None
        )
        if record:
            self._narrated.append(text)

    def _etiqueta_nombres(
        self, text: str, players: list[Player], texto: GroupText
    ) -> str:
        """Convierte en menciones los nombres que el narrador haya escrito.

        El modelo recibe nombres y escribe prosa con ellos; aquí esa prosa
        pasa a señalar a la persona de verdad. Se ofrecen todos los jugadores
        y no sólo los vivos: el narrador también habla de quien acaba de caer.
        """
        contactos = {
            p.get("name", ""): p.get("jid", "") for p in players if p.get("name")
        }
        return tag_names(text, contactos, texto)

    @staticmethod
    def _voces(state: WerewolfState) -> dict[str, list[dict[str, str]]]:
        """Lo último que dijo el pueblo, para que la escena lo recoja.

        Se omite la clave cuando no hay nada —la primera noche, por ejemplo—
        porque un HECHO vacío sólo invita al modelo a rellenarlo por su
        cuenta, que es justo lo que no queremos.
        """
        dichas = list(state.get("debate_log") or [])
        return {"se_dijo": dichas} if dichas else {}

    def _flush_narrative(self) -> dict[str, list[str]]:
        """Devuelve lo narrado desde la última llamada, para el estado.

        Se mezcla en el diccionario que devuelve cada nodo, de modo que
        ``narrative_log`` acabe en el checkpoint sin que cada nodo tenga que
        acordarse de nada más.
        """
        if not self._narrated:
            return {}
        pendiente, self._narrated = self._narrated, []
        return {"narrative_log": pendiente}

    async def _dm(self, jid: str, text: str) -> None:
        await self.ctx.transport.send_direct(jid, text)

    async def _dm_all(self, pairs: list[tuple[str, str]]) -> None:
        """Envía varios privados con un tope de tiempo total.

        El transporte serializa los envíos para no disparar el rate limit de
        WhatsApp, así que un WAHA lento multiplicaría su latencia por el número
        de jugadores. El presupuesto corta ahí: quien no reciba su privado
        simplemente no actúa esa noche, que el juego ya lo contempla.
        """
        if not pairs:
            return

        budget = dm_budget(len(pairs))
        gather = asyncio.gather(
            *(self._dm(jid, text) for jid, text in pairs), return_exceptions=True
        )
        try:
            await asyncio.wait_for(gather, timeout=budget)
        except TimeoutError:
            # CancelledError no se captura a propósito: si la cancelación viene
            # de fuera (`#cancelar`, apagado) tiene que seguir su camino.
            log.warning(
                "werewolf.dm_budget_exceeded",
                session_id=self.ctx.session_id,
                mensajes=len(pairs),
                budget=budget,
            )

    @staticmethod
    async def _stop_task(task: asyncio.Task[None]) -> None:
        """Cancela una tarea auxiliar y se traga lo que devuelva.

        El relleno y los recordatorios son decorativos: ni su cancelación ni un
        fallo suyo pueden salir por el ``finally`` del nodo y tumbar la partida.
        """
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            log.warning("werewolf.aux_task_failed", error=str(exc))

    @contextlib.asynccontextmanager
    async def _fillers(self, seconds: float, *, round_no: int):
        """Manda ambientación al grupo mientras se espera.

        Es la espera ciega de la noche. El juicio no la usa: allí se escucha
        lo que se dice y se comenta (ver :meth:`_listen_to_debate`).
        """
        task: asyncio.Task[None] | None = None
        if self.timers.filler_interval > 0 and seconds > self.timers.filler_interval * 1.5:
            task = asyncio.create_task(self._filler_loop(seconds, round_no=round_no))
        try:
            yield
        finally:
            if task is not None:
                await self._stop_task(task)

    async def _filler_loop(self, seconds: float, *, round_no: int) -> None:
        interval = self.timers.filler_interval
        # Plazo por reloj y no suma de esperas: entre vuelta y vuelta hay una
        # llamada al modelo, y contar sólo los `sleep` haría que la cuenta
        # atrás anunciara tiempo que ya no queda.
        deadline = time.monotonic() + seconds
        index = 0
        while True:
            restante = deadline - time.monotonic()
            if restante <= interval * 0.5:
                return
            await asyncio.sleep(min(interval, restante))
            if deadline - time.monotonic() <= interval * 0.5:
                return
            await self._ambience(
                "relleno", round_no=round_no, deadline=deadline, index=index
            )
            index += 1

    async def _ambience(
        self,
        scene: str,
        *,
        round_no: int,
        deadline: float,
        index: int,
        max_words: int = 35,
        extra: dict[str, Any] | None = None,
        players: list[Player] | None = None,
    ) -> None:
        """Publica ambientación mientras se espera, si aún queda ventana.

        Con ``players`` se etiquetan los nombres que salgan en la narración.
        La espera de la noche no los pasa —nadie debería ser nombrado ahí—,
        pero el comentario del juicio vive precisamente de decir quién señaló
        a quién.

        Lo usan las esperas de la noche y el juicio. La cuenta atrás se toma
        del plazo en dos momentos —al pedir el texto y al enviarlo— porque
        entre ambos hay una llamada al modelo: anunciar el tiempo que quedaba
        antes de esperarla sería mentir, y publicar cuando ya se agotó pondría
        el mensaje encima de la fase siguiente.
        """
        try:
            facts: dict[str, Any] = {
                "ronda": round_no,
                "segundos_restantes": int(max(0.0, deadline - time.monotonic())),
            }
            if extra:
                facts.update(extra)
            text = await self.narrator.flavour(
                scene,
                facts,
                fallback=prompts.filler_for(index, self.lang),
                max_words=max_words,
                remember=False,
            )
            restante = deadline - time.monotonic()
            if restante <= 0:
                return
            texto = self._texto()
            if players:
                text = self._etiqueta_nombres(text, players, texto)
            await self._group(
                f"{text}\n\n" + self.t("wait.remaining", time=self._seconds(restante)),
                record=False,
                texto=texto,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            # La ambientación es prescindible: si el modelo o WAHA fallan, se
            # calla y la fase agota su ventana igual.
            log.warning(
                "werewolf.ambience_failed", scene=scene, round_no=round_no, error=str(exc)
            )
            index += 1

    async def _reminder(self, delay: float, text: str) -> asyncio.Task[None]:
        """Programa un único recordatorio al grupo."""

        async def _run() -> None:
            await asyncio.sleep(delay)
            await self._group(text, record=False)

        return asyncio.create_task(_run())

    @staticmethod
    def _text_of(message: InboundMessage) -> str:
        """El contenido útil de un mensaje (un voto de encuesta trae opciones)."""
        return ", ".join(message.poll_options) if message.poll_options else message.text

    @classmethod
    def _stop_when_resolved(cls, predicates: dict[str, Callable[[str], bool]]):
        """Corta la espera cuando todos han dado una respuesta *interpretable*.

        No basta con que hayan escrito algo: si alguien manda "ok" y luego su
        elección, parar en el "ok" perdería la respuesta de verdad.
        """
        if not predicates:
            return None

        def _stop(collected: list[InboundMessage]) -> bool:
            pending = set(predicates)
            for message in collected:
                if message.from_me or message.sender_id not in pending:
                    continue
                if predicates[message.sender_id](cls._text_of(message)):
                    pending.discard(message.sender_id)
            return not pending

        return _stop

    @staticmethod
    def _last_per_sender(messages: list[InboundMessage]) -> dict[str, InboundMessage]:
        """El último mensaje de cada remitente (rectificar está permitido)."""
        latest: dict[str, InboundMessage] = {}
        for message in messages:
            if message.from_me:
                continue
            latest[message.sender_id] = message
        return latest

    def _targets_block(self, players: list[Player], *, exclude: set[str] | None = None) -> str:
        exclude = exclude or set()
        options = [p for p in alive(players) if p.get("jid") not in exclude]
        return "\n".join(label(p) for p in sorted(options, key=lambda p: p.get("number", 0)))

    # ========================================================= fase 1: leva
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

    # ====================================================== fase 1b: reparto
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

    # ====================================================== fase 2: la noche
    async def noche_inicio(self, state: WerewolfState) -> dict[str, Any]:
        """Narra la noche y recoge en paralelo lobos, vidente y Cupido."""
        players = list(state["players"])
        round_no = state["round_no"]
        session_id = state["session_id"]

        await self.ctx.transport.set_group_locked(True)

        pack = wolves(players)
        seer = with_role(players, Role.VIDENTE)
        cupid = with_role(players, Role.CUPIDO) if round_no == 1 and not state.get("lovers") else []
        # Los objetivos se resuelven contra los vivos, que es lo que se muestra
        # en el privado. Contra la lista completa, escribir el número de un
        # muerto (los números no se reciclan) desperdiciaría la acción.
        vivos = alive(players)

        actors = pack + seer + cupid
        actor_jids = [p["jid"] for p in actors]
        # Se limpia antes de pedir para que no cuente lo que alguien escribió
        # de más en la ronda anterior.
        await self.ctx.inbox.clear(session_id, keys=[f"dm:{jid}" for jid in actor_jids])

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
        texto_noche = self._texto()
        night = self._etiqueta_nombres(night, players, texto_noche)
        await self._group(
            self.t(
                "night.open",
                round_no=round_no,
                flavour=night,
                time=self._seconds(self.timers.night),
            ),
            texto=texto_noche,
        )

        prompts_to_send: list[tuple[str, str]] = []
        wolf_names = ", ".join(p["name"] for p in pack)
        wolf_exclude = {p["jid"] for p in pack}
        for wolf in pack:
            prompts_to_send.append(
                (
                    wolf["jid"],
                    self.t(
                        "night.wolf_prompt",
                        round_no=round_no,
                        targets=self._targets_block(players, exclude=wolf_exclude),
                        note=(
                            self.t("night.wolf_pack_note", names=wolf_names)
                            if len(pack) > 1
                            else self.t("night.wolf_alone_note")
                        ),
                    ),
                )
            )
        for oracle in seer:
            prompts_to_send.append(
                (
                    oracle["jid"],
                    self.t(
                        "night.seer_prompt",
                        round_no=round_no,
                        targets=self._targets_block(players, exclude={oracle["jid"]}),
                    ),
                )
            )
        for love in cupid:
            prompts_to_send.append(
                (
                    love["jid"],
                    self.t(
                        "night.cupid_prompt",
                        targets=self._targets_block(players),
                    ),
                )
            )

        await self._dm_all(prompts_to_send)

        # Cada rol se considera "resuelto" con distinto criterio.
        predicates: dict[str, Callable[[str], bool]] = {}
        for wolf in pack:
            predicates[wolf["jid"]] = (
                lambda text: parse_player_reference(
                    text, vivos, exclude=list(wolf_exclude)
                )
                is not None
            )
        for oracle in seer:
            jid = oracle["jid"]
            predicates[jid] = (
                lambda text, _jid=jid: parse_player_reference(
                    text, vivos, exclude=[_jid]
                )
                is not None
            )
        for love in cupid:
            predicates[love["jid"]] = (
                lambda text: parse_two_player_references(text, vivos) is not None
            )

        collected: list[InboundMessage] = []
        if actor_jids:
            async with self._fillers(self.timers.night, round_no=round_no):
                collected = await self.ctx.inbox.collect(
                    session_id,
                    timeout=self.timers.night,
                    direct=actor_jids,
                    stop_when=self._stop_when_resolved(predicates),
                )

        latest = self._last_per_sender(collected)
        night_actions: dict[str, Any] = {}
        updates: dict[str, Any] = {"night_actions": night_actions, "phase": "noche"}

        # --- lobos: mayoría entre lo que hayan pedido -------------------
        wolf_votes: dict[str, str] = {}
        for wolf in pack:
            message = latest.get(wolf["jid"])
            if message is None:
                continue
            target = parse_player_reference(message.text, vivos, exclude=list(wolf_exclude))
            if target is not None:
                wolf_votes[wolf["jid"]] = target["jid"]

        if wolf_votes:
            top, _ = tally_votes(wolf_votes)
            chosen = self.rng.choice(sorted(top)) if len(top) > 1 else top[0]
            night_actions["wolf_target"] = chosen
            night_actions["wolf_votes"] = wolf_votes
            victim = by_jid(players, chosen)
            if len(pack) > 1:
                nombre = victim["name"] if victim else chosen
                clave = "night.wolves_decided" if len(top) == 1 else "night.wolves_tie"
                aviso = self.t(clave, name=nombre)
                await self._dm_all([(w["jid"], aviso) for w in pack])
        else:
            # Sin respuesta no hay ataque: mejor una noche en calma que una
            # muerte decidida por el sistema.
            night_actions["wolf_target"] = None
            log.info("werewolf.wolves_silent", session_id=session_id, round_no=round_no)

        # --- vidente: se le responde en el acto -------------------------
        for oracle in seer:
            message = latest.get(oracle["jid"])
            if message is None:
                await self._dm(oracle["jid"], self.t("night.seer_timeout"))
                continue
            target = parse_player_reference(
                message.text, vivos, exclude=[oracle["jid"]]
            )
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

        # --- cupido: enamora a dos --------------------------------------
        for love in cupid:
            message = latest.get(love["jid"])
            pair = (
                parse_two_player_references(message.text, vivos) if message else None
            )
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

        await self.ctx.record(
            "noche.acciones",
            round_no=round_no,
            phase="noche",
            detail=night_actions,
            is_secret=True,
        )
        updates.update(self._flush_narrative())
        return updates

    # ================================================== fase 2b: la bruja
    def necesita_bruja(self, state: WerewolfState) -> str:
        """Router: la bruja sólo se consulta si está viva y le quedan pociones."""
        witch = with_role(list(state["players"]), Role.BRUJA)
        potions = state.get("witch_potions") or {}
        if witch and any(potions.values()):
            return "noche_bruja"
        return "resolucion"

    async def noche_bruja(self, state: WerewolfState) -> dict[str, Any]:
        """Informa a la bruja de la víctima y recoge su decisión."""
        players = list(state["players"])
        round_no = state["round_no"]
        session_id = state["session_id"]
        night_actions = dict(state.get("night_actions") or {})
        potions = dict(state.get("witch_potions") or {"vida": True, "muerte": True})

        brujas = with_role(players, Role.BRUJA)
        if not brujas:
            # El router `necesita_bruja` ya lo comprueba; esto es un cinturón
            # de seguridad: una excepción aquí dejaría el grupo silenciado.
            log.warning("werewolf.no_witch", session_id=session_id, round_no=round_no)
            return {"night_actions": night_actions, "witch_potions": potions}
        witch = brujas[0]
        await self.ctx.inbox.clear(session_id, keys=[f"dm:{witch['jid']}"])

        target_jid = night_actions.get("wolf_target")
        victim = by_jid(players, target_jid) if target_jid else None

        disponibles = []
        if potions.get("vida"):
            disponibles.append(self.t("witch.potion_life"))
        if potions.get("muerte"):
            disponibles.append(self.t("witch.potion_death"))

        if victim is not None:
            cabecera = self.t("witch.attacked", name=victim["name"])
        else:
            cabecera = self.t("witch.quiet")

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

        async with self._fillers(self.timers.witch, round_no=round_no):
            collected = await self.ctx.inbox.collect(
                session_id,
                timeout=self.timers.witch,
                direct=[witch["jid"]],
                stop_when=self._stop_when_resolved({witch["jid"]: witch_decided}),
            )

        latest = self._last_per_sender(collected)
        message = latest.get(witch["jid"])
        if message is None:
            await self._dm(witch["jid"], self.t("witch.timeout"))
            return {
                "night_actions": night_actions,
                "witch_potions": potions,
                **self._flush_narrative(),
            }

        choice = parse_witch_choice(message.text)

        if choice == "vida" and potions.get("vida") and victim is not None:
            potions["vida"] = False
            night_actions["witch_heal"] = victim["jid"]
            await self._dm(
                witch["jid"], self.t("witch.healed", name=victim["name"])
            )
        elif choice == "vida":
            motivo = self.t(
                "witch.reason_used"
                if not potions.get("vida")
                else "witch.reason_nobody"
            )
            await self._dm(witch["jid"], self.t("witch.cannot_heal", reason=motivo))
        elif choice == "muerte" and potions.get("muerte"):
            poison_target = parse_player_reference(
                message.text, alive(players), exclude=[witch["jid"]]
            )
            if poison_target is None:
                await self._dm(witch["jid"], self.t("witch.poison_unclear"))
            else:
                potions["muerte"] = False
                night_actions["witch_poison"] = poison_target["jid"]
                await self._dm(
                    witch["jid"],
                    self.t("witch.poisoned", name=poison_target["name"]),
                )
        elif choice == "muerte":
            await self._dm(witch["jid"], self.t("witch.death_used"))
        else:
            await self._dm(witch["jid"], self.t("witch.abstain"))

        await self.ctx.record(
            "noche.bruja",
            round_no=round_no,
            phase="noche",
            detail={"eleccion": choice, "pociones": potions},
            is_secret=True,
        )
        return {
            "night_actions": night_actions,
            "witch_potions": potions,
            **self._flush_narrative(),
        }

    # ============================================ fase 3: resolución nocturna
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

    # ================================================== fase 3b: el amanecer
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

    def _dawn_block(
        self, state: WerewolfState, players: list[Player], texto: GroupText
    ) -> tuple[str, dict[str, Any]]:
        """Las muertes de la noche, en mecánica y en HECHOS.

        Devuelve las dos formas juntas porque salen de lo mismo y se usan en el
        mismo sitio: el bloque va al mensaje y los hechos, a la narración que
        lo acompaña. Las etiquetas se acumulan en ``texto``, así que hay que
        llamarlo con el compositor del mensaje que va a llevarlas.
        """
        deaths = list(state.get("deaths_last_night") or [])
        victims = [v for v in (by_jid(players, jid) for jid in deaths) if v is not None]
        vivos = len(alive(players))

        if not victims:
            saved = bool((state.get("night_actions") or {}).get("saved"))
            hechos = {
                "victimas": [],
                "intervencion_misteriosa": saved,
                "supervivientes": vivos,
            }
            return self.t("day.nobody_died"), hechos

        lineas = [self._death_line(victim, texto) for victim in victims]
        lineas.append("")
        lineas.append(self.t("day.ignore_the_fallen"))
        hechos = {
            "victimas": [
                {"nombre": v["name"], "causa": v.get("death_cause")} for v in victims
            ],
            "supervivientes": vivos,
        }
        return "\n".join(lineas), hechos

    def _death_line(self, victim: Player, texto: GroupText) -> str:
        """Una muerte, contada con algo más que un parte médico."""
        causa = self._cause_text(victim.get("death_cause"))
        etiqueta = tag(victim, texto)
        if self.settings.werewolf_reveal_role_on_death:
            return self.t(
                "day.death_line",
                tag=etiqueta,
                cause=causa,
                role=role_title(victim, self.lang),
            )
        return self.t("day.death_line_plain", tag=etiqueta, cause=causa)

    # ============================================ evaluación de la victoria
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

    def ruta_tras_reclutamiento(self, state: WerewolfState) -> str:
        return "final" if state.get("finished") else "reparto"

    # ==================================================== fase 4: el juicio
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

    async def veredicto(self, state: WerewolfState) -> dict[str, Any]:
        """Lincha al más votado, revela su rol y cierra el día."""
        players = list(state["players"])
        round_no = state["round_no"]
        session_id = state["session_id"]
        votes = dict(state.get("votes") or {})

        top, count = tally_votes(votes)
        # El recuento y el anuncio comparten compositor: las menciones que
        # acumule el listado de votos tienen que viajar con el mismo mensaje.
        texto_recuento = self._texto()
        recuento = votes_breakdown(
            votes, players, texto_recuento, self.lang
        )

        lynched_jid: str | None = None
        if len(top) == 1:
            lynched_jid = top[0]
        elif len(top) > 1 and self.settings.werewolf_tie_break == "random":
            lynched_jid = self.rng.choice(sorted(top))

        if lynched_jid is None:
            flavour = await self.narrator.flavour(
                "sin_linchamiento",
                {
                    "ronda": round_no,
                    "empate": len(top) > 1,
                    **self._voces(state),
                },
                fallback=self.respaldo("no_lynch"),
                max_words=60,
            )
            flavour = self._etiqueta_nombres(flavour, players, texto_recuento)
            await self._group(
                self.t("verdict.no_lynch", tally=recuento, flavour=flavour),
                texto=texto_recuento,
            )
            await self.ctx.record(
                "veredicto.sin_linchamiento",
                round_no=round_no,
                phase="veredicto",
                detail={"empatados": len(top)},
            )
            return {
                "players": players,
                "lynched": None,
                "votes": {},
                "round_no": round_no + 1,
                "phase": "noche",
                "resume_to": "noche",
                "deaths_last_night": [],
                **self._flush_narrative(),
            }

        condenado = by_jid(players, lynched_jid)
        players, deaths = await self._apply_deaths(
            players,
            [(lynched_jid, "linchamiento")],
            round_no=round_no,
            lovers=list(state.get("lovers") or []),
            session_id=session_id,
        )

        flavour = await self.narrator.flavour(
            "veredicto",
            {
                "ronda": round_no,
                "linchado": condenado["name"] if condenado else lynched_jid,
                "votos": count,
                "rol_revelado": (
                    info(condenado["role"], self.lang).title if condenado else None
                ),
                "se_dijo": list(state.get("debate_log") or []),
            },
            fallback=self.respaldo("verdict"),
            max_words=80,
        )

        flavour = self._etiqueta_nombres(flavour, players, texto_recuento)
        lineas = [
            self.t("verdict.header", round_no=round_no), "", recuento, "", flavour, "",
        ]
        for death in deaths:
            victim = by_jid(players, death["jid"])
            titulo = role_title(victim, self.lang) if victim else death["role"]
            etiqueta = tag(victim, texto_recuento) if victim else death["name"]
            lineas.append(
                self.t(
                    "day.death_line",
                    tag=etiqueta,
                    cause=self._cause_text(death["cause"]),
                    role=titulo,
                )
            )
        vivos = alive(players)
        lineas.extend(
            [
                "",
                self.t("day.ignore_the_fallen"),
                "",
                self.t("verdict.still_alive", count=len(vivos)),
                tagged_roster(players, texto_recuento),
            ]
        )

        await self._group("\n".join(lineas), texto=texto_recuento)
        await self.ctx.record(
            "veredicto",
            round_no=round_no,
            phase="veredicto",
            detail={"linchado": condenado["name"] if condenado else lynched_jid,
                    "muertes": [d["name"] for d in deaths]},
        )

        return {
            "players": players,
            "lynched": lynched_jid,
            "votes": {},
            "round_no": round_no + 1,
            "phase": "noche",
            "resume_to": "noche",
            "deaths_last_night": [d["jid"] for d in deaths],
            **self._flush_narrative(),
        }

    # ======================================================= fase 5: cierre
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



