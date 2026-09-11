"""Paralelismo real: acciones simultáneas, dos partidas a la vez, escrituras concurrentes.

El servicio hace varias cosas al mismo tiempo por diseño: el webhook encola
mientras el grafo recoge, los roles nocturnos contestan a la vez, y SQLite
recibe el histórico mientras la partida anota su traza. Estos tests fuerzan esa
simultaneidad en vez de asumirla.
"""

from __future__ import annotations

import asyncio
import random
import shutil
import tempfile
from contextlib import AsyncExitStack
from pathlib import Path

import pytest
from sqlalchemy import text as sql

from app.core.checkpointer import open_checkpointer
from app.core.db import Store
from app.core.inbox import MemoryInbox, RedisInbox
from app.core.llm import LLMClient
from app.games.werewolf.game import WerewolfGame
from app.games.werewolf.nodes import Timers, WerewolfNodes
from app.games.werewolf.roles import Role
from app.games.werewolf.state import Player, initial_state
from app.main import build_redis_client
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
    render_mentions,
)

pytest.importorskip("fakeredis")

import fakeredis
from fakeredis.aioredis import FakeRedis
from redis.asyncio import BlockingConnectionPool


def _fake_redis(max_connections: int = 32) -> FakeRedis:
    """Fakeredis con el mismo tipo de pool que monta la aplicación."""
    pool = BlockingConnectionPool(
        max_connections=max_connections,
        timeout=10,
        connection_class=fakeredis.aioredis.FakeConnection,
        decode_responses=True,
    )
    return FakeRedis(connection_pool=pool)


@pytest.fixture
def tmpdir_path():
    ruta = Path(tempfile.mkdtemp(prefix="wag-test-"))
    yield ruta
    shutil.rmtree(ruta, ignore_errors=True)


def _player(number: int, role: Role) -> Player:
    return Player(
        jid=make_jid(number),
        name=f"Jugador{number}",
        number=number,
        role=str(role),
        alive=True,
        death_round=None,
        death_cause=None,
    )


async def _espera_hasta(condicion, *, timeout: float = 15.0) -> bool:
    """Sondea hasta que se cumple una condición o expira el plazo."""
    loop = asyncio.get_running_loop()
    limite = loop.time() + timeout
    while loop.time() < limite:
        if condicion():
            return True
        await asyncio.sleep(0.02)
    return False


def _mesa(count: int, transport: FakeTransport, inbox, **kwargs):
    jids = [make_jid(i) for i in range(1, count + 1)]
    names = {jid: f"Jugador{i}" for i, jid in enumerate(jids, start=1)}
    ctx = make_context(transport=transport, inbox=inbox, **kwargs)
    script = ScriptedPlayers(
        inbox=inbox, session_id=ctx.session_id, jids=jids, names=names
    )
    transport.on_group = script.on_group
    transport.on_direct = script.on_direct
    return ctx, script


# =====================================================================
# Acciones nocturnas simultáneas
# =====================================================================
async def test_todos_los_roles_nocturnos_contestan_a_la_vez():
    """Lobos, vidente y Cupido responden en el mismo instante.

    El nodo abre una sola ventana para los tres y tiene que quedarse con la
    respuesta correcta de cada uno, sin mezclarlas ni perder ninguna.
    """
    transport = FakeTransport()
    inbox = MemoryInbox()
    ctx = make_context(transport=transport, inbox=inbox)
    nodes = WerewolfNodes(
        ctx,
        timers=Timers(night=3.0, filler_interval=0.0),
        rng=random.Random(5),
    )

    players = [
        _player(1, Role.LOBO),
        _player(2, Role.LOBO),
        _player(3, Role.VIDENTE),
        _player(4, Role.CUPIDO),
        _player(5, Role.ALDEANO),
        _player(6, Role.ALDEANO),
        _player(7, Role.ALDEANO),
        _player(8, Role.BRUJA),
    ]
    state = dict(initial_state("s-par", GROUP_ID))
    state["players"] = players
    state["round_no"] = 1

    respuestas = {
        players[0]["jid"]: "5",   # lobo A
        players[1]["jid"]: "5",   # lobo B (mayoría clara)
        players[2]["jid"]: "1",   # vidente pregunta por el lobo 1
        players[3]["jid"]: "6 7",  # cupido enamora a 6 y 7
    }
    pendientes: list[asyncio.Task] = []

    async def responde(jid, text):
        if jid in respuestas and (
            "devoráis" in text or "identidad" in text or "Elige a dos" in text
        ):
            # Todos empujan a la vez, tras un jitter mínimo.
            async def empuja():
                await asyncio.sleep(random.random() * 0.05)
                await inbox.push("s-par", inbound(jid, respuestas[jid]))

            pendientes.append(asyncio.create_task(empuja()))

    transport.on_direct = responde
    updates = await nodes.noche_inicio(state)
    await asyncio.gather(*pendientes)

    acciones = updates["night_actions"]
    assert acciones["wolf_target"] == players[4]["jid"], "la manada eligió al 5"
    assert acciones["seer_query"] == players[0]["jid"]
    assert acciones["seer_result"] is True, "el 1 sí es lobo"
    assert updates["lovers"] == [players[5]["jid"], players[6]["jid"]]

    # La vidente recibió su respuesta y nadie más.
    videncias = [t for jid, t in transport.direct_messages if "Las cartas hablan" in t]
    assert len(videncias) == 1
    assert "ES UN HOMBRE LOBO" in videncias[0]


