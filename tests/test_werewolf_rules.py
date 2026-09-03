"""Reglas deterministas: reparto, cadenas de muerte y condiciones de victoria."""

from __future__ import annotations

import random

import pytest

from app.games.werewolf.nodes import WerewolfNodes
from app.games.werewolf.parsing import (
    looks_like_join,
    parse_player_reference,
    parse_witch_choice,
    tally_votes,
    witch_decided,
)
from app.games.werewolf.roles import ABSOLUTE_MIN_PLAYERS, Role, distribute_roles
from app.games.werewolf.state import Player, alive, by_jid, initial_state
from tests.conftest import make_context, make_settings


def _player(number: int, role: Role, *, alive_flag: bool = True) -> Player:
    return Player(
        jid=f"5730011111{number:02d}@c.us",
        name=f"Jugador{number}",
        number=number,
        role=str(role),
        alive=alive_flag,
        death_round=None,
        death_cause=None,
    )


def _nodes(**settings_kwargs) -> WerewolfNodes:
    ctx = make_context(settings=make_settings(**settings_kwargs))
    return WerewolfNodes(ctx, rng=random.Random(1234))


# ============================================================ reparto de roles
@pytest.mark.parametrize("total", list(range(ABSOLUTE_MIN_PLAYERS, 27)))
def test_reparto_es_consistente_y_el_pueblo_arranca_en_mayoria(total):
    roles = distribute_roles(total, rng=random.Random(total))
    lobos = [r for r in roles if r is Role.LOBO]
    pueblo = [r for r in roles if r is not Role.LOBO]

    assert len(roles) == total
    assert len(lobos) >= 1
    assert len(lobos) < len(pueblo), "los lobos no pueden empezar en paridad"
    # Siempre queda al menos un aldeano raso para que haya a quién deducir.
    assert Role.ALDEANO in roles
    # Los roles especiales son únicos.
    for special in (Role.VIDENTE, Role.BRUJA, Role.CAZADOR, Role.CUPIDO):
        assert roles.count(special) <= 1


def test_reparto_rechaza_partidas_demasiado_pequenas():
    with pytest.raises(ValueError, match="al menos"):
        distribute_roles(ABSOLUTE_MIN_PLAYERS - 1)


def test_reparto_es_reproducible_con_la_misma_semilla():
    a = distribute_roles(9, rng=random.Random(42))
    b = distribute_roles(9, rng=random.Random(42))
    assert a == b


# ========================================================= cadenas de muerte
async def test_los_enamorados_mueren_juntos():
    nodes = _nodes()
    players = [_player(1, Role.LOBO), _player(2, Role.ALDEANO), _player(3, Role.ALDEANO)]
    lovers = [players[1]["jid"], players[2]["jid"]]

    updated, deaths = await nodes._apply_deaths(
        players, [(players[1]["jid"], "lobos")], round_no=1, lovers=lovers
    )

    assert [d["cause"] for d in deaths] == ["lobos", "amor"]
    assert len(alive(updated)) == 1
    assert by_jid(updated, lovers[1])["death_cause"] == "amor"


async def test_el_cazador_dispara_al_morir():
    nodes = _nodes()
    players = [_player(1, Role.LOBO), _player(2, Role.CAZADOR), _player(3, Role.ALDEANO)]
    hunter = players[1]

    # El cazador elige al jugador 1 en su último aliento.
    async def fake_ask(victim, current):
        return players[0]["jid"] if victim["jid"] == hunter["jid"] else None

    nodes._ask_hunter = fake_ask  # type: ignore[method-assign]

    updated, deaths = await nodes._apply_deaths(
        players, [(hunter["jid"], "linchamiento")], round_no=2, lovers=[]
    )

    assert [d["cause"] for d in deaths] == ["linchamiento", "cazador"]
    assert by_jid(updated, players[0]["jid"])["alive"] is False


async def test_cadena_cazador_mas_amor_no_entra_en_bucle():
    """Cazador que dispara a un enamorado: se resuelve y termina."""
    nodes = _nodes()
    players = [
        _player(1, Role.LOBO),
        _player(2, Role.CAZADOR),
        _player(3, Role.ALDEANO),
        _player(4, Role.ALDEANO),
    ]
    lovers = [players[2]["jid"], players[3]["jid"]]

    async def fake_ask(victim, current):
        return players[2]["jid"]

    nodes._ask_hunter = fake_ask  # type: ignore[method-assign]

    updated, deaths = await nodes._apply_deaths(
        players, [(players[1]["jid"], "lobos")], round_no=1, lovers=lovers
    )

    assert [d["cause"] for d in deaths] == ["lobos", "cazador", "amor"]
    assert len(alive(updated)) == 1


