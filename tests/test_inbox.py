"""Buzones de mensajes: memoria y Redis deben comportarse igual.

Los tests se ejecutan contra las dos implementaciones con los mismos casos: es
la única forma de que el buzón de producción (Redis) no se desvíe del que usan
el resto de los tests (memoria).
"""

from __future__ import annotations

import asyncio

import pytest

from app.core.inbox import MemoryInbox, RedisInbox
from app.waha.models import Scope
from tests.conftest import GROUP_ID, inbound

pytest.importorskip("fakeredis", reason="fakeredis hace falta para probar RedisInbox")

# Va después del importorskip a propósito: si fakeredis no está instalado, el
# módulo se salta entero en vez de fallar al importar.
from fakeredis.aioredis import FakeRedis

SESSION = "s-1"
JUGADOR = "573001111101@c.us"
OTRO = "573001111102@c.us"


def _memory() -> MemoryInbox:
    return MemoryInbox()


def _redis() -> RedisInbox:
    return RedisInbox(FakeRedis(decode_responses=True), ttl_seconds=60)


@pytest.fixture(params=["memoria", "redis"])
def inbox(request):
    return _memory() if request.param == "memoria" else _redis()


def _group_msg(text: str, sender: str = JUGADOR):
    return inbound(sender, text, scope=Scope.GROUP, chat_id=GROUP_ID)


async def test_recoge_lo_que_ya_estaba_encolado(inbox):
    await inbox.push(SESSION, _group_msg("Yo"))
    await inbox.push(SESSION, _group_msg("me apunto", OTRO))

    recogidos = await inbox.collect(SESSION, timeout=0.2, group=True)
    assert [m.text for m in recogidos] == ["Yo", "me apunto"]


async def test_espera_los_mensajes_que_llegan_tarde(inbox):
    async def tarde():
        await asyncio.sleep(0.1)
        await inbox.push(SESSION, _group_msg("llego justo"))

    _, recogidos = await asyncio.gather(
        tarde(), inbox.collect(SESSION, timeout=1.0, group=True, stop_when=lambda c: bool(c))
    )
    assert [m.text for m in recogidos] == ["llego justo"]


async def test_devuelve_vacio_si_nadie_escribe(inbox):
    recogidos = await inbox.collect(SESSION, timeout=0.15, group=True)
    assert recogidos == []


async def test_separa_el_grupo_de_los_privados(inbox):
    await inbox.push(SESSION, _group_msg("en el grupo"))
    await inbox.push(SESSION, inbound(JUGADOR, "en privado"))

    solo_grupo = await inbox.collect(SESSION, timeout=0.1, group=True)
    assert [m.text for m in solo_grupo] == ["en el grupo"]

    solo_privado = await inbox.collect(SESSION, timeout=0.1, direct=[JUGADOR])
    assert [m.text for m in solo_privado] == ["en privado"]


async def test_escucha_varios_privados_a_la_vez(inbox):
    """La noche espera a lobos, vidente y bruja en paralelo."""
    await inbox.push(SESSION, inbound(JUGADOR, "3"))
    await inbox.push(SESSION, inbound(OTRO, "curar"))

    recogidos = await inbox.collect(
        SESSION,
        timeout=0.3,
        direct=[JUGADOR, OTRO],
        stop_when=lambda c: len({m.sender_id for m in c}) == 2,
    )
    assert {m.text for m in recogidos} == {"3", "curar"}


async def test_stop_when_corta_antes_del_timeout(inbox):
    await inbox.push(SESSION, _group_msg("uno"))
    loop = asyncio.get_running_loop()
    inicio = loop.time()
    recogidos = await inbox.collect(
        SESSION, timeout=5.0, group=True, stop_when=lambda c: len(c) >= 1
    )
    assert len(recogidos) == 1
    assert loop.time() - inicio < 2.0, "no debía esperar el timeout completo"


async def test_clear_vacia_solo_las_claves_indicadas(inbox):
    await inbox.push(SESSION, _group_msg("grupo"))
    await inbox.push(SESSION, inbound(JUGADOR, "privado"))

    await inbox.clear(SESSION, keys=["group"])

    assert await inbox.collect(SESSION, timeout=0.05, group=True) == []
    quedan = await inbox.collect(SESSION, timeout=0.05, direct=[JUGADOR])
    assert [m.text for m in quedan] == ["privado"]


