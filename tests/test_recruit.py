"""Reclutamiento: criterio del LLM con redes de seguridad deterministas."""

from __future__ import annotations

from typing import Any

from app.core.llm import LLMClient
from app.games.recruit import select_players
from app.waha.models import Scope
from tests.conftest import GROUP_ID, inbound, make_settings


class FakeLLM(LLMClient):
    """LLM guionizado: devuelve lo que le diga el test."""

    def __init__(self, respuesta: dict[str, Any] | None, *, available: bool = True):
        super().__init__(make_settings(llm_provider="none"))
        self._respuesta = respuesta
        self._forced_available = available
        self.llamadas: list[str] = []

    @property
    def available(self) -> bool:  # type: ignore[override]
        return self._forced_available

    async def complete_json(self, system, user, *, temperature=None):  # type: ignore[override]
        self.llamadas.append(user)
        return self._respuesta


def _msgs(*pares: tuple[str, str]):
    """(nombre, texto) -> mensajes de grupo, uno por persona."""
    return [
        inbound(
            f"57300111{index:04d}@c.us",
            texto,
            scope=Scope.GROUP,
            chat_id=GROUP_ID,
            name=nombre,
        )
        for index, (nombre, texto) in enumerate(pares, start=1)
    ]


async def test_sin_llm_decide_el_parser_determinista():
    mensajes = _msgs(("Ana", "Yo"), ("Beto", "hola"), ("Caro", "me apunto"))
    jugadores = await select_players(mensajes, llm=FakeLLM(None, available=False))
    assert [j.name for j in jugadores] == ["Ana", "Caro"]


async def test_el_llm_puede_sumar_a_quien_el_parser_no_entiende():
    """El valor del LLM: interpretar respuestas que ninguna regla cubre."""
    mensajes = _msgs(("Ana", "Yo"), ("Beto", "va que va, ponme"), ("Caro", "hola"))
    llm = FakeLLM({"jugadores": [1, 2]})
    jugadores = await select_players(mensajes, llm=llm)

    assert [j.name for j in jugadores] == ["Ana", "Beto"]
    # Se le pasó lo que dijo cada persona, no los teléfonos.
    assert "va que va, ponme" in llm.llamadas[0]
    assert "57300111" not in llm.llamadas[0]


async def test_un_si_inequivoco_entra_aunque_el_llm_lo_omita():
    """Red de seguridad: el modelo no puede dejar fuera un "Yo" claro."""
    mensajes = _msgs(("Ana", "Yo"), ("Beto", "Yo juego"))
    jugadores = await select_players(mensajes, llm=FakeLLM({"jugadores": []}))
    assert {j.name for j in jugadores} == {"Ana", "Beto"}


async def test_un_no_inequivoco_queda_fuera_aunque_el_llm_lo_incluya():
    """Red de seguridad en el otro sentido: un "no" explícito manda."""
    mensajes = _msgs(("Ana", "Yo"), ("Beto", "yo no"), ("Caro", "paso, la próxima"))
    jugadores = await select_players(mensajes, llm=FakeLLM({"jugadores": [1, 2, 3]}))
    assert [j.name for j in jugadores] == ["Ana"]


async def test_quien_se_arrepiente_y_vuelve_si_entra():
    """Un "no" seguido de un "sí" cuenta como sí."""
    ana = inbound("573001110001@c.us", "paso", scope=Scope.GROUP, chat_id=GROUP_ID, name="Ana")
    ana2 = inbound(
        "573001110001@c.us", "bueno va, yo juego", scope=Scope.GROUP,
        chat_id=GROUP_ID, name="Ana",
    )
    jugadores = await select_players([ana, ana2], llm=FakeLLM(None, available=False))
    assert [j.name for j in jugadores] == ["Ana"]


async def test_respeta_el_orden_de_llegada():
    mensajes = _msgs(("Ana", "Yo"), ("Beto", "Yo"), ("Caro", "Yo"))
    jugadores = await select_players(mensajes, llm=FakeLLM(None, available=False))
    assert [j.name for j in jugadores] == ["Ana", "Beto", "Caro"]


async def test_recorta_al_maximo_de_jugadores():
    mensajes = _msgs(*[(f"J{i}", "Yo") for i in range(1, 11)])
    jugadores = await select_players(
        mensajes, llm=FakeLLM(None, available=False), max_players=4
    )
    assert len(jugadores) == 4
    assert [j.name for j in jugadores] == ["J1", "J2", "J3", "J4"]


async def test_una_persona_solo_cuenta_una_vez():
    mensajes = _msgs(("Ana", "Yo")) + _msgs(("Ana", "Yo otra vez"))
    # Ambos mensajes son del mismo JID porque el índice arranca en 1.
    jugadores = await select_players(mensajes, llm=FakeLLM(None, available=False))
    assert len(jugadores) == 1


async def test_sin_mensajes_no_hay_jugadores():
    assert await select_players([], llm=FakeLLM({"jugadores": [1]})) == []


async def test_los_mensajes_del_bot_se_ignoran():
    mensaje = inbound(
        "573009999999@c.us", "Yo", scope=Scope.GROUP, chat_id=GROUP_ID, name="Bot"
    ).model_copy(update={"from_me": True})
    assert await select_players([mensaje], llm=FakeLLM(None, available=False)) == []


async def test_una_respuesta_rara_del_llm_no_rompe_nada():
    """Si el modelo devuelve basura, se cae al criterio determinista."""
    mensajes = _msgs(("Ana", "Yo"), ("Beto", "quizá"))
    for basura in ({"jugadores": "todos"}, {"otra_cosa": [1]}, {}, None):
        jugadores = await select_players(mensajes, llm=FakeLLM(basura))
        assert [j.name for j in jugadores] == ["Ana"], basura


async def test_el_llm_puede_devolver_los_indices_como_texto_u_objetos():
    """Los modelos no siempre respetan el formato pedido."""
    mensajes = _msgs(("Ana", "hmm"), ("Beto", "bueno"))
    for forma in ([1, 2], ["1", "2"], [{"id": 1}, {"numero": 2}]):
        jugadores = await select_players(mensajes, llm=FakeLLM({"jugadores": forma}))
        assert {j.name for j in jugadores} == {"Ana", "Beto"}, forma


async def test_indices_fuera_de_rango_se_descartan():
    mensajes = _msgs(("Ana", "hmm"))
    jugadores = await select_players(mensajes, llm=FakeLLM({"jugadores": [1, 7, 0, -3]}))
    assert [j.name for j in jugadores] == ["Ana"]


async def test_sin_nombre_se_usa_el_numero_de_telefono():
    mensaje = inbound("573001110009@c.us", "Yo", scope=Scope.GROUP, chat_id=GROUP_ID)
    (jugador,) = await select_players([mensaje], llm=FakeLLM(None, available=False))
    assert jugador.name == "573001110009"
