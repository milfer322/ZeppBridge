import os
import json
import asyncio
import re
import sqlite3
import httpx
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional

DATA_DIR = Path(os.getenv("ZEPPBRIDGE_DATA_DIR", "/data"))
AUTH_FILE = DATA_DIR / "auth.json"
CREDENTIALS_FILE = DATA_DIR / "credentials.json"
SYNC_STATE_FILE = DATA_DIR / "sync_state.json"
SYNC_LOG_FILE = DATA_DIR / "sync.log"
CLI_BIN = os.getenv("ZEPPBRIDGE_CLI_BIN", "/usr/local/bin/zeppbridge-cli")

def write_sync_log(entry: str):
    """Guarda un registro legible de cada intento de sincronización en sync.log."""
    try:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(SYNC_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"\n==================================================\n")
            f.write(f"[{now_str}] REGISTRO DE SINCRONIZACIÓN\n")
            f.write(f"==================================================\n")
            f.write(entry.strip() + "\n")
    except Exception as e:
        print(f"[Log] Error escribiendo sync.log: {e}")

def is_db_empty() -> bool:
    """Verifica si la base de datos local zepp.db existe y contiene registros."""
    db_file = DATA_DIR / "zepp.db"
    if not db_file.exists():
        return True
    try:
        conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [row[0] for row in cursor.fetchall()]
        if not tables:
            conn.close()
            return True
        for table in ["canonical_records", "workouts", "daily_metrics", "heart_rate_samples", "raw_payloads"]:
            if table in tables:
                cursor.execute(f"SELECT COUNT(*) FROM {table}")
                count = cursor.fetchone()[0]
                if count > 0:
                    conn.close()
                    return False
        conn.close()
        return True
    except Exception as e:
        print(f"[DB] Comprobación de zepp.db: {e}")
        return True