async def test_una_ventana_nocturna_se_cierra_en_cuanto_contestan_todos():
    """No espera los 60 s si los tres roles ya respondieron."""
    transport = FakeTransport()
    inbox = MemoryInbox()
    ctx = make_context(transport=transport, inbox=inbox)
    nodes = WerewolfNodes(
        ctx, timers=Timers(night=10.0, filler_interval=0.0), rng=random.Random(1)
    )
    players = [_player(1, Role.LOBO), _player(2, Role.VIDENTE), _player(3, Role.ALDEANO)]
    state = dict(initial_state("s-par2", GROUP_ID))
    state["players"] = players
    state["round_no"] = 2

    async def responde(jid, text):
        if "devoráis" in text or "identidad" in text:
            await inbox.push("s-par2", inbound(jid, "3"))

    transport.on_direct = responde

    loop = asyncio.get_running_loop()
    inicio = loop.time()
    await nodes.noche_inicio(state)
    assert loop.time() - inicio < 3.0, "no debía agotar la ventana entera"


async def test_una_votacion_con_mucho_ruido_cuenta_solo_los_votos():
    """Durante la votación llega charla, emojis y votos mezclados."""
    transport = FakeTransport()
    inbox = MemoryInbox()
    ctx = make_context(transport=transport, inbox=inbox)
    nodes = WerewolfNodes(
        ctx, timers=Timers(vote=2.0, filler_interval=0.0), rng=random.Random(2)
    )
    players = [_player(n, Role.ALDEANO) for n in range(1, 7)]
    players[0]["role"] = str(Role.LOBO)
    state = dict(initial_state("s-par3", GROUP_ID))
    state["players"] = players
    state["round_no"] = 1

    async def responde(text):
        if "*VOTACIÓN*" not in text:
            return

        async def rafaga(jid: str, mensajes: list[str]):
            for texto in mensajes:
                await asyncio.sleep(random.random() * 0.02)
                await inbox.push(
                    "s-par3",
                    inbound(jid, texto, scope=Scope.GROUP, chat_id=GROUP_ID),
                )

        guiones = [
            (players[0]["jid"], ["jajaja", "😂", "yo voto 2"]),
            (players[1]["jid"], ["esto es injusto", "1"]),
            (players[2]["jid"], ["nos vemos a las 3:30", "voto por el 1"]),
            (players[3]["jid"], ["paso"]),
            (players[4]["jid"], ["1"]),
            (players[5]["jid"], ["mmm", "no sé", "el 1 seguro"]),
        ]
        await asyncio.gather(*(rafaga(jid, msgs) for jid, msgs in guiones))

    transport.on_group = responde
    updates = await nodes.votacion(state)

    votos = updates["votes"]
    # El 4 se abstuvo; los demás votaron. La hora "3:30" no cuenta como voto.
    assert players[3]["jid"] not in votos
    assert votos[players[0]["jid"]] == players[1]["jid"]
    assert votos[players[2]["jid"]] == players[0]["jid"]
    assert len(votos) == 5

    veredicto = await nodes.veredicto({**state, "votes": votos})
    linchado = veredicto["lynched"]
    assert linchado == players[0]["jid"], "el 1 recibió 4 votos"


