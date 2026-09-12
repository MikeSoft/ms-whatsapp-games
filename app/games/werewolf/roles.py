"""Catálogo de roles de El Hombre Lobo y reparto según el número de jugadores."""

from __future__ import annotations

import random
from dataclasses import dataclass, replace
from enum import StrEnum

from app.i18n import DEFAULT_LANGUAGE, Language


class Role(StrEnum):
    LOBO = "lobo"
    ALDEANO = "aldeano"
    VIDENTE = "vidente"
    BRUJA = "bruja"
    CAZADOR = "cazador"
    CUPIDO = "cupido"


class Team(StrEnum):
    LOBOS = "lobos"
    PUEBLO = "pueblo"


@dataclass(frozen=True)
class RoleInfo:
    role: Role
    title: str
    #: Plural explícito: "Hombres Lobo", no "Hombre Lobos".
    plural: str
    emoji: str
    team: Team
    #: Descripción que se envía por privado al jugador.
    briefing: str
    #: Si actúa de noche respondiendo por privado.
    acts_at_night: bool = False
    #: Si sólo actúa la primera noche.
    first_night_only: bool = False


#: Los textos de cada rol en el otro idioma. La estructura —equipo, si actúa
#: de noche, emoji— no cambia con el idioma, así que sólo se traduce lo que
#: lee un jugador: el nombre, su plural y el briefing que le llega al privado.
ROLE_TEXTS_EN: dict[Role, tuple[str, str, str]] = {
    Role.LOBO: (
        "Werewolf",
        "Werewolves",
        "Each night I will ask you here who you want to devour. "
        "By day, act like the most innocent villager in town. "
        "You win when there are as many wolves as villagers.",
    ),
    Role.ALDEANO: (
        "Villager",
        "Villagers",
        "You have no powers: only your instinct and your way with words. "
        "By day, argue, accuse and vote. You win when no wolf is left alive.",
    ),
    Role.VIDENTE: (
        "Seer",
        "Seers",
        "Each night you may ask me about one player's identity and I will tell "
        "you whether they are a Werewolf. You are the village's most valuable "
        "role: if they find you out, you are eaten first.",
    ),
    Role.BRUJA: (
        "Witch",
        "Witches",
        "You have two single-use potions for the whole game: one of life, to "
        "revive the wolves' victim, and one of death, to kill whoever you "
        "want. Each night I will tell you who was attacked and you decide "
        "whether to step in.",
    ),
    Role.CAZADOR: (
        "Hunter",
        "Hunters",
        "If you die (at night to the wolves or by day to a lynching), with "
        "your last breath I will ask you who you are taking to the grave with "
        "you. One shot, one victim.",
    ),
    Role.CUPIDO: (
        "Cupid",
        "Cupids",
        "You only act on the first night: you choose two players who fall in "
        "love. If one dies, the other dies of grief at once. You may choose "
        "yourself.",
    ),
}

ROLES: dict[Role, RoleInfo] = {
    Role.LOBO: RoleInfo(
        role=Role.LOBO,
        title="Hombre Lobo",
        plural="Hombres Lobo",
        emoji="🐺",
        team=Team.LOBOS,
        acts_at_night=True,
        briefing=(
            "Cada noche te pediré por aquí a quién quieren devorar. "
            "De día finge ser el aldeano más inocente de la aldea. "
            "Ganan cuando queden tantos lobos como aldeanos."
        ),
    ),
    Role.ALDEANO: RoleInfo(
        role=Role.ALDEANO,
        title="Aldeano",
        plural="Aldeanos",
        emoji="🧑‍🌾",
        team=Team.PUEBLO,
        briefing=(
            "No tienes poderes: sólo tu intuición y tu labia. "
            "De día debate, acusa y vota. Ganas cuando no quede ningún lobo vivo."
        ),
    ),
    Role.VIDENTE: RoleInfo(
        role=Role.VIDENTE,
        title="Vidente",
        plural="Videntes",
        emoji="🔮",
        team=Team.PUEBLO,
        acts_at_night=True,
        briefing=(
            "Cada noche puedes preguntarme por la identidad de un jugador y te diré "
            "si es un Hombre Lobo o no. Eres el rol más valioso del pueblo: "
            "si te descubren, te devoran primero."
        ),
    ),
    Role.BRUJA: RoleInfo(
        role=Role.BRUJA,
        title="Bruja",
        plural="Brujas",
        emoji="🧪",
        team=Team.PUEBLO,
        acts_at_night=True,
        briefing=(
            "Tienes dos pociones de un solo uso en toda la partida: una de vida para "
            "revivir a la víctima de los lobos y una de muerte para asesinar a quien "
            "quieras. Cada noche te diré a quién atacaron y decides si intervienes."
        ),
    ),
    Role.CAZADOR: RoleInfo(
        role=Role.CAZADOR,
        title="Cazador",
        plural="Cazadores",
        emoji="🏹",
        team=Team.PUEBLO,
        briefing=(
            "Si mueres (de noche por los lobos o de día por linchamiento), en tu último "
            "aliento te preguntaré a quién te llevas a la tumba contigo. Un disparo, "
            "una víctima."
        ),
    ),
    Role.CUPIDO: RoleInfo(
        role=Role.CUPIDO,
        title="Cupido",
        plural="Cupidos",
        emoji="🏹💘",
        team=Team.PUEBLO,
        acts_at_night=True,
        first_night_only=True,
        briefing=(
            "Sólo actúas la primera noche: eliges a dos jugadores que se enamoran. "
            "Si uno muere, el otro muere de tristeza al instante. Puedes elegirte a ti."
        ),
    ),
}

