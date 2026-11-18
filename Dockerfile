FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY egsim ./egsim
RUN pip install --no-cache-dir . \
    && useradd --system --uid 10001 egsim \
    && mkdir -p /data && chown egsim /data

USER egsim
WORKDIR /data
EXPOSE 8090
# Konfiguration und Berichte liegen in /data (Volume). EGSIM_UI_PASSWORD schützt die UI per Basic Auth.
CMD ["egsim", "ui", "--host", "0.0.0.0", "--port", "8090", "-c", "/data/egsim-ui.json"]
