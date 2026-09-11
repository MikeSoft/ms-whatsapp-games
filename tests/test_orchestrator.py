"""El orquestador de punta a punta: webhook HTTP → partida → limpieza."""

from __future__ import annotations

import asyncio

import pytest

from app.config import Settings
from app.core.inbox import MemoryInbox
from app.core.llm import LLMClient
from app.orchestrator.commands import parse_command
from app.orchestrator.manager import Orchestrator
from app.waha.client import WahaClient
from app.waha.models import InboundMessage, Scope, SentMessage
from tests.conftest import GROUP_ID, MANAGER, inbound, make_jid, make_settings


# ================================================================= comandos
def test_parseo_de_comandos_tolera_mayusculas_y_acentos():
    assert parse_command("!juego hombreslobo").name == "juego"
    assert parse_command("!JUEGO lobos").args == ["lobos"]
    assert parse_command("!catálogo").name == "juegos"
    assert parse_command("!Cancelar").name == "cancelar"
    assert parse_command("!juego hombres lobo").argument == "hombres lobo"


def test_lo_que_no_es_comando_no_se_confunde():
    assert parse_command("juego sin prefijo") is None
    assert parse_command("!") is None
    assert parse_command("") is None
    assert parse_command("!noexiste") is None
    assert parse_command("hola !juego") is None


def test_el_prefijo_es_configurable():
    assert parse_command("/juego lobos", prefix="/").name == "juego"
    assert parse_command("!juego lobos", prefix="/") is None


def test_el_flag_de_ia_no_se_confunde_con_el_nombre_del_juego():
    """Un nombre puede llevar varias palabras, así que el flag se separa."""
    con_ia = parse_command("!juego hombreslobo ia")
    assert con_ia.argument == "hombreslobo"
    assert con_ia.has("ia")

    # El nombre de dos palabras sigue llegando entero.
    largo = parse_command("!juego hombres lobo IA")
    assert largo.argument == "hombres lobo"
    assert largo.has("ia")

    # Va donde sea y admite las otras formas.
    assert parse_command("!juego ai hombreslobo").argument == "hombreslobo"
    assert parse_command("!juego hombreslobo llm").has("ia")

    sin_ia = parse_command("!juego hombreslobo")
    assert sin_ia.argument == "hombreslobo"
    assert not sin_ia.has("ia")


