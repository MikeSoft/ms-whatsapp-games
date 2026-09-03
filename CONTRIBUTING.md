# Cómo contribuir

Reglas de ingeniería de este repositorio. Son autoritativas aquí y ganan a
cualquier costumbre general.

## Entorno

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env
```

Python 3.11 o superior. Nunca commitees un `.env`: `.gitignore` ya lo cubre,
pero revísalo antes de `git add -A`.

## Antes de abrir un pull request

```bash
.venv/bin/ruff check app tests
.venv/bin/python -m pytest
```

Las dos cosas tienen que pasar en limpio. Si un test falla, arréglalo o
explica en el PR por qué queda rojo; no lo saltes ni lo borres.

## Estilo

- Línea de 95 columnas, `ruff` con la configuración de `pyproject.toml`.
- **Identificadores en inglés, comentarios y textos de usuario en español.**
  El producto se juega en español; el código se lee como código.
- Los `except Exception` amplios son legítimos **sólo en los bordes** (WAHA,
  LLM, Redis, base de datos) y siempre con `# noqa: BLE001` y un comentario que
  diga qué se degrada. En la lógica del juego, no.
- Type hints en las firmas públicas. `from __future__ import annotations` en
  todos los módulos.
- Los docstrings explican *por qué*, no *qué*. Si hace falta describir qué hace
  la función, probablemente le falte un nombre mejor.

## Dónde va cada cosa

- **Una regla de juego nueva** → `app/games/werewolf/` y su test en
  `tests/test_werewolf_rules.py`.
- **Un juego nuevo** → `app/games/<juego>/`, registrado con `@register` y
  añadido a `BUILTIN_MODULES`. Ver la sección del README.
- **Una ruta de WAHA** → `app/waha/client.py`. En ningún otro sitio se hace
  HTTP contra WAHA.
- **Un campo de un payload de WAHA** → `app/waha/normalize.py`. El resto del
  código sólo conoce `InboundMessage`.
- **Un ajuste configurable** → `app/config.py` **y** `.env.example`. Los dos, o
  nadie sabrá que existe.

## Reglas que no se negocian

1. **El LLM nunca decide la mecánica.** Quién muere, quién vota a quién y quién
   gana se resuelve con reglas deterministas. El modelo narra y desambigua
   lenguaje coloquial; nada más. Todo lo que llame al LLM debe funcionar con
   `LLM_PROVIDER=none`.
2. **Ningún nodo puede dejar el grupo silenciado.** Si abres un camino nuevo en
   el grafo, asegúrate de que todas sus salidas —incluidos los errores y las
   cancelaciones— acaban reabriendo el chat.
3. **Nada de secretos en el grupo.** Los roles van por privado. Si añades un
   mensaje al grupo, pregúntate qué información filtra.
4. **El webhook no bloquea.** Encola y devuelve. Si necesitas esperar a alguien,
   se espera dentro de la tarea de la partida, no en el handler HTTP.

## Tests

Los tests corren sin WAHA, sin Redis y sin LLM. `tests/conftest.py` trae:

- `FakeTransport` — apunta lo enviado al grupo y a los privados.
- `ScriptedPlayers` — jugadores automáticos que leen los privados del bot y
  responden como personas.
- `fast_timers()` — tiempos en milisegundos, para jugar partidas completas.

Prefiere un test que **juegue una partida** a uno que simule el grafo. Si
arreglas un bug, añade el caso que lo pillaba antes de arreglarlo.

## Commits y ramas

- Rama por cambio, nunca directo a la rama principal.
- Mensajes en imperativo y en español: `añade el rol del Cazador`, no
  `añadido el rol del Cazador`.
- Asunto de 72 caracteres como máximo; el cuerpo explica el *por qué* si no es
  obvio.
- Sin atribución de herramientas, asistentes ni proveedores en mensajes de
  commit, PR ni comentarios de código.
- Nada de emojis en títulos ni descripciones de pull request.

## Documentación

En Markdown, y referenciando los ficheros por su ruta relativa al repositorio
(`app/games/werewolf/nodes.py`), nunca por una ruta que contenga un directorio
personal.
