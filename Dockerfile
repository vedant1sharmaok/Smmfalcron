# ============================================================
# Stage 1: Build React/Vite frontend
# ============================================================
FROM node:20-alpine AS frontend-builder

WORKDIR /frontend

COPY frontend/package*.json ./

RUN npm install

COPY frontend/ .

RUN npm run build


# ============================================================
# Stage 2: Python backend + built frontend
# ============================================================
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

WORKDIR /app

# System packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt ./

RUN pip install --upgrade pip && \
    pip install -r requirements.txt

# Copy backend/application source
COPY app ./app
COPY migrations ./migrations

# Copy frontend build
COPY --from=frontend-builder /frontend/dist ./frontend/dist

# Expose Render port
EXPOSE 8000

# Start FastAPI
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
