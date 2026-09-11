"""Etiquetado de contactos en los mensajes de grupo.

Nombrar a alguien es ambiguo: hay tocayos, nombres parecidos y gente sin
nombre público. Etiquetar al contacto deja claro **de quién** se habla y
**quién** ha quedado fuera, que es lo que necesita el resto de la mesa para
saber a quién ignorar.
"""

from __future__ import annotations

import json
import re

import httpx
import pytest

from app.games.mentions import GroupText, digits_of, mention_token, tag_names
from app.games.werewolf.game import WerewolfGame
from app.games.werewolf.state import tag, tagged_label, tagged_roster
from app.waha.client import WahaClient
from tests.conftest import GROUP_ID, fast_timers, make_settings, render_mentions

ANA = {"jid": "573001234567@c.us", "name": "Ana", "number": 1, "alive": True, "role": "lobo"}
BETO = {
    "jid": "573007654321@c.us",
    "name": "Beto",
    "number": 2,
    "alive": False,
    "role": "aldeano",
    "death_cause": "lobos",
    "death_round": 1,
}


# =========================================================== el compositor
def test_el_token_es_el_numero_sin_dominio():
    assert digits_of("573001234567@c.us") == "573001234567"
    assert digits_of("120363111@g.us") == "120363111"
    assert digits_of("") == ""
    assert mention_token("573001234567@c.us") == "@573001234567"


def test_acumula_los_etiquetados_en_orden_y_sin_repetir():
    texto = GroupText()
    assert texto.tag(ANA["jid"], "Ana") == "@573001234567"
    assert texto.tag(BETO["jid"], "Beto") == "@573007654321"
    # Etiquetar dos veces al mismo no lo duplica en la lista.
    assert texto.tag(ANA["jid"], "Ana") == "@573001234567"
    assert texto.mentions == [ANA["jid"], BETO["jid"]]


def test_desactivadas_cae_al_nombre_y_no_manda_menciones():
    """Para motores de WAHA que no resuelvan menciones."""
    texto = GroupText(enabled=False)
    assert texto.tag(ANA["jid"], "Ana") == "Ana"
    assert texto.mentions == []


def test_un_jid_sin_digitos_usa_el_nombre():
    texto = GroupText()
    assert texto.tag("@lid", "Anónimo") == "Anónimo"
    assert texto.mentions == []


def test_las_etiquetas_de_jugador_llevan_su_numero_de_lista():
    texto = GroupText()
    assert tag(ANA, texto) == "@573001234567"
    assert tagged_label(ANA, texto) == "1. @573001234567"
    assert tagged_roster([ANA, BETO], texto, only_alive=False) == (
        "1. @573001234567\n2. @573007654321"
    )
    assert tagged_roster([ANA, BETO], texto) == "1. @573001234567"


# ===================================================== el cliente de WAHA
async def test_send_text_manda_el_array_de_menciones():
    cuerpos: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        cuerpos.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "m1"})

    settings = make_settings(waha_min_send_interval=0.0)
    client = WahaClient(
        settings,
        client=httpx.AsyncClient(
            base_url="http://waha:3000", transport=httpx.MockTransport(handler)
        ),
    )
    await client.send_text(
        GROUP_ID, "☠️ @573001234567 ha caído", mentions=[ANA["jid"]]
    )
    await client.aclose()

    assert cuerpos[0]["mentions"] == [ANA["jid"]]
    assert "@573001234567" in cuerpos[0]["text"]


async def test_sin_menciones_no_se_manda_el_campo():
    """WAHA no debe recibir un `mentions` vacío que no significa nada."""
    cuerpos: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        cuerpos.append(json.loads(request.content))
        return httpx.Response(200, json={})

    settings = make_settings(waha_min_send_interval=0.0)
    client = WahaClient(
        settings,
        client=httpx.AsyncClient(
            base_url="http://waha:3000", transport=httpx.MockTransport(handler)
        ),
    )
    await client.send_text(GROUP_ID, "hola")
    await client.aclose()

    assert "mentions" not in cuerpos[0]


# ============================================== dentro de una partida real
async def test_una_partida_etiqueta_a_quien_menciona(table):
    """Los mensajes clave del grupo llevan etiquetado a quien nombran."""
    ctx, transport, _inbox, _script = table(8)
    game = WerewolfGame(ctx, timers=fast_timers())
    result = await game.run()
    assert result.status == "finished"

    jugadores = {p["jid"] for p in result.players}

    # 1. El anuncio de la mesa etiqueta a los ocho.
    (texto_mesa, menciones_mesa) = transport.group_matching("entran a la partida")[0]
    assert set(menciones_mesa) == jugadores
    assert "@" in texto_mesa

    # 2. Cada anuncio de muerte etiqueta a la víctima.
    for cuerpo, menciones in transport.group_matching("☠️"):
        for linea in cuerpo.splitlines():
            if not linea.startswith("☠️"):
                continue
            etiquetados = [m for m in menciones if f"@{digits_of(m)}" in linea]
            assert etiquetados, f"muerte sin etiquetar: {linea!r}"

    # 3. La revelación final etiqueta a todos.
    (_, menciones_final) = transport.group_matching("Todos los roles")[0]
    assert set(menciones_final) == jugadores

    # 4. Todo token que aparece en un texto está declarado en sus menciones,
    #    porque WhatsApp sólo resuelve los que vienen en el array.
    for cuerpo, menciones in zip(
        transport.group_messages, transport.group_mentions, strict=True
    ):
        declarados = {f"@{digits_of(m)}" for m in menciones}
        for token in re.findall(r"@\d{7,}", cuerpo):
            assert token in declarados, f"token sin declarar: {token} en {cuerpo!r}"


