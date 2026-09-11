"""El concurso de preguntas, jugado de principio a fin sin red."""

from __future__ import annotations

import random

from app.config import Settings
from app.core.inbox import MemoryInbox
from app.core.llm import LLMClient
from app.games.kahoot.brief import Brief, parse_brief
from app.games.kahoot.game import KahootGame
from app.games.kahoot.questions import (
    Question,
    fallback_questions,
    parse_questions,
    shuffle_options,
)
from app.waha.models import Scope
from tests.conftest import FakeTransport, inbound, make_context, make_settings


# =====================================================================
# La instrucción del máster
# =====================================================================
def test_la_instruccion_separa_las_cifras_del_tema():
    s = make_settings()

    b = parse_brief("15 preguntas de 10 segundos sobre cine de los ochenta", s)
    assert (b.questions, b.seconds, b.options) == (15, 10, 5)
    assert b.topic == "cine de los ochenta"

    # El andamiaje de la orden no es el tema, ni por delante ni por detrás.
    assert parse_brief("haz preguntas sobre biología con 4 opciones", s).topic == "biología"
    assert parse_brief("genera preguntas de historia de Roma", s).topic == "historia de Roma"


def test_sin_instruccion_se_usan_los_valores_por_defecto():
    b = parse_brief("", make_settings())
    assert (b.questions, b.seconds, b.options) == (10, 10, 5)
    assert b.topic_or_default == "cultura general"


def test_lo_que_pida_el_master_se_acota_a_lo_que_el_juego_admite():
    """Una encuesta de WhatsApp no admite 99 opciones, se pida lo que se pida."""
    b = parse_brief("500 preguntas de 1 segundo con 99 opciones", make_settings())
    assert b.questions == 30
    assert b.seconds == 3
    assert b.options == 12


# =====================================================================
# Lo que devuelve el modelo
# =====================================================================
def _cruda(texto="¿Capital de Francia?", opciones=None, correcta=0):
    return {
        "pregunta": texto,
        "opciones": opciones if opciones is not None else ["París", "Roma", "Berlín"],
        "correcta": correcta,
    }


def test_una_pregunta_bien_formada_se_acepta():
    (q,) = parse_questions({"preguntas": [_cruda()]}, options=3)
    assert q.text == "¿Capital de Francia?"
    assert q.answer == "París"


def test_se_descarta_lo_que_no_se_puede_publicar():
    """Una encuesta rota no se arregla después de enviarla."""
    malas = [
        _cruda(opciones=["París", "Roma"]),              # faltan opciones
        _cruda(correcta=9),                              # índice fuera de rango
        _cruda(correcta=None),                           # sin correcta
        _cruda(texto=""),                                # sin enunciado
        {"pregunta": "¿Y?", "opciones": "no es lista"},  # forma inesperada
        "ni siquiera es un objeto",
    ]
    assert parse_questions({"preguntas": malas}, options=3) == []


def test_las_opciones_repetidas_tumban_la_pregunta():
    """WhatsApp no admite dos opciones iguales en la misma encuesta."""
    cruda = _cruda(opciones=["París", "parís", "Roma"])
    assert parse_questions({"preguntas": [cruda]}, options=3) == []


def test_la_correcta_puede_venir_como_texto_en_vez_de_indice():
    """Algunos modelos devuelven la respuesta en vez de su posición."""
    (q,) = parse_questions({"preguntas": [_cruda(correcta="Roma")]}, options=3)
    assert q.answer == "Roma"


def test_no_se_repite_la_misma_pregunta_en_una_tanda():
    data = {"preguntas": [_cruda(), _cruda(), _cruda(texto="¿Capital de Italia?")]}
    assert len(parse_questions(data, options=3)) == 2


