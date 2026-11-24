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

TARGET_VERSION="${1:-3.3.0}"
TARGET_VERSION="${TARGET_VERSION#v}"
GIT_REF="${2:-}"
if [ -z "$GIT_REF" ]; then
    if [ "$TARGET_VERSION" = "3.3.0" ]; then
        GIT_REF="main"
    else
        GIT_REF="v$TARGET_VERSION"
    fi
fi

echo "[-] Deteniendo contenedores anteriores..."
docker compose down --remove-orphans 2>/dev/null || true
docker rm -f zeppbridge 2>/dev/null || true

echo "[-] Reconstruyendo contenedor para ZeppBridge v$TARGET_VERSION (ref: $GIT_REF)..."
docker compose build --no-cache --build-arg ZEPPBRIDGE_VERSION="$TARGET_VERSION" --build-arg ZEPPBRIDGE_GIT_REF="$GIT_REF"

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
echo "  ¡Listo! ZeppBridge está activo en el puerto $PORT"
echo "  URL de acceso: http://<IP-DE-TU-SERVIDOR>:$PORT"
echo "======================================================"