# =====================================================================
# El buzón bajo carga
# =====================================================================
@pytest.mark.parametrize("backend", ["memoria", "redis"])
async def test_el_buzon_no_pierde_mensajes_bajo_rafaga(backend):
    """200 mensajes empujados en paralelo mientras se recogen."""
    inbox = (
        MemoryInbox()
        if backend == "memoria"
        else RedisInbox(_fake_redis(), ttl_seconds=60)
    )
    total = 200
    esperados = {f"m{i}" for i in range(total)}

    async def empuja(i: int):
        await inbox.push(
            "s-carga",
            inbound(make_jid(i % 20), f"m{i}", scope=Scope.GROUP, chat_id=GROUP_ID),
        )

    async def recoge():
        return await inbox.collect(
            "s-carga",
            timeout=6.0,
            group=True,
            stop_when=lambda c: len(c) >= total,
        )

    tarea = asyncio.create_task(recoge())
    await asyncio.sleep(0.05)
    await asyncio.gather(*(empuja(i) for i in range(total)))
    recogidos = await tarea

    assert {m.text for m in recogidos} == esperados
    assert len(recogidos) == total


@pytest.mark.parametrize("backend", ["memoria", "redis"])
async def test_varias_partidas_no_se_mezclan_los_buzones(backend):
    inbox = (
        MemoryInbox()
        if backend == "memoria"
        else RedisInbox(_fake_redis(), ttl_seconds=60)
    )

    async def carga(session: str, marca: str):
        for i in range(50):
            await inbox.push(
                session,
                inbound(make_jid(i % 5), f"{marca}-{i}", scope=Scope.GROUP, chat_id=GROUP_ID),
            )

    await asyncio.gather(carga("A", "a"), carga("B", "b"))
    a, b = await asyncio.gather(
        inbox.collect("A", timeout=2.0, group=True, stop_when=lambda c: len(c) >= 50),
        inbox.collect("B", timeout=2.0, group=True, stop_when=lambda c: len(c) >= 50),
    )

    assert all(m.text.startswith("a-") for m in a)
    assert all(m.text.startswith("b-") for m in b)
    assert len(a) == len(b) == 50


# =====================================================================
# SQLite con escrituras concurrentes
# =====================================================================
async def test_sqlite_aguanta_escrituras_concurrentes_de_varias_fuentes(tmpdir_path):
    """El webhook registra mensajes mientras la partida anota su traza.

    Sin WAL y `busy_timeout`, esta mezcla da "database is locked".
    """
    store = Store(f"sqlite+aiosqlite:///{tmpdir_path}/games.db")
    await store.init()
    await store.create_game_session(
        "s-1", game_key="hombreslobo", group_id=GROUP_ID, started_by=MANAGER
    )

    mensajes = [
        inbound(make_jid(i % 12), f"texto {i}", scope=Scope.GROUP, chat_id=GROUP_ID)
        for i in range(150)
    ]

    await asyncio.gather(
        *(store.log_inbound(m, game_session_id="s-1") for m in mensajes),
        *(
            store.log_game_event(
                "s-1", round_no=i // 10, phase="noche", kind="prueba", detail={"i": i}
            )
            for i in range(150)
        ),
        *(store.log_outbound(GROUP_ID, f"salida {i}", game_session_id="s-1") for i in range(50)),
    )

    async with store.session() as session:
        entrantes = (
            await session.execute(sql("select count(*) from messages where direction='in'"))
        ).scalar()
        salientes = (
            await session.execute(sql("select count(*) from messages where direction='out'"))
        ).scalar()
        eventos = (await session.execute(sql("select count(*) from game_events"))).scalar()

    assert entrantes == 150, "no se perdió ningún mensaje entrante"
    assert salientes == 50
    assert eventos == 150
    await store.aclose()


async def test_dos_partidas_escriben_su_traza_sin_pisarse(tmpdir_path):
    store = Store(f"sqlite+aiosqlite:///{tmpdir_path}/dos.db")
    await store.init()
    for sid, grupo in (("A", "120a@g.us"), ("B", "120b@g.us")):
        await store.create_game_session(
            sid, game_key="hombreslobo", group_id=grupo, started_by=MANAGER
        )

    async def traza(sid: str):
        for i in range(60):
            await store.log_game_event(sid, round_no=i, phase="noche", kind=f"{sid}-{i}")

    await asyncio.gather(traza("A"), traza("B"))
    await asyncio.gather(
        store.finish_game_session("A", status="finished", winner="pueblo", rounds=3),
        store.finish_game_session("B", status="finished", winner="lobos", rounds=4),
    )

    async with store.session() as session:
        por_sesion = dict(
            (await session.execute(
                sql("select session_id, count(*) from game_events group by session_id")
            )).all()
        )
        ganadores = dict(
            (await session.execute(sql("select id, winner from game_sessions"))).all()
        )

    assert por_sesion == {"A": 60, "B": 60}
    assert ganadores == {"A": "pueblo", "B": "lobos"}
    await store.aclose()


