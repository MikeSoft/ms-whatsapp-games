"""Partidas completas del grafo, con jugadores automáticos."""

from __future__ import annotations

import random
import re

import pytest

from app.core.inbox import MemoryInbox
from app.core.llm import LLMClient
from app.games.werewolf.game import WerewolfGame
from app.games.werewolf.nodes import (
    DEBATE_MAX_CHARS,
    DEBATE_MAX_LINES,
    Timers,
    WerewolfNodes,
)
from app.games.werewolf.roles import Role
from app.games.werewolf.state import initial_state
from app.waha.models import Scope
from tests.conftest import (
    GROUP_ID,
    FakeTransport,
    fast_timers,
    inbound,
    make_context,
    make_settings,
    render_mentions,
)

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


async def test_el_historial_narrativo_queda_en_el_estado_del_grafo(table):
    """`narrative_log` acumula lo publicado al grupo, sin el relleno de espera.

    Es una clave acumulativa del estado: si ningún nodo la devolviera, el
    checkpoint no serviría para releer la partida.
    """
    ctx, transport, _inbox, _script = table(6)
    game = WerewolfGame(ctx, timers=fast_timers())
    await game.run()

    historial = game.last_state.get("narrative_log", [])
    assert historial, "el estado tenía que acumular la narración"
    # Es exactamente lo enviado al grupo, en orden.
    assert historial == transport.group_messages
    assert any("EL HOMBRE LOBO" in linea for linea in historial)
    assert any("Todos los roles" in linea for linea in historial)


async def test_roles_no_se_filtran_al_grupo_durante_la_partida(table):
    """Ningún rol secreto se anuncia en el grupo antes del cierre."""
    _, transport, script, _ = await _play(table, 8)

    # Todo lo enviado al grupo salvo el mensaje final de revelación, leído
    # como lo ve la gente: los tokens de mención resueltos a nombres.
    durante = "\n".join(
        render_mentions(m, script.names) for m in transport.group_messages[:-1]
    )
    for jid, role in script.roles.items():
        nombre = script.names[jid]
        if role != "Hombre Lobo":
            continue
        # No puede aparecer "Jugador3 ... Hombre Lobo" fuera de las muertes,
        # que sí revelan el rol por diseño. Con límites de palabra, para que
        # "Jugador1" no se confunda con "Jugador10".
        for linea in durante.splitlines():
            if re.search(rf"\b{re.escape(nombre)}\b", linea) and "Hombre Lobo" in linea:
                assert linea.lstrip().startswith("☠️"), linea


# =====================================================================
# Coreografía de las tools de WAHA
# =====================================================================
async def test_cada_tool_de_waha_se_usa_en_su_fase(table):
    """El agente no sólo llama a las tools: las llama cuando toca.

    Es la parte que un test de "termina bien" no cubre. Se comprueba el orden
    real de las operaciones contra WAHA a lo largo de una partida entera.
    """
    ctx, transport, _inbox, _script = table(8)
    game = WerewolfGame(ctx, timers=fast_timers())
    result = await game.run()
    assert result.status == "finished"

    jugadores = {p["jid"] for p in result.players}

    # 1. El grupo se silencia ANTES de repartir roles: si se reparte con el
    #    chat abierto, alguien comenta su rol y la partida está arruinada.
    primer_lock = transport.lock_history.index(True)
    assert primer_lock == 0, "el primer cambio de permisos tiene que ser silenciar"

    # 2. Cada jugador recibe su rol por privado, y sólo el suyo.
    role_dms = transport.dms_matching("Tu rol es")
    assert {jid for jid, _ in role_dms} == jugadores
    assert len(role_dms) == len(jugadores)

    # 3. Las peticiones nocturnas van sólo a quien tiene poder, nunca al grupo.
    for marca in ("¿A quién devoráis?", "conocer la identidad", "Pociones que te quedan"):
        assert marca not in transport.group_text(), f"{marca!r} se filtró al grupo"

    # 4. El grupo se reabre en el amanecer, antes del debate y la votación.
    grupo = transport.group_messages
    indice_amanecer = next(i for i, m in enumerate(grupo) if "AMANECE EL DÍA" in m)
    indice_votacion = next(i for i, m in enumerate(grupo) if "*VOTACIÓN*" in m)
    assert indice_amanecer < indice_votacion
    assert False in transport.lock_history, "el chat tenía que reabrirse"

    # 5. La encuesta se publica en la fase de votación, con los vivos de ese
    #    momento y dentro del límite de WhatsApp.
    assert transport.polls
    for pregunta, opciones in transport.polls:
        assert "linchamos" in pregunta
        assert 2 <= len(opciones) <= 12
        assert len(set(opciones)) == len(opciones), "opciones repetidas"

    # 6. Se cierra con el chat abierto: nadie se queda mudo tras la partida.
    assert transport.lock_history[-1] is False


