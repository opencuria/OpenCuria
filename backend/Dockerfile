# ─────────────────────────────────────────────────────────────────────────────
# opencuria Backend — Production Docker Image
# ─────────────────────────────────────────────────────────────────────────────
# Multi-stage build: install deps in builder stage, copy to slim runtime image.
#
# Usage:
#   docker build -t opencuria-backend .
#   docker run -p 8000:8000 --env-file .env opencuria-backend
# ─────────────────────────────────────────────────────────────────────────────

FROM python:3.12-slim AS builder

WORKDIR /app

# Install system dependencies needed for psycopg2-binary & cryptography
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ─────────────────────────────────────────────────────────────────────────────
# Runtime stage
# ─────────────────────────────────────────────────────────────────────────────

FROM python:3.12-slim

WORKDIR /app

# Runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Copy installed Python packages from builder
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code
COPY . .

# Copy and set entrypoint
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Collect static files (with dummy secret key for collectstatic)
RUN DJANGO_SECRET_KEY=build-placeholder DJANGO_ENV=production \
    python manage.py collectstatic --noinput 2>/dev/null || true

EXPOSE 8000

ENTRYPOINT ["/entrypoint.sh"]