async def test_no_se_mata_dos_veces_al_mismo_jugador():
    nodes = _nodes()
    players = [_player(1, Role.LOBO), _player(2, Role.ALDEANO), _player(3, Role.ALDEANO)]
    victim = players[1]["jid"]

    _, deaths = await nodes._apply_deaths(
        players, [(victim, "lobos"), (victim, "veneno")], round_no=1, lovers=[]
    )
    assert len(deaths) == 1


# ====================================================== condición de victoria
async def _evaluate(players: list[Player], *, lovers=None, round_no=1, **settings_kwargs):
    nodes = _nodes(**settings_kwargs)
    state = initial_state("s", "g@g.us")
    state["players"] = players
    state["round_no"] = round_no
    state["lovers"] = lovers or []
    return await nodes.evaluar(state)


async def test_gana_el_pueblo_cuando_no_quedan_lobos():
    players = [
        _player(1, Role.LOBO, alive_flag=False),
        _player(2, Role.ALDEANO),
        _player(3, Role.VIDENTE),
    ]
    assert (await _evaluate(players))["winner"] == "pueblo"


async def test_ganan_los_lobos_en_paridad():
    players = [_player(1, Role.LOBO), _player(2, Role.ALDEANO)]
    assert (await _evaluate(players))["winner"] == "lobos"


async def test_la_partida_sigue_cuando_el_pueblo_es_mayoria():
    players = [_player(1, Role.LOBO), _player(2, Role.ALDEANO), _player(3, Role.VIDENTE)]
    result = await _evaluate(players)
    assert result["winner"] is None
    assert result["finished"] is False


async def test_ganan_los_enamorados_si_son_los_dos_ultimos_de_bandos_opuestos():
    players = [
        _player(1, Role.LOBO),
        _player(2, Role.ALDEANO),
        _player(3, Role.ALDEANO, alive_flag=False),
    ]
    lovers = [players[0]["jid"], players[1]["jid"]]
    # Sin la regla de enamorados esto sería victoria de los lobos (1 vs 1).
    assert (await _evaluate(players, lovers=lovers))["winner"] == "enamorados"


async def test_dos_enamorados_del_mismo_bando_no_activan_la_regla():
    players = [
        _player(1, Role.ALDEANO),
        _player(2, Role.ALDEANO),
        _player(3, Role.LOBO, alive_flag=False),
    ]
    lovers = [players[0]["jid"], players[1]["jid"]]
    assert (await _evaluate(players, lovers=lovers))["winner"] == "pueblo"


async def test_sin_supervivientes_no_gana_nadie():
    players = [
        _player(1, Role.LOBO, alive_flag=False),
        _player(2, Role.ALDEANO, alive_flag=False),
    ]
    assert (await _evaluate(players))["winner"] == "nadie"


async def test_el_tope_de_rondas_cierra_la_partida():
    players = [
        _player(1, Role.LOBO),
        _player(2, Role.ALDEANO),
        _player(3, Role.ALDEANO),
        _player(4, Role.ALDEANO),
    ]
    result = await _evaluate(players, round_no=99, max_rounds=5)
    assert result["winner"] == "nadie"


# ================================================================== parseo
def test_conteo_de_votos_detecta_empates():
    assert tally_votes({"a": "x", "b": "y"}) == (["x", "y"], 1) or tally_votes(
        {"a": "x", "b": "y"}
    ) == (["y", "x"], 1)
    assert tally_votes({"a": "x", "b": "x", "c": "y"}) == (["x"], 2)
    assert tally_votes({}) == ([], 0)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Yo", True),
        ("yo juego!", True),
        ("me apunto", True),
        ("yo no", False),
        ("no puedo hoy", False),
        ("la próxima", False),
        ("hola a todos", False),
    ],
)
def test_deteccion_de_inscripciones(text, expected):
    assert looks_like_join(text) is expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("curar", "vida"),
        ("uso la poción de vida", "vida"),
        ("veneno al 2", "muerte"),
        ("nada", "nada"),
        ("mmm no sé", "nada"),
    ],
)
def test_decision_de_la_bruja(text, expected):
    assert parse_witch_choice(text) == expected


def test_la_bruja_solo_cierra_turno_con_una_respuesta_clara():
    assert witch_decided("nada") is True
    assert witch_decided("curar") is True
    assert witch_decided("espera un momento") is False
    assert witch_decided("") is False


def test_referencia_a_jugador_prefiere_el_nombre_exacto():
    players = [_player(1, Role.ALDEANO), _player(2, Role.ALDEANO)]
    players[0]["name"] = "Ana"
    players[1]["name"] = "Ana María"

    assert parse_player_reference("ana", players)["name"] == "Ana"
    assert parse_player_reference("Ana María", players)["name"] == "Ana María"
    assert parse_player_reference("2", players)["number"] == 2
    assert parse_player_reference("nadie de aqui", players) is None
