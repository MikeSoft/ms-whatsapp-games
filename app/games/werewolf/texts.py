"""Todo lo que El Hombre Lobo le escribe a la gente, en los dos idiomas.

La mecánica no vive aquí: estas son las frases con las que se cuenta, y por eso
están juntas y fuera de los nodos. Los huecos ``{}`` los rellena el código, así
que una traducción tiene que conservarlos todos.

Las escenas narradas no están aquí sino en :mod:`prompts`, que es donde vive lo
que se le pide al modelo y los textos de respaldo cuando no hay modelo.
"""

from __future__ import annotations

from app.i18n import Catalogue

#: Cómo se cuenta cada muerte. Varias formas por causa: el parte de bajas se
#: repite cada ronda y con una sola frase por causa la partida acaba sonando a
#: formulario. El hecho —quién murió y de qué— lo decide el código; lo que
#: cambia es cómo se dice.
CAUSES: dict[str, dict[str, tuple[str, ...]]] = {
    "es": {
        "lobos": (
            "fue devorado por los lobos",
            "amaneció con la puerta arrancada y el zarpazo todavía fresco",
            "no llegó al amanecer: los lobos se lo llevaron monte adentro",
        ),
        "veneno": (
            "apareció envenenado",
            "se quedó frío en su cama, sin una sola herida encima",
            "amaneció con un frasco vacío entre los dedos",
        ),
        "amor": (
            "murió de tristeza al perder a su amor",
            "no quiso quedarse cuando se llevaron a su amor",
            "siguió a su amor antes de que cantara el gallo",
        ),
        "cazador": (
            "cayó por el último disparo del cazador",
            "se llevó el último disparo del cazador",
            "cayó con el eco de ese disparo todavía en la plaza",
        ),
        "linchamiento": (
            "fue linchado por la aldea",
            "acabó con la soga de la aldea al cuello",
            "no alcanzó a terminar su defensa: la cuerda ya estaba lista",
        ),
        "": ("murió",),
    },
    "en": {
        "lobos": (
            "was devoured by the wolves",
            "was found with the door torn off and the claw marks still fresh",
            "never made it to dawn: the wolves dragged them into the woods",
        ),
        "veneno": (
            "turned up poisoned",
            "went cold in bed, without a single wound on them",
            "was found with an empty vial between their fingers",
        ),
        "amor": (
            "died of grief at losing their love",
            "would not stay once their love was taken",
            "followed their love before the cockerel crowed",
        ),
        "cazador": (
            "fell to the hunter's last shot",
            "caught the hunter's last shot",
            "fell with the echo of that shot still in the square",
        ),
        "linchamiento": (
            "was lynched by the village",
            "ended with the village's rope around their neck",
            "never finished their defence: the rope was already waiting",
        ),
        "": ("died",),
    },
}


