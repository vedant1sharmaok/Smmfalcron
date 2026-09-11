# ============================================================
# Stage 1 — Build frontend (React / Vite)
# ============================================================
FROM node:20-alpine AS frontend-builder

WORKDIR /frontend

COPY frontend/package*.json ./
RUN npm install

COPY frontend/ .
RUN npm run build


# ============================================================
# Stage 2 — Backend (Python 3.12)
# ============================================================
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

WORKDIR /app

# System deps: pg_dump for backup worker, curl for health checks
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential \
        curl \
        postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Python dependencies
COPY requirements.txt ./
RUN pip install --upgrade pip && \
    pip install -r requirements.txt

# Application source
COPY app ./app
COPY migrations ./migrations

# Copy compiled React Mini App into the static directory
COPY --from=frontend-builder /frontend/dist ./app/static/miniapp

EXPOSE 8000

# Start: run Alembic migrations then start the server.
# --workers 1 is intentional for Render free tier (limited RAM).
# Remove --workers 1 and set APP_WORKERS env var for paid plans.
CMD ["sh", "-c", "\
    alembic upgrade head && \
    uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --loop uvloop \
"]
