#!/bin/bash
set -e

echo "==> Running database migrations..."
python manage.py migrate --noinput

echo "==> Collecting static files..."
python manage.py collectstatic --noinput

echo "==> Starting Uvicorn API server (agent execution runs in the execution service)..."
# The explicit `websockets` dependency enables this backend without uvicorn[standard].
# Allow the existing 200 MiB Socket.IO frames carrying computer-use screenshots.
exec uvicorn config.asgi:application \
    --workers 1 \
    --host 0.0.0.0 \
    --port 8000 \
    --proxy-headers \
    --ws websockets-sansio \
    --ws-max-size $((200 * 1024 * 1024)) \
    --ws-max-queue 32 \
    --lifespan on
