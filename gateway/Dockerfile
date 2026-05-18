FROM python:3.12-slim-bookworm

WORKDIR /opt/gateway

# ffmpeg is required by voice_selection_api.py:_concat_segments_ffmpeg
# (voice clone path). e2fsprogs provides resize2fs/tune2fs for the guarded
# admin disk expansion endpoint.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg e2fsprogs \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8880

# Phase 1.5 硬化（2026-04-22）：通过 `python main.py` 入口启动，让
# main.py:320 的 `uvicorn.run(host=settings.gateway_host, ...)` 生效，
# 这样 AVT_GATEWAY_HOST / AVT_GATEWAY_PORT env var 就能控制监听地址。
# 之前的 CMD ["uvicorn", ..., "--host", "0.0.0.0", ...] 硬编码了 0.0.0.0，
# 会绕过 settings 层，在 host-network 部署下把 gateway 暴露到所有接口。
CMD ["python", "main.py"]
