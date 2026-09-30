# syntax=docker/dockerfile:1.7
# AI employees on SimpleX Chat. Build from the repository root:
#
#   docker build -f apps/simplex-ai-employees/Dockerfile -t simplex-ai-employees .
#
# The SimpleX core (libsimplex) is fetched at build time from the SimpleX release
# matching the Python SDK. To run a libsimplex you built from this repository
# instead, mount it and set SIMPLEX_LIBS_DIR (see docker-compose.yml).

FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Ho_Chi_Minh \
    # inside the container the UI must listen on all interfaces; compose publishes it on 127.0.0.1 only
    AI_ADMIN_UI_HOST=0.0.0.0 \
    AI_STOREFRONT_HOST=0.0.0.0

# postgresql-client: pg_dump/pg_restore for backups of an office on PostgreSQL (~35 MB)
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates tzdata libgmp10 zlib1g libffi8 libssl3t64 \
      postgresql-client \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 app

WORKDIR /app
COPY packages/simplex-chat-python /app/packages/simplex-chat-python
COPY apps/simplex-ai-employees /app/apps/simplex-ai-employees

# Behind a TLS-inspecting proxy, pass its CA for the build only (it is not kept in the image):
#   docker build --secret id=ca_cert,src=/path/to/proxy-ca.crt ...
RUN --mount=type=secret,id=ca_cert,required=false \
    if [ -f /run/secrets/ca_cert ]; then export PIP_CERT=/run/secrets/ca_cert; fi \
 && python3 -m venv /app/venv \
 && /app/venv/bin/pip install /app/packages/simplex-chat-python "/app/apps/simplex-ai-employees[postgres]"

ARG DOWNLOAD_LIBSIMPLEX=1
ENV XDG_CACHE_HOME=/app/cache
RUN --mount=type=secret,id=ca_cert,required=false \
    if [ -f /run/secrets/ca_cert ]; then export SSL_CERT_FILE=/run/secrets/ca_cert; fi \
 && if [ "$DOWNLOAD_LIBSIMPLEX" = "1" ]; then /app/venv/bin/python -m simplex_chat install; fi \
 && mkdir -p /data /backups /app/cache && chown -R app:app /data /backups /app/cache

USER app
VOLUME ["/data"]
EXPOSE 8080
ENV PATH=/app/venv/bin:$PATH \
    AI_BACKUP_DIR=/backups
# GET /healthz on the admin UI answers 200 while the database does; AI_HEALTHCHECK_URL=off disables it
HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
    CMD ["python", "-c", "import os,sys,urllib.request as u; url=os.environ.get('AI_HEALTHCHECK_URL') or 'http://127.0.0.1:8080/healthz'; sys.exit(0 if url=='off' else (0 if u.urlopen(url, timeout=5).status==200 else 1))"]
ENTRYPOINT ["python", "-m", "ai_employees"]
CMD ["run", "/data/employees.yaml"]
