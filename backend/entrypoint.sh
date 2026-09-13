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

echo "==> Starting Daphne ASGI server..."
# Align with Django DAPHNE_WEBSOCKET_MAX_MESSAGE/FRAME_SIZE (200 MiB).
# Daphne 4.2.2+ defaults both to 1 MiB (CVE-2026-44545), which drops
# computer-use PNG screenshots on the runner WebSocket. Both flags are
# required: a screenshot is one frame.
WS_MAX=$((200 * 1024 * 1024))
exec daphne \
    -b 0.0.0.0 \
    -p 8000 \
    --proxy-headers \
    --websocket-max-message-size "$WS_MAX" \
    --websocket-max-frame-size "$WS_MAX" \
    --access-log - \
    config.asgi:application
