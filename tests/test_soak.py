"""Prueba de resistencia: muchas partidas con jugadores caóticos.

Los tests dirigidos comprueban casos que se me ocurrieron. Esto comprueba los
que no: mesas de tamaño variable donde la gente calla, contesta basura,
rectifica, se abstiene, señala a muertos y sigue escribiendo después de morir.

Tras cada partida se verifican las invariantes que deben cumplirse *siempre*,
gane quien gane:

* la partida cierra (nunca se cuelga) y el grupo queda abierto;
* el ganador anunciado concuerda con quién quedó vivo;
* el estado es coherente: todos tienen rol, los muertos tienen causa;
* ningún rol secreto se filtró al grupo antes de morir su dueño.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field

import pytest

from app.core.inbox import MemoryInbox
from app.games.werewolf.game import WerewolfGame
from app.games.werewolf.nodes import Timers
from app.games.werewolf.roles import ROLES, Role
from app.waha.models import Scope
from tests.conftest import (
    GROUP_ID,
    FakeTransport,
    inbound,
    make_context,
    make_settings,
    render_mentions,
)

#: Roles cuyo nombre no puede aparecer junto al de un jugador vivo.
TITULOS_SECRETOS = {
    ROLES[Role.LOBO].title,
    ROLES[Role.VIDENTE].title,
    ROLES[Role.BRUJA].title,
    ROLES[Role.CAZADOR].title,
    ROLES[Role.CUPIDO].title,
}

BASURA = [
    "",
    "jajaja",
    "no sé",
    "😂",
    "qué?",
    "999",
    "-1",
    "a las 3:30",
    "esto está roto",
    "asdfgh",
]


def soak_timers() -> Timers:
    return Timers(
        recruit=0.05, night=0.15, witch=0.15, hunter=0.15,
        debate=0.01, vote=0.15, filler_interval=0.0,
    )


@dataclass
class JugadoresCaoticos:
    """Mesa que se comporta como gente real un viernes por la noche."""

    inbox: MemoryInbox
    session_id: str
    jids: list[str]
    names: dict[str, str]
    rng: random.Random

    roles: dict[str, str] = field(default_factory=dict)
    #: Quién ha muerto ya, según los anuncios públicos del bot.
    muertos: set[str] = field(default_factory=set)
    numeros: dict[str, int] = field(default_factory=dict)
    inscrito: bool = False

    def _nombre_a_jid(self) -> dict[str, str]:
        return {nombre: jid for jid, nombre in self.names.items()}

    async def _di(self, jid: str, texto: str) -> None:
        await self.inbox.push(
            self.session_id, inbound(jid, texto, name=self.names.get(jid))
        )

    async def _di_al_grupo(self, jid: str, texto: str) -> None:
        await self.inbox.push(
            self.session_id,
            inbound(jid, texto, scope=Scope.GROUP, chat_id=GROUP_ID, name=self.names.get(jid)),
        )

    def _respuesta(self, opciones: list[int]) -> str:
        """Una elección plausible, o cualquier otra cosa."""
        dado = self.rng.random()
        if dado < 0.55 and opciones:
            return str(self.rng.choice(opciones))
        if dado < 0.70:
            return self.rng.choice(BASURA)
        if dado < 0.80 and self.numeros:
            # Señala a alguien que ya está muerto.
            muertos = [self.numeros[j] for j in self.muertos if j in self.numeros]
            return str(self.rng.choice(muertos)) if muertos else "paso"
        if dado < 0.90:
            return "paso"
        return ""  # silencio

    @staticmethod
    def _opciones(text: str) -> list[int]:
        return [int(n) for n in re.findall(r"^(\d{1,2})\.\s", text, re.MULTILINE)]

    async def on_group(self, raw: str) -> None:
        text = render_mentions(raw, self.names)
        if "se abren las inscripciones" in text and not self.inscrito:
            self.inscrito = True
            for jid in self.jids:
                dado = self.rng.random()
                if dado < 0.75:
                    await self._di_al_grupo(
                        jid, self.rng.choice(["Yo", "yo juego", "me apunto", "va"])
                    )
                elif dado < 0.9:
                    await self._di_al_grupo(jid, self.rng.choice(["yo no", "paso", "hola"]))
                # el resto no dice nada
            return

        if "entran a la partida" in text:
            # Aprende la numeración pública de la mesa.
            for numero, nombre in _pares_numerados(text):
                jid = self._nombre_a_jid().get(nombre)
                if jid is not None:
                    self.numeros[jid] = numero
            return

        if "☠️" in text:
            # Alguien murió: se apunta para que los vivos puedan señalarlo mal.
            for nombre, jid in self._nombre_a_jid().items():
                if f"*{nombre}*" in text:
                    self.muertos.add(jid)
            return

        if "*VOTACIÓN*" in text:
            opciones = self._opciones(text)
            for jid in self.jids:
                # Los muertos también insisten: no deben contar.
                veces = self.rng.choice([0, 1, 1, 2])
                for _ in range(veces):
                    await self._di_al_grupo(jid, self._respuesta(opciones))

    async def on_direct(self, jid: str, text: str) -> None:
        rol = re.search(r"Tu rol es \*(.+?)\*", text)
        if rol:
            self.roles[jid] = rol.group(1)
            return

        opciones = self._opciones(text)
        if "¿A quién devoráis?" in text or "identidad" in text:
            for _ in range(self.rng.choice([0, 1, 1, 2])):
                await self._di(jid, self._respuesta(opciones))
        elif "Elige a dos jugadores" in text:
            if len(opciones) >= 2 and self.rng.random() < 0.7:
                par = self.rng.sample(opciones, 2)
                await self._di(jid, f"{par[0]} {par[1]}")
            else:
                await self._di(jid, self.rng.choice(BASURA))
        elif "Pociones que te quedan" in text:
            await self._di(
                jid,
                self.rng.choice(
                    ["curar", "nada", "paso", f"veneno {self.rng.choice(opciones or [1])}",
                     "mmm", ""]
                ),
            )
        elif "Acabas de morir" in text:
            await self._di(
                jid,
                self.rng.choice(
                    ["nadie", str(self.rng.choice(opciones or [1])), "paso", "asdf"]
                ),
            )


def _pares_numerados(text: str) -> list[tuple[int, str]]:
    return [(int(n), nombre.strip()) for n, nombre in re.findall(r"^(\d{1,2})\.\s+(.+)$", text, re.MULTILINE)]


def _comprobar_invariantes(
    result, transport: FakeTransport, max_rounds: int, names: dict[str, str]
) -> None:
    """Lo que tiene que cumplirse en toda partida, gane quien gane."""
    assert result.status in {"finished", "aborted"}, result.status

    # 1. El grupo nunca queda silenciado.
    assert transport.locked is False, "el grupo quedó mudo"

    if result.status == "aborted":
        assert result.players == []
        assert result.winner is None
        return

    jugadores = result.players
    vivos = [p for p in jugadores if p["alive"]]
    muertos = [p for p in jugadores if not p["alive"]]
    lobos_vivos = [p for p in vivos if p["role"] == str(Role.LOBO)]
    aldeanos_vivos = [p for p in vivos if p["role"] != str(Role.LOBO)]

    # 2. Estado coherente.
    validos = {r.value for r in Role}
    for p in jugadores:
        assert p["role"] in validos, p
        assert isinstance(p["number"], int) and p["number"] >= 1
    for p in muertos:
        assert p["death_cause"], f"muerto sin causa: {p}"
        assert p["death_round"] is not None, f"muerto sin ronda: {p}"
    for p in vivos:
        assert p["death_cause"] is None and p["death_round"] is None

    # 3. Numeración única.
    numeros = [p["number"] for p in jugadores]
    assert len(set(numeros)) == len(numeros), "números repetidos"

    # 4. El ganador concuerda con el recuento final.
    if result.winner == "pueblo":
        assert not lobos_vivos, "el pueblo ganó con lobos vivos"
    elif result.winner == "lobos":
        assert len(lobos_vivos) >= len(aldeanos_vivos), "los lobos ganaron sin paridad"
        assert lobos_vivos, "los lobos ganaron sin lobos"
    elif result.winner == "enamorados":
        assert len(vivos) == 2, "los enamorados ganaron sin ser dos"
        assert len(lobos_vivos) == 1, "la pareja tenía que ser de bandos opuestos"
    elif result.winner == "nadie":
        assert not vivos or result.rounds >= max_rounds - 1, (
            f"tablas sin justificación: vivos={len(vivos)} rondas={result.rounds}"
        )
    else:  # pragma: no cover
        raise AssertionError(f"ganador inesperado: {result.winner}")

    # 5. Cada jugador recibió su rol por privado, y sólo el suyo.
    roles_dm = transport.dms_matching("Tu rol es")
    assert len({jid for jid, _ in roles_dm}) == len(jugadores)
    for jid, _ in roles_dm:
        assert sum(1 for j, _ in roles_dm if j == jid) == 1, "doble reparto"

    # 6. El cierre revela a todos, una vez.
    # Los mensajes del grupo etiquetan contactos; se leen como los ve la gente.
    grupo = [render_mentions(m, names) for m in transport.group_messages]
    cierre = grupo[-1]
    assert "Todos los roles" in cierre
    # Sólo el resumen de roles: si la partida terminó en el amanecer, el cierre
    # trae además las muertes de esa noche, que llevan el mismo separador.
    resumen = cierre.split("Todos los roles", 1)[1]
    # Se compara por líneas completas: "1. Jugador1" es subcadena de
    # "11. Jugador13", así que contar apariciones daría falsos positivos.
    etiquetas = [
        linea.split(" — ", 1)[0].strip()
        for linea in resumen.splitlines()
        if " — " in linea
    ]
    assert len(etiquetas) == len(jugadores)
    assert len(set(etiquetas)) == len(jugadores), "algún jugador salió dos veces"
    for p in jugadores:
        assert f"{p['number']}. {p['name']}" in etiquetas

    # 7. Ningún rol secreto se nombró junto a un jugador que siguió vivo.
    durante = grupo[:-1]
    nombres_vivos = {p["name"] for p in vivos}
    for mensaje in durante:
        for linea in mensaje.splitlines():
            if not any(titulo in linea for titulo in TITULOS_SECRETOS):
                continue
            # Con límites de palabra: "Jugador1" no puede darse por encontrado
            # dentro de "Jugador10", que es otro jugador distinto.
            filtrados = [
                n for n in nombres_vivos if re.search(rf"\b{re.escape(n)}\b", linea)
            ]
            assert not filtrados, f"rol filtrado de un vivo: {linea!r}"


@pytest.mark.parametrize("seed", list(range(24)))
async def test_partida_caotica_mantiene_las_invariantes(seed):
    """24 semillas: mesas de 4 a 14 con gente que se porta mal."""
    rng = random.Random(seed)
    count = rng.randint(4, 14)
    settings = make_settings(werewolf_min_players=4, max_rounds=12)

    jids = [f"5730022222{i:02d}@c.us" for i in range(1, count + 1)]
    names = {jid: f"Jugador{i}" for i, jid in enumerate(jids, start=1)}
    inbox = MemoryInbox()
    transport = FakeTransport()
    ctx = make_context(
        settings=settings, transport=transport, inbox=inbox, session_id=f"soak-{seed}"
    )
    script = JugadoresCaoticos(
        inbox=inbox, session_id=ctx.session_id, jids=jids, names=names, rng=rng
    )
    transport.on_group = script.on_group
    transport.on_direct = script.on_direct

    game = WerewolfGame(ctx, timers=soak_timers(), rng=random.Random(seed + 500))
    result = await game.run()

    _comprobar_invariantes(result, transport, settings.max_rounds, names)

    if result.status == "finished":
        # El historial del estado coincide con lo enviado al grupo.
        assert game.last_state["narrative_log"] == transport.group_messages
        # Y quien nunca dijo "sí" no acabó jugando.
        jugando = {p["name"] for p in result.players}
        assert jugando <= set(names.values())


@pytest.mark.parametrize("seed", [7, 21, 42])
async def test_una_mesa_totalmente_muda_termina_igual(seed):
    """Nadie contesta nada en toda la partida: se decide por linchamientos vacíos."""
    settings = make_settings(werewolf_min_players=4, max_rounds=6)

    jids = [f"5730033333{i:02d}@c.us" for i in range(1, 7)]
    names = {jid: f"Muda{i}" for i, jid in enumerate(jids, start=1)}
    inbox = MemoryInbox()
    transport = FakeTransport()
    ctx = make_context(
        settings=settings, transport=transport, inbox=inbox, session_id=f"muda-{seed}"
    )

    async def solo_se_inscriben(text: str) -> None:
        if "se abren las inscripciones" in text:
            for jid in jids:
                await inbox.push(
                    ctx.session_id,
                    inbound(jid, "Yo", scope=Scope.GROUP, chat_id=GROUP_ID, name=names[jid]),
                )

    transport.on_group = solo_se_inscriben

    game = WerewolfGame(ctx, timers=soak_timers(), rng=random.Random(seed))
    result = await game.run()

    assert result.status == "finished"
    # Sin ataques ni votos, la partida agota las rondas y queda en tablas.
    assert result.winner == "nadie"
    assert result.rounds >= settings.max_rounds - 1
    assert transport.locked is False
    _comprobar_invariantes(result, transport, settings.max_rounds, names)
