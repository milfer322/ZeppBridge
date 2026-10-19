import os
import json
import asyncio
import re
import sqlite3
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
                return json.load(f)
        except Exception:
            pass
    return {
        "last_cloud_sync_at": None,
        "last_cloud_sync_outcome": None,
        "records_written": 0
    }

def record_successful_sync(records: int, outcome: str = "updated"):
    """Registra la marca de tiempo tras un sync exitoso."""
    now_iso = datetime.utcnow().isoformat() + "Z"
    info = {
        "last_cloud_sync_at": now_iso,
        "last_cloud_sync_outcome": outcome,
        "records_written": records
    }
    try:
        with open(SYNC_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(info, f, indent=2)
    except Exception:
        pass

def get_stored_token(user_id: Optional[str] = None) -> Optional[str]:
    """Recupera el app_token guardado, soportando formato oficial {'tokens': {uid: token}}."""
    if not CREDENTIALS_FILE.exists():
        return None
    try:
        with open(CREDENTIALS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if "tokens" in data and isinstance(data["tokens"], dict):
                if user_id and user_id in data["tokens"]:
                    return data["tokens"][user_id]
                # Si no se pasa user_id o no coincide, tomar el primer token
                if data["tokens"]:
                    return next(iter(data["tokens"].values()))
            return data.get("app_token")
    except Exception:
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

def ensure_auth_files_valid():
    """Verifica y normaliza auth.json y credentials.json para cumplir estrictamente el contrato del CLI oficial."""
    if AUTH_FILE.exists():
        try:
            with open(AUTH_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            host = data.get("region_host", "")
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
            
            # Asegurar copia auth.user-id
            uid = data.get("user_id")
            if uid:
                uid_file = DATA_DIR / "auth.user-id"
                if not uid_file.exists():
                    with open(uid_file, "w", encoding="utf-8") as f:
                        f.write(str(uid).strip())
        except Exception as e:
            print(f"[Auth] Error normalizando auth.json: {e}")

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
    try:
        os.chmod(DATA_DIR, 0o700)
    except Exception:
        pass

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

# Asegurar chequeo al iniciar el módulo
ensure_auth_files_valid()

async def run_sync_command(mode: str = "incremental", days: Optional[int] = None) -> Dict[str, Any]:
    """Ejecuta zeppbridge-cli sync y garantiza un SyncReport válido."""
    now_iso = datetime.utcnow().isoformat() + "Z"
    st = get_auth_status()

    # Si no hay token guardado, devolver fallo con estructura completa de SyncReport
    token = get_stored_token(st.get("user_id"))
    if not st["configured"] or not token:
        return {
            "success": False,
            "outcome": "failed",
            "started_at": now_iso,
            "finished_at": now_iso,
            "last_cloud_sync_at": None,
            "records_written": 0,
            "total_records": 0,
            "streams": [],
            "message": "No hay cuenta conectada o el token no es válido",
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

        # Blindaje del contrato SyncReport (evita 'Cannot read properties of undefined (reading filter)')
        if "streams" not in parsed or not isinstance(parsed["streams"], list):
            parsed["streams"] = []
        if "outcome" not in parsed:
            parsed["outcome"] = "updated" if parsed.get("success") else "failed"
        
        # Unificar records_written y total_records
        records = parsed.get("records_written", parsed.get("total_records", 0))
        parsed["records_written"] = records
        parsed["total_records"] = records

        if "started_at" not in parsed:
            parsed["started_at"] = now_iso
        if "finished_at" not in parsed:
            parsed["finished_at"] = datetime.utcnow().isoformat() + "Z"
        
        # Registrar sincronización si trajo registros
        if records > 0:
            record_successful_sync(records, parsed["outcome"])
            parsed["last_cloud_sync_at"] = parsed["finished_at"]
        else:
            last_info = get_last_sync_info()
            parsed["last_cloud_sync_at"] = last_info.get("last_cloud_sync_at")

        return parsed

    except Exception as e:
        err_msg = f"Error ejecutando {CLI_BIN}: {e}"
        print(f"[Sync] {err_msg}")
        write_sync_log(f"Comando: {' '.join(args)}\nEXCEPCIÓN CRÍTICA:\n{err_msg}")
        return {
            "success": False,
            "outcome": "failed",
            "started_at": now_iso,
            "finished_at": datetime.utcnow().isoformat() + "Z",
            "last_cloud_sync_at": None,
            "records_written": 0,
            "total_records": 0,
            "streams": [],
            "message": f"Error ejecutando sincronizador: {str(e)}",
            "message_code": "err.sync.failed"
        }
