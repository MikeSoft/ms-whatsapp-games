"""Verificación de los webhooks entrantes.

WAHA puede firmar cada entrega con HMAC. Si se configura
``WAHA_WEBHOOK_HMAC_SECRET`` se comprueba la firma; si se configura
``WEBHOOK_SHARED_SECRET`` se comprueba una cabecera de secreto compartido.
Sin ninguno de los dos el endpoint queda abierto, lo que sólo es aceptable si
el servicio no está expuesto a internet.
"""

from __future__ import annotations

import hashlib
import hmac

from app.config import Settings
from app.logging_conf import get_logger

log = get_logger("security")

HMAC_HEADER = "x-webhook-hmac"
HMAC_ALGO_HEADER = "x-webhook-hmac-algorithm"
SHARED_SECRET_HEADER = "x-webhook-secret"

_ALGORITHMS = {
    "sha512": hashlib.sha512,
    "sha256": hashlib.sha256,
    "sha1": hashlib.sha1,
}


def verify_webhook(
    settings: Settings,
    *,
    body: bytes,
    headers: dict[str, str],
) -> tuple[bool, str]:
    """Devuelve ``(ok, motivo)``.

    Se normalizan las cabeceras a minúsculas antes de llamar.
    """
    if settings.webhook_shared_secret:
        provided = headers.get(SHARED_SECRET_HEADER, "")
        if not hmac.compare_digest(provided, settings.webhook_shared_secret):
            return False, "secreto compartido inválido"

    secret = settings.waha_webhook_hmac_secret
    if secret:
        signature = headers.get(HMAC_HEADER, "")
        if not signature:
            return False, "falta la cabecera HMAC"

        algo_name = (headers.get(HMAC_ALGO_HEADER) or "sha512").lower()
        algorithm = _ALGORITHMS.get(algo_name)
        if algorithm is None:
            return False, f"algoritmo HMAC no soportado: {algo_name}"

        expected = hmac.new(secret.encode("utf-8"), body, algorithm).hexdigest()
        if not hmac.compare_digest(expected, signature.strip().lower()):
            return False, "firma HMAC inválida"

    return True, "ok"


def is_unprotected(settings: Settings) -> bool:
    return not (settings.waha_webhook_hmac_secret or settings.webhook_shared_secret)
