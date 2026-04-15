FROM python:3.12-slim-bookworm

WORKDIR /opt/gateway

# ffmpeg is required by voice_selection_api.py:_concat_segments_ffmpeg
# (voice clone path). slim image doesn't include it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8880

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8880", "--log-level", "info"]
