FROM python:3.12-slim

# OCI-Metadaten; Version/Revision/Quelle setzt die Release-Pipeline zusätzlich (docker/metadata-action)
ARG VERSION=dev
LABEL org.opencontainers.image.title="websearch" \
      org.opencontainers.image.description="Parallel, captcha-avoiding web search (CLI, MCP server, monitoring)" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}"

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
# Erst die gepinnten Abhängigkeiten (requirements.lock, erzeugt mit
#   uv pip compile pyproject.toml --universal --python-version 3.10 -o requirements.lock
# ), dann das Paket selbst ohne Abhängigkeitsauflösung -> reproduzierbare Images.
# Eigene Schicht für die Abhängigkeiten: Codeänderungen bauen nur die letzte Schicht neu.
# Auch das Build-Backend (hatchling) ist gepinnt (build-requirements.lock) und wird ohne Build-Isolation
# genutzt - sonst holte pip beim Bauen eine beliebige aktuelle hatchling-Version.
COPY requirements.lock build-requirements.lock ./
RUN pip install -r requirements.lock -r build-requirements.lock
COPY pyproject.toml README.md LICENSE ./
COPY websearch ./websearch
# Paket installieren, Quellkopie wieder entfernen (liegt in site-packages), unprivilegierter Nutzer
RUN pip install --no-deps --no-build-isolation . \
 && rm -rf /app/websearch /app/build \
 && useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin websearch \
 && mkdir /data && chown websearch /data
USER websearch
ENV WEBSEARCH_CACHE=/data/cache.db WEBSEARCH_MONITOR_DB=/data/monitor.db
VOLUME /data
# kein HEALTHCHECK im Image: derselbe Container läuft als MCP-Server oder Monitor - die passenden
# Prüfungen (/healthz bzw. Heartbeat-Datei) stehen in docker-compose.yml
ENTRYPOINT ["websearch"]