# =====================================================================
# Dos partidas completas en paralelo
# =====================================================================
async def test_dos_partidas_completas_a_la_vez_con_checkpointer_real(tmpdir_path):
    """Dos grafos concurrentes sobre el mismo checkpointer de SQLite.

    Comparten una única conexión aiosqlite, así que es donde aparecería un
    bloqueo o una mezcla de estados entre hilos del grafo.
    """
    settings = make_settings(checkpointer="sqlite")
    settings = settings.model_copy(
        update={"checkpointer_path": str(tmpdir_path / "checkpoints.db")}
    )

    async with AsyncExitStack() as stack:
        checkpointer = await open_checkpointer(settings, stack)

        partidas = []
        for indice in range(2):
            transport = FakeTransport()
            inbox = MemoryInbox()
            ctx, _script = _mesa(
                6, transport, inbox, session_id=f"paralela-{indice}", settings=settings
            )
            ctx.checkpointer = checkpointer
            partidas.append(
                (WerewolfGame(ctx, timers=fast_timers(), rng=random.Random(indice)), transport)
            )

        resultados = await asyncio.gather(*(game.run() for game, _ in partidas))

    for (game, transport), result in zip(partidas, resultados, strict=True):
        assert result.status == "finished"
        assert result.winner in {"lobos", "pueblo", "enamorados", "nadie"}
        assert len(result.players) == 6
        assert transport.locked is False
        # Cada partida narró su propia historia, no la de la otra.
        assert game.last_state["session_id"] == game.ctx.session_id

    # Los estados no se contaminaron entre sí.
    a, b = (game.last_state for game, _ in partidas)
    assert {p["jid"] for p in a["players"]} == {p["jid"] for p in b["players"]}
    assert a["narrative_log"] != b["narrative_log"]


async def test_el_orquestador_lleva_dos_grupos_en_paralelo(monkeypatch):
    """Cada grupo tiene su propia partida: se juega donde se pide."""
    enviados: list[tuple[str, str]] = []

    async def fake_send_text(self, chat_id, text, *, reply_to=None, mentions=None):
        enviados.append((chat_id, text))
        return SentMessage(ok=True, chat_id=chat_id, message_id="m")

    async def fake_send_poll(self, chat_id, question, options, *, multiple_answers=False):
        enviados.append((chat_id, f"[encuesta] {question}"))
        return SentMessage(ok=True, chat_id=chat_id, message_id="poll")

    async def fake_admins_only(self, group_id, admins_only):
        return True

    monkeypatch.setattr(WahaClient, "send_text", fake_send_text)
    monkeypatch.setattr(WahaClient, "send_poll", fake_send_poll)
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

    grupo_a, grupo_b = "120000000000a@g.us", "120000000000b@g.us"
    await asyncio.gather(
        orch.handle(
            inbound(MANAGER, "!juego hombreslobo", scope=Scope.GROUP, chat_id=grupo_a)
        ),
        orch.handle(
            inbound(MANAGER, "!juego hombreslobo", scope=Scope.GROUP, chat_id=grupo_b)
        ),
    )

    activas = orch.snapshot()["partidas_activas"]
    assert len(activas) == 2
    assert {p["grupo"] for p in activas} == {grupo_a, grupo_b}
    assert len({p["session_id"] for p in activas}) == 2

    # Cuatro personas se apuntan sólo en el grupo A.
    for indice in range(1, 5):
        await orch.handle(
            inbound(
                make_jid(indice), "Yo", scope=Scope.GROUP,
                chat_id=grupo_a, name=f"JugadorA{indice}",
            )
        )

    def _grupo(chat: str) -> list[str]:
        return [t for destino, t in enviados if destino == chat]

    # La del grupo A cierra con sus cuatro; la del B aborta por falta de gente.
    assert await _espera_hasta(
        lambda: any("4 jugadores* entran a la partida" in t for t in _grupo(grupo_a))
    ), "el grupo A tenía que arrancar con 4"
    # "Inscripciones cerradas" sólo sale al abortar; "Hacen falta" también
    # aparece en el mensaje de apertura, así que no distingue nada.
    assert await _espera_hasta(
        lambda: any("Inscripciones cerradas" in t for t in _grupo(grupo_b))
    ), "en el grupo B nadie dijo Yo"

    # Los avisos no se cruzaron de grupo.
    assert not any("entran a la partida" in t for t in _grupo(grupo_b))
    assert not any("Inscripciones cerradas" in t for t in _grupo(grupo_a))
    # Y los privados del reparto fueron sólo a los jugadores del grupo A.
    privados = {chat for chat, _ in enviados if chat.endswith("@c.us") and chat != MANAGER}
    assert privados == {make_jid(i) for i in range(1, 5)}

    await orch.shutdown()
    assert orch.snapshot()["partidas_activas"] == []


