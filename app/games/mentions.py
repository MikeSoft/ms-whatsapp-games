"""Composición de mensajes de grupo con contactos etiquetados.

WhatsApp resuelve ``@<número>`` mostrando el nombre del contacto, y WAHA acepta
la lista de JID etiquetados en el campo ``mentions`` de ``sendText``. Etiquetar
en vez de sólo nombrar quita toda ambigüedad: se ve exactamente **de quién** se
habla, **a quién** mataron y, por tanto, **a quién ignorar** el resto de la
partida.

Es genérico a propósito: cualquier juego futuro compone así sus mensajes.

    texto = GroupText(enabled=True)
    cuerpo = f"☠️ {texto.tag(jid, 'Ana')} ha caído"
    await transport.send_group(cuerpo, mentions=texto.mentions)
"""

from __future__ import annotations

from dataclasses import dataclass, field


def digits_of(jid: str) -> str:
    """``"573001234567@c.us"`` -> ``"573001234567"``."""
    return "".join(ch for ch in (jid or "").split("@", 1)[0] if ch.isdigit())


def mention_token(jid: str) -> str:
    """El token que WhatsApp sustituye por el nombre del contacto."""
    return f"@{digits_of(jid)}"


@dataclass
class GroupText:
    """Acumula los contactos etiquetados mientras se compone un mensaje.

    Se crea uno por mensaje: así la lista de ``mentions`` que se manda a WAHA
    contiene exactamente a quienes aparecen en ese texto, sin arrastrar los del
    mensaje anterior.
    """

    #: Con ``False`` se escriben nombres planos. Sirve para motores de WAHA sin
    #: soporte de menciones, donde el token quedaría como un número crudo.
    enabled: bool = True
    _mentioned: list[str] = field(default_factory=list)

    def tag(self, jid: str, fallback: str) -> str:
        """Etiqueta a un contacto, o devuelve ``fallback`` si están desactivadas."""
        digits = digits_of(jid)
        if not self.enabled or not digits:
            return fallback
        if jid not in self._mentioned:
            self._mentioned.append(jid)
        return f"@{digits}"

    @property
    def mentions(self) -> list[str]:
        """Los JID etiquetados, en orden de aparición."""
        return list(self._mentioned)

    def __bool__(self) -> bool:
        return bool(self._mentioned)