# ============================================================= orquestador
@pytest.fixture
def orchestrator(monkeypatch):
    """Orquestador con WAHA simulado y buzón en memoria."""
    outbox: list[tuple[str, str]] = []
    locks: list[bool] = []

    async def fake_send_text(self, chat_id, text, *, reply_to=None, mentions=None):
        outbox.append((chat_id, text))
        return SentMessage(ok=True, chat_id=chat_id, message_id=f"m{len(outbox)}")

    async def fake_send_poll(self, chat_id, question, options, *, multiple_answers=False):
        outbox.append((chat_id, f"[encuesta] {question}"))
        return SentMessage(ok=True, chat_id=chat_id, message_id="poll")

    async def fake_admins_only(self, group_id, admins_only):
        locks.append(admins_only)
        return True

    monkeypatch.setattr(WahaClient, "send_text", fake_send_text)
    monkeypatch.setattr(WahaClient, "send_poll", fake_send_poll)
    monkeypatch.setattr(WahaClient, "set_admins_only", fake_admins_only)

    def _build(**overrides) -> tuple[Orchestrator, list, list]:
        settings: Settings = make_settings(
            recruit_seconds=1,
            night_action_seconds=1,
            witch_action_seconds=1,
            hunter_action_seconds=1,
            debate_seconds=1,
            vote_seconds=1,
            filler_interval_seconds=0,
            **overrides,
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

    return _build


def _cmd(text: str) -> InboundMessage:
    return inbound(MANAGER, text, scope=Scope.GROUP, chat_id=GROUP_ID, name="Máster")


async def test_sin_el_flag_de_ia_la_partida_no_usa_el_modelo(orchestrator):
    """Que haya clave configurada no basta: el gasto lo decide el máster."""
    orch, _outbox, _ = orchestrator(llm_provider="deepseek", llm_api_key="sk-de-prueba")
    assert orch.llm.available is True

    await orch.handle(_cmd("!juego hombreslobo"))
    (partida,) = orch._games.values()
    assert partida.game.ctx.llm.available is False
    assert partida.game.ctx.flags == frozenset()

    await orch.shutdown()


async def test_con_el_flag_de_ia_la_partida_recibe_el_modelo(orchestrator):
    orch, outbox, _ = orchestrator(llm_provider="deepseek", llm_api_key="sk-de-prueba")

    await orch.handle(_cmd("!juego hombreslobo ia"))
    (partida,) = orch._games.values()
    assert partida.game.ctx.llm.available is True
    assert partida.game.ctx.flags == frozenset({"ia"})
    assert any("narración generada" in text for _, text in outbox)

    await orch.shutdown()


async def test_pedir_ia_sin_clave_configurada_no_rompe_la_partida(orchestrator):
    """Se lanza igual, con narrativa estática: el LLM nunca es crítico."""
    orch, outbox, _ = orchestrator()

    await orch.handle(_cmd("!juego hombreslobo ia"))
    (partida,) = orch._games.values()
    assert partida.game.ctx.llm.available is False
    assert any("narración estática" in text for _, text in outbox)

    await orch.shutdown()


async def test_solo_el_master_puede_lanzar_una_partida(orchestrator):
    orch, outbox, _ = orchestrator()
    intruso = inbound(
        "573009999999@c.us", "!juego hombreslobo", scope=Scope.GROUP, chat_id=GROUP_ID
    )
    await orch.handle(intruso)

    assert orch.snapshot()["partidas_activas"] == []
    assert outbox == []
    await orch.shutdown()


async def test_lanzar_dos_veces_el_mismo_juego_se_rechaza(orchestrator):
    orch, outbox, _ = orchestrator()
    await orch.handle(_cmd("!juego hombreslobo"))
    assert len(orch.snapshot()["partidas_activas"]) == 1

    await orch.handle(_cmd("!juego hombreslobo"))
    assert len(orch.snapshot()["partidas_activas"]) == 1
    assert any("Ya hay una partida" in text for _, text in outbox)

    await orch.shutdown()


async def test_partida_sin_jugadores_se_cierra_y_libera_el_grupo(orchestrator):
    """Nadie dice "Yo": la partida aborta y no deja el grupo silenciado."""
    orch, outbox, locks = orchestrator()
    await orch.handle(_cmd("!juego hombreslobo"))

    running = next(iter(orch._games.values()))
    await asyncio.wait_for(running.task, timeout=15)
    await asyncio.sleep(0.05)

    assert orch.snapshot()["partidas_activas"] == []
    texto = "\n".join(text for _, text in outbox)
    assert "se abren las inscripciones" in texto
    assert "Hacen falta" in texto
    # No se silenció un grupo por una partida que nunca arrancó.
    assert True not in locks


async def test_los_jugadores_se_inscriben_por_el_webhook(orchestrator):
    """Los "Yo" que llegan del webhook entran al buzón y forman la mesa."""
    orch, outbox, locks = orchestrator()
    await orch.handle(_cmd("!juego hombreslobo"))

    # Cinco personas se apuntan mientras corre la ventana de inscripción.
    for index in range(1, 6):
        await orch.handle(
            inbound(
                make_jid(index),
                "Yo" if index % 2 else "me apunto",
                scope=Scope.GROUP,
                chat_id=GROUP_ID,
                name=f"Jugador{index}",
            )
        )

    await asyncio.sleep(1.4)
    texto = "\n".join(text for _, text in outbox)
    assert "5 jugadores* entran a la partida" in texto
    # El reparto manda un privado a cada jugador.
    privados = {chat for chat, _ in outbox if chat.endswith("@c.us") and chat != MANAGER}
    assert len(privados) == 5
    # Y el grupo queda silenciado para la noche.
    assert locks[0] is True

    await orch.cancel(GROUP_ID)
    await orch.shutdown()


async def test_cancelar_corta_la_partida_y_reabre_el_grupo(orchestrator):
    orch, outbox, locks = orchestrator()
    await orch.handle(_cmd("!juego hombreslobo"))
    assert len(orch.snapshot()["partidas_activas"]) == 1

    await orch.handle(_cmd("!cancelar"))

    assert orch.snapshot()["partidas_activas"] == []
    texto = "\n".join(text for _, text in outbox)
    assert "Partida cancelada" in texto
    # on_cancel deja el chat abierto.
    assert locks and locks[-1] is False

    await orch.shutdown()


async def test_los_mensajes_del_propio_bot_no_vuelven_al_juego(orchestrator):
    """Sin este corte, los mensajes del máster crearían un bucle."""
    orch, _, _ = orchestrator()
    await orch.handle(_cmd("!juego hombreslobo"))
    running = next(iter(orch._games.values()))

    propio = inbound("573000000001@c.us", "Yo", scope=Scope.GROUP, chat_id=GROUP_ID)
    propio = propio.model_copy(update={"from_me": True})
    await orch.handle(propio)

    recogidos = await orch.inbox.collect(running.session_id, timeout=0.01, group=True)
    assert recogidos == []

    await orch.cancel(GROUP_ID)
    await orch.shutdown()


async def test_los_duplicados_del_webhook_se_descartan(orchestrator):
    orch, _, _ = orchestrator()
    await orch.handle(_cmd("!juego hombreslobo"))
    running = next(iter(orch._games.values()))

    mensaje = inbound(make_jid(1), "Yo", scope=Scope.GROUP, chat_id=GROUP_ID)
    await orch.handle(mensaje)
    await orch.handle(mensaje)

    recogidos = await orch.inbox.collect(running.session_id, timeout=0.01, group=True)
    assert len(recogidos) == 1

    await orch.cancel(GROUP_ID)
    await orch.shutdown()


async def test_un_privado_llega_al_buzon_del_jugador(orchestrator):
    orch, _, _ = orchestrator()
    await orch.handle(_cmd("!juego hombreslobo"))
    running = next(iter(orch._games.values()))

    jugador = make_jid(3)
    await orch.handle(inbound(jugador, "mato al 2"))

    recogidos = await orch.inbox.collect(
        running.session_id, timeout=0.01, direct=[jugador]
    )
    assert [m.text for m in recogidos] == ["mato al 2"]

    await orch.cancel(GROUP_ID)
    await orch.shutdown()


async def test_apagar_el_servicio_cancela_las_partidas(orchestrator):
    orch, _, locks = orchestrator()
    await orch.handle(_cmd("!juego hombreslobo"))
    assert len(orch.snapshot()["partidas_activas"]) == 1

    await orch.shutdown()
    assert orch.snapshot()["partidas_activas"] == []
    assert locks and locks[-1] is False


async def test_se_juega_en_el_grupo_desde_el_que_se_lanza(orchestrator):
    """Pedir la partida en un grupo y verla arrancar en otro no se entiende.

    Pasaba cuando un grupo configurado tenía prioridad sobre el del propio
    mensaje: el máster escribía en un grupo y el bot contestaba en otro.
    """
    otro = "120363999999999999@g.us"
    orch, outbox, _ = orchestrator()

    await orch.handle(
        inbound(MANAGER, "!juego hombreslobo", scope=Scope.GROUP, chat_id=otro,
                name="Máster")
    )

    (partida,) = orch._games.values()
    assert partida.group_id == otro
    # Y la confirmación también va a donde se preguntó.
    assert any(chat == otro for chat, _ in outbox)

    await orch.shutdown()


async def test_por_privado_no_se_adivina_el_grupo(orchestrator):
    """Sin grupo del que deducirlo, se pide que lo mande donde se juega.

    Antes existía un grupo configurable para esto, y era la causa de que una
    partida pedida en un sitio arrancara en otro.
    """
    orch, outbox, _ = orchestrator()

    await orch.handle(inbound(MANAGER, "!juego hombreslobo", scope=Scope.DIRECT))

    assert orch.snapshot()["partidas_activas"] == []
    assert any("se juega donde se pide" in text for _, text in outbox)

    await orch.shutdown()
