# Comparativa y Diferencias: ZeppBridge Web vs ZeppBridge Original ⌚🔄

Este documento describe detalladamente las diferencias arquitectónicas, de infraestructura, funcionales y de experiencia de usuario entre el proyecto original **[ZeppBridge](https://github.com/lingcang728/ZeppBridge)** (creado por `lingcang728`) y este fork **[ZeppBridge Web (Docker)](https://github.com/milfer322/ZeppBridge)**.

---

## 📊 Tabla Comparativa Resumida

| Característica / Módulo | ZeppBridge Original (Tauri Desktop) | ZeppBridge Web (Este Fork Docker) |
| :--- | :--- | :--- |
| **Plataforma de Ejecución** | Aplicación nativa de escritorio (Rust + Tauri v2) para Windows y macOS. | Contenedor Docker autónomo (FastAPI + Vue 3 / Vite) para servidores, NAS o mini PCs. |
| **Acceso del Usuario** | Requiere instalar y ejecutar el ejecutable `.exe` / `.dmg` en cada equipo cliente. | **100% en el Navegador Web (Web SPA)** desde cualquier PC, teléfono móvil o tablet. |
| **Seguridad y Control de Acceso** | Sin autenticación ni protección por contraseña (asume seguridad física en el equipo local monousuario). | **ZeppBridge Shield (2FA / TOTP):** Pantalla de login obligatoria para toda la web y APIs. Email + contraseña con hash seguro (PBKDF2-SHA256, 100k iteraciones) + 2FA con Google Authenticator, Authy, Apple Passwords o 1Password, limitador anti fuerza bruta y sesiones seguras de 30 días. |
| **Consumo de Recursos** | Requiere entorno gráfico de escritorio y memoria del cliente. | Servidor headless ultraligero (< 60 MB RAM en reposo) sin escritorio virtual (sin VNC ni Selkies). |
| **Capa de Comunicación IPC** | Invocación nativa Tauri `@tauri-apps/api/core` (`invoke()`). | Interceptada de forma transparente por `web-ipc.js` y traducida a endpoints REST de FastAPI. |
| **Sincronización en 2º Plano** | Solo sincroniza mientras la app de escritorio permanezca abierta en el PC. | **Servicio 24/7 desatendido:** Sincronizador automático en segundo plano en el servidor (por defecto cada 5 min). |
| **Bot de Telegram** | No disponible. | **Bot interactivo bidireccional:** Comandos `/hoy`, `/buenosdias`, `/sync`, `/pulso`, `/informe`, `/entrenamientos`. |
| **Informes de IA Programados** | Consultas manuales puntuales dentro de la app con proveedores locales o chinos. | **Motor Cron + Google Gemini:** Informes periódicos automáticos y **reporte matutino "Buenos días"** al despertar. |
| **Análisis de Apnea y Respiración** | Métricas básicas de calidad respiratoria durante el sueño si el dispositivo lo soporta, sin trazado visual de eventos. | **Módulo SomnusCare Integrado:** Gráfica continua de pulso nocturno minuto a minuto, detección automática de arousals/microdespertares ($\ge 8\text{ ppm}$ en ventana móvil), cálculo de desaturaciones (ODI) y leyenda explicativa de eventos. |
| **Formato de Horario** | Horario de 12 horas con AM / PM por defecto. | **Horario de 24 horas (00:00 - 23:59)** fijado por defecto en todo el sistema. |
| **Formato de Fechas** | Formato americano Mes/Día (MM/DD) en varias vistas y gráficas. | **Formato europeo/español Día/Mes (DD/MM)** aplicado en todas las gráficas, tablas y tooltips. |
| **Análisis Cardíaco Especializado** | Solo plantillas genéricas de sueño y workout. | **Nuevos intents de IA:** Módulos dedicados para 💓 Pulsaciones (7 días) y 📈 Cardio (30 días) evaluando HRV y RHR. |
| **Gestión de Respaldos (Backups)** | Base de datos SQLite guardada en carpetas del sistema operativo del usuario. | **Descarga de backup `.db` en 1 clic**, restauración directa desde la interfaz y exportador CSV para Excel. |
| **Planificador y Novedades Upstream** | Añadido progresivamente hasta v3.1.1 en upstream oficial. | **Totalmente preservado y sincronizado (v3.1.1):** Diseñador de intervalos, retrospectiva de anillos, diario deportivo, deportes ampliados (#143), carga TLB, migración SQLite 47 (`device_local_state`), ocultación de dispositivos y supresión de onboarding invasivo. |

---

## 🏗️ Diferencias de Arquitectura

### 1. ZeppBridge Original (Escritorio Tauri)

El proyecto oficial está diseñado como un cliente local monolítico de escritorio:

```mermaid
flowchart LR
    subgraph PC_Cliente["Ordenador del Usuario (Windows / macOS)"]
        UI["Interfaz Vue 3 / Vite (WebView)"]
        IPC["Tauri IPC invoke()"]
        Rust["Backend Rust / Tauri Runtime"]
        SQLite[("Base de Datos SQLite")]
        UI -->|Llamadas locales| IPC
        IPC --> Rust
        Rust --> SQLite
    end
    Rust -->|HTTPS| Cloud["Nube de Amazfit / Zepp (Huami)"]
```

* **Inconveniente:** Para mantener tus datos sincronizados, debes tener tu ordenador personal encendido con la aplicación abierta. No es accesible desde el móvil fuera de casa ni desde otros dispositivos de la red.

---

### 2. ZeppBridge Web (Arquitectura Headless Web + Docker)

Este fork desacopla la interfaz de usuario de las dependencias nativas del sistema operativo mediante una capa de emulación IPC transparente y un servidor backend ligero en Python:

```mermaid
flowchart TD
    subgraph Clientes["Cualquier Dispositivo (Móvil / Tablet / PC)"]
        Browser["Navegador Web (Chrome, Safari, Firefox)"]
        WebIPC["web-ipc.js (Emulador de Tauri invoke)"]
        Browser --> WebIPC
    end

    subgraph Servidor_Docker["Servidor Docker (NAS / Mini PC / Homelab)"]
        Shield["🛡️ ZeppBridge Shield (Auth Gate Middleware)"]
        FastAPI["Backend FastAPI (Python 3.11)"]
        CronEngine["Planificador Cron en Segundo Plano"]
        AutoSync["Auto-Sync Daemon (cada 5 min)"]
        TelegramService["Servicio de Telegram Bot"]
        GeminiService["Integración Google Gemini AI"]
        SomnusEngine["Motor SomnusCare (Apneas & HRV)"]
        DB[("Volumen Persistente: ./config/")]

        WebIPC -->|HTTP con Cookie de Sesión| Shield
        Shield -->|Sesión Válida / Rutas Auth| FastAPI
        FastAPI --> DB
        FastAPI --> SomnusEngine
        AutoSync --> DB
        CronEngine --> GeminiService
        CronEngine --> TelegramService
    end

    AutoSync -->|HTTPS| ZeppCloud["Nube Oficial de Zepp"]
    TelegramService <-->|API Telegram| TelegramApp["App Móvil de Telegram"]
    GeminiService -->|API Gemini| GoogleAI["Google Gemini AI"]
```

---

## 🔍 Detalle de Mejoras y Novedades Exclusivas

### 1. Emulador de IPC Web (`web-ipc.js`)
* En lugar de reescribir todo el código fuente del frontend oficial de ZeppBridge (lo que provocaría conflictos constantes al actualizar con el creador), se desarrolló un puente JavaScript `web-ipc.js`.
* Este archivo se inyecta en el frontend oficial e intercepta las llamadas `window.__TAURI__.core.invoke(command, payload)`.
* Las llamadas se traducen de forma instantánea a llamadas `fetch('/api/bridge/...')`, lo que permite que el frontend de ZeppBridge funcione en cualquier navegador web sin notar que ya no está dentro de Tauri.

### 2. Bot de Telegram Interactivo
* Puedes vincular un bot de Telegram con tu propio `token` y `chat_id`.
* El bot escucha mensajes de chat y permite:
  * `/hoy`: Resumen instantáneo de pasos, calorías, sueño y FC del día.
  * `/buenosdias`: Análisis matutino del último descanso nocturno.
  * `/sync`: Forzar una sincronización inmediata con los servidores de Zepp.
  * `/pulso`: Métricas de frecuencia cardíaca máxima, mínima y en reposo.
  * `/informe`: Solicitar un análisis clínico a Google Gemini en ese momento.

### 3. Informes Automáticos Programables (Cron + Gemini + Telegram)
* El usuario puede configurar tareas programadas con periodicidad diaria, semanal o mensual a horas específicas.
* A la hora configurada, el servidor recopila los datos biomédicos de la base de datos local SQLite, envía un prompt médico estructurado a **Google Gemini**, y envía el informe formateado directamente a tu canal de Telegram.
* **Informe Matutino "Buenos Días":** Un reporte automático diario (por defecto a las 08:30) que te despierta con una evaluación de la calidad de tu sueño, fases REM/profundo, pulso basal y un consejo motivacional para tu jornada deportiva.

### 4. Adaptación Horaria y Regional (24h y DD/MM)
* En la versión original de escritorio, las horas se muestran frecuentemente en formato americano de 12 horas con AM/PM y las fechas en formato Mes/Día (MM/DD).
* Este fork preconfigura y fija por defecto:
  * **Horario de 24 horas (00:00 a 23:59)** en toda la interfaz.
  * **Formato Día/Mes (DD/MM)** en todas las gráficas de Recharts (frecuencia cardíaca, pasos, sueño), tablas de actividades y modales de entrenamiento.

### 5. Copias de Seguridad y Portabilidad
* Menú de copia de seguridad con descarga directa de la base de datos SQLite `zepp.db`.
* Función para restaurar una base de datos directamente desde el navegador web.
* Exportación de entrenamientos y métricas diarias a formato `.csv` compatible con Excel y Google Sheets.

### 6. Métricas Reales en Estado de Entrenamiento (Training Load, Balance TLB y Zonas de FC)
* **Carga de Entrenamiento y Ratio Agudo:Crónico (TLB):** Zepp no registra la carga de entrenamiento en `daily_metrics`, sino por sesión en `workouts`. El fork calcula automáticamente el histórico móvil de fatiga (7 días), fitness (28 días) y ratio de carga para mostrar el estado de equilibrio de entrenamiento real.
* **Zonas de Frecuencia Cardíaca Dinámicas:** En la versión base las zonas de FC (`/training`) estaban fijadas con 0 segundos y límites estáticos. Este fork calcula los umbrales personalizados según tu frecuencia máxima real (Zonas 1 a 5) y computa el tiempo real invertido en cada zona a partir de las muestras de entrenamiento (`workout_samples`) y registros de pulso.
* **Compatibilidad Universal con Portapapeles:** Polyfill para copia segura en entornos web HTTP / no-HTTPS, evitando errores de `writeText`.

### 7. ZeppBridge Shield: Control de Acceso y Doble Factor (2FA / TOTP)
* **Blindaje integral de la aplicación:** A diferencia de la versión de escritorio oficial concebida para un único usuario físico local, ZeppBridge Web opera en la red local o internet. Un middleware interceptor de seguridad (`auth_security_gate`) bloquea cualquier petición a la interfaz SPA y a las rutas `/api/bridge/*` si no existe una sesión autenticada.
* **Primer Acceso Autoguiado:**
  1. El usuario define su correo electrónico y contraseña maestra en la pantalla de bienvenida.
  2. El sistema genera un secreto TOTP estándar (RFC 6238) y muestra un **código QR en alta resolución (PNG)** generado con biblioteca `segno` con zona de silencio ISO/IEC 18004, compatible al 100% con cualquier app de autenticación (**Google Authenticator, Authy, Apple Passwords / iOS, 1Password o Microsoft Authenticator**).
  3. Ofrece enlace directo `otpauth://` para abrir la app de autenticación en un clic desde el móvil y clave manual formateada en bloques de 4 caracteres legibles con botón de copia.
  4. Valida el código de 6 dígitos antes de crear y sellar la cuenta.
* **Criptografía Robusta y Mitigación de Fuerza Bruta:**
  * Hasheo de contraseñas mediante **PBKDF2-HMAC-SHA256 con 100.000 iteraciones** y salt criptográfico único por instalación.
  * Tokens de sesión firmados criptográficamente con validez de 30 días, transmitidos en cookies `HttpOnly` y `SameSite=Lax`.
  * Limitador de intentos por dirección IP (bloqueo automático tras 5 intentos fallidos consecutivos durante 15 minutos).
  * Botón de **Cerrar Sesión (Logout)** ubicado en *Ajustes ➔ Personalizaciones (Docker)* con redirección inmediata al login.

### 8. SomnusCare: Detección y Análisis de Apneas del Sueño
* **Monitorización Cardiorrespiratoria Nocturna:** Cruza los datos de las fases de sueño (profundo, ligero, REM, despierto) con los registros de pulso minuto a minuto (`heart_rate_samples`) y saturación de oxígeno (`spo2`).
* **Detección Automática de Microdespertares y Arousals:** Analiza la curva continua de frecuencia cardíaca nocturna detectando taquicardias transitorias repentinas ($\ge 8\text{ ppm}$ de incremento sobre la media móvil basal), indicativas de esfuerzos respiratorios o eventos obstructivos/hipopneas.
* **Visualización Dinámica:** Gráfica interactiva integrada en cada tarjeta de sesión de sueño con marcadores de eventos, cálculo del Índice de Desaturación de Oxígeno (ODI) estimado y leyenda médica explicativa.

### 9. Experiencia Web Optimizada y Supresión de Onboarding
* **Supresión Permanente de Tutoriales Invasivos:** En las versiones oficiales de ZeppBridge, cada actualización de versión o limpieza de estado local provocaba la reaparición repetitiva del tutorial emergente guiado (onboarding tour) en cada pantalla. Este fork inyecta el flag de finalización en el estado reactivo del frontend (`app-data-store`), manteniendo una experiencia limpia y sin interrupciones.

---

## 🔄 Compatibilidad con el Proyecto Upstream

Este fork mantiene una sincronización estricta con las versiones oficiales de `lingcang728/ZeppBridge`:
* **Versión Base Sincronizada:** **v3.1.1** (alineado con la última release oficial y rama `main` upstream, incorporando la migración SQLite 47 `device_local_state`, soporte para ocultar dispositivos, sincronización selectiva, recuperación de credenciales oficiales, deportes adicionales #143, canal avanzado de distancias, retrospectiva anual de anillos y diario de actividad).
* Los parches se aplican de forma no destructiva mediante `patch-frontend.cjs` durante el proceso de construcción de la imagen de Docker, lo que garantiza que cualquier corrección o nueva función añadida por el autor original pueda incorporarse sin romper la funcionalidad web ni las personalizaciones añadidas.
