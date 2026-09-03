"""Cliente WAHA: reintentos, degradación y forma de las peticiones."""

from __future__ import annotations

import json

import httpx
import pytest

from app.waha.client import WahaClient, WahaError
from app.waha.models import WahaEvent
from app.waha.normalize import normalise
from tests.conftest import GROUP_ID, make_settings


def build_client(handler, **overrides) -> WahaClient:
    settings = make_settings(
        waha_base_url="http://waha:3000",
        waha_api_key="clave-de-prueba",
        waha_min_send_interval=0.0,
        **overrides,
    )
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(
        base_url=settings.waha_base_url,
        transport=transport,
        headers={"X-Api-Key": settings.waha_api_key or ""},
    )
    return WahaClient(settings, client=http)


# ==================================================================== envío
async def test_send_text_manda_la_peticion_esperada():
    vistas: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        vistas.append(request)
        return httpx.Response(201, json={"id": {"_serialized": "true_123@c.us_ABC"}})

    client = build_client(handler)
    result = await client.send_text("573001234567@c.us", "hola")

    assert result.ok is True
    assert result.message_id == "true_123@c.us_ABC"
    (request,) = vistas
    assert request.url.path == "/api/sendText"
    assert request.headers["x-api-key"] == "clave-de-prueba"
    body = json.loads(request.content)
    assert body == {"session": "default", "chatId": "573001234567@c.us", "text": "hola"}
    await client.aclose()


async def test_dry_run_no_toca_la_red():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no debía hacerse ninguna petición")

    client = build_client(handler, waha_dry_run=True)
    result = await client.send_text(GROUP_ID, "no sale de aquí")
    assert result.ok is True
    assert result.message_id == "dry-run"
    await client.aclose()


async def test_un_error_de_envio_no_lanza_excepcion():
    """Si un privado no llega, la partida sigue: ese jugador no actúa."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="chat no encontrado")

    client = build_client(handler)
    result = await client.send_text("573000000000@c.us", "hola")

    assert result.ok is False
    assert "chat no encontrado" in (result.error or "")
    await client.aclose()


async def test_reintenta_los_errores_temporales():
    intentos = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        intentos["n"] += 1
        if intentos["n"] < 3:
            return httpx.Response(503, text="temporalmente no disponible")
        return httpx.Response(200, json={"id": "ok"})

    client = build_client(handler, waha_send_max_retries=3)
    result = await client.send_text(GROUP_ID, "insiste")

    assert result.ok is True
    assert intentos["n"] == 3
    await client.aclose()


async def test_los_envios_reintentan_menos_que_las_consultas():
    """Un envío se rinde antes a propósito.

    Los envíos van serializados por el rate limit, así que insistir en uno
    retrasa a todos los demás. Un privado perdido sólo significa que ese
    jugador no actúa; un nodo bloqueado rompe la partida entera.
    """
    intentos = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        intentos["n"] += 1
        return httpx.Response(503, text="caído")

    client = build_client(handler, waha_max_retries=5, waha_send_max_retries=2)
    result = await client.send_text(GROUP_ID, "hola")

    assert result.ok is False
    assert intentos["n"] == 2, "el envío usa su propio presupuesto, no el general"

    # Una consulta sí agota el presupuesto general.
    intentos["n"] = 0
    await client.session_status()
    assert intentos["n"] == 5
    await client.aclose()


async def test_no_reintenta_los_errores_del_cliente():
    intentos = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        intentos["n"] += 1
        return httpx.Response(422, text="petición inválida")

    client = build_client(handler, waha_max_retries=3)
    result = await client.send_text(GROUP_ID, "mal formada")

    assert result.ok is False
    assert intentos["n"] == 1, "un 422 no mejora reintentando"
    await client.aclose()


async def test_agotar_los_reintentos_devuelve_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = build_client(handler, waha_max_retries=2)
    result = await client.send_text(GROUP_ID, "hola")
    assert result.ok is False
    await client.aclose()


# ================================================================ encuestas
async def test_send_poll_respeta_el_formato_de_waha():
    vistas: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        vistas.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "poll-1"})

    client = build_client(handler)
    ok = await client.send_poll(GROUP_ID, "¿A quién?", ["1. Ana", "2. Beto"])

    assert ok.ok is True
    assert vistas[0]["poll"] == {
        "name": "¿A quién?",
        "options": ["1. Ana", "2. Beto"],
        "multipleAnswers": False,
    }
    await client.aclose()


async def test_una_encuesta_con_una_sola_opcion_se_rechaza():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("WhatsApp exige dos opciones; no hay que preguntar")

    client = build_client(handler)
    result = await client.send_poll(GROUP_ID, "¿?", ["sólo una"])
    assert result.ok is False
    await client.aclose()


async def test_las_opciones_se_recortan_al_limite_de_whatsapp():
    vistas: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        vistas.append(json.loads(request.content))
        return httpx.Response(200, json={})

    client = build_client(handler)
    await client.send_poll(GROUP_ID, "¿?", [f"{i}. Jugador" for i in range(1, 20)])
    assert len(vistas[0]["poll"]["options"]) == 12
    await client.aclose()


# =================================================================== grupos
async def test_silenciar_el_grupo_usa_el_endpoint_de_waha():
    vistas: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        vistas.append(request)
        return httpx.Response(200, json={})

    client = build_client(handler)
    assert await client.set_admins_only(GROUP_ID, True) is True

    (request,) = vistas
    assert request.method == "PUT"
    assert request.url.path == (
        f"/api/default/groups/{GROUP_ID}/settings/security/messages-admin-only"
    )
    await client.aclose()


async def test_sin_waha_plus_silenciar_falla_sin_romper_la_partida():
    """El endpoint es de WAHA Plus: su ausencia degrada, no aborta."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="Not Found")

    client = build_client(handler)
    assert await client.set_admins_only(GROUP_ID, True) is False
    await client.aclose()


async def test_se_puede_desactivar_la_gestion_de_permisos():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("con MANAGE_GROUP_PERMISSIONS=false no se llama a WAHA")

    client = build_client(handler, manage_group_permissions=False)
    assert await client.set_admins_only(GROUP_ID, True) is False
    await client.aclose()


async def test_estado_de_sesion_degrada_a_desconocido():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = build_client(handler, waha_max_retries=1)
    assert (await client.session_status())["status"] == "UNKNOWN"
    await client.aclose()


async def test_un_error_de_red_se_propaga_como_waha_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sin ruta al host")

    client = build_client(handler, waha_max_retries=1)
    with pytest.raises(WahaError):
        await client._request("GET", "/api/sessions/default")
    await client.aclose()


# ============================================== ida y vuelta con el webhook
async def test_lo_que_manda_waha_se_normaliza_de_vuelta():
    """Cierra el círculo: el formato que envía WAHA es el que se sabe leer."""
    evento = WahaEvent(
        event="message",
        session="default",
        payload={
            "id": "false_120363@g.us_ABC",
            "from": GROUP_ID,
            "participant": "573001234567@c.us",
            "fromMe": False,
            "body": "Yo",
            "timestamp": 1_700_000_000,
            "_data": {"notifyName": "Ana"},
        },
    )
    mensaje = normalise(evento)
    assert mensaje is not None
    assert mensaje.chat_id == GROUP_ID
    assert mensaje.sender_id == "573001234567@c.us"
    assert mensaje.display_name == "Ana"
    assert mensaje.text == "Yo"