TEXTS: Catalogue = {
    "es": {
        # ------------------------------------------------------- tiempos
        "time.seconds": "{total} segundos",
        "time.minute": "{minutes} minuto",
        "time.minutes": "{minutes} minutos",
        "time.min_sec": "{minutes} min {rest} s",
        "wait.remaining": "⏳ Quedan ~{time}.",
        # -------------------------------------------------- reclutamiento
        "recruit.open": (
            "🐺 *EL HOMBRE LOBO* — se abren las inscripciones.\n\n"
            "Escribe *YO* en los próximos {time} para entrar a la partida.\n"
            "Hacen falta al menos {minimum} jugadores (máximo {maximum})."
        ),
        "recruit.reminder": (
            "⏳ Quedan ~{time} para cerrar inscripciones. Todavía puedes escribir *YO*."
        ),
        "recruit.not_enough": (
            "🌫️ Inscripciones cerradas. Sólo se apuntó: {names}.\n\n"
            "Hacen falta {minimum} jugadores para empezar. "
            "Vuelve a lanzar el juego cuando haya más gente."
        ),
        "recruit.nobody": "nadie",
        # ---------------------------------------------------------- reparto
        "deal.announce": (
            "🎭 *{count} jugadores* entran a la partida:\n{roster}\n\n"
            "El reparto de esta noche:\n{summary}\n\n"
            "🔇 El grupo queda en silencio. Revisa tu chat privado: te acabo de "
            "enviar tu rol secreto."
        ),
        "deal.dm_role": (
            "{emoji} Tu rol es *{title}*.\n\n{briefing}\n\n"
            "Jugadores de la partida:\n{roster}"
        ),
        "deal.pack": "\n\n🐺 Tu manada: *{names}*. Podéis confiar entre vosotros.",
        "deal.lone_wolf": "\n\n🐺 Eres el único lobo. Nadie te cubrirá: disimula bien.",
        # ------------------------------------------------------------ noche
        "night.open": (
            "🌙 *NOCHE {round_no}*\n\n{flavour}\n\n"
            "El grupo está en silencio. Los roles con poder tienen {time} para "
            "responderme por privado."
        ),
        "night.wolf_prompt": (
            "🐺 *Noche {round_no}.* ¿A quién devoráis?\n\n{targets}\n\n"
            "Responde con el número o el nombre. {note}"
        ),
        "night.wolf_pack_note": (
            "Tu manada ({names}) también está decidiendo: gana la opción más votada."
        ),
        "night.wolf_alone_note": "Decides tú solo.",
        "night.wolves_decided": "🐺 La manada ha decidido: *{name}*.",
        "night.wolves_tie": "🐺 Había empate; el instinto eligió a *{name}*.",
        "night.seer_prompt": (
            "🔮 *Noche {round_no}.* ¿De quién quieres conocer la identidad?\n\n"
            "{targets}\n\nResponde con el número o el nombre."
        ),
        "night.seer_timeout": "🔮 Se te agotó el tiempo. Esta noche no ves nada.",
        "night.seer_unclear": "🔮 No entendí a quién te referías, y la visión se apagó.",
        "night.seer_is_wolf": "*ES UN HOMBRE LOBO*. 🐺",
        "night.seer_not_wolf": "*no es un Hombre Lobo*.",
        "night.seer_result": (
            "🔮 Las cartas hablan sobre *{name}*: {verdict}\n\n"
            "Guárdate la información o úsala de día: tú decides."
        ),
        "night.cupid_prompt": (
            "🏹💘 *Primera noche.* Elige a dos jugadores que se enamoran.\n\n"
            "{targets}\n\n"
            "Responde con los dos números separados por un espacio (por ejemplo: 2 5). "
            "Si uno muere, el otro muere de tristeza."
        ),
        "night.cupid_unclear": (
            "🏹 No entendí los dos nombres, así que la flecha se perdió en la niebla. "
            "Esta partida no habrá enamorados."
        ),
        "night.cupid_done": "🏹💘 Has enamorado a *{first}* y *{second}*.",
        "night.lovers_note": (
            "💘 Cupido te ha unido a *{name}*. Si uno de los dos muere, el otro muere "
            "de tristeza. Protegeos."
        ),
        # ------------------------------------------------------------ bruja
        "witch.attacked": "🧪 Esta noche los lobos atacaron a *{name}*.",
        "witch.quiet": "🧪 Esta noche los lobos no atacaron a nadie.",
        "witch.potion_life": "*vida* (revive a la víctima de esta noche)",
        "witch.potion_death": "*muerte* (asesina a quien elijas)",
        "witch.prompt": (
            "{header}\n\nPociones que te quedan: {potions}.\n\n{targets}\n\n"
            "Responde *curar*, *veneno <número>*, o *nada*. Tienes {time}."
        ),
        "witch.timeout": "🧪 Se acabó el tiempo. No usaste ninguna poción.",
        "witch.healed": "🧪 Has usado la poción de vida en *{name}*. Ya no te queda.",
        "witch.cannot_heal": "🧪 No pudiste curar: {reason}.",
        "witch.reason_used": "ya la usaste",
        "witch.reason_nobody": "no hay a quién revivir",
        "witch.poison_unclear": (
            "🧪 No entendí a quién querías envenenar, así que guardas la poción."
        ),
        "witch.poisoned": "🧪 Has envenenado a *{name}*. Ya no te queda.",
        "witch.death_used": "🧪 Ya usaste la poción de muerte.",
        "witch.abstain": "🧪 Decides no intervenir esta noche.",
        # ---------------------------------------------------------- cazador
        "hunter.prompt": (
            "🏹 *Acabas de morir.* En tu último aliento puedes disparar una vez.\n\n"
            "{targets}\n\n"
            "Responde con el número, o *nadie* si prefieres irte en paz. Tienes {time}."
        ),
        "hunter.lowered": "🏹 Bajas el arma. No te llevas a nadie.",
        "hunter.missed": "🏹 El disparo se pierde en la niebla.",
        "hunter.takes": "🏹 Te llevas a *{name}* contigo.",
        # ------------------------------------------------------------- día
        "day.header": "🌅 *AMANECE EL DÍA {round_no}*",
        "day.nobody_died": "🕊️ Esta noche no murió nadie.",
        "day.death_line": "☠️ {tag} {cause} — era {role}",
        "day.death_line_plain": "☠️ {tag} {cause}",
        "day.ignore_the_fallen": "Quien haya caído ya no participa: ignorad lo que escriba.",
        "day.trial": (
            "⚖️ *EL JUICIO — día {round_no}*\n\n"
            "Tenéis {time} para acusaros. Al terminar abriré la votación.\n\n"
            "Sospechosos ({count}):\n{roster}\n\n"
            "🔊 El chat está abierto."
        ),
        # -------------------------------------------------------- votación
        "vote.poll_question": "¿A quién linchamos? (día {round_no})",
        "vote.use_poll": "Vota en la encuesta de arriba",
        "vote.use_text": "Escribe en el grupo el número del acusado",
        "vote.tally_one": "• {name}: {count} voto",
        "vote.tally_many": "• {name}: {count} votos",
        "vote.tally_empty": "• nadie recibió votos",
        "vote.body": (
            "🗳️ *VOTACIÓN* — {time}.\n\n"
            "{instruction}. También vale escribir el número o el nombre aquí.\n"
            "Escribe *paso* para abstenerte. Sólo cuentan los votos de los vivos.\n\n"
            "{roster}"
        ),
        # -------------------------------------------------------- veredicto
        "verdict.no_lynch": (
            "⚖️ *VEREDICTO*\n\n{tally}\n\n{flavour}\n\n🤷 Hoy no se lincha a nadie."
        ),
        "verdict.header": "⚖️ *VEREDICTO — día {round_no}*",
        "verdict.still_alive": "Siguen vivos ({count}):",
        # ------------------------------------------------------------ final
        "final.wolves_win": "🐺 *GANAN LOS HOMBRES LOBO*",
        "final.village_win": "🎉 *GANA EL PUEBLO*",
        "final.lovers_win": "💞 *GANAN LOS ENAMORADOS*",
        "final.draw": "🌫️ *LA PARTIDA QUEDA EN TABLAS*",
        "final.all_roles": "🎭 *Todos los roles:*\n{summary}",
        "final.rounds": "Rondas jugadas: {rounds}.",
        "final.thanks": "Gracias por jugar. 🐺",
        "final.survived": "sobrevivió",
        "final.died": "murió",
    },
    "en": {
        # ------------------------------------------------------------ time
        "time.seconds": "{total} seconds",
        "time.minute": "{minutes} minute",
        "time.minutes": "{minutes} minutes",
        "time.min_sec": "{minutes} min {rest} s",
        "wait.remaining": "⏳ About {time} left.",
        # --------------------------------------------------------- sign-ups
        "recruit.open": (
            "🐺 *WEREWOLF* — sign-ups are open.\n\n"
            "Type *ME* in the next {time} to join the game.\n"
            "At least {minimum} players are needed (maximum {maximum})."
        ),
        "recruit.reminder": (
            "⏳ About {time} left to sign up. You can still type *ME*."
        ),
        "recruit.not_enough": (
            "🌫️ Sign-ups closed. Only these joined: {names}.\n\n"
            "{minimum} players are needed to start. "
            "Launch the game again when more people are around."
        ),
        "recruit.nobody": "nobody",
        # ------------------------------------------------------------ deal
        "deal.announce": (
            "🎭 *{count} players* are in:\n{roster}\n\n"
            "Tonight's line-up:\n{summary}\n\n"
            "🔇 The group goes quiet. Check your private chat: I have just sent "
            "you your secret role."
        ),
        "deal.dm_role": (
            "{emoji} Your role is *{title}*.\n\n{briefing}\n\n"
            "Players in this game:\n{roster}"
        ),
        "deal.pack": "\n\n🐺 Your pack: *{names}*. You can trust each other.",
        "deal.lone_wolf": "\n\n🐺 You are the only wolf. Nobody will cover for you: blend in.",
        # ----------------------------------------------------------- night
        "night.open": (
            "🌙 *NIGHT {round_no}*\n\n{flavour}\n\n"
            "The group is quiet. The roles with powers have {time} to answer me "
            "in private."
        ),
        "night.wolf_prompt": (
            "🐺 *Night {round_no}.* Who do you devour?\n\n{targets}\n\n"
            "Answer with the number or the name. {note}"
        ),
        "night.wolf_pack_note": (
            "Your pack ({names}) is deciding too: the most voted option wins."
        ),
        "night.wolf_alone_note": "It is your call alone.",
        "night.wolves_decided": "🐺 The pack has decided: *{name}*.",
        "night.wolves_tie": "🐺 It was a tie; instinct chose *{name}*.",
        "night.seer_prompt": (
            "🔮 *Night {round_no}.* Whose identity do you want to know?\n\n"
            "{targets}\n\nAnswer with the number or the name."
        ),
        "night.seer_timeout": "🔮 You ran out of time. You see nothing tonight.",
        "night.seer_unclear": "🔮 I could not tell who you meant, and the vision faded.",
        "night.seer_is_wolf": "*IS A WEREWOLF*. 🐺",
        "night.seer_not_wolf": "*is not a Werewolf*.",
        "night.seer_result": (
            "🔮 The cards speak about *{name}*: {verdict}\n\n"
            "Keep it to yourself or use it by day: your call."
        ),
        "night.cupid_prompt": (
            "🏹💘 *First night.* Choose two players who fall in love.\n\n"
            "{targets}\n\n"
            "Answer with both numbers separated by a space (for example: 2 5). "
            "If one dies, the other dies of grief."
        ),
        "night.cupid_unclear": (
            "🏹 I could not make out both names, so the arrow was lost in the fog. "
            "There will be no lovers this game."
        ),
        "night.cupid_done": "🏹💘 You have made *{first}* and *{second}* fall in love.",
        "night.lovers_note": (
            "💘 Cupid has bound you to *{name}*. If one of you dies, the other dies "
            "of grief. Look after each other."
        ),
        # ------------------------------------------------------------ witch
        "witch.attacked": "🧪 Tonight the wolves attacked *{name}*.",
        "witch.quiet": "🧪 Tonight the wolves attacked nobody.",
        "witch.potion_life": "*life* (revives tonight's victim)",
        "witch.potion_death": "*death* (kills whoever you choose)",
        "witch.prompt": (
            "{header}\n\nPotions you have left: {potions}.\n\n{targets}\n\n"
            "Answer *heal*, *poison <number>*, or *nothing*. You have {time}."
        ),
        "witch.timeout": "🧪 Time is up. You used no potion.",
        "witch.healed": "🧪 You used the potion of life on *{name}*. It is gone now.",
        "witch.cannot_heal": "🧪 You could not heal: {reason}.",
        "witch.reason_used": "you already used it",
        "witch.reason_nobody": "there is nobody to revive",
        "witch.poison_unclear": (
            "🧪 I could not tell who you wanted to poison, so you keep the potion."
        ),
        "witch.poisoned": "🧪 You poisoned *{name}*. It is gone now.",
        "witch.death_used": "🧪 You already used the potion of death.",
        "witch.abstain": "🧪 You choose not to step in tonight.",
        # ----------------------------------------------------------- hunter
        "hunter.prompt": (
            "🏹 *You have just died.* With your last breath you may fire once.\n\n"
            "{targets}\n\n"
            "Answer with the number, or *nobody* if you would rather go in peace. "
            "You have {time}."
        ),
        "hunter.lowered": "🏹 You lower the weapon. You take nobody with you.",
        "hunter.missed": "🏹 The shot is lost in the fog.",
        "hunter.takes": "🏹 You take *{name}* with you.",
        # -------------------------------------------------------------- day
        "day.header": "🌅 *DAY {round_no} BREAKS*",
        "day.nobody_died": "🕊️ Nobody died tonight.",
        "day.death_line": "☠️ {tag} {cause} — they were {role}",
        "day.death_line_plain": "☠️ {tag} {cause}",
        "day.ignore_the_fallen": (
            "Whoever fell is out of the game: ignore whatever they write."
        ),
        "day.trial": (
            "⚖️ *THE TRIAL — day {round_no}*\n\n"
            "You have {time} to accuse each other. Then I will open the vote.\n\n"
            "Suspects ({count}):\n{roster}\n\n"
            "🔊 The chat is open."
        ),
        # ------------------------------------------------------------- vote
        "vote.poll_question": "Who do we lynch? (day {round_no})",
        "vote.use_poll": "Vote in the poll above",
        "vote.use_text": "Write the number of the accused in the group",
        "vote.tally_one": "• {name}: {count} vote",
        "vote.tally_many": "• {name}: {count} votes",
        "vote.tally_empty": "• nobody received a vote",
        "vote.body": (
            "🗳️ *VOTE* — {time}.\n\n"
            "{instruction}. Writing the number or the name here works too.\n"
            "Type *pass* to abstain. Only the living have a vote.\n\n"
            "{roster}"
        ),
        # ---------------------------------------------------------- verdict
        "verdict.no_lynch": (
            "⚖️ *VERDICT*\n\n{tally}\n\n{flavour}\n\n🤷 Nobody is lynched today."
        ),
        "verdict.header": "⚖️ *VERDICT — day {round_no}*",
        "verdict.still_alive": "Still alive ({count}):",
        # ------------------------------------------------------------- end
        "final.wolves_win": "🐺 *THE WEREWOLVES WIN*",
        "final.village_win": "🎉 *THE VILLAGE WINS*",
        "final.lovers_win": "💞 *THE LOVERS WIN*",
        "final.draw": "🌫️ *THE GAME ENDS IN A DRAW*",
        "final.all_roles": "🎭 *Every role:*\n{summary}",
        "final.rounds": "Rounds played: {rounds}.",
        "final.thanks": "Thanks for playing. 🐺",
        "final.survived": "survived",
        "final.died": "died",
    },
}
