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

def ensure_columns(conn: sqlite3.Connection, table_name: str, columns: List[tuple]):
    """Añade columnas a una tabla si no existen para garantizar paridad con upstream."""
    try:
        cursor = conn.cursor()
        cursor.execute(f"PRAGMA table_info({table_name})")
        existing = {row[1] for row in cursor.fetchall()}
        for col_name, col_type in columns:
            if col_name not in existing:
                try:
                    cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_type};")
                    print(f"[DB] Columna '{col_name}' añadida con éxito a '{table_name}'")
                except Exception as ce:
                    print(f"[DB] Aviso al añadir columna '{col_name}' a '{table_name}': {ce}")
    except Exception as e:
        print(f"[DB] Error comprobando columnas para '{table_name}': {e}")

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

            conn.execute("""
                CREATE TABLE IF NOT EXISTS workout_pauses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    workout_id TEXT NOT NULL,
                    start_time TEXT NOT NULL,
                    end_time TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    FOREIGN KEY(workout_id) REFERENCES workouts(workout_id) ON DELETE CASCADE
                );
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS workout_laps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    workout_id TEXT NOT NULL,
                    lap_index INTEGER NOT NULL,
                    start_time TEXT NOT NULL,
                    end_time TEXT NOT NULL,
                    distance_m REAL NOT NULL,
                    duration_seconds INTEGER NOT NULL,
                    avg_hr INTEGER,
                    max_hr INTEGER,
                    FOREIGN KEY(workout_id) REFERENCES workouts(workout_id) ON DELETE CASCADE
                );
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS workout_splits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    workout_id TEXT NOT NULL,
                    split_index INTEGER NOT NULL,
                    start_time TEXT NOT NULL,
                    end_time TEXT NOT NULL,
                    distance_m REAL NOT NULL,
                    duration_seconds INTEGER NOT NULL,
                    pace_min_per_km REAL,
                    avg_hr INTEGER,
                    max_hr INTEGER,
                    elevation_gain_m REAL,
                    elevation_loss_m REAL,
                    partial INTEGER NOT NULL,
                    FOREIGN KEY(workout_id) REFERENCES workouts(workout_id) ON DELETE CASCADE
                );
            """)

            # Asegurar columnas añadidas en migraciones oficiales
            ensure_columns(conn, "workouts", [
                ("synced_at", "TEXT"),
                ("gps_available", "INTEGER NOT NULL DEFAULT 0"),
                ("sample_count", "INTEGER NOT NULL DEFAULT 0"),
                ("zepp_source", "TEXT"),
                ("zepp_type", "INTEGER"),
                ("min_hr", "INTEGER"),
                ("total_steps", "INTEGER"),
                ("moving_seconds", "INTEGER"),
                ("elevation_gain_m", "REAL"),
                ("elevation_loss_m", "REAL"),
                ("max_altitude_m", "REAL"),
                ("min_altitude_m", "REAL"),
                ("training_effect", "REAL"),
                ("anaerobic_training_effect", "REAL"),
                ("rpe", "INTEGER"),
                ("avg_cadence_spm", "REAL"),
                ("max_cadence_spm", "REAL"),
                ("avg_stride_cm", "REAL"),
                ("workout_type_source", "TEXT DEFAULT 'missing'"),
                ("workout_type_override", "TEXT"),
                ("workout_type_conflict", "TEXT"),
                ("gear_id", "TEXT")
            ])

            ensure_columns(conn, "workout_samples", [
                ("stride", "REAL"),
                ("power_watts", "REAL"),
                ("ground_contact_ms", "REAL"),
                ("vertical_oscillation_mm", "REAL"),
                ("vertical_ratio_pct", "REAL"),
                ("equivalent_pace_s", "REAL"),
                ("distance_m", "REAL")
            ])

            ensure_columns(conn, "sleep_sessions", [
                ("rem_available", "INTEGER NOT NULL DEFAULT 1"),
                ("synced_at", "TEXT"),
                ("wake_count", "INTEGER"),
                ("rem_seconds", "INTEGER"),
                ("nap_total_seconds", "INTEGER"),
                ("unmeasurable_seconds", "INTEGER"),
                ("validation", "TEXT")
            ])

            ensure_columns(conn, "coverage_ledger", [
                ("attempts", "INTEGER NOT NULL DEFAULT 0"),
                ("last_attempt_at", "TEXT"),
                ("error_code", "TEXT")
            ])

            conn.commit()
        except Exception as e:
            print(f"[DB] Error creando tablas en ensure_tables: {e}")
        finally:
            conn.close()