def test_las_opciones_se_barajan_para_que_la_correcta_no_caiga_siempre_igual():
    """Sin barajar se gana mirando la posición, no sabiendo la respuesta."""
    preguntas = [
        Question(text=f"P{i}", options=("buena", "mala1", "mala2", "mala3"), correct=0)
        for i in range(40)
    ]
    barajadas = shuffle_options(preguntas, random.Random(3))

    assert all(q.answer == "buena" for q in barajadas)
    assert len({q.correct for q in barajadas}) > 1


def test_el_banco_de_respaldo_cubre_una_partida_entera_sin_modelo():
    """Regla de la casa: todo tiene que funcionar con LLM_PROVIDER=none."""
    b = parse_brief("10 preguntas con 4 opciones", make_settings())
    preguntas = fallback_questions(b)

    assert len(preguntas) == 10
    assert all(len(q.options) == 4 for q in preguntas)
    assert all(q.answer in q.options for q in preguntas)
    assert len({q.text for q in preguntas}) == 10


# =====================================================================
# La partida
# =====================================================================
def _con_modelo() -> Settings:
    return make_settings(llm_provider="deepseek", llm_api_key="sk-de-prueba")


PREGUNTAS = [
    {"pregunta": "¿Capital de Francia?", "opciones": ["París", "Roma", "Berlín"], "correcta": 0},
    {"pregunta": "¿Capital de Italia?", "opciones": ["París", "Roma", "Berlín"], "correcta": 1},
    {"pregunta": "¿Capital de Alemania?", "opciones": ["París", "Roma", "Berlín"], "correcta": 2},
]

ANA = "573001@c.us"
BETO = "573002@c.us"


def _rapido(**kwargs) -> Brief:
    """Un cuestionario que se juega en milisegundos."""
    base = {"topic": "pruebas", "questions": 3, "seconds": 0.05, "options": 3}
    base.update(kwargs)
    return Brief(**base)


def _mesa(
    monkeypatch,
    votos,
    *,
    settings: Settings | None = None,
    lock: bool = True,
    con_nombre: bool = True,
):
    """Monta el concurso con votantes automáticos.

    ``votos`` recibe (numero_de_pregunta, opciones) y devuelve una lista de
    ``(jid, opcion_elegida)``: así cada test decide quién acierta y quién no
    sin depender del orden en que se barajen las opciones.

    Con ``con_nombre=False`` los votos llegan sin nombre, que es como los
    manda WhatsApp de verdad: el evento ``poll.vote`` trae el identificador
    del votante y nada más.
    """
    transport = FakeTransport()
    inbox = MemoryInbox()
    resolved = settings or make_settings(
        llm_provider="deepseek", llm_api_key="sk-de-prueba", kahoot_lock_group=lock
    )
    ctx = make_context(
        settings=resolved, transport=transport, inbox=inbox, session_id="s-kahoot"
    )

    async def sin_red(self, system, user, **kwargs):
        return {"preguntas": PREGUNTAS}

    monkeypatch.setattr(LLMClient, "complete_json", sin_red)

    contador = {"n": 0}

    async def al_publicar(question: str, options: list[str]) -> None:
        contador["n"] += 1
        for jid, eleccion in votos(contador["n"], options):
            await inbox.push(
                "s-kahoot",
                inbound(
                    jid, "", scope=Scope.GROUP, poll_options=[eleccion],
                    name={ANA: "Ana", BETO: "Beto"}.get(jid) if con_nombre else None,
                ),
            )

    transport.on_poll = al_publicar
    return ctx, transport, inbox