async def test_el_webhook_encola_mientras_el_grafo_recoge(monkeypatch):
    """La simultaneidad del diseño: nadie espera a nadie.

    Se martillea `handle` con mensajes mientras la partida corre su ventana de
    inscripción, y todos los que llegan dentro del plazo cuentan.
    """
    enviados: list[tuple[str, str]] = []

    async def fake_send_text(self, chat_id, text, *, reply_to=None, mentions=None):
        enviados.append((chat_id, text))
        return SentMessage(ok=True, chat_id=chat_id, message_id="m")

    async def fake_send_poll(self, chat_id, question, options, *, multiple_answers=False):
        return SentMessage(ok=True, chat_id=chat_id, message_id="poll")

    async def fake_admins_only(self, group_id, admins_only):
        return True

    monkeypatch.setattr(WahaClient, "send_text", fake_send_text)
    monkeypatch.setattr(WahaClient, "send_poll", fake_send_poll)
    monkeypatch.setattr(WahaClient, "set_admins_only", fake_admins_only)

    settings = make_settings(
        recruit_seconds=2, night_action_seconds=1, witch_action_seconds=1,
        hunter_action_seconds=1, debate_seconds=1, vote_seconds=1,
        filler_interval_seconds=0, werewolf_min_players=4,
    )
    orch = Orchestrator(
        settings=settings,
        waha=WahaClient(settings),
        inbox=MemoryInbox(),
        store=None,
        llm=LLMClient(settings),
        checkpointer=None,
    )

    await orch.handle(
        inbound(MANAGER, "!juego hombreslobo", scope=Scope.GROUP, chat_id=GROUP_ID)
    )

    # 12 personas se apuntan en paralelo, con ruido intercalado.
    async def entra(indice: int):
        await asyncio.sleep(random.random() * 0.3)
        await orch.handle(
            inbound(
                make_jid(indice),
                random.choice(["Yo", "yo juego", "me apunto", "va"]),
                scope=Scope.GROUP,
                chat_id=GROUP_ID,
                name=f"Jugador{indice}",
            )
        )

    async def ruido(indice: int):
        await asyncio.sleep(random.random() * 0.3)
        await orch.handle(
            inbound(
                make_jid(50 + indice),
                random.choice(["hola", "qué es esto", "yo no", "paso"]),
                scope=Scope.GROUP,
                chat_id=GROUP_ID,
                name=f"Curioso{indice}",
            )
        )

    await asyncio.gather(
        *(entra(i) for i in range(1, 13)), *(ruido(i) for i in range(1, 8))
    )

    # El nodo de reclutamiento estaba recogiendo del mismo buzón al mismo
    # tiempo. Se comprueba a quién dio por inscrito, que es lo que importa.
    assert await _espera_hasta(
        lambda: any("entran a la partida" in t for _, t in enviados)
    ), "el reclutamiento tenía que cerrarse"

    (bruto,) = [t for _, t in enviados if "entran a la partida" in t]
    # El anuncio etiqueta contactos: se lee resolviendo los tokens, igual que
    # lo ve la gente en WhatsApp.
    nombres = {make_jid(i): f"Jugador{i}" for i in range(1, 13)}
    nombres.update({make_jid(50 + i): f"Curioso{i}" for i in range(1, 8)})
    anuncio = render_mentions(bruto, nombres)

    assert "12 jugadores* entran a la partida" in anuncio
    lineas = {linea.strip() for linea in anuncio.splitlines()}
    for indice in range(1, 13):
        assert any(
            linea.endswith(f". Jugador{indice}") for linea in lineas
        ), f"faltaba Jugador{indice}"
    assert "Curioso" not in anuncio, "ningún curioso se coló en la mesa"

    await orch.shutdown()


