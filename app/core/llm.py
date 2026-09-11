"""Acceso al modelo de lenguaje.

Arranca con DeepSeek por coste, pero al ser compatible con la API de OpenAI
basta cambiar ``LLM_PROVIDER`` / ``LLM_BASE_URL`` / ``LLM_MODEL`` para usar
otro proveedor.

Regla de diseño: **el LLM nunca es crítico**. Da ambientación y ayuda a
interpretar mensajes libres, pero si falla, se agota el tiempo o no hay clave
configurada, todo devuelve ``None`` y el juego sigue con textos y parsers
deterministas. Una partida no se cae porque una API tenga un mal día.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from app.config import Settings
from app.logging_conf import get_logger

log = get_logger("llm")

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)
_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


class LLMClient:
    """Envoltorio sobre un chat model de LangChain con degradación elegante."""

    def __init__(self, settings: Settings, *, enabled: bool = True) -> None:
        """``enabled=False`` construye un cliente apagado sin tocar la red.

        Sirve para las partidas que no piden narración con modelo: el juego
        recibe un cliente con la misma interfaz que siempre devuelve ``None``,
        así que cae en los textos estáticos sin ramas extra en los nodos.
        """
        self._settings = settings
        self._model: Any | None = None
        self._disabled_reason: str | None = None

        if not enabled:
            self._disabled_reason = "no se pidió narración con modelo"
            log.info("llm.disabled", reason=self._disabled_reason)
            return

        if not settings.llm_enabled:
            self._disabled_reason = (
                "LLM_PROVIDER=none" if settings.llm_provider == "none" else "falta LLM_API_KEY"
            )
            log.info("llm.disabled", reason=self._disabled_reason)
            return

        # El try cubre la construcción, no el import: una base_url mal formada
        # o un parámetro que el proveedor no acepte no debe tumbar el arranque.
        try:
            self._model = ChatOpenAI(
                model=settings.llm_model,
                base_url=settings.llm_base_url,
                api_key=settings.llm_api_key,
                temperature=settings.llm_temperature,
                max_tokens=settings.llm_max_tokens,
                timeout=settings.llm_timeout_seconds,
                max_retries=settings.llm_max_retries,
            )
            log.info("llm.ready", provider=settings.llm_provider, model=settings.llm_model)
        except Exception as exc:  # noqa: BLE001
            self._disabled_reason = str(exc)
            log.error("llm.init_failed", error=str(exc))

    @property
    def available(self) -> bool:
        return self._model is not None

    async def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str | None:
        """Devuelve texto libre, o ``None`` si el modelo no está disponible."""
        response = await self._invoke(system, user, temperature=temperature, max_tokens=max_tokens)
        if response is None:
            return None
        text = response.strip()
        return text or None

    async def complete_json(
        self,
        system: str,
        user: str,
        *,
        temperature: float | None = 0.1,
        max_tokens: int | None = None,
    ) -> dict[str, Any] | None:
        """Pide una respuesta JSON y la parsea de forma tolerante.

        ``max_tokens`` hace falta cuando la respuesta es una lista larga: el
        presupuesto de serie está pensado para una escena narrada, y un JSON
        truncado no se parsea y se pierde entero.
        """
        response = await self._invoke(
            system,
            user,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=True,
        )
        if response is None:
            return None
        return parse_json_object(response)

    async def _invoke(
        self,
        system: str,
        user: str,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> str | None:
        if self._model is None:
            return None

        model = self._model
        overrides: dict[str, Any] = {}
        if temperature is not None:
            overrides["temperature"] = temperature
        if max_tokens is not None:
            overrides["max_tokens"] = max_tokens
        if json_mode:
            # DeepSeek y OpenAI comparten este parámetro.
            overrides["response_format"] = {"type": "json_object"}
        if overrides:
            try:
                model = model.bind(**overrides)
            except Exception as exc:  # noqa: BLE001
                log.warning("llm.bind_failed", error=str(exc))

        messages = [SystemMessage(content=system), HumanMessage(content=user)]
        # El timeout del cliente cubre cada intento; este cubre el total,
        # reintentos incluidos, para que un nodo del grafo no se cuelgue.
        budget = self._settings.llm_timeout_seconds * (self._settings.llm_max_retries + 1) + 5

        try:
            response = await asyncio.wait_for(model.ainvoke(messages), timeout=budget)
        except TimeoutError:
            log.warning("llm.timeout", budget=budget)
            return None
        except Exception as exc:  # noqa: BLE001
            log.warning("llm.invoke_failed", error=str(exc))
            return None

        content = getattr(response, "content", response)
        return _flatten_content(content)


def _flatten_content(content: Any) -> str | None:
    """Aplana la respuesta, que puede venir como lista de bloques."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text") or block.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts) if parts else None
    return None


def parse_json_object(raw: str) -> dict[str, Any] | None:
    """Extrae un objeto JSON de una respuesta que puede traer ruido.

    Los modelos añaden a veces vallas de código o una frase de cortesía antes
    del JSON, así que se limpia y, como último recurso, se busca el primer
    bloque entre llaves.
    """
    if not raw:
        return None

    cleaned = _FENCE.sub("", raw).strip()
    for candidate in (cleaned, _extract_block(cleaned)):
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict):
            return data
        if isinstance(data, list):
            return {"items": data}
    log.warning("llm.json_parse_failed", preview=raw[:200])
    return None


def _extract_block(text: str) -> str | None:
    match = _JSON_BLOCK.search(text)
    return match.group(0) if match else None
