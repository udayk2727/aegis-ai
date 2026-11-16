# Local demo image for the dashboard. Build from the repository root, which is the
# context set in compose.yaml. The first stage installs the locked dependencies and
# builds the static files, so node_modules and dist never come from the host.
# The second stage serves the files and forwards the read-only API prefix.

FROM node:22-alpine AS build

WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM nginx:1.27-alpine

COPY frontend/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/frontend/dist /usr/share/nginx/html

EXPOSE 8080
