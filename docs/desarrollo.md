# Desarrollo

> Cómo montar el entorno, correr la suite y depurar contra una WAHA de verdad.
> Las reglas de ingeniería —ramas, commits, estilo, dónde va cada cosa— están
> en [CONTRIBUTING](../CONTRIBUTING.md) y mandan sobre este documento.

---

## Entorno

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env
```

Python 3.11 o superior.

```bash
.venv/bin/python -m pytest
.venv/bin/ruff check app tests
.venv/bin/uvicorn app.main:app --reload
```

Las dos primeras tienen que pasar en limpio antes de abrir un pull request.

### Si `python3 -m venv` no funciona

En Debian y Ubuntu el módulo `venv` va en un paquete aparte y `ensurepip`
falla. Si no puedes instalarlo, la suite corre igual dentro de un contenedor,
sin tocar la máquina:

```bash
docker run --rm -v "$PWD":/w -w /w -u "$(id -u):$(id -g)" \
  -e HOME=/tmp python:3.11-slim \
  sh -c 'pip install -q -r requirements-dev.txt && \
         ruff check app tests && python -m pytest'
```

---

## Cómo se prueba

Los tests corren **sin WAHA, sin Redis y sin LLM**, y sin leer el `.env` ni
las variables de entorno de la máquina: `tests/conftest.py` cierra las dos
puertas, porque con una sola un `export COMMAND_PREFIX=/` seguiría entrando y
poniendo la suite roja sin que nadie hubiera tocado código.

| Fichero | Qué cubre |
|---|---|
| `tests/conftest.py` | Dobles: `FakeTransport`, `ScriptedPlayers`, `render_mentions` |
| `tests/test_werewolf_rules.py` | Reglas deterministas: reparto, cadenas de muerte, victoria |
| `tests/test_werewolf_flow.py` | Partidas completas del grafo |
| `tests/test_adversarial.py` | Entradas hostiles: basura, muertos que actúan, inyección |
| `tests/test_concurrencia.py` | Acciones simultáneas, dos partidas a la vez, pools |
| `tests/test_resiliencia.py` | WAHA caído, transporte lento, Redis que se va |
| `tests/test_soak.py` | Muchas partidas con jugadores caóticos + invariantes |
| `tests/test_inbox.py` | Memoria y Redis con los **mismos** casos |
| `tests/test_mentions.py` | Etiquetado de contactos y de nombres en prosa |
| `tests/test_kahoot.py` | Instrucción, validación de preguntas, tandas completas |
| `tests/test_kahoot_aritmetica.py` | Evaluación de operaciones y lo que se rechaza |
| `tests/test_waha_client.py` | Reintentos, degradación, forma de las peticiones |
| `tests/test_api.py` | Endpoints, firma, encaminamiento |

Para un juego nuevo, el patrón que mejor funciona es **jugar una partida de
verdad** con jugadores automáticos en vez de simular el grafo:

```python
async def test_mi_juego_termina(table):
    ctx, transport, inbox, script = table(6)
    game = MiJuego(ctx, timers=fast_timers())
    result = await game.run()
    assert result.status == "finished"
    assert transport.locked is False        # nunca dejar el grupo mudo
```

`ScriptedPlayers` lee los privados del bot y responde como una persona; sólo
sabe lo que se le ha dicho, igual que un jugador real. Si tu juego manda otros
privados, extiéndelo con las respuestas que toquen.

Y cuando arregles un bug, **añade el caso que lo pillaba antes de arreglarlo**.


---

## Depurar contra WhatsApp

Levantar el servicio no obliga a quemar una sesión real:

- `WAHA_DRY_RUN=true` escribe los envíos en el log en vez de mandarlos. Todo lo
  demás corre igual, así que se puede seguir una partida entera sin que el
  grupo reciba nada.
- `LLM_PROVIDER=none` deja el modelo fuera y usa los textos estáticos. Es
  además la configuración que la suite da por supuesta.
- `COMMAND_PREFIX` te deja usar un prefijo distinto del de producción si el bot
  comparte grupo con otro.

Tres endpoints dicen qué está pasando sin entrar al contenedor:

```bash
curl localhost:8000/health/ready    # redis, base de datos, sesión de WAHA, LLM
curl localhost:8000/status          # partidas en marcha
curl localhost:8000/games           # juegos registrados
```

Y para ver si las entregas de WAHA están llegando y con firma válida:

```bash
docker compose logs -f api | grep webhook
```

Un `webhook.rejected` con `falta la cabecera HMAC` significa que el secreto
está puesto en el `.env` pero no en la sesión de WAHA, o al revés: ver
[la puesta en marcha](../README.md#si-tu-waha-ya-existe).

---

## Qué hay en cada sitio

| Documento | De qué habla |
|---|---|
| [`architecture.md`](architecture.md) | Mapa del código, contratos de un juego, concurrencia |
| [`juego-nuevo.md`](juego-nuevo.md) | El paso a paso para añadir un juego |
| [`../CONTRIBUTING.md`](../CONTRIBUTING.md) | Ramas, commits, estilo, revisión |
