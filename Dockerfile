# TeleDrive Production Dockerfile (Compatible with Railway.com, Docker Compose & VPS)
FROM python:3.11-slim

# Prevent python from writing pyc files and buffering stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PORT=8000

WORKDIR /app

# Install minimal OS dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    gcc \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application codebase
COPY backend/app ./app

# Create directory for persistent data (sessions, db state, railway volume mount)
RUN mkdir -p /app/data

# Expose HTTP port (Railway injects $PORT at runtime, fallback 8000)
EXPOSE 8000

# Container health monitoring
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:${PORT:-8000}/readyz || exit 1

# Launch ASGI server with dynamic port and proxy headers (Railway reverse proxy compatible)
CMD sh -c "python -m uvicorn app.main:app --host 0.0.0.0 --port \${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"
