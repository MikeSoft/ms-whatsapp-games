# Kahoot

Concurso de preguntas contrarreloj. El máster pide un tema en lenguaje
corriente, el modelo escribe el cuestionario y se publica de una en una como
encuesta de WhatsApp.

- **Comando**: `#juego kahoot <instrucción>` · alias `trivia`, `preguntas`,
  `quiz`, `concurso`, `cultura general`
- **No necesita el sufijo `ia`**: sin modelo el juego no es lo que promete, así
  que lo declara en su ficha y lo recibe siempre
- **Jugadores**: cualquiera del grupo que toque la encuesta

---

## Cómo se ve una partida

```
Máster  ›  #juego kahoot 10 preguntas de historia de Colombia

Bot     ›  🧠 CONCURSO DE PREGUNTAS
           Tema: historia de Colombia
           10 preguntas · 5 opciones · 10 segundos cada una
           🔇 (el grupo queda en silencio)

Bot     ›  [encuesta] 1/10 · ¿En qué año se proclamó la independencia?
           … 10 segundos … la encuesta desaparece

Bot     ›  📖 RESPUESTAS CORRECTAS
           1. ¿En qué año se proclamó la independencia?
              ✅ 1810
           …

Bot     ›  🏆 CLASIFICACIÓN
           🥇 @Ana — 8/10
           🥈 @Beto — 6/10
           👑 Gana @Ana.
```

---

## Lo que se puede pedir en el comando

Todo es opcional y va en lenguaje corriente; lo que no se diga usa el valor
por defecto. Las cifras se sacan con expresiones regulares y lo que sobra es
el tema: **la mecánica no la decide el modelo, sólo el contenido**.

| Se escribe | Qué cambia | Por defecto |
|---|---|---|
| `de historia de Roma`, `sobre cine` | El tema | cultura general |
| `15 preguntas` | Cuántas | `KAHOOT_QUESTIONS` (10) |
| `10 segundos` | Cuánto dura cada una | `KAHOOT_SECONDS_PER_QUESTION` (10) |
| `4 opciones` / `4 respuestas` | Alternativas por pregunta | `KAHOOT_OPTIONS` (5) |
| `difíciles`, `duras` | Sube la exigencia | calibración normal |
| `fáciles`, `sencillas` | La baja | |

El parseo aguanta cómo escribe la gente. De

```
has 5 preguntas relacionadas a colombia que duren 8 segundos y de a 3 respuestas
```

sale `tema='colombia', 5 preguntas, 8 s, 3 opciones`: las expresiones se
tragan el andamiaje de la frase, porque de `que duren 8 segundos` sobraría un
`que duren` que no es ningún tema.

---

## Cómo se juega una pregunta

1. Se **vacía el buzón del grupo**, para que lo escrito entre una pregunta y
   otra no cuente como respuesta de la que viene.
2. Se publica la encuesta.
3. Se recogen los votos durante la ventana.
4. Se **retira la encuesta**, para que no quede votable cuando ya no cuenta.

Sobre los votos:

- **Sólo cuenta el último de cada persona**, porque WhatsApp deja cambiar la
  respuesta mientras la encuesta está abierta.
- **Marcar varias opciones no es acertar.**
- Cuando WAHA informa del identificador de la encuesta votada, se exige que
  coincida con la abierta, de modo que un voto rezagado no se cuele.
- **Un voto de encuesta no trae nombre**, y en los grupos nuevos el votante
  llega como `@lid`. El nombre se resuelve contra los contactos de WAHA antes
  de publicar nada; publicar el identificador en crudo deja una clasificación
  en la que no se reconoce nadie.

### El desempate

A igualdad de aciertos gana **quien los consiguió antes**, medido por el orden
de llegada de los votos al buzón y no por reloj: el sello de tiempo de un voto
lo pone el teléfono que vota, y no hay forma de fiarse de que todos vayan en
hora.

Sólo cuenta la rapidez en lo que se acierta. Si contaran los fallos, quien
vota lo primero que ve le ganaría el desempate a quien se lo piensa y acierta
igual.

---

## De dónde salen las preguntas

Una llamada al modelo antes de empezar devuelve el cuestionario completo en un
JSON. Se le piden **tres preguntas de más** que las pedidas, porque la
validación descarta y sin margen cada descarte dejaría la tanda corta.

### Lo que se le exige al modelo

El prompt vive en `app/games/kahoot/questions.py`. Lo que más importa:

- **Distractores reconocibles, no relleno.** Del mismo tipo y magnitud que la
  correcta, y candidatos que alguien llegue a considerar: si se pregunta la
  capital de Italia, las otras opciones son ciudades italianas conocidas, no
  pueblos que nadie ubica. Acertar tiene que exigir saber el dato, no
  descartar lo ridículo.
- **Mismo formato en todas.** Longitud parecida y misma precisión, para que la
  correcta no destaque por ser la más larga o la más matizada.
- **Se justifica antes de responder.** El JSON pide el motivo *antes* del
  índice de la correcta, así que tiene que comprometerse con una razón antes
  de elegir. No se publica; queda en la traza para revisar una pregunta que
  alguien discuta.
- **Dificultad con una prueba aplicable**: ¿se acierta sin haber visto, leído
  o estudiado el tema? Si sí, no sirve. Los ejemplos negativos no funcionan
  —nombrar «no preguntes en qué ciudad viven los Simpson» se lo sugiere—, así
  que la regla está en positivo.

### Lo que valida el código

