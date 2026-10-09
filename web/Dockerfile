# Builds the static web app into /app/dist (copy it out, or see deploy/caddy/Dockerfile).
FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM scratch AS dist
COPY --from=build /app/dist /dist
