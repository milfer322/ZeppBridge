import os
import sqlite3
import json
from pathlib import Path
from typing import List, Dict, Any, Optional

DATA_DIR = Path(os.getenv("ZEPPBRIDGE_DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "zepp.db"

def init_db_pragmas():
    """Activa el modo WAL y configura el timeout para evitar bloqueos por concurrencia."""
    if not DB_PATH.exists():
        return
    try:
        conn = sqlite3.connect(DB_PATH.as_posix(), timeout=10.0)
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA busy_timeout = 10000;")
        conn.close()
        print("[DB] Modo WAL y busy_timeout configurados con éxito en zepp.db")
        ensure_tables()
    except Exception as e:
        print(f"[DB] Error al inicializar PRAGMAs en {DB_PATH}: {e}")

def get_db_connection() -> Optional[sqlite3.Connection]:
    """Retorna una conexión de solo lectura a la base de datos local SQLite."""
    if not DB_PATH.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 10000;")
        return conn
    except Exception as e:
        print(f"[DB] Error al conectar a {DB_PATH}: {e}")
        return None

def get_db_write_connection() -> Optional[sqlite3.Connection]:
    """Retorna una conexión de lectura-escritura a la base de datos local SQLite."""
    if not DB_PATH.exists():
        return None
    try:
        conn = sqlite3.connect(DB_PATH.as_posix(), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 10000;")
        return conn
    except Exception as e:
        print(f"[DB] Error al conectar rw a {DB_PATH}: {e}")
        return None

def execute_write(query: str, params: tuple = ()) -> Optional[int]:
    conn = get_db_write_connection()
    if not conn:
        return None
    try:
        cursor = conn.cursor()
        cursor.execute(query, params)
        conn.commit()
        return cursor.lastrowid
    except Exception as e:
        print(f"[DB] Error ejecutando write: {query} -> {e}")
        return None
    finally:
        conn.close()

def query_all(query: str, params: tuple = ()) -> List[Dict[str, Any]]:
    conn = get_db_connection()
    if not conn:
        return []
    try:
        cursor = conn.cursor()
        cursor.execute(query, params)
        rows = cursor.fetchall()
        return [dict(row) for row in rows]
    except Exception as e:
        print(f"[DB] Error ejecutando query: {query} -> {e}")
        return []
    finally:
        conn.close()

def query_one(query: str, params: tuple = ()) -> Optional[Dict[str, Any]]:
    conn = get_db_connection()
    if not conn:
        return None
    try:
        cursor = conn.cursor()
        cursor.execute(query, params)
        row = cursor.fetchone()
        return dict(row) if row else None
    except Exception as e:
        print(f"[DB] Error ejecutando query_one: {query} -> {e}")
        return None
    finally:
        conn.close()

def get_existing_tables() -> List[str]:
    conn = get_db_connection()
    if not conn:
        return []
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' OR type='view'")
        return [row[0] for row in cursor.fetchall()]
    finally:
        conn.close()

def ensure_tables():
    """Crea las tablas locales de correcciones y extensiones si no existen."""
    conn = get_db_write_connection()
    if conn:
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS workout_fit_overrides (
                    workout_id TEXT PRIMARY KEY,
                    file_name TEXT,
                    sha256 TEXT NOT NULL,
                    byte_len INTEGER NOT NULL,
                    fit BLOB NOT NULL,
                    imported_at TEXT NOT NULL,
                    fit_start_time TEXT NOT NULL,
                    fit_end_time TEXT NOT NULL,
                    fit_distance_m REAL,
                    fit_moving_seconds INTEGER,
                    fit_avg_hr INTEGER,
                    fit_max_hr INTEGER,
                    original_end_time TEXT,
                    original_distance_m REAL,
                    original_moving_seconds INTEGER,
                    original_avg_hr INTEGER,
                    original_max_hr INTEGER
                );
            """)
            conn.commit()
        except Exception as e:
            print(f"[DB] Error creando tablas en ensure_tables: {e}")
        finally:
            conn.close()
