FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .

# API_PORT vem do .env (default 8091)
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${API_PORT:-8091}"]
