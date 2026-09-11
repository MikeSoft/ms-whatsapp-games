# syntax=docker/dockerfile:1
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# El usuario va antes que el código: no depende de lo que se despliegue, así
# que su capa se reaprovecha en todos los builds en vez de rehacerse cada vez
# que cambia una línea de la app. Se llama "wag" y no "games" porque la imagen
# base de Debian ya trae un usuario "games" (uid 60) y useradd abortaría con
# "username already in use". Necesita escribir en /app/data (SQLite y los
# checkpoints del grafo).
RUN useradd --create-home --uid 10001 wag \
    && mkdir -p /app/data \
    && chown -R wag:wag /app

# Las dependencias se copian primero para aprovechar la caché de capas: sólo se
# reinstalan cuando cambia requirements.txt. Y cuando cambia, la caché de pip
# va montada, así que se reinstala sin volver a bajar de la red lo que ya se
# había bajado antes: es la diferencia entre un build de dos minutos y uno de
# veinte segundos.
COPY requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

COPY --chown=wag:wag app ./app

USER wag

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
