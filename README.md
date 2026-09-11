# ms-whatsapp-games

Microservicio en FastAPI que hace de **máster de juegos de texto por WhatsApp**.
Recibe los eventos de una instancia de [WAHA](https://waha.devlike.pro/), dirige
la partida en el grupo y habla con cada jugador por privado.

Trae dos juegos:

| Juego | Comando | Qué es |
|---|---|---|
| [🐺 El Hombre Lobo](docs/hombreslobo.md) | `#juego hombreslobo` | Los lobos devoran de noche; la aldea lincha de día. Roles por privado, grupo silenciado de noche, votación de día |
| [🧠 Kahoot](docs/kahoot.md) | `#juego kahoot <tema>` | Concurso de preguntas contrarreloj. El cuestionario lo escribe el modelo y se juega por encuestas |

> **Alcance.** Un solo número de bot: el de la sesión de WAHA que se configure.
> Ese número sí atiende a **varios grupos a la vez** —cada partida es
> independiente y se juega donde se pide— y obedece a **varios másteres**.

---

## Puesta en marcha

```bash
git clone git@github.com:MikeSoft/ms-whatsapp-games.git && cd ms-whatsapp-games
cp .env.example .env
```

Edita `.env` y pon como mínimo quién puede dar órdenes:

```env
MANAGER_NUMBER=+573001234567     # admite varios, separados por comas
LLM_API_KEY=sk-...               # sin clave: narrativa estática y el
                                 # concurso tira de su banco de preguntas
```

Levanta el stack —el microservicio, Redis para los buzones efímeros y WAHA como
pasarela— y vincula el número del bot:

```bash
docker compose --profile waha up -d --build
docker compose logs -f api
```

Sin `--profile waha` se levantan sólo el microservicio y Redis, que es lo que
quieres si ya tienes una pasarela: sigue leyendo.

1. Abre `http://localhost:3000` y arranca la sesión de WAHA.
2. Escanea el QR con el teléfono que hará de bot.
3. Añade ese número al grupo donde vais a jugar. Hazlo **administrador** si
   quieres que pueda silenciar el grupo; si no puede, las partidas se juegan
   igual sin silencio.

Comprueba que responde:

```bash
curl localhost:8000/health/ready
curl localhost:8000/games
```

Y desde el WhatsApp de un máster, en el grupo:

```
#juegos
#juego hombreslobo
```

### Si tu WAHA ya existe

No levantes la del stack: `docker compose up -d --build` deja fuera el perfil
`waha`. Apunta la aplicación a la tuya y su webhook a la aplicación:

```env
WAHA_BASE_URL=http://192.168.0.250:3000
WAHA_SESSION=nombre-de-tu-sesion
WAHA_WEBHOOK_HMAC_SECRET=<openssl rand -base64 32>
```

El webhook se configura **por sesión**. Las variables de entorno del contenedor
de WAHA sólo valen para la que trae este stack; en una que ya existe hay que
registrarlo con un `PUT`, que reemplaza la configuración entera —incluye lo que
la sesión ya tuviera o lo perderás:

```bash
curl -X PUT http://192.168.0.250:3000/api/sessions/<sesion> \
  -H 'Content-Type: application/json' \
  -d '{"config": {"webhooks": [{
        "url": "http://<esta-máquina>:8000/webhooks/waha",
        "events": ["message", "poll.vote"],
        "hmac": {"key": "<el mismo secreto del .env>"},
        "retries": {"delaySeconds": 2, "attempts": 3, "policy": "linear"}
      }]}}'
```

Tres cosas que cuestan un rato averiguar:

- **La URL la resuelve WAHA, no tú.** Si WAHA corre en un contenedor,
  `localhost` es su propio contenedor: usa la IP del anfitrión, o el nombre del
  servicio si comparten red de Docker.
- El `PUT` reinicia la sesión —vuelve a `WORKING` en segundos y sin reescanear
  el QR, pero no lo hagas en mitad de una partida.
- Con el secreto puesto, las entregas sin firma se rechazan con 401.
  `docker compose logs api | grep webhook` dice si llegan firmadas.

---

## Comandos

Sólo los aceptan los números de `MANAGER_NUMBER`.

| Comando | Qué hace |
|---|---|
| `#juegos` | Lista los juegos disponibles |
| `#juego <nombre>` | Inicia una partida |
| `#juego <nombre> ia` | Igual, con narración generada por el modelo |
| `#estado` | Qué partidas hay en marcha |
| `#cancelar` | Corta la partida y reabre el grupo |
| `#ayuda` | Recuerda los comandos |

El prefijo es configurable con `COMMAND_PREFIX`; por defecto `#`.

Tolera mayúsculas y acentos: `#Juego`, `#CATÁLOGO` y `#cancelar` funcionan
igual. Lo que se escriba **detrás del nombre** llega al juego como instrucción,
así que `#juego kahoot 15 preguntas de cine` pide quince preguntas de cine.

**Se juega en el grupo desde el que se pide**, siempre. Por privado no hay
grupo del que deducirlo, así que sólo se admite `#cancelar` cuando hay una
única partida viva.

Los másteres también juegan: si escriben `Yo` durante las inscripciones, entran
como cualquier otro.

---

## Cómo encaja todo

El webhook **encola** mensajes y devuelve; el juego los **recoge** dentro de una
ventana de tiempo, desde una tarea aparte. Por eso WAHA recibe su `200` en
milisegundos aunque el pueblo tarde un minuto en decidirse, y por eso una
partida no se cuelga porque alguien se fuera a cenar.

Sobre esa idea se apoyan las dos reglas que explican el resto del diseño: **el
modelo nunca decide la mecánica** —narra y desambigua, y todo funciona con
`LLM_PROVIDER=none`— y **toda espera tiene plazo**, para que ningún fallo deje
el grupo mudo esperando una noche que no termina.

👉 El diagrama, el mapa de módulos, los contratos que usa un juego, las reglas
que no se negocian y las cifras de concurrencia están en
[`docs/architecture.md`](docs/architecture.md). Las rutas de fallo se ejercitan
en `tests/test_resiliencia.py`.

---

## Configuración

Todas las variables están documentadas en `.env.example`, y las propias de cada
juego en su documento. Las transversales:

| Variable | Por defecto | Para qué |
|---|---|---|
| `MANAGER_NUMBER` | *(vacío)* | Números que pueden dar órdenes, separados por comas |
| `COMMAND_PREFIX` | `#` | Prefijo de los comandos |
| `WAHA_BASE_URL` | `http://waha:3000` | Dónde vive WAHA |
| `WAHA_SESSION` | `default` | Qué sesión usar |
| `WAHA_WEBHOOK_HMAC_SECRET` | *(vacío)* | Firma de los webhooks |
| `WAHA_DRY_RUN` | `false` | Escribe los envíos en el log en vez de mandarlos |
| `USE_MENTIONS` | `true` | Etiquetar contactos en vez de sólo nombrarlos |
| `LLM_PROVIDER` | `gemini` | `gemini`, `deepseek`, `openai` o `none` |
| `LLM_REASONING_EFFORT` | `low` | Cuánto piensa antes de escribir; vacío no lo manda |
| `MESSAGE_RETENTION_DAYS` | `30` | Purga del histórico al arrancar |

### Redis y SQLite: para qué cada uno

- **Redis** guarda lo efímero: los buzones de la ronda en curso, con TTL. Si no
  responde, el servicio arranca con un buzón en memoria y lo dice en el log: se
  sigue jugando, pero se pierden las colas al reiniciar.
- **SQLite** guarda lo que interesa conservar: el histórico de mensajes, la
  traza de cada partida y los checkpoints. Vive en el volumen `./data`.

---

## Se etiqueta, no se nombra

Los mensajes al grupo **etiquetan al contacto** en vez de escribir su nombre:
el cuerpo lleva el literal `@<id>` y el mismo contacto viaja como JID en el
array `mentions`. WhatsApp cruza las dos cosas y lo muestra como una mención
real, tocable — el nombre visible lo pone el dispositivo de cada lector.

```
☠️ @Hugo queda fuera de la partida
Ya no participa: ignorad lo que escriba.
```

Quita la ambigüedad de los tocayos y de quien no tiene nombre público, y deja
claro **a quién ignorar** el resto de la partida. Los privados siguen usando
nombres, que en un 1:1 son más legibles.

El `<id>` se saca del JID quitándole el dominio y el sufijo de dispositivo, y
no se convierte entre `@c.us` y `@lid`: se menciona a cada quien con la forma
con la que el grupo lo direcciona. Con `USE_MENTIONS=false` se vuelve a nombres
planos, para motores de WAHA que no resuelvan menciones.

---

## Documentación

| Documento | De qué habla |
|---|---|
| [`docs/hombreslobo.md`](docs/hombreslobo.md) | Roles, reparto, el grafo de fases, la narración y sus límites |
| [`docs/kahoot.md`](docs/kahoot.md) | La instrucción del comando, generación y validación de preguntas, puntuación |
| [`docs/architecture.md`](docs/architecture.md) | Mapa del código, los contratos de un juego, concurrencia |
| [`docs/juego-nuevo.md`](docs/juego-nuevo.md) | El paso a paso para añadir un juego |
| [`docs/desarrollo.md`](docs/desarrollo.md) | Entorno, la suite de pruebas y cómo depurar contra WAHA |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | Ramas, commits, estilo, dónde va cada cosa |

---

## Límites conocidos

- **Las rutas de WAHA cambian entre versiones y motores** (WEBJS / NOWEB /
  GOWS). Están concentradas en `app/waha/client.py` y la lectura de payloads en
  `app/waha/normalize.py`: si una versión difiere, esos dos ficheros son los
  únicos a tocar.
- **Silenciar el grupo requiere ser administrador**, y antes de intentarlo se
  comprueba. Si el bot no lo es, no se intenta y la partida sigue.
- **Reiniciar el servicio corta las partidas en curso.** Las que quedaron a
  medias se marcan `interrupted` al arrancar.
- **El buzón de Redis usa `BLPOP` con segundos enteros** para no depender de
  Redis >= 6. Si Redis se cae, el mensaje afectado se pierde con un log de
  error en vez de tumbar el webhook.
- **SQLite corre en modo WAL** con `busy_timeout`, para que el webhook pueda
  registrar mensajes mientras la partida escribe su traza.

Los propios de cada juego están en su documento.
