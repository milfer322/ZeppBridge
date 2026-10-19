#!/bin/bash
# ==============================================================================
# rebuild.sh - Script de reconstrucción rápida e inicialización ZeppBridge Web
# ==============================================================================
set -euo pipefail

cd "$(dirname "$0")"

echo "======================================================"
echo "    Reconstruyendo ZeppBridge (Web Nativo Docker)     "
echo "======================================================"

if [ ! -f .env ]; then
    echo "[-] Creando archivo .env con puerto predeterminado 38480..."
    cat << 'EOF' > .env
ZEPP_PORT=38480
TZ=Europe/Madrid
EOF
fi

set -a
# shellcheck disable=SC1091
source .env
set +a
PORT="${ZEPP_PORT:-38480}"

TARGET_VERSION="${1:-3.0.2}"
TARGET_VERSION="${TARGET_VERSION#v}"

echo "[-] Deteniendo contenedores anteriores..."
docker compose down --remove-orphans 2>/dev/null || true
docker rm -f zeppbridge 2>/dev/null || true

echo "[-] Reconstruyendo contenedor para ZeppBridge v$TARGET_VERSION..."
docker compose build --build-arg ZEPPBRIDGE_VERSION="$TARGET_VERSION"

echo "[-] Levantando contenedor en el puerto $PORT..."
docker compose up -d

echo "[-] Comprobando estado del servicio..."
for i in {1..10}; do
    if curl -fs "http://localhost:$PORT/health" >/dev/null 2>&1 || curl -fs "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
        echo "[+] Backend respondiendo correctamente."
        break
    fi
    sleep 2
done

echo ""
echo "======================================================"
echo "     ¡ZeppBridge Web Nativo iniciado con éxito!       "
echo "======================================================"
echo ""
echo "Abre la aplicación en tu navegador:"
echo "  👉 http://localhost:$PORT"
echo ""
