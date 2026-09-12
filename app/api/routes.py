"""Endpoints HTTP del microservicio."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel

from app.api.security import verify_webhook
from app.games import registry
from app.logging_conf import get_logger
from app.waha.models import WahaEvent
from app.waha.normalize import SUPPORTED_EVENTS, normalise

log = get_logger("api")

router = APIRouter()


class WebhookAck(BaseModel):
    ok: bool = True
    handled: bool = False
    reason: str | None = None


@router.post(
    "/webhooks/waha",
    response_model=WebhookAck,
    summary="Recibe los eventos de WAHA",
)
async def waha_webhook(request: Request, response: Response) -> WebhookAck:
    """Punto de entrada de WhatsApp.

    Se contesta 200 siempre que el evento se haya podido leer, incluso si no
    interesa: un 500 haría que WAHA reintentara la entrega en bucle.
    """
    settings = request.app.state.settings
    orchestrator = request.app.state.orchestrator

    body = await request.body()
    headers = {key.lower(): value for key, value in request.headers.items()}

    ok, reason = verify_webhook(settings, body=body, headers=headers)
    if not ok:
        log.warning("webhook.rejected", reason=reason)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=reason)

    try:
        event = WahaEvent.model_validate_json(body)
    except Exception as exc:
        log.warning("webhook.bad_payload", error=str(exc))
        raise HTTPException(status_code=422, detail="payload no reconocido") from exc

    if event.event not in SUPPORTED_EVENTS:
        return WebhookAck(ok=True, handled=False, reason=f"evento ignorado: {event.event}")

    message = normalise(event)
    if message is None:
        return WebhookAck(ok=True, handled=False, reason="evento sin mensaje utilizable")

    # Ojo: structlog reserva la clave "event" para el propio mensaje.
    log.info(
        "webhook.message",
        waha_event=event.event,
        scope=str(message.scope),
        sender=message.sender_id,
        chat=message.chat_id,
    )

    try:
        await orchestrator.handle(message)
    except Exception as exc:
        # No se devuelve 500: WAHA reintentaría y duplicaría el mensaje.
        log.exception("webhook.handle_failed", error=str(exc))
        response.status_code = status.HTTP_202_ACCEPTED
        return WebhookAck(ok=False, handled=False, reason="error interno registrado")

    return WebhookAck(ok=True, handled=True)


@router.get("/health", summary="Liveness")
async def health() -> dict[str, Any]:
    return {"status": "ok"}


@router.get("/health/ready", summary="Readiness")
async def readiness(request: Request, response: Response) -> dict[str, Any]:
    """Comprueba las dependencias de verdad, no lo que dice la configuración."""
    settings = request.app.state.settings
    checks: dict[str, Any] = {}

    redis_client = getattr(request.app.state, "redis", None)
    if redis_client is None:
        checks["redis"] = "no configurado (buzón en memoria)"
    else:
        try:
            await redis_client.ping()
            checks["redis"] = "ok"
        except Exception as exc:  # noqa: BLE001
            checks["redis"] = f"error: {exc}"

    store = getattr(request.app.state, "store", None)
    if store is None:
        checks["database"] = "no configurada"
    else:
        try:
            await store.recent_game_sessions(limit=1)
            checks["database"] = "ok"
        except Exception as exc:  # noqa: BLE001
            checks["database"] = f"error: {exc}"

    try:
        session = await request.app.state.waha.session_status()
        checks["waha"] = session.get("status", "UNKNOWN")
    except Exception as exc:  # noqa: BLE001
        checks["waha"] = f"error: {exc}"

    checks["llm"] = (
        f"{settings.llm_provider}:{settings.llm_model}"
        if request.app.state.orchestrator.llm.available
        else "deshabilitado (narrativa estática)"
    )

    degraded = [
        name
        for name, value in checks.items()
        if isinstance(value, str) and value.startswith("error")
    ]
    if degraded:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "degraded" if degraded else "ok", "checks": checks}


@router.get("/games", summary="Juegos registrados")
async def games(request: Request) -> dict[str, Any]:
    idioma = request.app.state.settings.game_language
    return {
        "juegos": [
            {
                "key": spec.key,
                "titulo": spec.title_in(idioma),
                "descripcion": spec.tagline_in(idioma),
                "alias": list(spec.aliases),
                "jugadores": spec.rango_jugadores(),
                "como_funciona": spec.how_to_in(idioma),
            }
            for spec in registry.specs()
        ]
    }


@router.get("/status", summary="Partidas en marcha")
async def game_status(request: Request) -> dict[str, Any]:
    return request.app.state.orchestrator.snapshot()
