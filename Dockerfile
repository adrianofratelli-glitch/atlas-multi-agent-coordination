# syntax=docker/dockerfile:1
#
# One container: nginx serves the React build and proxies /api to uvicorn.
#   docker build -t multi-agent .
#   docker build --build-context shared=../_shared -t multi-agent .   # with pov-shared (optional)

FROM node:22-alpine AS frontend-build
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
# Empty base URL = same origin: the browser calls /api/* on nginx, not :8031.
# VITE_ADMIN_KEY is deliberately not set: no secret is baked into the bundle.
ENV VITE_API_URL=""
RUN npm run build

# pov-shared is optional (the backend has local fallbacks). Without
# --build-context shared=..., this empty stage is used and nothing is installed.
FROM scratch AS shared

FROM python:3.12-slim AS shared-wheel
COPY --from=shared . /src
RUN mkdir /wheels \
  && if [ -f /src/pyproject.toml ]; then pip wheel --no-cache-dir --no-deps -w /wheels /src; fi

FROM python:3.12-slim AS runtime
RUN apt-get update \
  && apt-get install -y --no-install-recommends nginx curl \
  && rm -rf /var/lib/apt/lists/* \
  && useradd --create-home --uid 10001 app

WORKDIR /app

COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt
COPY --from=shared-wheel /wheels /tmp/wheels
RUN if ls /tmp/wheels/*.whl >/dev/null 2>&1; then pip install --no-cache-dir --no-deps /tmp/wheels/*.whl; fi \
  && rm -rf /tmp/wheels

COPY backend/ ./backend/
COPY --from=frontend-build /build/dist ./frontend/dist
COPY docker/nginx.conf /etc/nginx/nginx.conf
COPY docker/start.sh /start.sh
RUN chmod +x /start.sh && chown -R app:app /app
USER app

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8080/api/health || exit 1

CMD ["/start.sh"]
