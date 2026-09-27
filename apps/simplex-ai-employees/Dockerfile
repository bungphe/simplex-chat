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
    AI_ADMIN_UI_HOST=0.0.0.0

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates tzdata libgmp10 zlib1g libffi8 libssl3t64 \
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
 && mkdir -p /data /app/cache && chown -R app:app /data /app/cache

USER app
VOLUME ["/data"]
EXPOSE 8080
ENV PATH=/app/venv/bin:$PATH
ENTRYPOINT ["python", "-m", "ai_employees"]
CMD ["run", "/data/employees.yaml"]
