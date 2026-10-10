FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates procps \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Vendor hls.js so the UI works on isolated LANs.
RUN mkdir -p static/vendor \
 && curl -fsSL https://cdn.jsdelivr.net/npm/hls.js@1.5.17/dist/hls.min.js -o static/vendor/hls.min.js

EXPOSE 8080

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${APP_PORT:-8080} --proxy-headers --forwarded-allow-ips=*"]