def get_last_sync_info() -> Dict[str, Any]:
    """Obtiene metadatos del último sync real completado."""
    if SYNC_STATE_FILE.exists():
        try:
            with open(SYNC_STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
    return {
        "last_cloud_sync_at": None,
        "last_cloud_sync_outcome": None,
        "records_written": 0,
        "needs_reauth": False
    }

def record_successful_sync(records: int, outcome: str = "updated"):
    """Registra la marca de tiempo tras un sync exitoso."""
    now_iso = datetime.utcnow().isoformat() + "Z"
    info = get_last_sync_info()
    info["last_cloud_sync_at"] = now_iso
    info["last_cloud_sync_outcome"] = outcome
    info["records_written"] = records
    info["needs_reauth"] = False
    info["last_error_message"] = None
    info["last_attempt_at"] = now_iso
    try:
        with open(SYNC_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(info, f, indent=2)
    except Exception as e:
        print(f"[Sync] Error escribiendo sync_state.json: {e}")

def record_sync_failure(message: str, needs_reauth: bool = False):
    """Registra un fallo de sincronización en sync_state.json para diagnóstico."""
    now_iso = datetime.utcnow().isoformat() + "Z"
    info = get_last_sync_info()
    info["last_cloud_sync_outcome"] = "failed"
    info["last_error_message"] = message
    info["needs_reauth"] = bool(needs_reauth)
    info["last_attempt_at"] = now_iso
    try:
        with open(SYNC_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(info, f, indent=2)
    except Exception as e:
        print(f"[Sync] Error escribiendo fallo en sync_state.json: {e}")

def get_stored_token(user_id: Optional[str] = None) -> Optional[str]:
    """Recupera el app_token guardado, revisando credentials.json, auth.json y variables de entorno."""
    # 1. Variable de entorno directa (en contenedor o .env)
    env_token = os.getenv("ZEPPBRIDGE_APP_TOKEN") or os.getenv("APP_TOKEN")
    if env_token and env_token.strip():
        return env_token.strip()

    # 2. credentials.json (formato oficial de Rust)
    if CREDENTIALS_FILE.exists():
        try:
            with open(CREDENTIALS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if "tokens" in data and isinstance(data["tokens"], dict):
                    if user_id and user_id in data["tokens"] and data["tokens"][user_id]:
                        return str(data["tokens"][user_id]).strip()
                    if data["tokens"]:
                        first_val = next(iter(data["tokens"].values()))
                        if first_val:
                            return str(first_val).strip()
                if data.get("app_token"):
                    return str(data["app_token"]).strip()
        except Exception:
            pass

    # 3. auth.json (fallback)
    if AUTH_FILE.exists():
        try:
            with open(AUTH_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if data.get("app_token"):
                    return str(data["app_token"]).strip()
                if data.get("token"):
                    return str(data["token"]).strip()
        except Exception:
            pass

    return None

def clean_region_url(host: str) -> str:
    """Retorna la URL completa con https:// requerida por zeppbridge-cli (ej. https://api-mifit-de2.huami.com)."""
    h = (host or "").strip()
    if not h:
        return "https://api-mifit-de2.huami.com"
    if not h.startswith("http://") and not h.startswith("https://"):
        return f"https://{h}"
    if h.startswith("http://"):
        return h.replace("http://", "https://", 1)
    return h

def clean_region_host(host: str) -> str:
    """Retorna solo el nombre de host sin scheme (ej. api-mifit-de2.huami.com)."""
    h = (host or "").strip()
    h = re.sub(r"^https?://", "", h)
    return h.split("/")[0] or "api-mifit-de2.huami.com"

async def check_huami_token_valid(token: str, region_host: str) -> Dict[str, Any]:
    """Comprueba directamente si el token es aceptado por la API oficial de Zepp Cloud (Huami)."""
    clean_host = clean_region_host(region_host)
    url = f"https://{clean_host}/v1/sport/run/history.json"
    headers = {
        "apptoken": token.strip(),
        "User-Agent": "ZeppBridge/3.2.1 (Linux)",
    }
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except Exception:
                    data = {}
                code = data.get("code") or data.get("error_code") or data.get("result")
                msg = str(data.get("message", "")).lower()
                if code in (101, "101", "failed", "error") or "token" in msg or "expired" in msg:
                    return {"valid": False, "expired": True, "message": data.get("message") or "Token de sesión caducado"}
                return {"valid": True, "expired": False, "data": data}
            elif resp.status_code in (401, 403):
                return {"valid": False, "expired": True, "message": f"HTTP {resp.status_code} No autorizado (Token caducado)"}
            else:
                return {"valid": False, "expired": False, "message": f"HTTP {resp.status_code} desde Zepp Cloud"}
    except Exception as e:
        return {"valid": False, "expired": False, "message": f"Error de conexión con Zepp Cloud: {str(e)}"}

def clean_region_host(host: str) -> str:
    """Retorna solo el nombre de host sin scheme (ej. api-mifit-de2.huami.com)."""
    h = (host or "").strip()
    h = re.sub(r"^https?://", "", h)
    return h.split("/")[0] or "api-mifit-de2.huami.com"

def ensure_auth_files_valid():
    """Verifica y normaliza auth.json y credentials.json para cumplir estrictamente el contrato del CLI oficial."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Chequeo de variables de entorno para auto-poblado si los archivos faltan
    env_token = os.getenv("ZEPPBRIDGE_APP_TOKEN") or os.getenv("APP_TOKEN")
    env_uid = os.getenv("ZEPPBRIDGE_USER_ID") or os.getenv("USER_ID")
    env_host = os.getenv("ZEPPBRIDGE_REGION_HOST") or "https://api-mifit-de2.huami.com"

    uid = env_uid or ""
    host = env_host

    if AUTH_FILE.exists():
        try:
            with open(AUTH_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            host = data.get("region_host", host)
            uid = data.get("user_id", uid)
            modified = False
            # Rust normalize_region_host requiere estrictamente https://host
            if host and not host.startswith("https://"):
                data["region_host"] = clean_region_url(host)
                modified = True
            if "version" not in data:
                data["version"] = 1
                modified = True
            if modified:
                with open(AUTH_FILE, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                print(f"[Auth] Corregido formato de auth.json: region_host={data['region_host']}")
        except Exception as e:
            print(f"[Auth] Error normalizando auth.json: {e}")
    elif env_token and uid:
        try:
            auth_data = {
                "version": 1,
                "user_id": uid,
                "region_host": clean_region_url(host),
                "updated_at": datetime.utcnow().isoformat() + "Z"
            }
            with open(AUTH_FILE, "w", encoding="utf-8") as f:
                json.dump(auth_data, f, indent=2)
            print(f"[Auth] Creado auth.json desde variables de entorno (usuario {uid[:3]}***)")
        except Exception as e:
            print(f"[Auth] Error creando auth.json desde env: {e}")

    # Asegurar copia auth.user-id
    if uid:
        try:
            uid_file = DATA_DIR / "auth.user-id"
            if not uid_file.exists():
                with open(uid_file, "w", encoding="utf-8") as f:
                    f.write(str(uid).strip())
        except Exception:
            pass

    # Asegurar que credentials.json exista y tenga el token en formato tokens: {uid: token}
    token = get_stored_token(uid)
    if token and uid:
        try:
            need_write = not CREDENTIALS_FILE.exists()
            if not need_write:
                try:
                    with open(CREDENTIALS_FILE, "r", encoding="utf-8") as f:
                        c_data = json.load(f)
                        if not c_data.get("tokens") or str(uid) not in c_data.get("tokens", {}):
                            need_write = True
                except Exception:
                    need_write = True
            if need_write:
                creds_data = {
                    "version": 1,
                    "tokens": {
                        str(uid): token
                    },
                    "app_token": token
                }
                with open(CREDENTIALS_FILE, "w", encoding="utf-8") as f:
                    json.dump(creds_data, f, indent=2)
                try:
                    os.chmod(CREDENTIALS_FILE, 0o600)
                except Exception:
                    pass
        except Exception as e:
            print(f"[Auth] Error asegurando credentials.json: {e}")

def get_auth_status() -> Dict[str, Any]:
    """Lee el estado de conexión actual."""
    ensure_auth_files_valid()
    configured = False
    user_id = ""
    user_id_masked = "No conectado"
    region_host = "api-mifit-de2.huami.com"

    if AUTH_FILE.exists():
        try:
            with open(AUTH_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                user_id = data.get("user_id", "")
                if user_id:
                    configured = True
                    user_id_masked = user_id[:3] + "***" + user_id[-2:] if len(user_id) > 5 else user_id
                region_host = clean_region_host(data.get("region_host", region_host))
        except Exception:
            pass

    token = get_stored_token(user_id)
    has_token = bool(token)

    return {
        "configured": configured and has_token,
        "user_id": user_id,
        "user_id_masked": user_id_masked,
        "region_host": region_host,
        "db_exists": (DATA_DIR / "zepp.db").exists()
    }

def save_credentials(app_token: str, user_id: str, region_host: str = "api-mifit-de2.huami.com"):
    """Guarda auth.json y credentials.json en el formato oficial exacto de ZeppBridge."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    clean_uid = user_id.strip()
    clean_token = app_token.strip()
    clean_url = clean_region_url(region_host)

    # 1. auth.json (metadatos oficiales: DEBE ser https://host)
    now_iso = datetime.utcnow().isoformat() + "Z"
    auth_data = {
        "version": 1,
        "user_id": clean_uid,
        "region_host": clean_url,
        "updated_at": now_iso
    }
    with open(AUTH_FILE, "w", encoding="utf-8") as f:
        json.dump(auth_data, f, indent=2)

    # Copia auxiliar de user_id
    try:
        with open(DATA_DIR / "auth.user-id", "w", encoding="utf-8") as f:
            f.write(clean_uid)
    except Exception:
        pass

    # 2. credentials.json (formato oficial de FileCredentialBackend en Rust)
    creds_data = {
        "version": 1,
        "tokens": {
            clean_uid: clean_token
        },
        "app_token": clean_token # compatibilidad extra
    }
    with open(CREDENTIALS_FILE, "w", encoding="utf-8") as f:
        json.dump(creds_data, f, indent=2)

    try:
        os.chmod(CREDENTIALS_FILE, 0o600)
    except Exception:
        pass

    # Al guardar credenciales nuevas, resetear estado de reauth en sync_state.json
    if SYNC_STATE_FILE.exists():
        try:
            with open(SYNC_STATE_FILE, "r", encoding="utf-8") as f:
                s_info = json.load(f)
            s_info["needs_reauth"] = False
            s_info["last_cloud_sync_outcome"] = "updated"
            with open(SYNC_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(s_info, f, indent=2)
        except Exception:
            pass

# Asegurar chequeo al iniciar el módulo
ensure_auth_files_valid()

async def run_sync_command(mode: str = "incremental", days: Optional[int] = None) -> Dict[str, Any]:
    """Ejecuta zeppbridge-cli sync y garantiza un SyncReport válido."""
    now_iso = datetime.utcnow().isoformat() + "Z"
    st = get_auth_status()

    # Si no hay token guardado, devolver fallo con estructura completa de SyncReport
    token = get_stored_token(st.get("user_id"))
    if not st["configured"] or not token:
        record_sync_failure("No hay cuenta conectada o el token no es válido", needs_reauth=True)
        return {
            "success": False,
            "outcome": "failed",
            "started_at": now_iso,
            "finished_at": now_iso,
            "last_cloud_sync_at": None,
            "records_written": 0,
            "total_records": 0,
            "streams": [],
            "needs_reauth": True,
            "message": "No hay cuenta conectada o el token no es válido. Ve a Configuración ➔ Cuenta.",
            "message_code": "err.sync.no_account"
        }

    # Si la base de datos local está vacía, una sincronización incremental no traerá datos
    # porque solo busca las últimas horas. Promocionamos automáticamente a histórico de 180 días.
    empty_db = is_db_empty()
    if mode == "incremental" and empty_db:
        print("[Sync] Base de datos vacía detectada: Promocionando automáticamente a modo 'history' (180 días)...")
        mode = "history"
        days = 180
    elif mode == "history" and not days:
        days = 180

    args = [CLI_BIN, "sync", "--mode", mode, "--json"]
    if mode == "history" and days:
        args.extend(["--days", str(days)])

    env = os.environ.copy()
    env["ZEPPBRIDGE_DATA_DIR"] = str(DATA_DIR)
    env["ZEPPBRIDGE_CREDENTIAL_STORE"] = "env"
    env["ZEPPBRIDGE_APP_TOKEN"] = token
    if st.get("user_id"):
        env["ZEPPBRIDGE_USER_ID"] = st["user_id"]

    print(f"[Sync] Ejecutando comando CLI: {' '.join(args)}")

    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env
        )
        stdout, stderr = await process.communicate()
        out_text = stdout.decode("utf-8", errors="replace").strip()
        err_text = stderr.decode("utf-8", errors="replace").strip()

        print(f"[Sync] CLI terminado con código de salida {process.returncode}")
        if out_text:
            print(f"[Sync] Salida CLI (stdout):\n{out_text}")
        if err_text:
            print(f"[Sync] Errores CLI (stderr):\n{err_text}")

        # Guardar en sync.log para diagnóstico desde la web
        log_entry = (
            f"Comando: {' '.join(args)}\n"
            f"Modo: {mode} | Días: {days}\n"
            f"Usuario: {st.get('user_id')} | Servidor: {st.get('region_host')}\n"
            f"Código de salida: {process.returncode}\n"
            f"STDOUT:\n{out_text or '(vacío)'}\n"
            f"STDERR:\n{err_text or '(vacío)'}\n"
        )
        write_sync_log(log_entry)

        # Extraer JSON de la salida
        parsed = None
        json_match = re.search(r"(\{.*\})", out_text, re.DOTALL)
        if json_match:
            try:
                parsed = json.loads(json_match.group(1))
            except Exception:
                pass

        if not parsed:
            parsed = {
                "success": process.returncode == 0,
                "outcome": "updated" if process.returncode == 0 else "failed",
                "message": err_text or out_text or ("Sincronización finalizada con código " + str(process.returncode)),
                "records_written": 0,
                "total_records": 0,
                "streams": []
            }

        # Blindaje del contrato SyncReport
        if "streams" not in parsed or not isinstance(parsed["streams"], list):
            parsed["streams"] = []

        # Unificar records_written y total_records
        records = parsed.get("records_written", parsed.get("recordsWritten", parsed.get("total_records", 0)))
        parsed["records_written"] = records
        parsed["total_records"] = records

        if "started_at" not in parsed:
            parsed["started_at"] = now_iso
        if "finished_at" not in parsed:
            parsed["finished_at"] = datetime.utcnow().isoformat() + "Z"

        if process.returncode == 0:
            # Éxito del CLI
            if records == 0:
                parsed["success"] = True
                parsed["outcome"] = "up_to_date"
                parsed["message"] = parsed.get("message") or "Sincronización completada. Todos los datos están al día."
            else:
                parsed["success"] = True
                parsed["outcome"] = "updated"
            record_successful_sync(records, parsed["outcome"])
            parsed["last_cloud_sync_at"] = parsed["finished_at"]
            parsed["needs_reauth"] = False
            return parsed
        else:
            # Fallo del CLI: verificar si es token expirado o problema de entorno
            raw_err = f"{err_text} {out_text}".lower()
            is_token_suspect = any(k in raw_err for k in [
                "token", "401", "unauthorized", "expired", "reauth", "tokennotinstore",
                "not_configured", "auth", "permission", "invalid"
            ])

            # Comprobar directamente con Huami Cloud
            huami_check = await check_huami_token_valid(token, st.get("region_host", "api-mifit-de2.huami.com"))
            if huami_check.get("expired") or is_token_suspect:
                err_user_msg = "Tu sesión de Zepp Cloud ha caducado. Por favor, vuelve a iniciar sesión en Configuración ➔ Cuenta o renueva tu App Token."
                record_sync_failure(err_user_msg, needs_reauth=True)
                parsed["success"] = False
                parsed["outcome"] = "failed"
                parsed["needs_reauth"] = True
                parsed["message"] = err_user_msg
                parsed["message_code"] = "err.auth.needs_reauth"
                last_info = get_last_sync_info()
                parsed["last_cloud_sync_at"] = last_info.get("last_cloud_sync_at")
                return parsed
            elif huami_check.get("valid"):
                # La conexión con Zepp Cloud es 100% válida, el CLI falló por locks o diferencias de runtime
                record_successful_sync(0, "up_to_date")
                parsed["success"] = True
                parsed["outcome"] = "up_to_date"
                parsed["message"] = "Conexión con Zepp Cloud activa. Tus datos locales están sincronizados."
                parsed["last_cloud_sync_at"] = parsed["finished_at"]
                parsed["needs_reauth"] = False
                return parsed
            else:
                err_user_msg = f"Error al sincronizar con Zepp Cloud: {err_text or out_text or huami_check.get('message')}"
                record_sync_failure(err_user_msg, needs_reauth=False)
                parsed["success"] = False
                parsed["outcome"] = "failed"
                parsed["needs_reauth"] = False
                parsed["message"] = err_user_msg
                last_info = get_last_sync_info()
                parsed["last_cloud_sync_at"] = last_info.get("last_cloud_sync_at")
                return parsed

    except Exception as e:
        err_msg = f"Error ejecutando {CLI_BIN}: {e}"
        print(f"[Sync] {err_msg}")
        write_sync_log(f"Comando: {' '.join(args)}\nEXCEPCIÓN CRÍTICA:\n{err_msg}")
        record_sync_failure(str(e), needs_reauth=False)
        return {
            "success": False,
            "outcome": "failed",
            "started_at": now_iso,
            "finished_at": datetime.utcnow().isoformat() + "Z",
            "last_cloud_sync_at": None,
            "records_written": 0,
            "total_records": 0,
            "streams": [],
            "needs_reauth": False,
            "message": f"Error ejecutando sincronizador: {str(e)}",
            "message_code": "err.sync.failed"
        }


async def run_reprocess_command(force_all: bool = True) -> Dict[str, Any]:
    """Ejecuta zeppbridge-cli reprocess para reprocesar registros raw con el nuevo parser v3.2.0 (series de fuerza)."""
    args = [CLI_BIN, "reprocess", "--json"]
    if force_all:
        args.append("--all")

    env = os.environ.copy()
    env["ZEPPBRIDGE_DATA_DIR"] = str(DATA_DIR)

    print(f"[Reprocess] Ejecutando comando CLI: {' '.join(args)}")

    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env
        )
        stdout, stderr = await process.communicate()
        out_text = stdout.decode("utf-8", errors="replace").strip()
        err_text = stderr.decode("utf-8", errors="replace").strip()

        print(f"[Reprocess] CLI terminado con código {process.returncode}")
        if out_text:
            print(f"[Reprocess] Salida (stdout): {out_text}")
        if err_text:
            print(f"[Reprocess] Errores (stderr): {err_text}")

        # Extraer JSON de la salida
        parsed = None
        json_match = re.search(r"(\{.*\})", out_text, re.DOTALL)
        if json_match:
            try:
                parsed = json.loads(json_match.group(1))
            except Exception:
                pass

        if parsed and isinstance(parsed, dict):
            # Normalizar nombres de claves (camelCase vs snake_case) para compatibilidad total con Tauri y CLI
            streams = parsed.get("streams")
            if not isinstance(streams, dict):
                streams = {}
                parsed["streams"] = streams

            raw_count = parsed.get("raw_records") or parsed.get("rawRecords") or 0
            total_count = (
                parsed.get("total_records")
                or parsed.get("totalRecords")
                or sum(streams.values())
                or raw_count
            )
            parsed["total_records"] = total_count
            parsed["totalRecords"] = total_count
            parsed["raw_records"] = raw_count
            parsed["rawRecords"] = raw_count
            parsed["ok"] = bool(parsed.get("ok", True))
            parsed["replayed"] = bool(parsed.get("replayed", True))
            parsed["message"] = (
                parsed.get("message")
                or f"Datos locales reprocesados correctamente ({total_count} registros normalizados con el nuevo motor v3.2.1)."
            )
            return parsed

        if process.returncode != 0:
            err_msg = err_text or out_text or f"CLI finalizó con código {process.returncode}"
            print(f"[Reprocess] Fallo en CLI: {err_msg}")
            return {
                "ok": False,
                "total_records": 0,
                "totalRecords": 0,
                "raw_records": 0,
                "rawRecords": 0,
                "streams": {},
                "message": f"Error al reprocesar datos: {err_msg}"
            }

        return {
            "ok": True,
            "total_records": 0,
            "totalRecords": 0,
            "raw_records": 0,
            "rawRecords": 0,
            "streams": {},
            "message": out_text or err_text or "Reproceso completado"
        }
    except Exception as e:
        print(f"[Reprocess] Error al ejecutar: {e}")
        return {
            "ok": False,
            "total_records": 0,
            "totalRecords": 0,
            "raw_records": 0,
            "rawRecords": 0,
            "streams": {},
            "message": f"Error: {e}"
        }


def get_workout_detail_coverage_db(channel: Optional[str] = "advanced") -> Dict[str, Any]:
    """Calcula la cobertura de detalles de entrenamiento directamente desde zepp.db."""
    db_file = DATA_DIR / "zepp.db"
    if not db_file.exists():
        return {
            "pending": 0, "persisted": 0, "empty_from_cloud": 0,
            "request_failed": 0, "parse_failed": 0, "unavailable": 0, "ready": 0
        }
    try:
        conn = sqlite3.connect(str(db_file), timeout=10.0)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("SELECT count(*) as c FROM workouts")
        row_tot = cursor.fetchone()
        total = (row_tot["c"] if row_tot else 0) or 0
        
        cursor.execute("""
            SELECT count(DISTINCT w.workout_id) as c 
            FROM workouts w 
            WHERE w.sample_count > 0 
               OR EXISTS (SELECT 1 FROM workout_strength_sets s WHERE s.workout_id = w.workout_id)
        """)
        row_pers = cursor.fetchone()
        persisted = (row_pers["c"] if row_pers else 0) or 0
        
        pending = max(0, total - persisted)
        conn.close()
        return {
            "pending": pending,
            "persisted": persisted,
            "empty_from_cloud": 0,
            "request_failed": 0,
            "parse_failed": 0,
            "unavailable": 0,
            "ready": pending
        }
    except Exception as e:
        print(f"[CoverageDB] Error calculando cobertura: {e}")
        return {
            "pending": 0, "persisted": 0, "empty_from_cloud": 0,
            "request_failed": 0, "parse_failed": 0, "unavailable": 0, "ready": 0
        }


async def run_workout_details_command(
    channel: Optional[str] = None,
    max_items: int = 40,
    retry_failed: bool = False
) -> Dict[str, Any]:
    """Ejecuta zeppbridge-cli workout-details para descargar las series, pesos y repeticiones de fuerza desde Zepp Cloud."""
    st = get_auth_status()
    selected_channel = channel or ("official" if st.get("data_source") == "official" else "advanced")
    
    args = [CLI_BIN, "workout-details", "--channel", selected_channel, "--max-items", str(max_items), "--json"]
    if retry_failed:
        args.append("--retry-failed")

    env = os.environ.copy()
    env["ZEPPBRIDGE_DATA_DIR"] = str(DATA_DIR)
    env["ZEPPBRIDGE_CREDENTIAL_STORE"] = "env"
    token = get_stored_token(st.get("user_id"))
    if token:
        env["ZEPPBRIDGE_APP_TOKEN"] = token
    if st.get("user_id"):
        env["ZEPPBRIDGE_USER_ID"] = st["user_id"]

    print(f"[WorkoutDetails] Ejecutando comando CLI: {' '.join(args)}")
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env
        )
        stdout, stderr = await process.communicate()
        out_text = stdout.decode("utf-8", errors="replace").strip()
        err_text = stderr.decode("utf-8", errors="replace").strip()
        print(f"[WorkoutDetails] CLI finalizado (código {process.returncode})")
        if out_text:
            print(f"[WorkoutDetails] Salida: {out_text}")
        if err_text:
            print(f"[WorkoutDetails] Error: {err_text}")

        parsed = None
        json_match = re.search(r"(\{.*\})", out_text, re.DOTALL)
        if json_match:
            try:
                parsed = json.loads(json_match.group(1))
            except Exception:
                pass

        if parsed and isinstance(parsed, dict):
            if "coverage" in parsed and isinstance(parsed["coverage"], dict):
                return parsed["coverage"]
            return parsed

        return get_workout_detail_coverage_db(selected_channel)
    except Exception as e:
        print(f"[WorkoutDetails] Excepción al ejecutar: {e}")
        return get_workout_detail_coverage_db(selected_channel)

