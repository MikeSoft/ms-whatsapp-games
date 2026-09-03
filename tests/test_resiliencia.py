"""Rutas de fallo: WAHA caído, transporte lento, tareas auxiliares que revientan.

Todas comparten el mismo criterio: la partida puede degradarse o abortar, pero
**nunca** debe colgarse ni dejar el grupo silenciado.
"""

from __future__ import annotations

import asyncio

import pytest

from app.core.inbox import MemoryInbox, RedisInbox
from app.core.llm import LLMClient
from app.games.transport import WahaTransport
from app.games.werewolf.game import WerewolfGame
from app.games.werewolf.nodes import DM_BUDGET_MAX, Timers, WerewolfNodes, dm_budget
from app.orchestrator.manager import Orchestrator
from app.waha.client import WahaClient
from app.waha.models import Scope, SentMessage
from tests.conftest import (
    GROUP_ID,
    MANAGER,
    FakeTransport,
    ScriptedPlayers,
    fast_timers,
    inbound,
    make_context,
    make_jid,
    make_settings,
)


class ExplosiveTransport(FakeTransport):
    """Transporte que falla al enviar al grupo a partir del n-ésimo mensaje."""

    def __init__(self, fail_after: int = 2) -> None:
        super().__init__()
        self.fail_after = fail_after

    async def send_group(self, text: str, *, mentions: list[str] | None = None) -> None:
        if len(self.group_messages) >= self.fail_after:
            raise RuntimeError("WAHA se cayó a mitad de la partida")
        await super().send_group(text, mentions=mentions)


class SlowSerialTransport(FakeTransport):
    """Transporte lento que además serializa, como el cliente real de WAHA.

    Es la combinación peligrosa: los envíos se encolan tras un lock para no
    disparar el rate limit de WhatsApp, así que la latencia de uno se suma a
    la de todos los demás.
    """

    def __init__(self, delay: float) -> None:
        super().__init__()
        self.delay = delay
        self._lock = asyncio.Lock()

    async def send_direct(self, jid: str, text: str) -> None:
        async with self._lock:
            await asyncio.sleep(self.delay)
            await super().send_direct(jid, text)


def _mesa(count: int, transport: FakeTransport, **script_kwargs):
    jids = [make_jid(i) for i in range(1, count + 1)]
    names = {jid: f"Jugador{i}" for i, jid in enumerate(jids, start=1)}
    inbox = MemoryInbox()
    ctx = make_context(transport=transport, inbox=inbox)
    script = ScriptedPlayers(
        inbox=inbox, session_id=ctx.session_id, jids=jids, names=names, **script_kwargs
    )
    transport.on_group = script.on_group
    transport.on_direct = script.on_direct
    return ctx, inbox, script


# ============================================================ el juego solo
async def test_un_fallo_de_envio_propaga_para_que_el_supervisor_limpie():
    """El juego no disimula un WAHA caído: lo propaga.

    Tragárselo dejaría una partida zombi silenciando el grupo. El orquestador
    es quien tiene que reabrirlo y avisar al máster.
    """
    transport = ExplosiveTransport(fail_after=2)
    ctx, _inbox, _script = _mesa(6, transport)
    game = WerewolfGame(ctx, timers=fast_timers())

    with pytest.raises(RuntimeError, match="WAHA se cayó"):
        await game.run()


async def test_un_envio_masivo_lento_se_corta_por_presupuesto():
    """20 privados a 5 s serializados serían 100 s; el tope los deja en ~20.

    Sin este límite, un WAHA lento multiplica su latencia por el número de
    jugadores y el nodo se queda colgado más allá de su propia ventana.
    """
    transport = SlowSerialTransport(delay=5.0)
    ctx, _inbox, _script = _mesa(2, transport)
    nodes = WerewolfNodes(ctx, timers=fast_timers())

    pares = [(make_jid(i), f"privado {i}") for i in range(1, 21)]
    presupuesto = dm_budget(len(pares))
    # El techo es absoluto: si el presupuesto creciera con el número de
    # jugadores, nunca llegaría a cortar nada.
    assert presupuesto <= DM_BUDGET_MAX

    loop = asyncio.get_running_loop()
    inicio = loop.time()
    await nodes._dm_all(pares)
    transcurrido = loop.time() - inicio

    assert transcurrido < presupuesto + 2, "el envío masivo tenía que cortarse"
    assert transcurrido < 60, "sin tope habrían sido 20 x 5 s = 100 s"
    # Algunos llegaron; el resto se descartó, que es el compromiso aceptado.
    assert 0 < len(transport.direct_messages) < len(pares)


