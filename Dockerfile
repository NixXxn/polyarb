FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config
COPY docker-entrypoint.sh /docker-entrypoint.sh

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -e '.[dashboard,live]' \
    && chmod +x /docker-entrypoint.sh \
    && mkdir -p /data \
    && python -c "from pathlib import Path; from arbot.dashboard.app import app; p = Path(app.template_folder) / 'index.html'; assert p.is_file(), p"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ARBOT_DATA_DIR=/data \
    ARBOT_SETTINGS_PATH=/data/settings.yaml \
    ARBOT_PROCESS=all \
    HOST=0.0.0.0 \
    PORT=8788

VOLUME ["/data"]
EXPOSE 8788

HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT', '8788'))"

ENTRYPOINT ["/docker-entrypoint.sh"]
