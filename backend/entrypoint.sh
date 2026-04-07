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
exec daphne \
    -b 0.0.0.0 \
    -p 8000 \
    --proxy-headers \
    --access-log - \
    config.asgi:application
