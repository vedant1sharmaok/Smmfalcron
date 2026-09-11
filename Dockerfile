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

# System deps
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
COPY alembic.ini ./

# Copy compiled React Mini App
COPY --from=frontend-builder /frontend/dist ./app/static/miniapp

EXPOSE 8000

# Run migrations then start the server.
# DATABASE_URL must be set as a Render environment variable.
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --loop uvloop"]
