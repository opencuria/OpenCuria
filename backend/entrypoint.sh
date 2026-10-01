#!/bin/bash
set -e

echo "==> Running database migrations..."
python manage.py migrate --noinput

echo "==> Collecting static files..."
python manage.py collectstatic --noinput

# Auto-create superuser if env vars are set (idempotent)
if [ -n "$DJANGO_SUPERUSER_EMAIL" ] && [ -n "$DJANGO_SUPERUSER_PASSWORD" ]; then
    echo "==> Creating superuser (if not exists)..."
    python manage.py shell -c "
from apps.accounts.models import User
email = '$DJANGO_SUPERUSER_EMAIL'
if not User.objects.filter(email=email).exists():
    User.objects.create_superuser(
        username=email,
        email=email,
        password='$DJANGO_SUPERUSER_PASSWORD',
    )
    print(f'Superuser {email} created.')
else:
    print(f'Superuser {email} already exists.')
"
fi

echo "==> Starting Uvicorn ASGI server (single worker; scheduler shares its event loop)..."
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
