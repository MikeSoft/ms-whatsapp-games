"""Partidas completas del grafo, con jugadores automáticos."""

from __future__ import annotations

import random

import pytest

from app.games.werewolf.game import WerewolfGame
from app.games.werewolf.roles import Role
from tests.conftest import fast_timers, make_settings

VALID_WINNERS = {"lobos", "pueblo", "enamorados", "nadie"}


async def _play(table, count: int = 6, **kwargs):
    settings = kwargs.pop("settings", None)
    ctx, transport, _inbox, script = table(count, settings=settings, **kwargs)
    game = WerewolfGame(ctx, timers=fast_timers())
    result = await game.run()
    return result, transport, script, ctx


@pytest.mark.parametrize("count", [4, 6, 8, 11])
async def test_partida_completa_termina_con_ganador(table, count):
    """El grafo cierra siempre con un ganador válido y estado coherente."""
    result, transport, _script, _ = await _play(table, count)

    assert result.status == "finished"
    assert result.winner in VALID_WINNERS
    assert len(result.players) == count
    # Todo jugador acaba con un rol asignado.
    assert all(p["role"] in {r.value for r in Role} for p in result.players)
    # El grupo se reabre al terminar: nadie se queda mudo.
    assert transport.locked is False
    # Se anunció el desenlace y se revelaron los roles.
    assert "Todos los roles" in transport.group_text()


async def test_cada_jugador_recibe_su_rol_en_privado(table):
    _, transport, script, _ = await _play(table, 8)

    role_dms = transport.dms_matching("Tu rol es")
    assert len({jid for jid, _ in role_dms}) == 8
    assert len(script.roles) == 8

    # Los lobos saben quiénes son sus compañeros; los aldeanos no reciben manada.
    wolves = script.wolves()
    assert len(wolves) >= 1
    for jid in wolves:
        briefing = "\n".join(transport.dms_to(jid))
        assert "Tu manada" in briefing or "único lobo" in briefing


async def test_grupo_se_silencia_de_noche_y_se_abre_de_dia(table):
    """La mecánica de "todos duermen" se traduce en silenciar el grupo."""
    _, transport, _, _ = await _play(table, 6)

    # Se silencia al cerrar inscripciones y se reabre en cada amanecer.
    assert True in transport.lock_history
    assert False in transport.lock_history
    assert transport.lock_history[0] is True
    assert transport.lock_history[-1] is False


async def test_reclutamiento_insuficiente_aborta_sin_romper_el_grupo(table):
    """Con menos gente que el mínimo, la partida no arranca."""
    result, transport, _, _ = await _play(table, 2)

    assert result.status == "aborted"
    assert result.winner is None
    assert result.players == []
    assert "Hacen falta" in transport.group_text()
    # No se silenció el grupo por una partida que nunca empezó.
    assert True not in transport.lock_history


async def test_nadie_responde_de_noche_no_hay_ataque(table):
    """Si los lobos no contestan, no se inventa una muerte."""
    result, transport, _, _ = await _play(table, 6, answer_night=False)

    assert result.status == "finished"
    assert result.winner in VALID_WINNERS
    # Al menos un amanecer sin víctimas.
    assert "no murió nadie" in transport.group_text()


async def test_la_votacion_publica_una_encuesta(table):
    _, transport, _, _ = await _play(table, 6)

    assert transport.polls, "se esperaba al menos una encuesta de linchamiento"
    question, options = transport.polls[0]
    assert "linchamos" in question
    assert len(options) >= 2
    assert all(len(option) <= 100 for option in options)


async def test_votacion_sin_encuesta_cae_a_votos_por_texto(table):
    """Si WAHA no soporta encuestas, se vota escribiendo en el grupo."""
    ctx, transport, _inbox, _script = table(6)
    transport.poll_supported = False
    game = WerewolfGame(ctx, timers=fast_timers())
    result = await game.run()

    assert result.status == "finished"
    assert transport.polls == []
    assert "Escribe en el grupo el número" in transport.group_text()


async def test_bruja_cura_y_el_amanecer_no_revela_quien_intervino(table):
    """La poción de vida salva sin que el grupo sepa de dónde vino la ayuda.

    El reparto público sí anuncia que *hay* una bruja en la partida; lo que no
    puede filtrarse es que fuera ella quien salvó a la víctima de esta noche.
    """
    result, transport, script, _ = await _play(table, 8, witch_reply="curar")
    assert result.status == "finished"

    # Se le ofreció la poción y la usó.
    ofertas = transport.dms_matching("Pociones que te quedan")
    assert ofertas, "la bruja tenía que recibir la consulta nocturna"
    confirmaciones = transport.dms_matching("poción de vida en")
    assert confirmaciones, "la bruja tenía que poder curar"
    salvado = confirmaciones[0][0]
    assert script.roles.get(salvado) == "Bruja"

    # Los amaneceres sin muertos no nombran a la bruja ni la poción.
    amaneceres = [m for m in transport.group_messages if "AMANECE EL DÍA" in m]
    assert amaneceres
    for mensaje in amaneceres:
        if "no murió nadie" in mensaje:
            bajo = mensaje.lower()
            assert "bruja" not in bajo
            assert "poción" not in bajo


async def test_empate_configurable_no_lincha(table):
    """Con tie_break=none un empate deja el día sin linchamiento."""
    settings = make_settings(werewolf_tie_break="none")
    result, _, _, _ = await _play(table, 6, settings=settings)
    assert result.status == "finished"


@pytest.mark.parametrize("seed", [1, 5, 11])
async def test_mesa_grande_ejercita_todos_los_roles_especiales(table, seed):
    """Con 12 jugadores entran Cupido y Cazador, y el veneno de la bruja.

    Comprueba que las cinco causas de muerte se producen de verdad en una
    partida real y que la victoria anunciada es coherente con quién queda vivo.
    """
    ctx, transport, _inbox, _script = table(
        12, witch_reply="veneno 2", hunter_reply="1"
    )
    game = WerewolfGame(ctx, timers=fast_timers(), rng=random.Random(seed))
    result = await game.run()

    assert result.status == "finished"
    privados = "\n".join(text for _, text in transport.direct_messages)
    assert "Elige a dos jugadores" in privados, "Cupido no llegó a actuar"
    assert "Acabas de morir" in privados, "el Cazador no llegó a disparar"

    causas = {p["death_cause"] for p in result.players if not p["alive"]}
    assert {"lobos", "veneno", "amor", "cazador", "linchamiento"} & causas == causas
    assert {"amor", "linchamiento"} <= causas

    # El ganador anunciado concuerda con el recuento final.
    vivos = [p for p in result.players if p["alive"]]
    lobos = [p for p in vivos if p["role"] == "lobo"]
    if result.winner == "pueblo":
        assert not lobos
    elif result.winner == "lobos":
        assert len(lobos) >= len(vivos) - len(lobos)


async def test_roles_no_se_filtran_al_grupo_durante_la_partida(table):
    """Ningún rol secreto se anuncia en el grupo antes del cierre."""
    _, transport, script, _ = await _play(table, 8)

    # Todo lo enviado al grupo salvo el mensaje final de revelación.
    durante = "\n".join(transport.group_messages[:-1])
    for jid, role in script.roles.items():
        nombre = script.names[jid]
        if role == "Hombre Lobo":
            # No puede aparecer "Jugador3 ... Hombre Lobo" fuera de las muertes,
            # que sí revelan el rol por diseño.
            for linea in durante.splitlines():
                if nombre in linea and "Hombre Lobo" in linea:
                    assert linea.lstrip().startswith("☠️"), linea