#: Orden en que se añaden los roles especiales del pueblo al crecer la partida.
SPECIAL_PRIORITY: tuple[tuple[Role, int], ...] = (
    (Role.VIDENTE, 4),
    (Role.BRUJA, 6),
    (Role.CAZADOR, 8),
    (Role.CUPIDO, 10),
)

#: Mínimo absoluto para que la partida tenga sentido.
ABSOLUTE_MIN_PLAYERS = 3


def info(role: Role | str, language: Language = DEFAULT_LANGUAGE) -> RoleInfo:
    """La ficha del rol, con sus textos en el idioma de la partida."""
    details = ROLES[Role(role)]
    if language == DEFAULT_LANGUAGE:
        return details
    traduccion = ROLE_TEXTS_EN.get(Role(role))
    if traduccion is None:
        return details
    title, plural, briefing = traduccion
    return replace(details, title=title, plural=plural, briefing=briefing)


def is_wolf(role: Role | str) -> bool:
    return Role(role) is Role.LOBO


def team_of(role: Role | str) -> Team:
    return ROLES[Role(role)].team


def wolves_for(total: int) -> int:
    """Número de lobos recomendado para ``total`` jugadores."""
    if total <= 6:
        return 1
    if total <= 11:
        return 2
    if total <= 15:
        return 3
    if total <= 19:
        return 4
    return max(4, total // 5)


def distribute_roles(total: int, *, rng: random.Random | None = None) -> list[Role]:
    """Reparte roles para ``total`` jugadores y devuelve la lista mezclada.

    Garantiza que el pueblo sea siempre mayoría al empezar (si no, los lobos
    ganarían en la ronda uno) y añade roles especiales a medida que crece la
    partida, en el orden de :data:`SPECIAL_PRIORITY`.
    """
    if total < ABSOLUTE_MIN_PLAYERS:
        raise ValueError(f"se necesitan al menos {ABSOLUTE_MIN_PLAYERS} jugadores")

    rng = rng or random.Random()
    wolves = wolves_for(total)
    village_slots = total - wolves

    # Invariante del juego: el pueblo arranca en mayoría estricta.
    while wolves > 1 and wolves >= village_slots:
        wolves -= 1
        village_slots = total - wolves

    specials = [role for role, threshold in SPECIAL_PRIORITY if total >= threshold]
    # Deja siempre al menos un aldeano raso: da margen a la deducción.
    while specials and len(specials) >= village_slots:
        specials.pop()

    roles: list[Role] = [Role.LOBO] * wolves
    roles.extend(specials)
    roles.extend([Role.ALDEANO] * (total - len(roles)))

    if len(roles) != total:  # pragma: no cover - salvaguarda
        raise AssertionError(f"reparto inconsistente: {len(roles)} != {total}")

    rng.shuffle(roles)
    return roles


def roster_summary(roles: list[Role], language: Language = DEFAULT_LANGUAGE) -> str:
    """Resumen público del reparto, sin decir quién tiene qué.

    Se anuncia al grupo para que el pueblo sepa a qué se enfrenta.
    """
    counts: dict[Role, int] = {}
    for role in roles:
        counts[role] = counts.get(role, 0) + 1

    order = [Role.LOBO, Role.VIDENTE, Role.BRUJA, Role.CAZADOR, Role.CUPIDO, Role.ALDEANO]
    parts = []
    for role in order:
        count = counts.get(role, 0)
        if not count:
            continue
        details = info(role, language)
        label = details.title if count == 1 else details.plural
        parts.append(f"{details.emoji} {count} {label}")
    return "\n".join(parts)
