# ─────────────────────────────────────────────────────────────────────────────
# opencuria Webapp — Production Docker Image
# ─────────────────────────────────────────────────────────────────────────────
# Multi-stage build:
#   Stage 1: Node.js — install deps + vite build
#   Stage 2: nginx:alpine — serve static SPA
#
# Runtime configuration is injected via /config.json (generated at container
# startup by entrypoint.sh using environment variables). This avoids having
# to rebuild the image when changing domains.
# ─────────────────────────────────────────────────────────────────────────────

# --- Build stage ---
FROM node:22-alpine AS build

WORKDIR /app

COPY package.json package-lock.json ./
COPY webapp/package.json webapp/package.json
COPY mobile/package.json mobile/package.json
COPY packages/chat-core/package.json packages/chat-core/package.json
COPY packages/client/package.json packages/client/package.json
RUN npm ci --workspace webapp --workspace @opencuria/chat-core --workspace @opencuria/client --include-workspace-root --ignore-scripts

COPY packages ./packages
COPY webapp ./webapp
RUN npm run build-only --workspace webapp

# --- Serve stage ---
FROM nginx:alpine

# Remove default nginx config
RUN rm /etc/nginx/conf.d/default.conf

# Copy custom nginx config
COPY webapp/nginx.conf /etc/nginx/conf.d/default.conf

# Copy entrypoint that generates runtime config
COPY webapp/entrypoint.sh /docker-entrypoint.d/40-runtime-config.sh
RUN chmod +x /docker-entrypoint.d/40-runtime-config.sh

# Copy built app
COPY --from=build /app/webapp/dist /usr/share/nginx/html

EXPOSE 80
