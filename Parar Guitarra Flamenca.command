#!/bin/bash
# Detiene el servidor de Guitarra Flamenca.
cd "$(dirname "$0")" || exit 1
if [ -f .port ] && curl -sf -m 2 -X POST "http://127.0.0.1:$(cat .port)/api/quit" >/dev/null; then
  rm -f .pid .port; echo "==> Guitarra Flamenca parada."
elif [ -f .pid ] && kill "$(cat .pid)" 2>/dev/null; then
  rm -f .pid .port; echo "==> Guitarra Flamenca parada."
else
  echo "==> No estaba en marcha."
fi