El modelo propone; el código decide qué se publica. Una encuesta rota no se
arregla una vez enviada, así que se descarta sin piedad:

| Se descarta | Por qué |
|---|---|
| Opciones repetidas | WhatsApp no admite dos iguales en la misma encuesta |
| Número de opciones distinto al pedido | |
| Sin correcta, o con el índice fuera de rango | |
| El enunciado contiene su propia respuesta | Salió de verdad: *"¿cómo se llama el dueño de la taberna donde trabaja Moe Szyslak?"* → *Moe Szyslak* |
| Dos preguntas que son la misma con otras palabras | *"¿en qué año fue la batalla de Boyacá?"* y *"¿qué batalla de 1819 fue decisiva?"* |
| Dos preguntas con la misma respuesta | Acertar dos veces lo mismo aburre |

Y **las opciones se barajan siempre**, vengan del modelo o del banco de
respaldo: sin barajar se gana mirando la posición en vez de sabiendo la
respuesta.

### Sin modelo se juega igual

Si no hay LLM disponible se tira de un banco estático de cultura general y se
avisa en el grupo de que el tema pedido no se respetó. Es la regla de la casa:
nada depende del modelo para funcionar.

---

## Aritmética: el único tema que se verifica

Una pregunta de cálculo es la única cuyo resultado se puede comprobar sin
preguntarle a nadie, así que se comprueba.

El prompt pide **notación y no palabras** —`6+6*6+6/1`, no «seis más seis por
seis»—, que lo evaluado sea el **orden de las operaciones**, y que las falsas
sean los resultados de aplicar mal la regla:

```
5+3*2^2     17 | 41 | 11 | 32 | 121     -> 17
```

El 32 es lo que sale resolviendo de izquierda a derecha: ahí está el error que
la pregunta pretende detectar, en vez de rellenar con cifras al azar.

Y después **el código evalúa la expresión**. Si la opción marcada no es su
resultado, la pregunta se descarta. La evaluación recorre el árbol de sintaxis
aceptando sólo números y operadores aritméticos — **nunca `eval`**, porque ese
texto viene de un modelo de lenguaje que a su vez repite lo que escribe la
gente. Se rechazan nombres, llamadas, atributos y exponentes desmedidos, que
colgarían el proceso calculando un número que no cabe en memoria.

Las cuentas necesitan además su propia regla en los filtros de texto, porque
allí los criterios habituales no significan nada: en `10-4*2+8/4` el resultado
es 4 y el 4 está en la expresión, y `8+2*5-4` con `(8+2)*5-4` son justamente la
pareja que uno quiere preguntar.

---

## El modelo del concurso

Se configura aparte del de la narración, porque lo que se le pide no se
parece: un cuestionario exigente se genera **antes** de empezar y admite
esperar; una escena narrada a mitad de partida, no.

| Variable | Para qué |
|---|---|
| `KAHOOT_LLM_MODEL` | Modelo. Vacío usa `LLM_MODEL` |
| `KAHOOT_LLM_BASE_URL` | Otro proveedor entero. Vacío usa `LLM_BASE_URL` |
| `KAHOOT_LLM_API_KEY` | Su clave. Vacío usa `LLM_API_KEY` |
| `KAHOOT_LLM_REASONING_EFFORT` | `none`/`low`/`medium`/`high`, en modelos que razonan |

**Acotar el razonamiento importa mucho.** Medido con `gemini-3.8-flash` sobre
la misma tanda: sin límite tarda 20-25 s y gasta unos 5.000 tokens pensando;
con `low` tarda 5 s, no gasta ninguno y las preguntas salen igual de buenas.
Y sin techo suficiente el JSON llega truncado, que no se parsea: se pierde la
tanda entera, no la última pregunta.

---

## Ajustes

| Variable | Por defecto | Para qué |
|---|---|---|
| `KAHOOT_QUESTIONS` | `10` | Cuántas preguntas |
| `KAHOOT_SECONDS_PER_QUESTION` | `10` | Ventana de cada una |
| `KAHOOT_OPTIONS` | `5` | Alternativas por pregunta |
| `KAHOOT_MAX_QUESTIONS` | `30` | Tope de lo que puede pedir el comando |
| `KAHOOT_MAX_SECONDS` | `120` | Tope de la ventana |
| `KAHOOT_LOCK_GROUP` | `true` | Silenciar el grupo mientras se juega |
| `KAHOOT_HARDEN` | `true` | Segunda pasada de revisión (otra llamada al modelo) |

---

## Límites conocidos

- **La calidad de las preguntas es la del modelo.** Se valida la *forma* y, en
  aritmética, el *resultado*; los hechos no. Una pregunta puede salir ambigua
  o con más de una respuesta defendible.
- **Volver a una opción ya elegida no se registra.** Cambiar de respuesta
  funciona, pero si eliges A, cambias a B y vuelves a A, esa vuelta se
  descarta: WhatsApp reutiliza el identificador del voto y esa tercera pulsación
  es indistinguible de un reenvío del webhook.
- **Se puede votar con el grupo silenciado**, comprobado en partida real. Aun
  así, si al cerrarse la primera pregunta no ha votado nadie, el juego reabre
  el grupo solo y lo avisa.
- **La segunda pasada de revisión es inconsistente.** De media sube el nivel,
  pero en las pruebas ayudó en una tanda y lo empeoró en otra, y al empujar
  hacia lo obscuro aumenta el riesgo de que invente detalles.

---

Para el código: [`docs/architecture.md`](architecture.md).