async def test_llamadas_a_waha_concurrentes_se_serializan_pero_no_se_bloquean():
    """El lock de envío ordena los mensajes sin permitir un interbloqueo."""
    settings = make_settings(waha_min_send_interval=0.01)
    orden: list[str] = []

    class ClienteInstrumentado(WahaClient):
        async def send_text(self, chat_id, text, *, reply_to=None, mentions=None):
            async with self._send_lock:
                await self._throttle()
                orden.append(text)
                await asyncio.sleep(0.01)
            return SentMessage(ok=True, chat_id=chat_id, message_id="m")

    client = ClienteInstrumentado(settings)
    await asyncio.gather(*(client.send_text(GROUP_ID, f"m{i}") for i in range(30)))
    await client.aclose()

    assert len(orden) == 30
    assert len(set(orden)) == 30, "no se perdió ni duplicó ningún envío"


# =====================================================================
# Configuración de los pools: acotada y predecible
# =====================================================================
async def test_sqlite_usa_un_pool_pequeno_y_sin_desborde(tmpdir_path):
    """SQLite sólo admite un escritor.

    Con el pool por defecto (5 + 10 de desborde) las conexiones se pelean por
    el lock de escritura y cada una añade un hilo de aiosqlite. Medido, un pool
    de 2 rinde ~25% más y usa la mitad de hilos.
    """
    store = Store(f"sqlite+aiosqlite:///{tmpdir_path}/pool.db", pool_size=2)
    await store.init()
    pool = store._engine.pool
    assert pool.size() <= 2
    assert getattr(pool, "_max_overflow", 0) == 0, "sin desborde: el tope es el tope"
    await store.aclose()


async def test_una_base_en_memoria_no_recibe_argumentos_de_pool():
    """`:memory:` usa StaticPool, que rechaza pool_size.

    Sin la guarda, arrancar con DATABASE_URL en memoria explotaría.
    """
    store = Store("sqlite+aiosqlite:///:memory:", pool_size=2)
    await store.init()
    await store.recent_game_sessions(limit=1)
    await store.aclose()


async def test_sqlite_de_fichero_arranca_en_wal_con_busy_timeout(tmpdir_path):
    """WAL permite leer mientras se escribe; busy_timeout evita el 'locked'."""
    store = Store(f"sqlite+aiosqlite:///{tmpdir_path}/wal.db", busy_timeout=7.0)
    await store.init()
    async with store.session() as session:
        modo = (await session.execute(sql("PRAGMA journal_mode"))).scalar()
        espera = (await session.execute(sql("PRAGMA busy_timeout"))).scalar()
    assert str(modo).lower() == "wal"
    assert espera == 7000
    await store.aclose()


def test_el_cliente_de_redis_usa_un_pool_bloqueante_y_acotado():
    """El pool por defecto de redis-py *lanza* al agotarse.

    Bajo una ráfaga del webhook eso significa perder un voto, así que se usa
    uno que encola la petición hasta tener conexión libre.
    """
    settings = make_settings(
        redis_url="redis://localhost:6379/0",
        redis_max_connections=17,
        redis_pool_timeout=4.0,
    )
    client = build_redis_client(settings)
    pool = client.connection_pool

    assert isinstance(pool, BlockingConnectionPool)
    assert pool.max_connections == 17
    assert pool.timeout == 4.0


async def test_escrituras_masivas_en_sqlite_terminan_en_un_tiempo_razonable(tmpdir_path):
    """Cota holgada: una ronda de votación son ~24 escrituras, no 300.

    No se afina un número exacto (dependería de la máquina), sólo se detecta
    una regresión gruesa como perder el WAL o volver al pool grande.
    """
    store = Store(f"sqlite+aiosqlite:///{tmpdir_path}/carga.db")
    await store.init()
    await store.create_game_session(
        "s", game_key="hombreslobo", group_id=GROUP_ID, started_by=MANAGER
    )

    total = 300
    loop = asyncio.get_running_loop()
    inicio = loop.time()
    await asyncio.gather(
        *(
            store.log_inbound(
                inbound(make_jid(i % 24), f"t{i}", scope=Scope.GROUP, chat_id=GROUP_ID),
                game_session_id="s",
            )
            for i in range(total)
        )
    )
    transcurrido = loop.time() - inicio

    async with store.session() as session:
        filas = (await session.execute(sql("select count(*) from messages"))).scalar()
    assert filas == total
    assert transcurrido < 10.0, f"{total} escrituras tardaron {transcurrido:.1f}s"
    await store.aclose()