async def test_una_tanda_completa_puntua_y_ordena(monkeypatch):
    """Ana acierta las tres, Beto ninguna: la clasificación lo refleja."""

    def votos(numero, options):
        correcta = PREGUNTAS[numero - 1]["opciones"][PREGUNTAS[numero - 1]["correcta"]]
        fallo = next(o for o in options if o != correcta)
        return [(ANA, correcta), (BETO, fallo)]

    ctx, transport, _ = _mesa(monkeypatch, votos)
    juego = KahootGame(ctx, brief=_rapido(), breather=0, rng=random.Random(1))
    result = await juego.run()

    assert result.status == "finished"
    assert result.winner == "Ana"
    assert result.players == [
        {"nombre": "Ana", "aciertos": 3},
        {"nombre": "Beto", "aciertos": 0},
    ]

    # La clasificación etiqueta a la gente. El cuerpo lleva el `@<id>` pelado
    # a propósito: el nombre visible no lo manda el bot, lo pone el cliente de
    # cada lector al cruzar ese id con el JID del array `mentions`.
    clasificacion, menciones = transport.group_matching("CLASIFICACIÓN")[0]
    assert "@573001 — 3/3" in clasificacion
    assert "@573002 — 0/3" in clasificacion
    assert menciones == [ANA, BETO]
    # Las respuestas correctas van en un único mensaje.
    respuestas = [m for m in transport.group_messages if "RESPUESTAS CORRECTAS" in m]
    assert len(respuestas) == 1
    for pregunta in PREGUNTAS:
        assert pregunta["pregunta"] in respuestas[0]


async def test_cada_encuesta_se_retira_al_cerrarse(monkeypatch):
    """Una pregunta ya cerrada no puede quedar votable en el chat."""
    ctx, transport, _ = _mesa(monkeypatch, lambda n, o: [(ANA, o[0])])
    juego = KahootGame(ctx, brief=_rapido(), breather=0)
    await juego.run()

    assert len(transport.polls) == 3
    assert transport.deleted == ["poll-1", "poll-2", "poll-3"]


async def test_el_grupo_se_cierra_para_jugar_y_se_reabre_al_final(monkeypatch):
    ctx, transport, _ = _mesa(monkeypatch, lambda n, o: [(ANA, o[0])])
    await KahootGame(ctx, brief=_rapido(), breather=0).run()

    assert transport.lock_history[0] is True
    assert transport.locked is False


async def test_si_nadie_responde_la_primera_se_reabre_el_grupo(monkeypatch):
    """El silencio del grupo puede estar impidiendo votar; mejor comprobarlo.

    No sabemos con certeza si WhatsApp deja votar en un grupo restringido a
    administradores. Si nadie contesta la primera, se reabre y se avisa, en
    vez de jugar la tanda entera en el vacío.
    """
    ctx, transport, _ = _mesa(monkeypatch, lambda n, o: [])
    await KahootGame(ctx, brief=_rapido(), breather=0).run()

    assert transport.lock_history[0] is True
    # Se reabre ya en la primera pregunta, sin esperar al final.
    assert transport.lock_history[1] is False
    assert "reabro el chat" in transport.group_text()


async def test_el_ultimo_voto_de_cada_persona_es_el_que_cuenta(monkeypatch):
    """WhatsApp deja cambiar la respuesta mientras la encuesta esté abierta."""

    def votos(numero, options):
        correcta = PREGUNTAS[numero - 1]["opciones"][PREGUNTAS[numero - 1]["correcta"]]
        fallo = next(o for o in options if o != correcta)
        # Primero se equivoca y luego rectifica.
        return [(ANA, fallo), (ANA, correcta)]

    ctx, _transport, _ = _mesa(monkeypatch, votos)
    juego = KahootGame(ctx, brief=_rapido(), breather=0)
    result = await juego.run()

    assert result.players == [{"nombre": "Ana", "aciertos": 3}]


async def test_marcar_varias_opciones_no_es_acertar(monkeypatch):
    ctx, transport, inbox = _mesa(monkeypatch, lambda n, o: [])

    async def marca_todo(question: str, options: list[str]) -> None:
        await inbox.push(
            "s-kahoot",
            inbound(ANA, "", scope=Scope.GROUP, poll_options=list(options), name="Ana"),
        )

    transport.on_poll = marca_todo
    result = await KahootGame(ctx, brief=_rapido(), breather=0).run()

    assert result.players == [{"nombre": "Ana", "aciertos": 0}]