async def test_un_relleno_que_revienta_no_mata_la_partida():
    """La ambientación es decorativa: su fallo se registra y se sigue.

    Sólo se rompe la narración de relleno; la de las escenas sigue viva, que
    es exactamente el caso que importa: una tarea de fondo que peta no puede
    salir por el `finally` del nodo y tumbar la partida.
    """
    transport = FakeTransport()
    ctx, _inbox, _script = _mesa(6, transport)

    game = WerewolfGame(
        ctx,
        # filler_interval pequeño respecto al debate: el relleno se activa.
        timers=Timers(
            recruit=0.05, night=0.4, witch=0.4, hunter=0.4,
            debate=0.5, vote=0.4, filler_interval=0.1,
        ),
    )

    original = game.nodes.narrator.flavour
    escenas_relleno = {"relleno", "debate"}

    async def revienta_solo_el_relleno(scene, facts, **kwargs):
        if scene in escenas_relleno:
            raise RuntimeError("el narrador se atragantó")
        return await original(scene, facts, **kwargs)

    game.nodes.narrator.flavour = revienta_solo_el_relleno  # type: ignore[method-assign]

    result = await game.run()
    assert result.status == "finished"
    assert result.winner in {"lobos", "pueblo", "enamorados", "nadie"}
    assert transport.locked is False


# ====================================================== con el orquestador
@pytest.fixture
def orq(monkeypatch):
    outbox: list[tuple[str, str]] = []
    locks: list[bool] = []

    async def fake_send_text(self, chat_id, text, *, reply_to=None, mentions=None):
        outbox.append((chat_id, text))
        return SentMessage(ok=True, chat_id=chat_id, message_id="m")

    async def fake_admins_only(self, group_id, admins_only):
        locks.append(admins_only)
        return True

    monkeypatch.setattr(WahaClient, "send_text", fake_send_text)
    monkeypatch.setattr(WahaClient, "set_admins_only", fake_admins_only)

    settings = make_settings(
        recruit_seconds=1, night_action_seconds=1, witch_action_seconds=1,
        hunter_action_seconds=1, debate_seconds=1, vote_seconds=1,
        filler_interval_seconds=0,
    )
    orch = Orchestrator(
        settings=settings,
        waha=WahaClient(settings),
        inbox=MemoryInbox(),
        store=None,
        llm=LLMClient(settings),
        checkpointer=None,
    )
    return orch, outbox, locks


async def test_una_partida_que_falla_reabre_el_grupo_y_avisa_al_master(orq, monkeypatch):
    """Un fallo a media noche no puede dejar el grupo mudo para siempre."""
    orch, outbox, locks = orq
    llamadas = {"n": 0}

    async def envio_roto(self, text, *, mentions=None):
        llamadas["n"] += 1
        if llamadas["n"] > 1:
            raise RuntimeError("boom")

    monkeypatch.setattr(WahaTransport, "send_group", envio_roto)

    await orch.handle(
        inbound(MANAGER, "!juego hombreslobo", scope=Scope.GROUP, chat_id=GROUP_ID)
    )
    for index in range(1, 6):
        await orch.handle(
            inbound(
                make_jid(index), "Yo", scope=Scope.GROUP,
                chat_id=GROUP_ID, name=f"Jugador{index}",
            )
        )

    running = next(iter(orch._games.values()))
    await asyncio.wait({running.task})
    await asyncio.sleep(0.05)

    # La partida se soltó y el grupo quedó reabierto.
    assert orch.snapshot()["partidas_activas"] == []
    assert locks and locks[-1] is False
    # Y el máster se enteró.
    avisos = [text for chat, text in outbox if chat == MANAGER]
    assert any("falló" in text for text in avisos)


async def test_apagar_con_una_partida_a_medias_no_deja_nada_colgado(orq):
    orch, _outbox, locks = orq
    await orch.handle(
        inbound(MANAGER, "!juego hombreslobo", scope=Scope.GROUP, chat_id=GROUP_ID)
    )
    running = next(iter(orch._games.values()))

    await orch.shutdown()

    assert running.task.done()
    assert orch.snapshot()["partidas_activas"] == []
    assert locks and locks[-1] is False


class BrokenRedis:
    """Cliente de Redis que falla en todo."""

    def pipeline(self):
        return self

    def rpush(self, *_a, **_k):
        return self

    def expire(self, *_a, **_k):
        return self

    def sadd(self, *_a, **_k):
        return self

    async def execute(self):
        raise ConnectionError("Redis se fue")


async def test_un_redis_caido_no_aborta_el_procesamiento_del_webhook():
    """Un `push` fallido se registra y se sigue.

    Propagar no serviría de nada: el `message_id` ya quedó marcado como visto,
    así que el reintento de WAHA se descartaría igual. Se pierde ese mensaje,
    no el resto del procesamiento.
    """
    inbox = RedisInbox(BrokenRedis(), ttl_seconds=60)  # type: ignore[arg-type]
    mensaje = inbound(make_jid(1), "Yo", scope=Scope.GROUP, chat_id=GROUP_ID)

    # No lanza: el servicio sigue en pie.
    await inbox.push("s-1", mensaje)

