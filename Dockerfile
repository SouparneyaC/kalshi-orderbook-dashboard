# A way to try the service without installing Python. The deployment we measured runs under systemd (deploy/).
FROM python:3.15-slim-bookworm@sha256:96599df822d0e1c6bb700014a05c7f3c8cabc6e0f7ff3c4f9f69cee033b4b5d9

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first, so a code change does not reinstall them.
COPY requirements-lock.txt .
RUN pip install -r requirements-lock.txt

COPY app ./app

# A fixed non-root user. compose.yaml runs the container as the host user instead, so that it can read your key file.
RUN useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin tradedash \
    && mkdir /data && chown tradedash:tradedash /data

# Trade storage is off by default, as in our deployment. The measurement history goes to the /data volume.
ENV PERSIST_TRADES=0 \
    METRICS_DB=/data/metrics.duckdb
ARG GIT_COMMIT=unknown
ENV GIT_COMMIT=${GIT_COMMIT}

USER tradedash
EXPOSE 8000
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/', timeout=4)"]

CMD ["uvicorn", "app.server.main:app", "--host", "0.0.0.0", "--port", "8000"]
