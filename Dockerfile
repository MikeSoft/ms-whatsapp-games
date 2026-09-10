FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Las dependencias se copian primero para aprovechar la caché de capas: sólo
# se reinstalan cuando cambia requirements.txt.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# El servicio corre sin privilegios y necesita escribir en /app/data
# (SQLite y los checkpoints del grafo). El usuario se llama "wag" y no
# "games" porque la imagen base de Debian ya trae un usuario "games" (uid 60)
# y useradd abortaría con "username already in use".
RUN useradd --create-home --uid 10001 wag \
    && mkdir -p /app/data \
    && chown -R wag:wag /app
USER wag

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
