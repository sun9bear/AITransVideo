FROM python:3.12-slim-bookworm

# Phase D — optional Whisper subtitle alignment.
# Default ``INSTALL_WHISPER=0`` keeps the image lean (no faster-whisper /
# ctranslate2 / tokenizers — ~500 MB savings). Set ``INSTALL_WHISPER=1``
# at build time on deployments that opt into Whisper subtitle alignment;
# the runtime double-gate (env capability + admin policy) still controls
# whether the installed code path is actually exercised.
#   docker compose build --build-arg INSTALL_WHISPER=1 app
ARG INSTALL_WHISPER=0
ENV INSTALL_WHISPER=$INSTALL_WHISPER

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/aivideotrans/app/src \
    # HF/faster-whisper cache target. When the host bind-mounts this
    # directory (see docker-compose.yml app.volumes), pre-warmed model
    # weights survive container recreation. When no bind mount, models
    # download into the container's ephemeral writable layer (rebuild
    # would re-download 466MB+ for `small`).
    HF_HOME=/opt/aivideotrans/model_cache/hf

WORKDIR /opt/aivideotrans/app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        ffmpeg \
        nodejs \
        unzip \
        tini \
    && rm -rf /var/lib/apt/lists/*

COPY . /opt/aivideotrans/app

RUN pip install --no-cache-dir . \
    && pip install --no-cache-dir pyJianYingDraft \
    && if [ "$INSTALL_WHISPER" = "1" ]; then \
         echo "[Dockerfile] Installing optional Whisper extra (.[whisper])" \
         && pip install --no-cache-dir ".[whisper]"; \
       else \
         echo "[Dockerfile] Skipping Whisper extra (INSTALL_WHISPER=$INSTALL_WHISPER)"; \
       fi \
    && curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh \
    && /usr/local/bin/deno --version \
    && chmod +x \
        scripts/linux_app_service.sh \
        scripts/linux_compose_preflight.sh \
        scripts/linux_container_entrypoint.sh \
    && mkdir -p /opt/aivideotrans/model_cache/hf

ENTRYPOINT ["/usr/bin/tini", "--", "bash", "scripts/linux_container_entrypoint.sh"]
CMD ["bash", "scripts/linux_app_service.sh"]