async def test_un_voto_de_la_pregunta_anterior_no_cuenta_en_la_siguiente(monkeypatch):
    """Las opciones se repiten entre preguntas; el identificador desempata."""
    ctx, transport, inbox = _mesa(monkeypatch, lambda n, o: [])

    async def vota_tarde(question: str, options: list[str]) -> None:
        # Siempre vota "Roma", pero declarando que es de la encuesta 1.
        mensaje = inbound(ANA, "", scope=Scope.GROUP, poll_options=["Roma"], name="Ana")
        await inbox.push("s-kahoot", mensaje.model_copy(update={"poll_id": "poll-1"}))

    transport.on_poll = vota_tarde
    juego = KahootGame(ctx, brief=_rapido(), breather=0)
    result = await juego.run()

    # Sólo se le cuenta la pregunta 1, cuya respuesta correcta no es Roma.
    assert result.players == [{"nombre": "Ana", "aciertos": 0}]


async def test_sin_modelo_se_juega_con_el_banco_y_se_avisa(monkeypatch):
    """Regla de la casa: nada deja de funcionar con LLM_PROVIDER=none."""
    ctx, transport, _ = _mesa(
        monkeypatch, lambda n, o: [(ANA, o[0])], settings=make_settings()
    )
    juego = KahootGame(ctx, brief=_rapido(), breather=0)
    result = await juego.run()

    assert result.status == "finished"
    assert len(juego.questions) == 3
    assert "cultura general del repertorio" in transport.group_text()


async def test_cancelar_reabre_el_grupo(monkeypatch):
    ctx, transport, _ = _mesa(monkeypatch, lambda n, o: [])
    await KahootGame(ctx, brief=_rapido(), breather=0).on_cancel()

    assert transport.locked is False
    assert "cancelado" in transport.group_text()


async def test_sin_menciones_la_clasificacion_cae_al_nombre_resuelto(monkeypatch):
    """Un voto de encuesta no trae nombre y el votante llega como @lid.

    Con las menciones apagadas nadie pone el nombre por nosotros, así que hay
    que ir a buscarlo: publicar el identificador en crudo deja una
    clasificación en la que no se reconoce nadie.
    """
    ctx, transport, _ = _mesa(
        monkeypatch,
        lambda n, o: [(ANA, o[0])],
        settings=make_settings(
            llm_provider="deepseek", llm_api_key="sk-de-prueba", use_mentions=False
        ),
        con_nombre=False,
    )
    transport.contact_names[ANA] = "Paula Jara"

    await KahootGame(ctx, brief=_rapido(), breather=0).run()

    clasificacion, menciones = transport.group_matching("CLASIFICACIÓN")[0]
    assert "Paula Jara" in clasificacion
    assert "573001" not in clasificacion
    assert menciones == []


async def test_el_nombre_se_pide_una_sola_vez_por_persona(monkeypatch):
    """Diez preguntas no son diez consultas por votante."""
    ctx, transport, _ = _mesa(
        monkeypatch,
        lambda n, o: [(ANA, o[0])],
        settings=make_settings(
            llm_provider="deepseek", llm_api_key="sk-de-prueba", use_mentions=False
        ),
        con_nombre=False,
    )
    pedidos: list[str] = []
    transport.contact_names[ANA] = "Paula Jara"
    original = transport.contact_name

    async def contando(jid: str):
        pedidos.append(jid)
        return await original(jid)

    transport.contact_name = contando
    await KahootGame(ctx, brief=_rapido(), breather=0).run()

    assert pedidos == [ANA]