async def test_el_grupo_avisa_de_que_a_los_muertos_se_les_ignora(table):
    _, transport, _script, _ = await _jugar(table)
    texto = transport.group_text()
    assert "ya no participa" in texto


async def _jugar(table, count: int = 8):
    ctx, transport, _inbox, script = table(count)
    game = WerewolfGame(ctx, timers=fast_timers())
    result = await game.run()
    return result, transport, script, ctx


async def test_con_menciones_desactivadas_se_usan_nombres(table):
    """Modo compatible: nombres planos y ningún array de menciones."""
    settings = make_settings(use_mentions=False)
    ctx, transport, _inbox, _script = table(6, settings=settings)
    game = WerewolfGame(ctx, timers=fast_timers())
    result = await game.run()

    assert result.status == "finished"
    assert all(not m for m in transport.group_mentions), "no debía etiquetar"
    texto = transport.group_text()
    assert "@5730" not in texto
    # Los nombres siguen apareciendo, así que la partida se entiende igual.
    assert any(p["name"] in texto for p in result.players)


async def test_los_privados_siguen_usando_nombres(table):
    """En un 1:1 el nombre es más legible que una etiqueta.

    Además el jugador necesita reconocer a quién señala en la lista de
    objetivos, y ahí un número de teléfono no ayuda.
    """
    _, transport, script, _ = await _jugar(table, 8)

    objetivos = [t for _, t in transport.direct_messages if "devoráis" in t]
    assert objetivos
    for cuerpo in objetivos:
        assert "@5730" not in cuerpo
        assert any(nombre in cuerpo for nombre in script.names.values())


def test_el_render_de_prueba_imita_a_whatsapp():
    """El doble de test resuelve los tokens igual que el cliente real."""
    names = {ANA["jid"]: "Ana", BETO["jid"]: "Beto"}
    crudo = "☠️ @573007654321 cayó. Sigue viva @573001234567."
    assert render_mentions(crudo, names) == "☠️ Beto cayó. Sigue viva Ana."


@pytest.mark.parametrize("activadas", [True, False])
async def test_el_juego_termina_igual_con_o_sin_menciones(table, activadas):
    settings = make_settings(use_mentions=activadas)
    ctx, transport, _inbox, _script = table(6, settings=settings)
    game = WerewolfGame(ctx, timers=fast_timers())
    result = await game.run()

    assert result.status == "finished"
    assert result.winner in {"lobos", "pueblo", "enamorados", "nadie"}
    assert transport.locked is False


# =====================================================================
# Nombres que el narrador escribe en prosa
# =====================================================================
def test_los_nombres_de_la_narracion_se_convierten_en_menciones():
    """El modelo escribe "Ana acusa a Beto"; el grupo ve dos menciones."""
    texto = GroupText(enabled=True)
    contactos = {"Ana": "573001@c.us", "Beto": "573002@c.us"}

    salida = tag_names("Ana señala a Beto y Beto calla.", contactos, texto)

    assert salida == "@573001 señala a @573002 y @573002 calla."
    # Una sola vez cada uno, en orden de aparición.
    assert texto.mentions == ["573001@c.us", "573002@c.us"]


def test_el_nombre_mas_largo_gana_y_no_se_parten_palabras():
    """"Ana María" no puede quedar como una mención de Ana más " María"."""
    texto = GroupText(enabled=True)
    contactos = {"Ana": "573001@c.us", "Ana María": "573002@c.us"}

    salida = tag_names("Ana María mira a Ana. Ananás no es nadie.", contactos, texto)

    assert salida == "@573002 mira a @573001. Ananás no es nadie."


def test_etiquetar_funciona_con_identificadores_lid():
    """Los grupos nuevos de WhatsApp identifican a la gente por @lid."""
    texto = GroupText(enabled=True)
    salida = tag_names("Jb tiene la palabra.", {"Jb": "13817111126136@lid"}, texto)

    assert salida == "@13817111126136 tiene la palabra."
    assert texto.mentions == ["13817111126136@lid"]


def test_con_menciones_desactivadas_la_narracion_queda_intacta():
    texto = GroupText(enabled=False)
    salida = tag_names("Ana acusa a Beto.", {"Ana": "573001@c.us"}, texto)

    assert salida == "Ana acusa a Beto."
    assert texto.mentions == []
