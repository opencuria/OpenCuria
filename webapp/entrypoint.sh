#!/bin/sh
# Generate runtime configuration for the SPA.
# This runs at container startup (via nginx's docker-entrypoint.d mechanism)
# and writes a /config.json that the Vue app fetches at boot time.
#
# Environment variables:
#   VITE_API_BASE_URL   — full URL to the backend API (e.g. https://api.example.com/api/v1)
#   VITE_WS_BASE_URL    — full URL to the backend WebSocket (e.g. https://api.example.com)

set -e

API_BASE_URL="${VITE_API_BASE_URL:-/api/v1}"
WS_BASE_URL="${VITE_WS_BASE_URL:-}"

cat > /usr/share/nginx/html/config.json <<EOF
{
  "apiBaseUrl": "${API_BASE_URL}",
  "wsBaseUrl": "${WS_BASE_URL}"
}
EOF

echo "Runtime config written: apiBaseUrl=${API_BASE_URL}, wsBaseUrl=${WS_BASE_URL}"
