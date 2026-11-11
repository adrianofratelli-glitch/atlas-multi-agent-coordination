#!/bin/sh
set -e

cd /app/backend
# One worker only: metrics, warmup single-flight, rate limiter and the SSE
# fan-out are in-process state. With 2+ workers nginx would spread a client's
# requests across processes that do not share it.
uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 &
nginx -g 'daemon off;'
