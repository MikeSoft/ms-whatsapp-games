"""Interpretación determinista de los mensajes de los jugadores.

El LLM ayuda a decidir quién se apunta, pero todo lo que afecta a la mecánica
del juego (a quién matan, a quién votan) se resuelve aquí con reglas: una
partida no puede depender de que un modelo acierte, y un fallo de red no debe
cambiar quién muere.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence

from app.games.mentions import GroupText
from app.games.werewolf.state import Player, by_jid, label, tagged_label

#: Formas de apuntarse. Se comprueban tras descartar las negativas.
_JOIN_PATTERNS = (
    r"^yo$",
    r"^yo[!¡.\s]",
    r"\byo\s+(juego|voy|entro|quiero|le\s+entro)\b",
    r"\bme\s+(apunto|uno|sumo|meto)\b",
    r"\b(entro|voy|jugar|juego|dentro|apuntame|apúntame|anotame|anótame)\b",
    r"^(si|sí|sip|claro|dale|vale|ok|okey|listo|va)$",
    r"^(🙋|🙋‍♂️|🙋‍♀️|✋|🖐|👍|🐺|✅)+$",
)

#: Si aparece alguna de estas, el mensaje NO es una inscripción.
_JOIN_NEGATIVES = (
    r"\byo\s+no\b",
    r"\bno\s+(juego|puedo|entro|voy|quiero|me\s+apunto)\b",
    r"\b(paso|abstengo|luego|despues|después|otra\s+vez|mañana)\b",
)

_ABSTAIN_PATTERNS = (
    r"\b(paso|abstengo|nadie|ninguno|ninguna|nada|no\s+voto|blanco)\b",
)

_WITCH_HEAL = (r"\b(curar|cura|curo|salvar|salvo|salva|vida|revivir|revivo)\b",)
_WITCH_POISON = (
    r"\b(veneno|envenenar|envenena|envenenado|matar|mato|muerte|asesinar|asesino)\b",
)
_WITCH_NONE = (r"\b(nada|ninguna|paso|abstengo|nadie|guardo|reservo|no\s+uso)\b",)


#: Número de la lista de jugadores ("el 3", "voto por 3.").
#:
#: Se descarta el número que forme parte de una expresión mayor: sin esto, un
#: "3:30" o un "2-1" escritos durante la votación contarían como voto al
#: jugador 3 o al 2, y un voto falso decide quién muere.
_LIST_NUMBER = re.compile(r"(?<![\d:\-/])(\d{1,2})(?![\d:\-/])")


def normalise(text: str) -> str:
    """Minúsculas sin acentos, para comparar sin sorpresas."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", stripped.lower()).strip()


def _matches_any(text: str, patterns: Sequence[str]) -> bool:
    return any(re.search(pattern, text) for pattern in patterns)


def digits_of(jid: str) -> str:
    return "".join(ch for ch in (jid or "").split("@", 1)[0] if ch.isdigit())


def looks_like_join(text: str) -> bool:
    """¿Este mensaje del grupo es una inscripción a la partida?

    Es el respaldo determinista del reclutamiento por LLM, y también el filtro
    previo: lo que aquí es un "no" claro nunca se cuenta como inscripción.
    """
    norm = normalise(text)
    if not norm:
        return False
    if _matches_any(norm, _JOIN_NEGATIVES):
        return False
    return _matches_any(norm, _JOIN_PATTERNS)