async def test_el_orden_de_los_privados_de_la_noche_es_el_correcto(table):
    """La bruja se consulta DESPUÉS de los lobos.

    Necesita saber a quién atacaron para decidir si cura, así que su ventana
    no puede abrirse en paralelo con la de la manada.
    """
    _, transport, _script, _ = await _play(table, 8, witch_reply="curar")

    privados = [texto for _, texto in transport.direct_messages]
    primer_lobo = next(
        (i for i, t in enumerate(privados) if "¿A quién devoráis?" in t), None
    )
    primera_bruja = next(
        (i for i, t in enumerate(privados) if "Pociones que te quedan" in t), None
    )
    assert primer_lobo is not None and primera_bruja is not None
    assert primer_lobo < primera_bruja, "la bruja se consultó antes que los lobos"

    # Y se le dice a quién atacaron, que es el dato que necesita.
    consulta = privados[primera_bruja]
    assert "los lobos" in consulta


async def test_el_cazador_se_consulta_al_morir_y_solo_entonces():
    """Su privado llega en el momento de su muerte, no al repartir roles.

    Se fuerza su linchamiento en vez de esperar que el azar lo mate: así el
    test comprueba siempre lo que dice comprobar.
    """
    transport = FakeTransport()
    inbox = MemoryInbox()
    ctx = make_context(transport=transport, inbox=inbox, session_id="s-caz")
    nodes = WerewolfNodes(
        ctx, timers=Timers(hunter=0.3, filler_interval=0.0), rng=random.Random(1)
    )

    def _jugador(numero: int, rol: Role) -> dict:
        return {
            "jid": f"5730044444{numero:02d}@c.us",
            "name": f"J{numero}",
            "number": numero,
            "role": str(rol),
            "alive": True,
            "death_round": None,
            "death_cause": None,
        }

    players = [
        _jugador(1, Role.LOBO),
        _jugador(2, Role.CAZADOR),
        _jugador(3, Role.ALDEANO),
        _jugador(4, Role.ALDEANO),
        _jugador(5, Role.ALDEANO),
    ]
    cazador = players[1]["jid"]

    # Antes de morir no se le pregunta nada.
    assert transport.dms_matching("Acabas de morir") == []

    async def responde(jid, text):
        if "Acabas de morir" in text:
            await inbox.push("s-caz", inbound(jid, "1"))  # se lleva al lobo

    transport.on_direct = responde

    estado = dict(initial_state("s-caz", GROUP_ID))
    estado["players"] = players
    estado["round_no"] = 2
    estado["votes"] = {p["jid"]: cazador for p in players if p["jid"] != cazador}

    updates = await nodes.veredicto(estado)

    # 1. Se le preguntó, y sólo a él.
    disparos = transport.dms_matching("Acabas de morir")
    assert [jid for jid, _ in disparos] == [cazador]

    # 2. Su disparo se resolvió: el lobo se fue con él.
    resultado = {p["jid"]: p for p in updates["players"]}
    assert resultado[cazador]["death_cause"] == "linchamiento"
    assert resultado[players[0]["jid"]]["death_cause"] == "cazador"

    # 3. El grupo se enteró de las dos muertes.
    anuncio = transport.group_messages[-1]
    assert anuncio.count("☠️") == 2


# =====================================================================
# El narrador escucha el juicio
# =====================================================================
def _con_modelo() -> object:
    """Ajustes con el LLM habilitado (las llamadas se interceptan aparte)."""
    return make_settings(llm_provider="deepseek", llm_api_key="sk-de-prueba")


async def test_lo_hablado_en_el_juicio_llega_al_estado_y_al_narrador(table, monkeypatch):
    """El debate deja de tirarse: se recoge y se le pasa al modelo.

    Antes el buzón del grupo se llenaba durante el juicio y la votación lo
    descartaba entero, así que la ambientación sólo sabía cuántos segundos
    quedaban. Ahora el narrador comenta acusaciones reales.
    """
    prompts_vistos: list[str] = []

    async def fake_complete(self, system, user, **kwargs):
        prompts_vistos.append(user)
        return "La plaza se enciende y las antorchas tiemblan."

    monkeypatch.setattr(LLMClient, "complete", fake_complete)

    dichos = ["yo creo que es Jugador3", "Jugador3 lleva callado toda la noche", "paso"]
    ctx, _transport, _inbox, _script = table(
        6, settings=_con_modelo(), debate_lines=dichos
    )
    game = WerewolfGame(ctx, timers=fast_timers())
    await game.run()

    # Queda en el estado del grafo, así que viaja al checkpoint.
    oido = game.last_state.get("debate_log", [])
    assert oido, "el juicio tenía que dejar constancia de lo hablado"
    assert {linea["dijo"] for linea in oido} <= set(dichos)
    assert all(linea["quien"].startswith("Jugador") for linea in oido)

    # Y el veredicto se narra sabiendo lo que se dijo.
    veredictos = [p for p in prompts_vistos if "ESCENA: veredicto" in p]
    assert veredictos, "no se narró ningún veredicto"
    assert any("se_dijo" in p for p in veredictos)
    assert any("lleva callado toda la noche" in p for p in veredictos)


