# Caddy image that also carries the built web app at /srv/web.
# Build context is the repo root:  docker build -f deploy/caddy/Dockerfile .
# The node stage matches web/Dockerfile (kept in step by hand: Docker cannot FROM another Dockerfile).
FROM node:22-alpine AS web
WORKDIR /app
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM caddy:2
COPY --from=web /app/dist /srv/web
