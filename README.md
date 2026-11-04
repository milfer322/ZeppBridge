# ZeppBridge Web (Docker) ⌚🌐

> **Fork autónomo de [ZeppBridge](https://github.com/lingcang728/ZeppBridge) (actualizado y sincronizado a la última versión oficial v3.1.1) adaptado para funcionar 100% a través del navegador web, sin necesidad de instalar la aplicación de escritorio y completamente blindado con autenticación de doble factor (2FA / TOTP).**

Permite alojar tu propio servidor personal de sincronización y análisis de salud de **Amazfit / Zepp** en cualquier máquina con Docker (servidor local, NAS Unraid, Synology, Raspberry Pi, VPS o mini PC) y acceder a toda tu información deportiva y biomédica desde cualquier dispositivo (ordenador, móvil o tablet) simplemente abriendo una URL en tu navegador web.

---

## 🌟 ¿Qué es este Fork y qué ventajas ofrece?

El proyecto original de ZeppBridge es una aplicación de escritorio basada en Tauri/Rust. Este fork desacopla la interfaz de usuario oficial y el motor de sincronización para convertirlos en una **aplicación web moderna y autónoma (Web SPA)**:

* 🛡️ **ZeppBridge Shield · Seguridad Total con Login y 2FA (TOTP):** Toda la aplicación web, sus gráficas y sus APIs están blindadas. En el primer acceso, configuras tu usuario (email) y contraseña, y vinculas tu app de autenticación (**Google Authenticator, Authy, Apple Passwords / iOS, 1Password, Microsoft Authenticator**) mediante un código QR en alta resolución, botón de enlace directo para móvil o clave manual en bloques legibles. Nadie en tu red local ni en Internet puede acceder a tus datos de salud sin tu contraseña y el código de 6 dígitos generado en tu móvil. Incluye persistencia de sesiones seguras (`HttpOnly` / `SameSite=Lax`) para 30 días y botón de *Cerrar Sesión* (*Logout*).
* 🌐 **100% en el Navegador Web (Sin App de Escritorio):** No necesitas instalar nada en tus clientes. Toda la experiencia funciona en Chrome, Safari, Firefox o Edge en PC, móvil o tablet.
* ⚡ **Rendimiento Ultrarrápido y Mínimo Consumo:** En lugar de soluciones pesadas de escritorio virtual por streaming (como Selkies o VNC que devoran CPU y ancho de banda), el frontend oficial se ejecuta directamente en el navegador del usuario y el servidor corre con un backend FastAPI ultra ligero (< 60 MB RAM en reposo).
* 🫁 **Análisis SomnusCare de Respiración y Apnea Nocturna:** Módulo especializado integrado en cada sesión de sueño con gráfica continua minuto a minuto de frecuencia cardíaca nocturna, resaltando automáticamente picos de aceleración cardíaca ($\ge 8\text{ ppm}$, microdespertares/arousals) e índice de desaturación de oxígeno (ODI) nocturno con tooltips detallados.
* 🕒 **Horario 24 Horas y Formato Europeo (DD/MM) por Defecto:** Configurado para mostrar las horas en formato 24h y las fechas en Día/Mes en todas las gráficas, métricas, tooltips y tablas, evitando confusiones con el formato americano.
* 🏃 **Planificador de Entrenamientos y Actividades GPS al Frente:**
  * **Planificador Semanal (`/ai/plan`):** Diseñador visual de sesiones por pasos e intervalos (series, calentamiento, ritmos, zonas de frecuencia cardíaca Z1-Z5 y enfriamiento) sincronizable directamente con tu reloj Amazfit.
  * **Historial GPS (`/workouts`):** Mapas de rutas, desniveles, vueltas automáticas (*splits* por km) y cadencia.
  * **Carga de Entrenamiento (`/training`):** Métricas de Training Load Balance (TLB), VO2 Máx y tiempo de recuperación.
* 🏷️ **Gestor de Códigos de Deportes:** Permite asignar etiquetas y nombres personalizados a cualquier deporte que registre el reloj.
* 🤖 **Bot de Telegram Interactivo:** Habla con tu servidor de ZeppBridge directamente desde Telegram en tu móvil:
  * `/hoy`: Resumen instantáneo de actividad y descanso del día.
  * `/buenosdias`: Análisis del descanso nocturno recién despertado.
  * `/sync`: Lanza una sincronización inmediata con la nube de Zepp.
  * `/pulso`: Métricas de frecuencia cardíaca y descanso.
  * `/informe`: Análisis clínico profundo con Google Gemini.
* 📋 **Informes Automatizados Programables (Cron + Gemini + Telegram):** Programa informes recurrentes (diarios, semanales o mensuales) para que la IA de Gemini analice tus datos biomédicos y te los envíe formateados a tu chat de Telegram.
* ☀️ **Informe Matutino Automático:** Recibe cada mañana un resumen motivacional de cómo ha ido tu sueño antes de levantarte.
* 🔄 **Auto-Sync en Segundo Plano:** Sincronización incremental periódica configurable (por defecto cada 5 minutos).
* 💾 **Respaldos de Base de Datos y Exportación CSV:** Copia de seguridad en 1 clic de tu base de datos SQLite y exportación de sesiones a formato CSV.
* 🚀 **Actualizado a ZeppBridge v3.1.1 Oficial:** Totalmente alineado con la última versión upstream, incluyendo la migración SQLite 47 (`device_local_state`), soporte para ocultar dispositivos, sincronización selectiva, recuperación de credenciales y supresión permanente de tutoriales emergentes invasivos tras cada actualización.
* ✨ **Módulo Integrado de Diferencias:** Panel visual en Configuración y documentación completa para consultar en cualquier momento todas las diferencias arquitectónicas y funcionales respecto al ZeppBridge oficial.

> 📖 **Comparativa Detallada:** Consulta la guía **[DIFFERENCES.md](file:///a:/dockers_customs/zeppbridge/DIFFERENCES.md)** para ver la tabla comparativa exhaustiva, diagramas de arquitectura y desglose de novedades frente a la app de escritorio original.

---

## 🚀 Inicio Rápido con Docker

### 1. Clonar el repositorio
```bash
git clone https://github.com/milfer322/ZeppBridge.git
cd ZeppBridge
```

### 2. Configurar variables de entorno (Opcional)
Copia la plantilla de entorno:
```bash
cp .env.example .env
```
*(Puedes editar `.env` para cambiar el puerto de acceso si lo deseas).*

### 3. Iniciar con Docker Compose
```bash
docker compose up -d --build
```

O si utilizas Linux / macOS:
```bash
chmod +x rebuild.sh
./rebuild.sh
```

### 4. Abrir en tu navegador y configurar tu 2FA
Accede desde tu navegador web a:
👉 **`http://<IP-DE-TU-SERVIDOR>:27480`** (o `http://localhost:27480`)

Al entrar por primera vez, el asistente te pedirá:
1. Tu **Correo electrónico** (que actuará como usuario) y tu **Contraseña**.
2. Te mostrará el **código QR en alta resolución** para escanear con tu aplicación Authenticator (Google Authenticator, Authy, Apple Passwords, 1Password) o un botón para abrirlo directamente si estás en el móvil.
3. Introduce el código de 6 dígitos para verificar la vinculación ¡y listo!

---

## 📁 Persistencia de Datos

Toda la información personal (base de datos SQLite `zepp.db`, configuración de seguridad y 2FA `auth_security.json`, tokens de acceso oficiales y configuraciones de informes) se guarda en la carpeta local `./config`, que se monta automáticamente en el contenedor:

```text
.
├── backend/               # Servidor FastAPI, seguridad 2FA y API bridge
├── Dockerfile             # Multi-stage build (Vue 3/Vite + Runtime Python)
├── docker-compose.yml     # Despliegue con Docker Compose
├── patch-frontend.cjs     # Parches de integración para el frontend web
├── web-ipc.js             # Emulador de IPC para el navegador
├── config/                # [IGNORADO EN GIT] Base de datos persistente (zepp.db) y 2FA
└── README.md
```

---

## 🔒 Privacidad y Seguridad

> [!NOTE]
> **Protección Integral con ZeppBridge Shield (2FA TOTP):** A diferencia de la aplicación de escritorio original que no disponía de control de acceso web, este fork incorpora un sistema de autenticación completo y obligatorio:
> - **Autenticación en 2 Pasos obligatoria:** Email + Contraseña (hasheada con PBKDF2-HMAC-SHA256, 100.000 iteraciones y salt aleatorio único) + Código 2FA TOTP (RFC 6238).
> - **Compatible con todas las apps de Authenticator:** Google Authenticator, Authy, Contraseñas de Apple / iOS, 1Password y Microsoft Authenticator.
> - **Protección contra fuerza bruta:** Detección de abusos y bloqueo automático por IP tras 5 intentos fallidos consecutivos.
> - **Sesiones persistentes y seguras:** Soporte de 30 días con cookies `HttpOnly` y `SameSite=Lax`, con botón de *Cerrar Sesión* (*Logout*) en Ajustes ➔ Personalizaciones.

* **Tus datos nunca salen de tu red:** La base de datos SQLite `zepp.db`, las credenciales y las claves secretas 2FA se guardan exclusivamente en tu servidor (`./config`).
* **Sin telemetría oculta:** No se envían datos a servidores de terceros que no hayas configurado explícitamente (como tu propio bot de Telegram o tu propia clave de Google Gemini para los informes).
* **Fácil de respaldar:** Basta con guardar la carpeta `./config` para tener un duplicado exacto de todo tu historial, configuraciones y usuarios autorizados.

---

## ⚖️ Licencia y Reconocimientos

Este proyecto es un fork y extensión para la web de **[ZeppBridge](https://github.com/lingcang728/ZeppBridge)** por `lingcang728`.
Se encuentra actualizado con la última versión de su creador original (**v3.1.1** y rama `main`, incorporando soporte de deportes adicionales #143, canal avanzado de distancias, retrospectiva de anillos anuales, diario deportivo, planificador de entrenamientos y migración de estado de dispositivos).
Distribuido bajo la misma licencia de código abierto que el proyecto original.
