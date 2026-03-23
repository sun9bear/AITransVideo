FROM python:3.12-slim-bookworm

WORKDIR /opt/gateway

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8880

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8880", "--log-level", "info"]
