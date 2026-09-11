"""Endpoints HTTP: verificación de firma, encaminamiento y healthchecks."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.orchestrator.manager import Orchestrator
from app.waha.client import WahaClient
from app.waha.models import SentMessage
from tests.conftest import GROUP_ID, MANAGER, make_settings

HMAC_SECRET = "un-secreto-de-prueba"


@pytest.fixture
def sent(monkeypatch):
    """Captura todo lo que el servicio intenta enviar por WAHA."""
    outbox: list[tuple[str, str]] = []

    async def fake_send_text(self, chat_id, text, *, reply_to=None, mentions=None):
        outbox.append((chat_id, text))
        return SentMessage(ok=True, chat_id=chat_id, message_id=f"m{len(outbox)}")

    async def fake_send_poll(self, chat_id, question, options, *, multiple_answers=False):
        outbox.append((chat_id, f"[encuesta] {question}"))
        return SentMessage(ok=True, chat_id=chat_id, message_id="poll")

    async def fake_admins_only(self, group_id, admins_only):
        outbox.append((group_id, f"[lock={admins_only}]"))
        return True

    async def fake_session_status(self):
        return {"status": "WORKING"}

    monkeypatch.setattr(WahaClient, "send_text", fake_send_text)
    monkeypatch.setattr(WahaClient, "send_poll", fake_send_poll)
    monkeypatch.setattr(WahaClient, "set_admins_only", fake_admins_only)
    monkeypatch.setattr(WahaClient, "session_status", fake_session_status)
    return outbox


def build_client(**overrides) -> TestClient:
    settings = make_settings(
        database_url="sqlite+aiosqlite:///:memory:",
        checkpointer="memory",
        **overrides,
    )
    return TestClient(create_app(settings))


def message_event(
    text: str,
    *,
    sender: str = MANAGER,
    group: bool = False,
    message_id: str = "evt-1",
) -> dict:
    payload = {
        "id": message_id,
        "timestamp": 1_700_000_000,
        "fromMe": False,
        "body": text,
        "_data": {"notifyName": "Máster"},
    }
    if group:
        payload["from"] = GROUP_ID
        payload["participant"] = sender
    else:
        payload["from"] = sender
    return {"event": "message", "session": "default", "payload": payload}


def sign(body: bytes, secret: str = HMAC_SECRET) -> dict[str, str]:
    digest = hmac.new(secret.encode(), body, hashlib.sha512).hexdigest()
    return {"X-Webhook-Hmac": digest, "X-Webhook-Hmac-Algorithm": "sha512"}


# ================================================================== salud
def test_health_responde_ok(sent):
    with build_client() as client:
        assert client.get("/health").json() == {"status": "ok"}


def test_readiness_reporta_cada_dependencia(sent):
    with build_client() as client:
        body = client.get("/health/ready").json()
    assert set(body["checks"]) == {"redis", "database", "waha", "llm"}
    assert body["checks"]["database"] == "ok"
    assert body["checks"]["waha"] == "WORKING"
    # Sin clave de LLM, la narrativa es estática y se dice explícitamente.
    assert "deshabilitado" in body["checks"]["llm"]


def test_endpoint_de_juegos_lista_el_hombre_lobo(sent):
    with build_client() as client:
        body = client.get("/games").json()
    keys = [game["key"] for game in body["juegos"]]
    assert "hombreslobo" in keys


# ================================================================ seguridad
def test_webhook_rechaza_firma_hmac_invalida(sent):
    with build_client(waha_webhook_hmac_secret=HMAC_SECRET) as client:
        response = client.post(
            "/webhooks/waha",
            json=message_event("!juegos"),
            headers={"X-Webhook-Hmac": "0" * 128, "X-Webhook-Hmac-Algorithm": "sha512"},
        )
    assert response.status_code == 401
    assert "HMAC" in response.json()["detail"]


def test_webhook_acepta_firma_hmac_valida(sent):
    event = message_event("!juegos")
    body = json.dumps(event).encode()
    with build_client(waha_webhook_hmac_secret=HMAC_SECRET) as client:
        response = client.post(
            "/webhooks/waha",
            content=body,
            headers={"Content-Type": "application/json", **sign(body)},
        )
    assert response.status_code == 200
    assert response.json()["handled"] is True


def test_webhook_exige_hmac_cuando_esta_configurado(sent):
    with build_client(waha_webhook_hmac_secret=HMAC_SECRET) as client:
        response = client.post("/webhooks/waha", json=message_event("!juegos"))
    assert response.status_code == 401
    assert "falta" in response.json()["detail"]


def test_webhook_valida_el_secreto_compartido(sent):
    with build_client(webhook_shared_secret="s3cr3t") as client:
        malo = client.post(
            "/webhooks/waha",
            json=message_event("!juegos"),
            headers={"X-Webhook-Secret": "otro"},
        )
        bueno = client.post(
            "/webhooks/waha",
            json=message_event("!juegos", message_id="evt-2"),
            headers={"X-Webhook-Secret": "s3cr3t"},
        )
    assert malo.status_code == 401
    assert bueno.status_code == 200


# ============================================================== rutas y flujo
def test_eventos_no_soportados_se_ignoran_con_200(sent):
    with build_client() as client:
        response = client.post(
            "/webhooks/waha",
            json={"event": "message.ack", "payload": {"id": "x"}},
        )
    assert response.status_code == 200
    assert response.json()["handled"] is False
    assert "ignorado" in response.json()["reason"]


def test_payload_ilegible_devuelve_422(sent):
    with build_client() as client:
        response = client.post(
            "/webhooks/waha",
            content=b"no soy json",
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 422


def test_el_master_puede_listar_juegos_por_whatsapp(sent):
    with build_client() as client:
        response = client.post("/webhooks/waha", json=message_event("!juegos"))
    assert response.status_code == 200
    respuestas = [text for _, text in sent]
    assert any("Juegos disponibles" in text for text in respuestas)
    assert any("hombreslobo" in text for text in respuestas)


def test_comando_de_un_numero_ajeno_se_ignora(sent):
    ajeno = "573009999999@c.us"
    with build_client() as client:
        client.post("/webhooks/waha", json=message_event("!juegos", sender=ajeno))
    assert sent == [], "sólo el número del máster puede dar órdenes"


def test_comando_desconocido_no_se_confunde_con_uno_valido(sent):
    with build_client() as client:
        client.post("/webhooks/waha", json=message_event("!inventado"))
    assert sent == []


def test_estado_sin_partidas(sent):
    with build_client() as client:
        client.post("/webhooks/waha", json=message_event("!estado"))
        snapshot = client.get("/status").json()
    assert any("No hay ninguna partida" in text for _, text in sent)
    assert snapshot["partidas_activas"] == []
    assert "hombreslobo" in snapshot["juegos_registrados"]


def test_ayuda_lista_los_comandos(sent):
    with build_client() as client:
        client.post("/webhooks/waha", json=message_event("!ayuda"))
    texto = "\n".join(text for _, text in sent)
    assert "!juego" in texto and "!cancelar" in texto


def test_juego_desconocido_avisa_al_master(sent):
    with build_client() as client:
        client.post("/webhooks/waha", json=message_event("!juego parchis"))
    texto = "\n".join(text for _, text in sent)
    assert "No conozco el juego" in texto


def test_cancelar_sin_partida_lo_dice(sent):
    with build_client() as client:
        client.post("/webhooks/waha", json=message_event("!cancelar"))
    assert any("No había ninguna partida" in text for _, text in sent)


def test_un_fallo_interno_devuelve_202_y_no_500(sent, monkeypatch):
    """Un 500 haría que WAHA reintentara la entrega en bucle."""

    async def handle_roto(self, message):
        raise RuntimeError("algo se rompió por dentro")

    monkeypatch.setattr(Orchestrator, "handle", handle_roto)

    with build_client() as client:
        response = client.post("/webhooks/waha", json=message_event("!juegos"))

    assert response.status_code == 202
    body = response.json()
    assert body["ok"] is False
    assert body["handled"] is False


def test_mensajes_repetidos_se_procesan_una_sola_vez(sent):
    """WAHA reintenta entregas; un "Yo" contado dos veces falsearía la leva."""
    event = message_event("!juegos", message_id="mismo-id")
    with build_client() as client:
        client.post("/webhooks/waha", json=event)
        primera = len(sent)
        client.post("/webhooks/waha", json=event)
        segunda = len(sent)
    assert primera > 0
    assert segunda == primera

