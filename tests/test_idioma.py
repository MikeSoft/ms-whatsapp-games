"""El idioma de la partida: lo que sale cambia, lo que entra se entiende igual.

`GAME_LANGUAGE=en` tiene que dar una partida jugable de punta a punta en
inglés, sin que se cuele una frase en español por una clave sin traducir.
"""

from __future__ import annotations

import pytest

from app.games.kahoot.texts import TEXTS as KAHOOT_TEXTS
from app.games.werewolf.game import WerewolfGame
from app.games.werewolf.parsing import is_abstention, looks_like_join, parse_witch_choice
from app.games.werewolf.prompts import FALLBACKS, FILLERS, system_for
from app.games.werewolf.roles import Role, info, roster_summary
from app.games.werewolf.texts import CAUSES
from app.games.werewolf.texts import TEXTS as WEREWOLF_TEXTS
from app.i18n import Texts, missing_keys
from app.orchestrator.texts import TEXTS as ORCHESTRATOR_TEXTS
from tests.conftest import fast_timers, make_settings, render_mentions

CATALOGOS = {
    "hombreslobo": WEREWOLF_TEXTS,
    "kahoot": KAHOOT_TEXTS,
    "orquestador": ORCHESTRATOR_TEXTS,
}

# Frases cortas que delatan español en un mensaje que tenía que salir en
# inglés. Se buscan como palabra suelta para no cazar nombres propios.
RASTROS_DE_ESPANOL = (
    " los ", " las ", " para ", " está ", " qué ", " quién ", " jugadores ",
    "Escribe", "Responde", "noche", "partida", "grupo",
)


@pytest.mark.parametrize("nombre", sorted(CATALOGOS))
def test_los_catalogos_estan_completos_en_los_dos_idiomas(nombre):
    """Una clave sin traducir se detecta aquí y no en mitad de una partida.

    El catálogo cae al español cuando le falta una clave, que es lo correcto
    en producción —mejor una frase en el idioma que no toca que una partida
    rota— pero a cambio hay que comprobar la paridad en algún sitio.
    """
    assert missing_keys(CATALOGOS[nombre]) == {"en": set()}


@pytest.mark.parametrize("nombre", sorted(CATALOGOS))
def test_las_traducciones_conservan_los_huecos(nombre):
    """Un `{name}` perdido en la traducción es un KeyError en tiempo de juego."""
    import re

    catalogo = CATALOGOS[nombre]
    for clave, plantilla in catalogo["es"].items():
        huecos_es = set(re.findall(r"\{(\w+)", plantilla))
        huecos_en = set(re.findall(r"\{(\w+)", catalogo["en"][clave]))
        assert huecos_es == huecos_en, clave


def test_los_respaldos_del_narrador_tambien_estan_en_los_dos():
    assert set(FALLBACKS["es"]) == set(FALLBACKS["en"])
    assert set(CAUSES["es"]) == set(CAUSES["en"])
    assert len(FILLERS["en"]) == len(FILLERS["es"])


def test_al_modelo_se_le_pide_el_idioma_de_la_partida():
    """El narrador escribe en inglés porque su prompt va en inglés."""
    assert "You are the Narrator" in system_for("en")
    assert "Eres el Narrador" in system_for("es")


def test_los_roles_se_nombran_en_el_idioma_de_la_partida():
    assert info(Role.LOBO, "en").title == "Werewolf"
    assert info(Role.LOBO, "es").title == "Hombre Lobo"
    assert "Werewolves" in roster_summary([Role.LOBO, Role.LOBO], "en")


def test_un_idioma_desconocido_cae_al_espanol_en_vez_de_romper():
    """Degradar es preferible: una partida no se cae por un ajuste raro."""
    t = Texts(WEREWOLF_TEXTS, "pt")  # type: ignore[arg-type]
    assert t("day.nobody_died") == WEREWOLF_TEXTS["es"]["day.nobody_died"]


# =====================================================================
# Lo que entra se entiende en los dos idiomas, juegue la mesa en el que juegue
# =====================================================================
@pytest.mark.parametrize(
    "texto",
    ["me", "I'm in", "count me in", "yo", "me apunto", "dale"],
)
def test_apuntarse_funciona_en_los_dos_idiomas(texto):
    assert looks_like_join(texto)


@pytest.mark.parametrize("texto", ["not me", "I'm out", "yo no", "paso"])
def test_quien_se_excluye_no_entra_en_ninguno_de_los_dos(texto):
    assert not looks_like_join(texto)


@pytest.mark.parametrize("texto", ["pass", "nobody", "skip", "paso", "nadie"])
def test_la_abstencion_se_entiende_en_los_dos_idiomas(texto):
    assert is_abstention(texto)


@pytest.mark.parametrize(
    "texto,esperado",
    [
        ("heal", "vida"),
        ("curar", "vida"),
        ("poison 3", "muerte"),
        ("veneno 3", "muerte"),
        ("nothing", "nada"),
        ("nada", "nada"),
    ],
)
def test_la_bruja_contesta_en_el_idioma_que_quiera(texto, esperado):
    assert parse_witch_choice(texto) == esperado


# =====================================================================
# Una partida entera en inglés
# =====================================================================
async def test_una_partida_en_ingles_no_deja_caer_ni_una_frase_en_espanol(table):
    """De la convocatoria al recuento de roles, todo sale en inglés."""
    settings = make_settings(game_language="en")
    ctx, transport, _inbox, script = table(6, settings=settings)
    game = WerewolfGame(ctx, timers=fast_timers())

    result = await game.run()

    assert result.status == "finished"
    assert transport.locked is False

    nombres = {jid: f"Jugador{i}" for i, jid in enumerate(script.jids, start=1)}
    for mensaje in transport.group_messages:
        texto = render_mentions(mensaje, nombres)
        for rastro in RASTROS_DE_ESPANOL:
            assert rastro not in texto, f"quedó español en el grupo: {texto[:120]}"

    # Y las señas del inglés están donde tienen que estar.
    todo = "\n".join(transport.group_messages)
    assert "WEREWOLF" in todo
    assert "Type *ME*" in todo
    assert "THE TRIAL" in todo or "Every role" in todo


async def test_los_privados_de_los_roles_tambien_van_en_ingles(table):
    settings = make_settings(game_language="en")
    ctx, transport, _inbox, _script = table(6, settings=settings)
    game = WerewolfGame(ctx, timers=fast_timers())

    await game.run()

    reparto = [text for _jid, text in transport.direct_messages if "Your role is" in text]
    assert reparto, "nadie recibió su rol en inglés"
    assert not any("Tu rol es" in text for _jid, text in transport.direct_messages)
