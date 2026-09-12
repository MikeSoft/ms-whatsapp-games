"""Plumbing compartido por todos los nodos: hablar, esperar y medir el tiempo.

Aquí no hay ninguna fase del juego. Están los presupuestos de tiempo, el
compositor de mensajes, el envío masivo de privados, la ambientación de las
esperas y los pequeños ayudantes que todas las fases usan. Cada fase vive en
su propio módulo y hereda de :class:`NodeBase`.
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
from app.games.werewolf import prompts
from app.games.werewolf.narrator import Narrator
from app.games.werewolf.state import (
    Player,
    WerewolfState,
    alive,
    by_jid,
    label,
    role_title,
    tag,
)
from app.games.werewolf.texts import CAUSES, TEXTS
from app.i18n import Texts
from app.logging_conf import get_logger
from app.waha.models import InboundMessage

log = get_logger("werewolf")


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


class NodeBase:
    """Lo que toda fase necesita para hablar con el grupo y con la gente."""

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
