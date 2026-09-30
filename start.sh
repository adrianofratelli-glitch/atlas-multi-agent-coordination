#!/bin/bash
set -e
cd "$(dirname "$0")"

BACKEND_RUNNING=0
FRONTEND_RUNNING=0
lsof -nP -iTCP:8031 -sTCP:LISTEN >/dev/null 2>&1 && BACKEND_RUNNING=1
lsof -nP -iTCP:5191 -sTCP:LISTEN >/dev/null 2>&1 && FRONTEND_RUNNING=1

if [ "$BACKEND_RUNNING" -eq 1 ] && [ "$FRONTEND_RUNNING" -eq 1 ]; then
  echo "PoV já está completa nas portas 8031 e 5191. Abrindo no navegador."
  if [ "${POV_NO_OPEN:-0}" != "1" ]; then open http://127.0.0.1:5191; fi
  exit 0
fi

if [ "$BACKEND_RUNNING" -eq 1 ] || [ "$FRONTEND_RUNNING" -eq 1 ]; then
  echo "Inicialização interrompida: só uma porta da PoV está ocupada."
  echo "Backend 8031: $BACKEND_RUNNING | Frontend 5191: $FRONTEND_RUNNING"
  echo "Encerre o processo parcial e execute ./start.sh novamente."
  exit 1
fi

source .venv/bin/activate
(cd backend && python run.py) &
BACKEND_PID=$!

if [ "${POV_DEV:-0}" != "1" ] && {
  [ ! -f frontend/dist/index.html ] ||
  [ -n "$(find frontend/src -type f -newer frontend/dist/index.html -print -quit)" ] ||
  [ frontend/package-lock.json -nt frontend/dist/index.html ] ||
  [ frontend/package.json -nt frontend/dist/index.html ];
}; then
  (cd frontend && npm run build)
fi
if [ "${POV_DEV:-0}" = "1" ]; then
  (cd frontend && exec node_modules/.bin/vite --host 127.0.0.1 --port 5191 --strictPort) &
else
  (cd frontend && exec node_modules/.bin/vite preview --host 127.0.0.1 --port 5191 --strictPort) &
fi
FRONTEND_PID=$!

trap "kill $BACKEND_PID $FRONTEND_PID 2>/dev/null" EXIT INT TERM

READY=0
for _ in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8031/api/health >/dev/null 2>&1 &&
     curl -fsS http://127.0.0.1:5191 >/dev/null 2>&1; then
    READY=1
    break
  fi
  sleep 1
done

if [ "$READY" -ne 1 ]; then
  echo "PoV não ficou pronta em 30 segundos. Verifique os logs acima."
  exit 1
fi

if [ "${POV_NO_OPEN:-0}" != "1" ]; then open http://127.0.0.1:5191; fi

wait
