# ============================================================
# Stage 1 — Build React/Vite Mini App
# ============================================================
FROM node:20-alpine AS frontend-builder

WORKDIR /frontend

COPY frontend/package*.json ./

RUN npm install

COPY frontend/ .

RUN npm run build


# ============================================================
# Stage 2 — Python/FastAPI application
# ============================================================
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

WORKDIR /app

# ------------------------------------------------------------
# System dependencies
# ------------------------------------------------------------
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential \
        curl \
    && rm -rf /var/lib/apt/lists/*


# ------------------------------------------------------------
# Python dependencies
# ------------------------------------------------------------
COPY requirements.txt ./requirements.txt

RUN pip install --upgrade pip && \
    pip install -r requirements.txt


# ------------------------------------------------------------
# Application source
# ------------------------------------------------------------
COPY app ./app
COPY migrations ./migrations


# ------------------------------------------------------------
# Frontend build
#
# Vite is configured to output to:
# app/static/miniapp
# ------------------------------------------------------------
COPY --from=frontend-builder /frontend/../app/static/miniapp ./app/static/miniapp


# ------------------------------------------------------------
# Render port
# ------------------------------------------------------------
EXPOSE 8000


# ------------------------------------------------------------
# Start FastAPI
# ------------------------------------------------------------
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
