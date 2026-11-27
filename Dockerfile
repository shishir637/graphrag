# Context Layer: one image with the web app, API, MCP server and the three engines (CPU only).
# Build:  docker compose build      Run: docker compose up -d      Open: http://localhost:8080

# ---- web app (React) ----
FROM node:22-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

# ---- server ----
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 HF_HOME=/models CL_DATA_DIR=/app/var CL_HOST=0.0.0.0 CL_PORT=8080
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 curl && rm -rf /var/lib/apt/lists/*
WORKDIR /app
# CPU-only PyTorch first, so requirements.txt does not pull multi-GB GPU builds
RUN pip install torch==2.12.0 torchvision==0.27.0 --index-url https://download.pytorch.org/whl/cpu
COPY requirements.txt requirements-semantic.txt requirements-app.txt ./
RUN sed -i -E '/^(torch|torchvision)==/d' requirements.txt && pip install -r requirements.txt
COPY . .
COPY --from=web /web/dist ./web/dist
RUN useradd --create-home --uid 10001 cl && mkdir -p /app/var /app/outputs /app/qdrant_data /app/sl_store /models \
    && chown -R cl /app /models
USER cl
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s CMD curl -fsS http://localhost:8080/api/v1/health || exit 1
CMD ["python", "-m", "app"]
