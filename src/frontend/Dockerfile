# ─────────────────────────────────────────────────────────────────────────────
# Flume core console — Vite SPA on nginx. No dashboard Go API, no workers.
# ─────────────────────────────────────────────────────────────────────────────

FROM node:22-alpine AS build

WORKDIR /app
COPY src/frontend/src/package.json src/frontend/src/package-lock.json ./
RUN npm ci --prefer-offline

COPY src/frontend/src/ ./
ARG VITE_CORE_UI=true
ENV VITE_CORE_UI=$VITE_CORE_UI
RUN npm run build

FROM nginx:1.27-alpine

# wget is used by the compose/image healthcheck.
RUN apk add --no-cache wget

COPY src/frontend/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /dist /usr/share/nginx/html

EXPOSE 8765

HEALTHCHECK --interval=10s --timeout=5s --retries=5 --start-period=10s \
    CMD wget --spider --quiet http://127.0.0.1:8765/ || exit 1
