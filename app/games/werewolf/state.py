"""Estado del grafo de El Hombre Lobo.

Es la "verdad absoluta" de la partida y viaja por todos los nodos. Se usa
``TypedDict`` con estructuras primitivas (listas y diccionarios) para que el
checkpointer de LangGraph pueda serializarlo sin sorpresas.

Las claves anotadas con ``operator.add`` acumulan: un nodo devuelve **sólo lo
nuevo** y LangGraph lo concatena con lo que ya había.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Literal, TypedDict

from app.games.mentions import GroupText
from app.games.werewolf.roles import Role, info

Phase = Literal[
    "reclutamiento",
    "reparto",
    "noche",
    "resolucion",
    "amanecer",
    "debate",
    "votacion",
    "veredicto",
    "fin",
]


class Player(TypedDict, total=False):
    jid: str
    name: str
    #: Número estable que se usa en encuestas y privados (1..N).
    number: int
    role: str
    alive: bool
    death_round: int | None
    death_cause: str | None


class WerewolfState(TypedDict, total=False):
    # ------------------------------------------------------------- identidad
    session_id: str
    group_id: str
    phase: Phase
    round_no: int

    # -------------------------------------------------------------- jugadores
    players: list[Player]
    #: Pareja de enamorados elegida por Cupido (0 o 2 JID).
    lovers: list[str]

    # ------------------------------------------------------ acciones de noche
    #: Resultado crudo de la noche en curso: objetivo de los lobos, consulta
    #: de la vidente, decisión de la bruja, etc.
    night_actions: dict[str, Any]
    #: Pociones de la bruja que siguen disponibles.
    witch_potions: dict[str, bool]

    # ------------------------------------------------------- resultados/turno
    deaths_last_night: list[str]
    lynched: str | None
    votes: dict[str, str]
    #: Lo que se dijo en voz alta durante el juicio de la ronda en curso, como
    #: ``{"quien": nombre, "dijo": texto}``. Alimenta la narración, que lo
    #: comenta sin darlo por cierto. Se reemplaza en cada ronda.
    debate_log: list[dict[str, str]]

    # --------------------------------------------------------------- narrativa
    #: Todo lo narrado al grupo, en orden. Es una clave acumulativa: los nodos
    #: devuelven **sólo lo nuevo** y LangGraph lo concatena. Queda dentro del
    #: checkpoint, así que sirve para releer una partida ya jugada.
    narrative_log: Annotated[list[str], operator.add]

    # ------------------------------------------------------------------- final
    winner: str | None
    finished: bool
    abort_reason: str | None
    #: A dónde volver tras comprobar la victoria. El nodo ``evaluar`` se
    #: alcanza desde el amanecer y desde el veredicto, y de cada sitio sigue
    #: por un camino distinto.
    resume_to: Literal["debate", "noche"]


def initial_state(session_id: str, group_id: str) -> WerewolfState:
    return WerewolfState(
        session_id=session_id,
        group_id=group_id,
        phase="reclutamiento",
        round_no=0,
        players=[],
        lovers=[],
        night_actions={},
        witch_potions={"vida": True, "muerte": True},
        deaths_last_night=[],
        lynched=None,
        votes={},
        debate_log=[],
        narrative_log=[],
        winner=None,
        finished=False,
        abort_reason=None,
        resume_to="debate",
    )


# --------------------------------------------------------------------- lecturas
def alive(players: list[Player]) -> list[Player]:
    return [p for p in players if p.get("alive", True)]


def by_jid(players: list[Player], jid: str) -> Player | None:
    for player in players:
        if player.get("jid") == jid:
            return player
    return None


def with_role(players: list[Player], role: Role | str, *, only_alive: bool = True) -> list[Player]:
    wanted = str(Role(role))
    pool = alive(players) if only_alive else players
    return [p for p in pool if p.get("role") == wanted]


def wolves(players: list[Player], *, only_alive: bool = True) -> list[Player]:
    return with_role(players, Role.LOBO, only_alive=only_alive)


def label(player: Player) -> str:
    """``"3. Ana"`` — la etiqueta que ven los jugadores."""
    return f"{player.get('number', '?')}. {player.get('name', 'anónimo')}"


def roster_lines(players: list[Player], *, only_alive: bool = True) -> str:
    pool = alive(players) if only_alive else players
    return "\n".join(label(p) for p in sorted(pool, key=lambda p: p.get("number", 0)))


# --------------------------------------------------- versiones con etiqueta
# Los mensajes al grupo etiquetan al contacto en vez de sólo nombrarlo: así se
# ve de quién se habla incluso si dos jugadores tienen nombres parecidos, y
# quién ha quedado fuera. En los privados se siguen usando nombres, que son
# más legibles cuando hay que reconocer a alguien en una lista.
def tag(player: Player, texto: GroupText) -> str:
    """Etiqueta a un jugador, cayendo a su nombre si están desactivadas."""
    return texto.tag(player.get("jid", ""), player.get("name", "anónimo"))


def tagged_label(player: Player, texto: GroupText) -> str:
    """``"3. @573001234567"`` — la etiqueta de lista con mención."""
    return f"{player.get('number', '?')}. {tag(player, texto)}"


def tagged_roster(
    players: list[Player], texto: GroupText, *, only_alive: bool = True
) -> str:
    pool = alive(players) if only_alive else players
    return "\n".join(
        tagged_label(p, texto) for p in sorted(pool, key=lambda p: p.get("number", 0))
    )


def poll_options(players: list[Player]) -> list[str]:
    """Opciones de encuesta, recortadas al límite de WhatsApp (100 caracteres)."""
    return [label(p)[:100] for p in sorted(alive(players), key=lambda p: p.get("number", 0))]


# ------------------------------------------------------------------- escrituras
def kill(
    players: list[Player],
    jid: str,
    *,
    round_no: int,
    cause: str,
) -> tuple[list[Player], Player | None]:
    """Devuelve una copia de ``players`` con ``jid`` muerto.

    No muta la lista original: los nodos del grafo devuelven estado nuevo en
    lugar de modificar el que reciben.
    """
    updated: list[Player] = []
    victim: Player | None = None
    for player in players:
        if player.get("jid") == jid and player.get("alive", True):
            new_player: Player = {**player, "alive": False}
            new_player["death_round"] = round_no
            new_player["death_cause"] = cause
            victim = new_player
            updated.append(new_player)
        else:
            updated.append(player)
    return updated, victim


def role_title(player: Player) -> str:
    """Rol para mostrar. Tolerante: es una función de presentación.

    Un rol desconocido (estado corrupto, checkpoint de una versión anterior)
    no puede reventar el mensaje final y dejar el grupo silenciado.
    """
    raw = player.get("role") or Role.ALDEANO
    try:
        details = info(raw)
    except ValueError:
        return f"❓ {raw}"
    return f"{details.emoji} {details.title}"


def public_summary(players: list[Player], texto: GroupText | None = None) -> str:
    """Revelación final de todos los roles, para cerrar la partida."""
    lines = []
    for player in sorted(players, key=lambda p: p.get("number", 0)):
        estado = "sobrevivió" if player.get("alive", True) else "murió"
        etiqueta = tagged_label(player, texto) if texto is not None else label(player)
        lines.append(f"{etiqueta} — {role_title(player)} ({estado})")
    return "\n".join(lines)