def test_la_instruccion_aguanta_como_escribe_la_gente():
    """Frases reales, no la forma canónica que uno imagina al escribir el parser.

    Lo que importa es que las cifras salgan exactas y que el tema no arrastre
    el andamiaje de la frase: "que duren", "de a", "y con" no son temas.
    """
    s = make_settings()
    casos = [
        (
            "has 5 preguntas relacionadas a colombia que duren 8 segundos "
            "y de a 3 respuestas",
            ("colombia", 5, 8, 3),
        ),
        ("haz 8 preguntas de cultura general que duren 15 segundos",
         ("cultura general", 8, 15, 5)),
        ("quiero preguntas difíciles de anime, 12 preguntas, 6 segundos",
         ("difíciles de anime", 12, 6, 5)),
        ("10 preguntas sobre sexo con 5 respuestas por pregunta",
         ("sexo", 10, 10, 5)),
        ("hazme preguntas de historia del rock cada una de 12 segundos",
         ("historia del rock", 10, 12, 5)),
        ("preguntas de fútbol colombiano", ("fútbol colombiano", 10, 10, 5)),
    ]
    for frase, esperado in casos:
        b = parse_brief(frase, s)
        assert (b.topic_or_default, b.questions, b.seconds, b.options) == esperado, frase


async def test_a_igualdad_de_aciertos_gana_quien_respondio_antes(monkeypatch):
    """El desempate es por rapidez, no por el azar del diccionario.

    Ana y Beto aciertan las tres, pero Beto vota siempre primero. La
    clasificación tiene que ponerlo por encima.
    """

    def votos(numero, options):
        correcta = PREGUNTAS[numero - 1]["opciones"][PREGUNTAS[numero - 1]["correcta"]]
        # Beto entra antes al buzón en todas las preguntas.
        return [(BETO, correcta), (ANA, correcta)]

    ctx, _transport, _ = _mesa(monkeypatch, votos)
    result = await KahootGame(ctx, brief=_rapido(), breather=0).run()

    assert result.players == [
        {"nombre": "Beto", "aciertos": 3},
        {"nombre": "Ana", "aciertos": 3},
    ]
    assert result.winner == "Beto"


async def test_responder_rapido_y_mal_no_da_ventaja(monkeypatch):
    """Sólo cuenta la rapidez en lo que se acierta.

    Si contaran también los fallos, quien vota lo primero que ve ganaría el
    desempate a quien se lo piensa y acierta igual.
    """

    def votos(numero, options):
        correcta = PREGUNTAS[numero - 1]["opciones"][PREGUNTAS[numero - 1]["correcta"]]
        fallo = next(o for o in options if o != correcta)
        # Beto dispara primero pero falla la primera; luego acierta.
        if numero == 1:
            return [(BETO, fallo), (ANA, correcta)]
        return [(BETO, correcta), (ANA, correcta)]

    ctx, _transport, _ = _mesa(monkeypatch, votos)
    result = await KahootGame(ctx, brief=_rapido(), breather=0).run()

    # Ana acierta 3 y Beto 2: no hay empate que desempatar.
    assert result.players == [
        {"nombre": "Ana", "aciertos": 3},
        {"nombre": "Beto", "aciertos": 2},
    ]


async def test_cambiar_de_respuesta_no_conserva_la_rapidez_del_primer_intento(
    monkeypatch,
):
    """Si rectificás, tu rapidez es la de la respuesta que cuenta.

    Beto dispara primero y falla, y corrige al final de la ventana. Ana
    contesta bien a la primera, después del disparo de Beto pero antes que su
    corrección. Los dos acaban con tres aciertos, así que el orden lo decide
    el desempate: tiene que ganar Ana.
    """

    def votos(numero, options):
        correcta = PREGUNTAS[numero - 1]["opciones"][PREGUNTAS[numero - 1]["correcta"]]
        fallo = next(o for o in options if o != correcta)
        return [(BETO, fallo), (ANA, correcta), (BETO, correcta)]

    ctx, _transport, _ = _mesa(monkeypatch, votos)
    result = await KahootGame(ctx, brief=_rapido(), breather=0).run()

    assert result.players == [
        {"nombre": "Ana", "aciertos": 3},
        {"nombre": "Beto", "aciertos": 3},
    ]
    assert result.winner == "Ana"
