FROM python:3.10-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# dlib is compiled from source: it needs a C++ toolchain, cmake and BLAS/LAPACK.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake pkg-config libopenblas-dev liblapack-dev libx11-dev curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .
RUN useradd --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /app/uploads/faces && chown -R appuser /app
USER appuser

ENV PORT=8000
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD curl -fsS http://127.0.0.1:${PORT}/healthz || exit 1

# init-db is idempotent (tables, indexes, admin account); then start the real server.
CMD ["sh", "-c", "python run.py init-db && exec gunicorn -c gunicorn.conf.py wsgi:app"]