async def test_un_clear_parcial_no_rompe_la_purga_final(inbox):
    """Regresión: vaciar sólo el grupo dejaba huérfanos los privados.

    `votacion` limpia únicamente la clave del grupo antes de contar votos. Si
    ese borrado parcial se lleva el índice de la partida, la purga final ya no
    encuentra los buzones privados y los deja atrás.
    """
    await inbox.push(SESSION, _group_msg("charla del debate"))
    await inbox.push(SESSION, inbound(JUGADOR, "secreto"))

    await inbox.clear(SESSION, keys=["group"])
    await inbox.push(SESSION, _group_msg("voto"))
    await inbox.clear(SESSION)

    assert await inbox.collect(SESSION, timeout=0.05, group=True) == []
    assert await inbox.collect(SESSION, timeout=0.05, direct=[JUGADOR]) == []


async def test_dos_colectores_a_la_vez_no_se_roban_el_aviso(inbox):
    """Dos esperas sobre la misma partida deben despertar las dos.

    Con un único evento compartido por sesión, el `clear()` de un colector se
    comía el aviso del otro y éste agotaba su ventana entera.
    """

    async def llega_tarde():
        await asyncio.sleep(0.1)
        await inbox.push(SESSION, _group_msg("para el grupo"))
        await inbox.push(SESSION, inbound(JUGADOR, "para el privado"))

    loop = asyncio.get_running_loop()
    inicio = loop.time()
    _, del_grupo, del_privado = await asyncio.gather(
        llega_tarde(),
        inbox.collect(SESSION, timeout=3.0, group=True, stop_when=lambda c: bool(c)),
        inbox.collect(SESSION, timeout=3.0, direct=[JUGADOR], stop_when=lambda c: bool(c)),
    )

    assert [m.text for m in del_grupo] == ["para el grupo"]
    assert [m.text for m in del_privado] == ["para el privado"]
    assert loop.time() - inicio < 1.5, "ninguno debía esperar su ventana completa"


async def test_clear_completo_vacia_todo(inbox):
    await inbox.push(SESSION, _group_msg("grupo"))
    await inbox.push(SESSION, inbound(JUGADOR, "privado"))

    await inbox.clear(SESSION)

    assert await inbox.collect(SESSION, timeout=0.05, group=True) == []
    assert await inbox.collect(SESSION, timeout=0.05, direct=[JUGADOR]) == []


async def test_las_partidas_no_se_pisan_entre_si(inbox):
    await inbox.push("partida-a", _group_msg("de la A"))
    await inbox.push("partida-b", _group_msg("de la B"))

    a = await inbox.collect("partida-a", timeout=0.1, group=True)
    b = await inbox.collect("partida-b", timeout=0.1, group=True)
    assert [m.text for m in a] == ["de la A"]
    assert [m.text for m in b] == ["de la B"]


async def test_mark_seen_solo_acepta_un_id_una_vez(inbox):
    assert await inbox.mark_seen("abc") is True
    assert await inbox.mark_seen("abc") is False
    assert await inbox.mark_seen("def") is True


async def test_sin_claves_no_espera_nada(inbox):
    """Una noche sin roles activos no debe bloquear la partida."""
    loop = asyncio.get_running_loop()
    inicio = loop.time()
    assert await inbox.collect(SESSION, timeout=5.0) == []
    assert loop.time() - inicio < 0.5


async def test_el_mensaje_conserva_todos_sus_campos(inbox):
    """El buzón de Redis serializa a JSON: no puede perder datos."""
    original = inbound(
        JUGADOR,
        "2. Ana",
        scope=Scope.GROUP,
        chat_id=GROUP_ID,
        name="Beto",
        poll_options=["2. Ana"],
    )
    await inbox.push(SESSION, original)
    (recuperado,) = await inbox.collect(SESSION, timeout=0.1, group=True)

    assert recuperado.message_id == original.message_id
    assert recuperado.sender_id == original.sender_id
    assert recuperado.sender_name == "Beto"
    assert recuperado.scope == Scope.GROUP
    assert recuperado.kind == "poll_vote"
    assert recuperado.poll_options == ["2. Ana"]
    assert recuperado.timestamp == original.timestamp
