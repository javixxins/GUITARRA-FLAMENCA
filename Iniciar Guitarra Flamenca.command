#!/bin/bash
# Arranca el servidor de Guitarra Flamenca y abre la app en el navegador.
# Usa el primer puerto libre entre 8850 y 8859.
cd "$(dirname "$0")" || exit 1

if [ ! -x .venv/bin/python ]; then
  echo "==> Primera vez: instalando…"
  ./instalar.command
  exit 0
fi

# ¿ya está en marcha?
for P in $(seq 8850 8859); do
  if curl -s -m 1 "http://127.0.0.1:$P/api/config" | grep -q '"app": *"guitarra-flamenca"'; then
    echo "==> Guitarra Flamenca ya estaba en marcha (puerto $P)."
    open "http://127.0.0.1:$P"; exit 0
  fi
done

rm -f .port
mkdir -p logs
echo "==> Arrancando Guitarra Flamenca…"
nohup .venv/bin/python -W ignore server.py --no-browser > logs/server.log 2>&1 &

for i in $(seq 1 120); do
  if [ -f .port ] && curl -s -m 1 "http://127.0.0.1:$(cat .port)/api/config" >/dev/null; then
    echo "==> Lista en http://127.0.0.1:$(cat .port)"
    open "http://127.0.0.1:$(cat .port)"
    echo ""
    echo "Puedes cerrar esta ventana: la app sigue funcionando en segundo plano."
    exit 0
  fi
  sleep 0.5
done
echo "❌ No ha arrancado. Esto dice el registro:"
tail -20 logs/server.log
