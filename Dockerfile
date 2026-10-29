# ==============================================================================
# Stage 1: Compilar el Frontend oficial de ZeppBridge (Vue 3 + Vite)
# ==============================================================================
FROM node:22-bookworm-slim AS frontend-builder

ARG ZEPPBRIDGE_VERSION=3.1.0
ARG ZEPPBRIDGE_GIT_REF=v3.1.0

WORKDIR /src

# Instalar git para clonar el repositorio
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*

# Clonar el tag oficial del repositorio o rama principal
RUN git clone --depth 1 --branch "${ZEPPBRIDGE_GIT_REF}" https://github.com/lingcang728/ZeppBridge.git /src || \
    git clone --depth 1 https://github.com/lingcang728/ZeppBridge.git /src

# Asegurar carpeta public/ y copiar el conector web y parche de frontend
RUN mkdir -p /src/public
COPY web-ipc.js /src/public/web-ipc.js
COPY patch-frontend.cjs /src/patch-frontend.cjs

# Aplicar parche al frontend (inyección de IPC, salud cardíaca y menú Custom)
RUN node /src/patch-frontend.cjs

# Instalar dependencias y compilar bundle web optimizado
ENV ZEPPBRIDGE_BUILD_SHA="web-native"
RUN npm ci --ignore-scripts || npm install
RUN npx vite build


# ==============================================================================
# Stage 2: Runtime ligero con Python FastAPI + zeppbridge-cli oficial
# ==============================================================================
FROM python:3.11-slim-bookworm

LABEL maintainer="antigravity"
LABEL description="ZeppBridge Web Nativo - Frontend oficial desacoplado con FastAPI y CLI oficial"

ARG ZEPPBRIDGE_VERSION=3.1.0

WORKDIR /app

# Instalar dependencias mínimas del sistema y descargar zeppbridge-cli
RUN set -eux && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        unzip \
        libdbus-1-3 \
        tzdata \
        libsqlite3-0 \
        libssl3 && \
    # Descargar zeppbridge-tools oficial (CLI y MCP en Rust compilado)
    TEMP_DIR=$(mktemp -d) && \
    cd "$TEMP_DIR" && \
    (curl -fsSL -o tools.zip "https://github.com/lingcang728/ZeppBridge/releases/download/v${ZEPPBRIDGE_VERSION}/zeppbridge-tools-${ZEPPBRIDGE_VERSION}-Linux-x64.zip" || \
     curl -fsSL -o tools.zip "https://github.com/lingcang728/ZeppBridge/releases/download/v${ZEPPBRIDGE_VERSION}/zeppbridge-tools-${ZEPPBRIDGE_VERSION}-x86_64-unknown-linux-gnu.zip") && \
    unzip -q tools.zip -d /usr/local/bin/ && \
    chmod +x /usr/local/bin/zeppbridge-* && \
    rm -rf "$TEMP_DIR" && \
    apt-get purge -y unzip curl && \
    apt-get autoremove -y && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Instalar librerías de Python
COPY backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copiar código del servidor backend
COPY backend/app /app/app

# Copiar el frontend oficial compilado desde el Stage 1
COPY --from=frontend-builder /src/dist /app/static
COPY --from=frontend-builder /src/src/assets/devices/catalog.json /app/catalog.json

# Variables de entorno por defecto
ENV ZEPPBRIDGE_DATA_DIR=/data \
    ZEPPBRIDGE_CREDENTIAL_STORE=env \
    ZEPPBRIDGE_CLI_BIN=/usr/local/bin/zeppbridge-cli \
    PYTHONUNBUFFERED=1

# Volumen para persistencia de la base de datos zepp.db, auth.json y credenciales
VOLUME /data

# Puerto HTTP web
EXPOSE 8000

# Comando de inicio del servidor web nativo
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