def parse_player_reference(
    text: str,
    players: Sequence[Player],
    *,
    exclude: Sequence[str] = (),
) -> Player | None:
    """Resuelve a qué jugador se refiere un mensaje libre.

    Acepta el número de la lista ("3", "el 3", "voto por 3"), el nombre
    ("mato a Ana"), una mención por teléfono, o el nombre suelto.
    """
    norm = normalise(text)
    if not norm:
        return None

    excluded = set(exclude)
    pool = [p for p in players if p.get("jid") not in excluded]
    if not pool:
        return None

    # 1. Mención por número de teléfono (@573001234567).
    for run in re.findall(r"\d{7,}", norm):
        for player in pool:
            if run and run in digits_of(player.get("jid", "")):
                return player

    # 2. Número de la lista.
    for token in _LIST_NUMBER.findall(norm):
        number = int(token)
        for player in pool:
            if player.get("number") == number:
                return player

    # 3. Nombre completo (primero los más largos, para que "Ana María" gane
    #    a "Ana" cuando ambos existen).
    by_length = sorted(pool, key=lambda p: -len(normalise(p.get("name", ""))))
    for player in by_length:
        name = normalise(player.get("name", ""))
        if name and re.search(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])", norm):
            return player

    # 4. Cualquier token significativo del nombre (nombre de pila, apodo).
    tokens = set(re.findall(r"[a-z0-9]{3,}", norm))
    for player in by_length:
        parts = set(re.findall(r"[a-z0-9]{3,}", normalise(player.get("name", ""))))
        if parts & tokens:
            return player

    return None


def parse_two_player_references(
    text: str,
    players: Sequence[Player],
) -> tuple[Player, Player] | None:
    """Extrae dos jugadores distintos de un mensaje (para Cupido)."""
    first = parse_player_reference(text, players)
    if first is None:
        return None
    second = parse_player_reference(text, players, exclude=[first.get("jid", "")])
    if second is None:
        return None
    return first, second


def is_abstention(text: str) -> bool:
    return _matches_any(normalise(text), _ABSTAIN_PATTERNS)


def witch_decided(text: str) -> bool:
    """¿La bruja ha dicho algo interpretable como decisión?

    Sirve para cortar su espera en cuanto responde de verdad, sin que un
    "espera" o un "hmm" cuenten como turno consumido.
    """
    norm = normalise(text)
    if not norm:
        return False
    return (
        _matches_any(norm, _WITCH_HEAL)
        or _matches_any(norm, _WITCH_POISON)
        or _matches_any(norm, _WITCH_NONE)
    )


def parse_witch_choice(text: str) -> str:
    """Devuelve ``"vida"``, ``"muerte"`` o ``"nada"``.

    Ante la duda devuelve ``"nada"``: no gastar una poción por un mensaje
    ambiguo es mejor que gastarla mal.
    """
    norm = normalise(text)
    if not norm:
        return "nada"
    if _matches_any(norm, _WITCH_NONE) and not _matches_any(norm, _WITCH_HEAL):
        return "nada"
    if _matches_any(norm, _WITCH_HEAL):
        return "vida"
    if _matches_any(norm, _WITCH_POISON):
        return "muerte"
    return "nada"


def tally_votes(votes: dict[str, str]) -> tuple[list[str], int]:
    """Devuelve los JID más votados y cuántos votos tienen.

    Empatados incluidos: quien llama decide qué hacer con el empate.
    """
    counts: dict[str, int] = {}
    for target in votes.values():
        if target:
            counts[target] = counts.get(target, 0) + 1
    if not counts:
        return [], 0
    top = max(counts.values())
    return [jid for jid, count in counts.items() if count == top], top


def votes_breakdown(
    votes: dict[str, str],
    players: Sequence[Player],
    texto: GroupText | None = None,
) -> str:
    """Recuento legible para anunciar al grupo.

    Con ``texto`` se etiqueta a los acusados en vez de nombrarlos, y las
    menciones quedan acumuladas ahí para mandarlas junto al mensaje.
    """
    counts: dict[str, int] = {}
    for target in votes.values():
        if target:
            counts[target] = counts.get(target, 0) + 1

    lines = []
    for jid, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        player = by_jid(list(players), jid)
        if player is None:
            name = jid
        elif texto is not None:
            name = tagged_label(player, texto)
        else:
            name = label(player)
        lines.append(f"• {name}: {count} voto{'s' if count != 1 else ''}")
    return "\n".join(lines) if lines else "• nadie recibió votos"
