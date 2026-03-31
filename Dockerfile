FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/aivideotrans/app/src

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

RUN pip install --no-cache-dir \
        assemblyai \
        "dashscope>=1.25.2" \
        google-genai \
        pydub \
        requests \
        websocket-client \
        "yt-dlp[default]" \
    && curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh \
    && /usr/local/bin/deno --version \
    && chmod +x \
        scripts/linux_app_service.sh \
        scripts/linux_compose_preflight.sh \
        scripts/linux_container_entrypoint.sh

ENTRYPOINT ["/usr/bin/tini", "--", "bash", "scripts/linux_container_entrypoint.sh"]
CMD ["bash", "scripts/linux_app_service.sh"]