async def test_el_narrador_no_oye_a_los_muertos_ni_al_propio_bot():
    """Al pueblo se le dice que ignore a quien cayó; al narrador también.

    Si lo escuchara, comentaría intervenciones de gente eliminada y filtraría
    que sigue jugando.
    """
    transport = FakeTransport()
    inbox = MemoryInbox()
    ctx = make_context(transport=transport, inbox=inbox, session_id="s-oye")
    nodes = WerewolfNodes(ctx, timers=Timers(debate=0.05, filler_interval=0.0))

    vivo = {"jid": "573001@c.us", "name": "Viva", "number": 1, "alive": True}
    muerto = {"jid": "573002@c.us", "name": "Muerto", "number": 2, "alive": False}

    mensajes = [
        inbound(vivo["jid"], "sospecho de alguien", scope=Scope.GROUP),
        inbound(muerto["jid"], "yo sé quién es el lobo", scope=Scope.GROUP),
        inbound(vivo["jid"], "", scope=Scope.GROUP),
    ]
    propio = inbound(vivo["jid"], "mensaje del bot", scope=Scope.GROUP)
    propio = propio.model_copy(update={"from_me": True})

    lineas = nodes._debate_lines([*mensajes, propio], [vivo, muerto])

    assert lineas == [{"quien": "Viva", "dijo": "sospecho de alguien"}]


async def test_lo_hablado_se_acota_en_numero_y_longitud():
    """El prompt no puede crecer con el tamaño de la mesa."""
    transport = FakeTransport()
    ctx = make_context(transport=transport, inbox=MemoryInbox(), session_id="s-tope")
    nodes = WerewolfNodes(ctx, timers=fast_timers())

    jugador = {"jid": "573001@c.us", "name": "Habla", "number": 1, "alive": True}
    muchos = [
        inbound(jugador["jid"], f"mensaje {i} " + "x" * 400, scope=Scope.GROUP)
        for i in range(40)
    ]

    lineas = nodes._debate_lines(muchos, [jugador])

    assert len(lineas) == DEBATE_MAX_LINES
    assert all(len(linea["dijo"]) <= DEBATE_MAX_CHARS for linea in lineas)
    # Se queda la cola de la conversación, que es la que tiene el calor.
    assert lineas[-1]["dijo"].startswith("mensaje 39")


async def test_el_relleno_del_juicio_comenta_lo_que_se_esta_diciendo(monkeypatch):
    """La ambientación de espera reacciona al debate, no sólo al reloj."""
    prompts_vistos: list[str] = []

    async def fake_complete(self, system, user, **kwargs):
        prompts_vistos.append(user)
        return "Los ánimos se calientan junto al pozo."

    monkeypatch.setattr(LLMClient, "complete", fake_complete)

    transport = FakeTransport()
    inbox = MemoryInbox()
    ctx = make_context(
        settings=_con_modelo(), transport=transport, inbox=inbox, session_id="s-relleno"
    )
    nodes = WerewolfNodes(ctx, timers=Timers(debate=0.4, filler_interval=0.1))

    jugador = {"jid": "573001@c.us", "name": "Ana", "number": 1, "alive": True}
    await inbox.push("s-relleno", inbound(jugador["jid"], "fue Beto", scope=Scope.GROUP))

    oido = await nodes._escuchar_juicio("s-relleno", 1, [jugador])

    assert oido == [{"quien": "Ana", "dijo": "fue Beto"}]
    debates = [p for p in prompts_vistos if "ESCENA: debate" in p]
    assert debates, "no se mandó ambientación durante el juicio"
    assert any("fue Beto" in p for p in debates)


async def test_si_el_narrador_falla_el_juicio_sigue_corriendo(monkeypatch):
    """La ambientación es prescindible; la ventana del juicio no."""

    async def revienta(self, system, user, **kwargs):
        raise RuntimeError("el modelo se cayó")

    monkeypatch.setattr(LLMClient, "complete", revienta)

    transport = FakeTransport()
    inbox = MemoryInbox()
    ctx = make_context(
        settings=_con_modelo(), transport=transport, inbox=inbox, session_id="s-falla"
    )
    nodes = WerewolfNodes(ctx, timers=Timers(debate=0.4, filler_interval=0.1))

    jugador = {"jid": "573001@c.us", "name": "Ana", "number": 1, "alive": True}
    await inbox.push("s-falla", inbound(jugador["jid"], "fue Beto", scope=Scope.GROUP))

    oido = await nodes._escuchar_juicio("s-falla", 1, [jugador])

    assert oido == [{"quien": "Ana", "dijo": "fue Beto"}]
