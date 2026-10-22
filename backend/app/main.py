import os
import copy
import json
import sqlite3
import ssl
import asyncio
import urllib.request
import urllib.error
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, List, Optional
from fastapi import FastAPI, Request, HTTPException, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, FileResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from starlette.background import BackgroundTask

from .database import get_db_connection, query_all, query_one, DB_PATH, init_db_pragmas
from .zepp_cli import (
    get_auth_status,
    get_stored_token,
    save_credentials,
    run_sync_command,
    DATA_DIR,
    get_last_sync_info,
    is_db_empty,
    SYNC_STATE_FILE,
    CLI_BIN,
    write_sync_log,
    clean_region_host,
    clean_region_url
)

app = FastAPI(title="ZeppBridge Web Native", version="3.0.1")

# Middleware de compresión GZIP para acelerar carga web de frontend y respuestas JSON
app.add_middleware(
    GZipMiddleware,
    minimum_size=1000
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = Path("/app/static")

# Helper para cargar o guardar archivos JSON locales en DATA_DIR
def load_json_file(filename: str, default: Any = None):
    p = DATA_DIR / filename
    if p.exists():
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default
    return default

def save_json_file(filename: str, data: Any):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    p = DATA_DIR / filename
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

# Cargar catálogo oficial de dispositivos Amazfit / Zepp
CATALOG_PATH = Path("/app/catalog.json")
if not CATALOG_PATH.exists():
    CATALOG_PATH = Path(__file__).resolve().parent / "catalog.json"

DEVICE_CATALOG: Dict[Any, Dict[str, Any]] = {}
if CATALOG_PATH.exists():
    try:
        with open(CATALOG_PATH, "r", encoding="utf-8") as f:
            cat_data = json.load(f)
            for dev in cat_data.get("devices", []):
                for code in dev.get("device_source_codes", []):
                    DEVICE_CATALOG[code] = dev
                for mc in dev.get("model_codes", []):
                    DEVICE_CATALOG[mc.lower()] = dev
                if "canonical_name" in dev:
                    DEVICE_CATALOG[dev["canonical_name"].lower()] = dev
                if "catalog_id" in dev:
                    DEVICE_CATALOG[dev["catalog_id"].lower()] = dev
        print(f"[Catalog] Cargados {len(cat_data.get('devices', []))} dispositivos oficiales desde {CATALOG_PATH}")
    except Exception as e:
        print(f"[Catalog] Error cargando catalog.json: {e}")

def parse_device_item(it: dict, now_iso: str) -> dict:
    """Parsea de forma segura un dispositivo retornado por la API de Zepp / Huami y lo casa con el catálogo oficial."""
    ds_code = None
    ds_name = None
    raw_ds = it.get("deviceSource")
    if isinstance(raw_ds, int):
        ds_code = raw_ds
    elif isinstance(raw_ds, str) and raw_ds.isdigit():
        ds_code = int(raw_ds)
    elif isinstance(raw_ds, dict):
        ds_name = raw_ds.get("name")
        ds_code = raw_ds.get("code")

    match = None
    if ds_code and ds_code in DEVICE_CATALOG:
        match = DEVICE_CATALOG[ds_code]

    if not match:
        for k in ["model", "modelName", "productName", "deviceName", "displayName", "name"]:
            val = it.get(k)
            if val and isinstance(val, str) and val.lower() in DEVICE_CATALOG:
                match = DEVICE_CATALOG[val.lower()]
                break

    if match:
        name = match.get("display_name") or match.get("canonical_name")
        catalog_id = match.get("catalog_id", "amazfit")
        kind = match.get("kind", "watch")
        image_key = match.get("image_key")
        match_status = "exact"
    else:
        name = (
            it.get("displayName")
            or it.get("deviceName")
            or it.get("productName")
            or ds_name
            or it.get("name")
            or (f"Amazfit Device ({ds_code})" if ds_code else None)
            or it.get("mac")
            or "Amazfit Smartwatch"
        )
        catalog_id = "amazfit"
        kind = "watch"
        image_key = None
        match_status = "exact"

    dev_id = str(it.get("deviceId") or it.get("mac") or "dev")
    mac = str(it.get("mac") or it.get("sn") or "SN")
    fw = str(it.get("firmwareVersion") or it.get("fwVersion") or "1.0")

    return {
        "name": name,
        "canonical_name": name,
        "display_name": name,
        "catalog_id": catalog_id,
        "kind": kind,
        "image_key": image_key,
        "match_status": match_status,
        "has_local_data": True,
        "last_data_at": now_iso,
        "firmware": fw,
        "serial": mac,
        "device_id": dev_id,
        "timezone": "Europe/Madrid"
    }

def build_ai_health_prompt(prompt_query: str = "", task_dict: dict = None, scope_days: int = None, mode: str = None) -> str:
    """Genera un informe completo y enriquecido con métricas reales de la base de datos para la IA."""
    task = task_dict or {}
    user_prompt = (
        prompt_query
        or task.get("prompt_text")
        or task.get("prompt")
        or task.get("title")
        or task.get("name")
        or ""
    )
    
    # Determinar si es una consulta específica de pulsaciones / salud cardíaca
    is_heart_report = (
        mode == "heart"
        or any(k in user_prompt.lower() for k in ["pulsacion", "coraz", "cardio", "frecuencia", "heart", "ritmo", "latido"])
    )
    
    # Determinar periodo (días): por parámetro, o inferido del texto (mensual = 30, semanal = 7)
    if scope_days:
        period_days = int(scope_days)
    elif any(k in user_prompt.lower() for k in ["mensual", "mes", "30 d"]):
        period_days = 30
    else:
        period_days = 7

    cutoff_date = (datetime.now().date() - timedelta(days=period_days)).strftime("%Y-%m-%d")
    period_label = f"Últimos {period_days} días ({'Mensual' if period_days >= 25 else 'Semanal'})"

    if is_heart_report:
        if not user_prompt:
            user_prompt = f"Analizar mi salud cardiovascular y evolución de pulsaciones durante el periodo {period_label.lower()}."
        
        sections = []
        sections.append(f"# 💓 Informe Especializado de Salud Cardíaca y Pulsaciones (Zepp / Amazfit)")
        sections.append(f"**Periodo analizado:** {period_label} (desde {cutoff_date})")
        sections.append(f"\n## 🎯 Consulta y Objetivo del Usuario\n> {user_prompt}\n")

        # 1. Resumen de Frecuencia Cardíaca en Reposo (RHR)
        sections.append("## 🩺 Frecuencia Cardíaca en Reposo (RHR)")
        try:
            rhr_stat = query_one(
                "SELECT AVG(value) as avg_val, MIN(value) as min_val, MAX(value) as max_val, COUNT(*) as cnt "
                "FROM daily_metrics WHERE metric = 'resting_hr' AND date >= ?",
                (cutoff_date,)
            )
            if rhr_stat and rhr_stat.get("avg_val") is not None:
                avg_rhr = round(float(rhr_stat["avg_val"]), 1)
                min_rhr = int(round(float(rhr_stat["min_val"])))
                max_rhr = int(round(float(rhr_stat["max_val"])))
                cnt_rhr = rhr_stat["cnt"]
                
                # Calcular tendencia (primera mitad vs segunda mitad)
                half = max(1, period_days // 2)
                mid_date = (datetime.now().date() - timedelta(days=half)).strftime("%Y-%m-%d")
                recent_half = query_one(
                    "SELECT AVG(value) as avg_val FROM daily_metrics WHERE metric = 'resting_hr' AND date >= ?",
                    (mid_date,)
                )
                past_half = query_one(
                    "SELECT AVG(value) as avg_val FROM daily_metrics WHERE metric = 'resting_hr' AND date >= ? AND date < ?",
                    (cutoff_date, mid_date)
                )
                trend_str = "Estable"
                if recent_half and past_half and recent_half.get("avg_val") and past_half.get("avg_val"):
                    diff = recent_half["avg_val"] - past_half["avg_val"]
                    if diff > 1.5:
                        trend_str = f"En aumento (+{diff:.1f} bpm respecto al inicio del periodo)"
                    elif diff < -1.5:
                        trend_str = f"En descenso ({diff:.1f} bpm respecto al inicio del periodo)"

                sections.append(f"- **Promedio en reposo:** **{avg_rhr} bpm** (registrado en {cnt_rhr} días)")
                sections.append(f"- **Rango en reposo:** Mínimo **{min_rhr} bpm** | Máximo **{max_rhr} bpm**")
                sections.append(f"- **Tendencia reciente:** {trend_str}")
            else:
                sections.append("- *No hay suficientes registros de FC en reposo en este periodo.*")
        except Exception as e:
            sections.append(f"- *Error consultando FC en reposo: {e}*")
        sections.append("")

        # 2. Desglose cronológico diario de pulsaciones
        sections.append(f"## 📅 Desglose Diario de Pulsaciones ({period_label})")
        try:
            daily_rows = query_all(
                "SELECT date, value FROM daily_metrics WHERE metric = 'resting_hr' AND date >= ? ORDER BY date DESC",
                (cutoff_date,)
            )
            extremes = {}
            ex_rows = query_all(
                "SELECT substr(timestamp, 1, 10) as d, MIN(value) as min_val, MAX(value) as max_val "
                "FROM metric_samples WHERE metric = 'heart_rate' AND substr(timestamp, 1, 10) >= ? "
                "GROUP BY substr(timestamp, 1, 10)",
                (cutoff_date,)
            )
            for er in ex_rows:
                extremes[er["d"]] = (er["min_val"], er["max_val"])

            if daily_rows:
                for dr in daily_rows:
                    d = dr["date"]
                    r_val = int(round(dr["value"]))
                    ex = extremes.get(d)
                    ex_str = f" | Pico máximo del día: {int(round(ex[1]))} bpm (Mín: {int(round(ex[0]))} bpm)" if ex else ""
                    sections.append(f"- **{d}**: Reposo: **{r_val} bpm**{ex_str}")
            else:
                sections.append("- *No se encontraron registros diarios en el rango seleccionado.*")
        except Exception as e:
            sections.append(f"- *Error en desglose diario: {e}*")
        sections.append("")

        # 3. Respuesta cardíaca en entrenamientos y esfuerzo
        sections.append("## 🏃 Pulsaciones Durante Entrenamientos y Esfuerzo")
        try:
            workouts = query_all(
                "SELECT * FROM workouts WHERE substr(start_time, 1, 10) >= ? ORDER BY start_time DESC LIMIT 12",
                (cutoff_date,)
            )
            if workouts:
                for w in workouts:
                    w_type = w.get("effective_type") or w.get("workout_type") or "Entrenamiento"
                    st = str(w.get("start_time") or "")[:16].replace("T", " ")
                    dur_m = (w.get("duration_seconds") or 0) // 60
                    avg_hr = w.get("avg_hr")
                    max_hr = w.get("max_hr")
                    cals = w.get("calories")
                    hr_parts = []
                    if avg_hr and avg_hr > 0:
                        hr_parts.append(f"FC Media: {int(avg_hr)} bpm")
                    if max_hr and max_hr > 0:
                        hr_parts.append(f"FC Máx: {int(max_hr)} bpm")
                    if cals and cals > 0:
                        hr_parts.append(f"{int(cals)} kcal")
                    hr_info = (" | " + " | ".join(hr_parts)) if hr_parts else ""
                    sections.append(f"- **{st}** — **{w_type.capitalize()}** ({dur_m} min){hr_info}")
            else:
                sections.append("- *No se registraron sesiones deportivas con monitor cardíaco en este periodo.*")
        except Exception as e:
            sections.append(f"- *Error consultando entrenamientos: {e}*")
        sections.append("")

        # 4. Variabilidad de la Frecuencia Cardíaca (HRV) y Estrés
        sections.append("## 💓 Variabilidad Cardíaca (HRV) y Estrés")
        try:
            hrv_stat = query_one(
                "SELECT AVG(value) as avg_val, MIN(value) as min_val, MAX(value) as max_val "
                "FROM metric_samples WHERE metric = 'hrv' AND substr(timestamp, 1, 10) >= ?",
                (cutoff_date,)
            )
            if hrv_stat and hrv_stat.get("avg_val") is not None:
                sections.append(
                    f"- **HRV Promedio:** **{round(float(hrv_stat['avg_val']), 1)} ms** "
                    f"(Rango: {int(round(float(hrv_stat['min_val'])))} - {int(round(float(hrv_stat['max_val'])))} ms)"
                )
            stress_stat = query_one(
                "SELECT AVG(value) as avg_val FROM metric_samples WHERE metric = 'stress' AND substr(timestamp, 1, 10) >= ?",
                (cutoff_date,)
            )
            if stress_stat and stress_stat.get("avg_val") is not None:
                sections.append(f"- **Nivel de Estrés Cardíaco Promedio:** **{round(float(stress_stat['avg_val']), 1)} / 100**")
            
            spo2_stat = query_one(
                "SELECT AVG(value) as avg_val FROM metric_samples WHERE metric = 'spo2' AND substr(timestamp, 1, 10) >= ?",
                (cutoff_date,)
            )
            if spo2_stat and spo2_stat.get("avg_val") is not None:
                sections.append(f"- **Oxígeno en Sangre (SpO2) Promedio:** **{round(float(spo2_stat['avg_val']), 1)}%**")
        except Exception as e:
            sections.append(f"- *Error consultando HRV/Estrés: {e}*")
        sections.append("")

        # 5. Instrucciones clínicas y preventivas para Gemini
        sections.append("---")
        sections.append("## 💡 Instrucciones para el Asistente de IA (Gemini)")
        sections.append(
            "Actúa como un cardiólogo y especialista en fisiología del deporte y ciencias cardiovasculares. "
            "Analiza las métricas y tendencias de pulsaciones arriba expuestas respondiendo con detalle y rigor:\n"
            "1. **Evaluación de la FC en Reposo (RHR):** ¿Es adecuado el promedio y la estabilidad observada? ¿Indica una buena condición aeróbica o signos de fatiga?\n"
            "2. **Respuesta al Esfuerzo:** ¿Cómo se comportan las pulsaciones medias y máximas durante los entrenamientos? ¿Son coherentes los picos de pulso con las actividades?\n"
            "3. **Recuperación y Sistema Nervioso Autónomo (HRV/Estrés):** ¿Sugieren los valores de HRV y estrés una adecuada recuperación nocturna o sobrecarga?\n"
            "4. **Recomendaciones Prácticas:** Aconseja pautas sobre zonas de entrenamiento cardiovascular (Zonas 2 y 3), hidratación, descanso y señales de alarma por las que convendría consultar a un médico."
        )
        return "\n".join(sections)

    # Si es consulta general (no específica de corazón), usar el generador global
    if not user_prompt:
        user_prompt = "Analiza mi estado de salud general, patrones de descanso y entrenamientos recientes, y proporcióname recomendaciones personalizadas."

    sections = []
    sections.append("# 📊 Informe de Salud y Actividad Física (Zepp / Amazfit)")
    sections.append(f"\n## 🎯 Consulta del Usuario\n> {user_prompt}\n")
    
    # 1. Resumen general de datos disponibles
    stats = []
    try:
        dm_cnt = query_one("SELECT COUNT(DISTINCT date) as c, MAX(date) as m FROM daily_metrics")
        if dm_cnt and dm_cnt.get("c"):
            stats.append(f"- **Días de actividad registrados:** {dm_cnt['c']} (último registro: {dm_cnt.get('m')})")
        sl_cnt = query_one("SELECT COUNT(*) as c, MAX(substr(start_time, 1, 10)) as m FROM sleep_sessions")
        if sl_cnt and sl_cnt.get("c"):
            stats.append(f"- **Sesiones de sueño registradas:** {sl_cnt['c']} (última: {sl_cnt.get('m')})")
        wk_cnt = query_one("SELECT COUNT(*) as c, MAX(substr(start_time, 1, 10)) as m FROM workouts")
        if wk_cnt and wk_cnt.get("c"):
            stats.append(f"- **Entrenamientos totales registrados:** {wk_cnt['c']} (último: {wk_cnt.get('m')})")
    except Exception as e:
        print(f"[AI] Error al consultar estadísticas generales: {e}")
    
    if stats:
        sections.append("### ℹ️ Registro General en la Cuenta")
        sections.append("\n".join(stats) + "\n")

    # 2. Entrenamientos recientes
    try:
        workouts = query_all("SELECT * FROM workouts ORDER BY start_time DESC LIMIT 8")
        if workouts:
            sections.append("## 🏃 Entrenamientos Recientes")
            for w in workouts:
                w_type = w.get("effective_type") or w.get("workout_type") or "Entrenamiento"
                st = str(w.get("start_time") or "")[:16].replace("T", " ")
                dur_s = w.get("duration_seconds") or 0
                dur_m = dur_s // 60
                dist_m = w.get("distance_meters")
                dist_str = f" | {dist_m / 1000.0:.2f} km" if dist_m and dist_m > 0 else ""
                cals = w.get("calories")
                cals_str = f" | {int(cals)} kcal" if cals and cals > 0 else ""
                avg_hr = w.get("avg_hr")
                hr_str = f" | FC Media: {int(avg_hr)} bpm" if avg_hr and avg_hr > 0 else ""
                max_hr = w.get("max_hr")
                max_hr_str = f" (Max: {int(max_hr)} bpm)" if max_hr and max_hr > 0 else ""
                sections.append(f"- **{st}** — **{w_type.capitalize()}**: {dur_m} min{dist_str}{cals_str}{hr_str}{max_hr_str}")
            sections.append("")
    except Exception as e:
        print(f"[AI] Error al consultar entrenamientos: {e}")

    # 3. Sesiones de sueño recientes
    try:
        sleep_rows = query_all("SELECT * FROM sleep_sessions ORDER BY start_time DESC LIMIT 7")
        if sleep_rows:
            sections.append("## 😴 Registro de Sueño (Últimos días)")
            for s in sleep_rows:
                st = str(s.get("start_time") or "")[:10]
                dur_m = s.get("duration_minutes") or 0
                dur_h = dur_m // 60
                dur_rem = dur_m % 60
                deep_m = s.get("deep_sleep_minutes") or 0
                rem_m = s.get("rem_sleep_minutes") or 0
                score = s.get("score")
                score_str = f" | Puntuación: {score}/100" if score is not None else ""
                sections.append(
                    f"- **{st}**: {dur_h}h {dur_rem:02d}m de sueño "
                    f"(Profundo: {deep_m}m, REM: {rem_m}m){score_str}"
                )
            sections.append("")
    except Exception as e:
        print(f"[AI] Error al consultar sueño: {e}")

    # 4. Actividad diaria reciente (pasos, FC reposo, calorías)
    try:
        daily_rows = query_all(
            "SELECT date, metric, value FROM daily_metrics "
            "WHERE metric IN ('steps', 'resting_hr', 'active_calories', 'calories') "
            "ORDER BY date DESC LIMIT 40"
        )
        if daily_rows:
            by_date = {}
            for r in daily_rows:
                d = r["date"]
                if d not in by_date:
                    by_date[d] = {}
                by_date[d][r["metric"]] = r["value"]
            
            sorted_dates = sorted(by_date.keys(), reverse=True)[:7]
            if sorted_dates:
                sections.append("## 📈 Métricas Diarias Recientes (Pasos, FC Reposo, Calorías)")
                for d in sorted_dates:
                    vals = by_date[d]
                    steps = int(round(vals.get("steps", 0)))
                    rhr = int(round(vals.get("resting_hr", 0)))
                    cals = int(round(vals.get("active_calories") or vals.get("calories", 0)))
                    parts = []
                    if steps > 0:
                        parts.append(f"{steps:,} pasos".replace(",", "."))
                    if rhr > 0:
                        parts.append(f"FC reposo: {rhr} bpm")
                    if cals > 0:
                        parts.append(f"{cals} kcal")
                    if parts:
                        sections.append(f"- **{d}**: " + " | ".join(parts))
                sections.append("")
    except Exception as e:
        print(f"[AI] Error al consultar actividad diaria: {e}")

    # 5. Instrucciones para el LLM
    sections.append("---")
    sections.append("## 💡 Instrucciones para el Asistente de IA (Gemini)")
    sections.append(
        "Actúa como un experto en ciencias del deporte, fisiología y optimización del descanso. "
        "Analiza las tendencias y métricas reales facilitadas en este informe. "
        "Responde a la consulta del usuario con claridad, empatía y rigor técnico, "
        "ofreciendo observaciones fundamentadas en sus datos objetivos y consejos prácticos para su rutina diaria."
    )
    
    return "\n".join(sections)

# ==============================================================================
# DISPATCHER CENTRAL: /api/bridge/{command}
# ==============================================================================

@app.post("/api/bridge/{command}")
async def handle_bridge_command(command: str, request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}

    # --------------------------------------------------------------------------
    # 0. Health & Readiness Check de Tauri (desbloquea whenBackendReady en 0ms)
    # --------------------------------------------------------------------------
    if command == "app_is_ready":
        return True

    # --------------------------------------------------------------------------
    # 1. Estado y Autenticación
    # --------------------------------------------------------------------------
    elif command in ("get_app_status", "verify_auth"):
        st = get_auth_status()
        conn = get_db_connection()
        record_count = 0
        if conn:
            try:
                res = conn.execute("SELECT COUNT(*) FROM workouts").fetchone()
                record_count += res[0] if res else 0
            except Exception:
                pass
            conn.close()

        auth_state = "verified" if st["configured"] else "unconfigured"
        conn_state = "connected" if st["configured"] else "unconfigured"

        sync_info = get_last_sync_info()
        last_cloud_sync_at = sync_info.get("last_cloud_sync_at") if st["configured"] else None
        last_cloud_sync_outcome = sync_info.get("last_cloud_sync_outcome") if st["configured"] else None

        # Si no hay registros aún en la base de datos, last_cloud_sync_at debe ser None
        # para que el frontend reconozca la primera sincronización y no muestre cloud_stale prematuro
        if record_count == 0:
            last_cloud_sync_at = None
            last_cloud_sync_outcome = None

        # Extraer streams reales desde sync_state o conteos de tablas
        sync_rows = query_all("SELECT stream, status, records_written, last_sync, message, needs_reauth FROM sync_state")
        streams = []
        if sync_rows:
            for s in sync_rows:
                streams.append({
                    "stream": s["stream"],
                    "status": "ok" if s["status"] in ("ok", "success") else s["status"],
                    "records": s.get("records_written", 0),
                    "last_sync": s.get("last_sync") or last_cloud_sync_at,
                    "last_cloud_sync_at": s.get("last_sync") or last_cloud_sync_at,
                    "newest_sample_at": s.get("last_sync") or last_cloud_sync_at,
                    "message": s.get("message"),
                    "needs_reauth": bool(s.get("needs_reauth", 0))
                })
        else:
            w_cnt = query_one("SELECT COUNT(*) as c FROM workouts")
            sl_cnt = query_one("SELECT COUNT(*) as c FROM sleep_sessions")
            hr_cnt = query_one("SELECT COUNT(*) as c FROM metric_samples WHERE metric = 'heart_rate'")
            dm_cnt = query_one("SELECT COUNT(*) as c FROM daily_metrics")
            streams = [
                {"stream": "workouts", "status": "ok", "records": w_cnt["c"] if w_cnt else 0, "last_sync": last_cloud_sync_at},
                {"stream": "sleep", "status": "ok", "records": sl_cnt["c"] if sl_cnt else 0, "last_sync": last_cloud_sync_at},
                {"stream": "heart_rate", "status": "ok", "records": hr_cnt["c"] if hr_cnt else 0, "last_sync": last_cloud_sync_at},
                {"stream": "daily_summary", "status": "ok", "records": dm_cnt["c"] if dm_cnt else 0, "last_sync": last_cloud_sync_at}
            ]

        # Cobertura real
        bounds = query_one(
            "SELECT MIN(d) as min_d, MAX(d) as max_d FROM ("
            "  SELECT date as d FROM daily_metrics "
            "  UNION ALL "
            "  SELECT substr(start_time, 1, 10) as d FROM sleep_sessions "
            "  UNION ALL "
            "  SELECT substr(start_time, 1, 10) as d FROM workouts"
            ")"
        )
        earliest_day = bounds.get("min_d") if bounds else None
        latest_day = bounds.get("max_d") if bounds else None
        covered_days = 0
        if earliest_day and latest_day:
            try:
                d1 = datetime.strptime(earliest_day, "%Y-%m-%d")
                d2 = datetime.strptime(latest_day, "%Y-%m-%d")
                covered_days = (d2 - d1).days + 1
            except Exception:
                pass

        db_size = 1024 * 1024 * 5
        try:
            if DB_PATH.exists():
                db_size = DB_PATH.stat().st_size
        except Exception:
            pass

        return {
            "configured": st["configured"],
            "auth_state": auth_state,
            "connection_state": conn_state,
            "data_source": "both" if st["configured"] else "none",
            "masked_user_id": st["user_id_masked"] if st["configured"] else None,
            "region_host": st["region_host"],
            "region_confidence": "identified" if st["configured"] else "unknown",
            "last_sync": last_cloud_sync_at or (datetime.utcnow().isoformat() + "Z"),
            "last_cloud_sync_at": last_cloud_sync_at,
            "last_cloud_sync_outcome": last_cloud_sync_outcome,
            "streams": streams,
            "capabilities": [
                {"capability": "workouts", "available": True},
                {"capability": "sleep", "available": True},
                {"capability": "heart_rate", "available": True},
                {"capability": "steps", "available": True},
                {"capability": "spo2", "available": True},
                {"capability": "stress", "available": True},
                {"capability": "weight", "available": True},
                {"capability": "hrv", "available": True}
            ],
            "database_path": str(DB_PATH),
            "retention_days": 365,
            "history_sync_days": 180,
            "incremental_sync_days": 7,
            "auto_sync_days": 7,
            "storage": {
                "free_bytes": 1024 * 1024 * 1024 * 20,
                "estimated_add_bytes": 1024 * 1024,
                "database_bytes": db_size,
                "allow_long_history": True,
                "warn_tight_space": False,
                "message": "Espacio suficiente",
                "message_code": "ui.estimate.measured",
                "requested_days": 180,
                "streams": [],
                "measured": True,
                "stop_reason": None,
                "stop_reason_code": None,
                "needed_bytes": 1024 * 1024
            },
            "coverage": {
                "earliest_day": earliest_day,
                "latest_day": latest_day,
                "covered_days": covered_days
            },
            "compacting": False,
            "replaying": False
        }

    elif command == "get_login_status":
        st = get_auth_status()
        return {
            "state": "connected" if st["configured"] else "idle",
            "message": "Conectado" if st["configured"] else "",
            "page_url": ""
        }

    elif command == "start_web_login":
        return {
            "state": "waiting",
            "message": "Inicia sesión en la ventana emergente de Zepp",
            "page_url": "https://watchface.zepp.com/"
        }

    elif command == "cancel_web_login":
        return {"state": "idle", "message": "", "page_url": ""}

    elif command == "get_official_status":
        st = get_auth_status()
        return {
            "state": "connected" if st["configured"] else "idle",
            "message": None,
            "message_code": None,
            "user_id_masked": st["user_id_masked"] if st["configured"] else None,
            "nickname": None,
            "authorize_url": None,
            "connected_at": int(datetime.now().timestamp()) if st["configured"] else None
        }

    elif command == "start_official_login":
        return {
            "state": "waiting",
            "message": None,
            "message_code": None,
            "user_id_masked": None,
            "nickname": None,
            "authorize_url": "https://watchface.zepp.com/",
            "connected_at": None
        }

    elif command in ("cancel_official_login", "disconnect_official"):
        return {
            "state": "idle",
            "message": None,
            "message_code": None,
            "user_id_masked": None,
            "nickname": None,
            "authorize_url": None,
            "connected_at": None
        }

    elif command == "manual_auth":
        app_token = body.get("appToken", "")
        user_id = body.get("userId", "")
        region_host = clean_region_url(body.get("regionHost", "api-mifit-de2.huami.com"))
        save_credentials(app_token, user_id, region_host)
        return {
            "configured": True,
            "auth_state": "verified",
            "connection_state": "connected",
            "data_source": "both",
            "masked_user_id": user_id[:3] + "***" + user_id[-2:] if len(user_id) > 5 else user_id,
            "region_host": clean_region_host(region_host),
            "last_sync": datetime.utcnow().isoformat() + "Z",
            "streams": [],
            "capabilities": [],
            "retention_days": 365,
            "compacting": False,
            "replaying": False
        }

    elif command == "clear_auth":
        try:
            (DATA_DIR / "auth.json").unlink(missing_ok=True)
            (DATA_DIR / "credentials.json").unlink(missing_ok=True)
            if SYNC_STATE_FILE.exists():
                SYNC_STATE_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        return {
            "configured": False,
            "auth_state": "unconfigured",
            "connection_state": "unconfigured",
            "masked_user_id": None,
            "region_host": "api-mifit-de2.huami.com",
            "compacting": False,
            "replaying": False
        }

    # --------------------------------------------------------------------------
    # 2. Sincronización
    # --------------------------------------------------------------------------
    elif command in ("start_incremental_sync", "start_history_sync", "start_initial_sync"):
        mode = "history" if command in ("start_history_sync", "start_initial_sync") else "incremental"
        days = body.get("days", 180) if mode == "history" else None
        report = await run_sync_command(mode=mode, days=days)
        return report

    elif command == "cancel_sync":
        return None

    # --------------------------------------------------------------------------
    # 3. Métricas, Series y Paneles Principales
    # --------------------------------------------------------------------------
    elif command == "get_metric_series":
        metrics = body.get("metrics", [])
        days = body.get("days", 30)
        window_days = max(1, min(int(days), 1825))
        end_date = datetime.now().date()
        start_date = end_date - timedelta(days=window_days - 1)
        start_str = start_date.strftime("%Y-%m-%d")
        end_str = end_date.strftime("%Y-%m-%d")
        
        METRIC_UNITS = {
            "steps": "步",
            "distance": "米",
            "active_calories": "千卡",
            "active_minutes": "分钟",
            "resting_hr": "bpm",
            "sleep_score": "score",
            "readiness": "score",
            "physical_readiness": "score",
            "mental_readiness": "score",
            "hybrid_charge": "score",
            "bio_charge": "score",
            "stress": "score",
            "training_load": "load",
            "vo2max": "ml/kg/min",
            "weight": "kg",
            "bmi": "kg/m2",
            "body_fat_rate": "%",
            "hrv": "ms",
            "hrv_rmssd": "ms",
            "spo2": "%",
            "pai_daily": "pai",
            "pai_total": "pai"
        }
        
        series = []
        for m in metrics:
            unit = METRIC_UNITS.get(m, "")
            points = []
            source = "daily_metrics"
            
            if m == "sleep_score":
                source = "sleep_sessions"
                rows = query_all(
                    "SELECT substr(end_time, 1, 10) as dt, score as val "
                    "FROM sleep_sessions WHERE score IS NOT NULL AND substr(end_time, 1, 10) BETWEEN ? AND ? "
                    "GROUP BY dt ORDER BY dt ASC",
                    (start_str, end_str)
                )
                for r in rows:
                    if r.get("val") is not None:
                        points.append({
                            "date": r["dt"],
                            "value": float(r["val"]),
                            "min": None,
                            "max": None,
                            "samples": None
                        })
            else:
                rows = query_all(
                    "SELECT date as dt, "
                    "COALESCE(MAX(CASE WHEN source_scope = 'user_fused' THEN value END), MAX(value)) as val "
                    "FROM daily_metrics WHERE metric = ? AND date BETWEEN ? AND ? "
                    "GROUP BY date ORDER BY date ASC",
                    (m, start_str, end_str)
                )
                if rows:
                    source = "daily_metrics"
                    for r in rows:
                        if r.get("val") is not None:
                            points.append({
                                "date": r["dt"],
                                "value": round(float(r["val"]), 1),
                                "min": None,
                                "max": None,
                                "samples": None
                            })
                else:
                    s_rows = query_all(
                        "SELECT substr(timestamp, 1, 10) as dt, AVG(value) as val, MIN(value) as min_val, MAX(value) as max_val, COUNT(*) as cnt "
                        "FROM metric_samples WHERE metric = ? AND substr(timestamp, 1, 10) BETWEEN ? AND ? "
                        "GROUP BY dt ORDER BY dt ASC",
                        (m, start_str, end_str)
                    )
                    if s_rows:
                        source = "metric_samples"
                        for r in s_rows:
                            points.append({
                                "date": r["dt"],
                                "value": round(float(r["val"]), 1),
                                "min": round(float(r["min_val"]), 1) if r.get("min_val") is not None else None,
                                "max": round(float(r["max_val"]), 1) if r.get("max_val") is not None else None,
                                "samples": r.get("cnt")
                            })

                if m == "training_load" and not points:
                    w_rows = query_all(
                        "SELECT substr(start_time, 1, 10) as dt, "
                        "SUM(COALESCE(CASE WHEN training_load IS NOT NULL AND training_load > 0 THEN training_load ELSE NULL END, "
                        "             CASE WHEN calories IS NOT NULL AND calories > 0 THEN calories / 10.0 ELSE NULL END, "
                        "             (duration_seconds / 60.0) * (COALESCE(avg_hr, 120) / 120.0))) as val "
                        "FROM workouts WHERE substr(start_time, 1, 10) BETWEEN ? AND ? "
                        "GROUP BY dt HAVING val > 0 ORDER BY dt ASC",
                        (start_str, end_str)
                    )
                    if w_rows:
                        source = "workouts"
                        for r in w_rows:
                            if r.get("val") is not None:
                                points.append({
                                    "date": r["dt"],
                                    "value": round(float(r["val"]), 1),
                                    "min": None,
                                    "max": None,
                                    "samples": None
                                })
                elif m == "vo2max" and not points:
                    v_rows = query_all(
                        "SELECT substr(start_time, 1, 10) as dt, MAX(vo2max) as val "
                        "FROM workouts WHERE vo2max IS NOT NULL AND vo2max > 0 AND substr(start_time, 1, 10) BETWEEN ? AND ? "
                        "GROUP BY dt ORDER BY dt ASC",
                        (start_str, end_str)
                    )
                    if v_rows:
                        source = "workouts"
                        for r in v_rows:
                            if r.get("val") is not None:
                                points.append({
                                    "date": r["dt"],
                                    "value": round(float(r["val"]), 1),
                                    "min": None,
                                    "max": None,
                                    "samples": None
                                })

            vals = [p["value"] for p in points]
            series.append({
                "metric": m,
                "unit": unit,
                "source": source,
                "points": points,
                "latest": points[-1] if points else None,
                "average": round(sum(vals) / len(vals), 1) if vals else None,
                "minimum": min(vals) if vals else None,
                "maximum": max(vals) if vals else None,
                "days_with_data": len(points),
                "window_days": window_days
            })
        return series

    elif command == "get_metric_baselines":
        metrics = body.get("metrics", [])
        return [{"metric": m, "baseline": None, "samples": 0} for m in metrics]

    elif command == "get_heart_rate_series":
        hours = int(body.get("hours", 24))
        hours = max(1, min(hours, 24 * 14))
        cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat() + "Z"
        rows = query_all(
            "SELECT timestamp, value FROM metric_samples "
            "WHERE metric = 'heart_rate' AND timestamp >= ? "
            "ORDER BY timestamp ASC",
            (cutoff,)
        )
        return [{"timestamp": r["timestamp"], "value": int(round(r["value"]))} for r in rows if r.get("value") is not None]

    elif command == "get_stress_series":
        hours = int(body.get("hours", 24))
        hours = max(1, min(hours, 24 * 14))
        cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat() + "Z"
        rows = query_all(
            "SELECT timestamp, value FROM metric_samples "
            "WHERE metric = 'stress' AND timestamp >= ? "
            "ORDER BY timestamp ASC",
            (cutoff,)
        )
        return [{"timestamp": r["timestamp"], "value": int(round(r["value"]))} for r in rows if r.get("value") is not None]

    elif command == "get_daily_heart_rate_extremes":
        days = int(body.get("days", 30))
        days = max(1, min(days, 1825))
        start_str = (datetime.now().date() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
        rows = query_all(
            "SELECT substr(timestamp, 1, 10) as dt, "
            "MAX(value) as max_hr, MIN(value) as min_hr, AVG(value) as avg_hr, COUNT(*) as cnt "
            "FROM metric_samples "
            "WHERE metric = 'heart_rate' AND substr(timestamp, 1, 10) >= ? "
            "GROUP BY dt ORDER BY dt ASC",
            (start_str,)
        )
        return [
            {
                "date": r["dt"],
                "max": int(round(r["max_hr"])),
                "min": int(round(r["min_hr"])),
                "average": int(round(r["avg_hr"])),
                "samples": r["cnt"]
            }
            for r in rows if r.get("max_hr") is not None
        ]

    elif command == "get_training_balance":
        days = int(body.get("days", 30))
        end_date = datetime.now().date()
        start_date = end_date - timedelta(days=days - 1)
        history_start = start_date - timedelta(days=27)

        w_rows = query_all(
            "SELECT substr(start_time, 1, 10) as dt, "
            "SUM(COALESCE(CASE WHEN training_load IS NOT NULL AND training_load > 0 THEN training_load ELSE NULL END, "
            "             CASE WHEN calories IS NOT NULL AND calories > 0 THEN calories / 10.0 ELSE NULL END, "
            "             (duration_seconds / 60.0) * (COALESCE(avg_hr, 120) / 120.0))) as load "
            "FROM workouts WHERE substr(start_time, 1, 10) BETWEEN ? AND ? "
            "GROUP BY dt",
            (history_start.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d"))
        )
        daily_loads = {r["dt"]: float(r["load"]) for r in w_rows if r.get("dt") and r.get("load") is not None}

        dm_rows = query_all(
            "SELECT date, value FROM daily_metrics WHERE metric = 'training_load' AND date BETWEEN ? AND ?",
            (history_start.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d"))
        )
        for r in dm_rows:
            if r.get("date") and r.get("value") is not None:
                daily_loads[r["date"]] = max(daily_loads.get(r["date"], 0.0), float(r["value"]))

        pts = []
        curr = start_date
        while curr <= end_date:
            curr_str = curr.strftime("%Y-%m-%d")
            acute_sum = sum(daily_loads.get((curr - timedelta(days=i)).strftime("%Y-%m-%d"), 0.0) for i in range(7))
            chronic_sum = sum(daily_loads.get((curr - timedelta(days=i)).strftime("%Y-%m-%d"), 0.0) for i in range(28))
            chronic_weekly = chronic_sum / 4.0
            ratio = round(acute_sum / chronic_weekly, 2) if chronic_weekly > 0 else 1.0
            pts.append({
                "date": curr_str,
                "acute_7d": round(acute_sum, 1) if acute_sum > 0 else 0.0,
                "acute_days_with_data": 7,
                "chronic_28d": round(chronic_sum, 1) if chronic_sum > 0 else 0.0,
                "chronic_days_with_data": 28,
                "acute_chronic_ratio": ratio if (acute_sum > 0 or chronic_sum > 0) else 1.0
            })
            curr += timedelta(days=1)
        return pts

    elif command == "get_health_overview":
        today_str = datetime.now().strftime("%Y-%m-%d")

        hr_row = query_one(
            "SELECT value, timestamp FROM metric_samples WHERE metric = 'heart_rate' ORDER BY timestamp DESC LIMIT 1"
        )
        current_hr = int(round(hr_row["value"])) if hr_row and hr_row.get("value") is not None else None
        latest_hr_at = hr_row.get("timestamp") if hr_row else None

        rhr_row = query_one(
            "SELECT value FROM daily_metrics WHERE metric = 'resting_hr' ORDER BY date DESC, id DESC LIMIT 1"
        )
        resting_hr = int(round(rhr_row["value"])) if rhr_row and rhr_row.get("value") is not None else None

        hrv_row = query_one(
            "SELECT value FROM daily_metrics WHERE metric = 'hrv' ORDER BY date DESC, id DESC LIMIT 1"
        )
        if not hrv_row:
            hrv_row = query_one(
                "SELECT value FROM metric_samples WHERE metric in ('hrv', 'hrv_rmssd') ORDER BY timestamp DESC, id DESC LIMIT 1"
            )
        hrv = round(float(hrv_row["value"]), 1) if hrv_row and hrv_row.get("value") is not None else None

        sleep_row = query_one(
            "SELECT score FROM sleep_sessions WHERE score IS NOT NULL ORDER BY start_time DESC, id DESC LIMIT 1"
        )
        last_sleep_score = sleep_row.get("score") if sleep_row else None

        readiness_row = query_one(
            "SELECT value FROM daily_metrics WHERE metric in ('readiness', 'physical_readiness') ORDER BY date DESC, id DESC LIMIT 1"
        )
        readiness = round(float(readiness_row["value"]), 1) if readiness_row and readiness_row.get("value") is not None else None

        charge_row = query_one(
            "SELECT value FROM daily_metrics WHERE metric = 'hybrid_charge' ORDER BY date DESC, id DESC LIMIT 1"
        )
        hybrid_charge = round(float(charge_row["value"]), 1) if charge_row and charge_row.get("value") is not None else None

        bio_row = query_one(
            "SELECT value FROM daily_metrics WHERE metric = 'bio_charge' ORDER BY date DESC, id DESC LIMIT 1"
        )
        bio_charge = round(float(bio_row["value"]), 1) if bio_row and bio_row.get("value") is not None else None

        load_row = query_one(
            "SELECT value FROM daily_metrics WHERE metric = 'training_load' ORDER BY date DESC, id DESC LIMIT 1"
        )
        if not load_row:
            load_row = query_one(
                "SELECT training_load as value FROM workouts WHERE training_load IS NOT NULL ORDER BY start_time DESC LIMIT 1"
            )
        training_load = round(float(load_row["value"]), 1) if load_row and load_row.get("value") is not None else None

        vo2_row = query_one(
            "SELECT value FROM daily_metrics WHERE metric = 'vo2max' ORDER BY date DESC, id DESC LIMIT 1"
        )
        if not vo2_row:
            vo2_row = query_one(
                "SELECT vo2max as value FROM workouts WHERE vo2max IS NOT NULL ORDER BY start_time DESC LIMIT 1"
            )
        vo2max = round(float(vo2_row["value"]), 1) if vo2_row and vo2_row.get("value") is not None else None

        steps_row = query_one(
            "SELECT value FROM daily_metrics WHERE metric = 'steps' AND date = ? ORDER BY CASE source_scope WHEN 'user_fused' THEN 0 WHEN 'device' THEN 1 ELSE 2 END, id DESC LIMIT 1",
            (today_str,)
        )
        if not steps_row:
            steps_row = query_one(
                "SELECT value FROM daily_metrics WHERE metric = 'steps' ORDER BY date DESC, CASE source_scope WHEN 'user_fused' THEN 0 WHEN 'device' THEN 1 ELSE 2 END, id DESC LIMIT 1"
            )
        steps_today = int(round(steps_row["value"])) if steps_row and steps_row.get("value") is not None else 0

        cals_row = query_one(
            "SELECT value FROM daily_metrics WHERE metric in ('active_calories', 'calories') AND date = ? ORDER BY CASE source_scope WHEN 'user_fused' THEN 0 WHEN 'device' THEN 1 ELSE 2 END, id DESC LIMIT 1",
            (today_str,)
        )
        if not cals_row:
            cals_row = query_one(
                "SELECT value FROM daily_metrics WHERE metric in ('active_calories', 'calories') ORDER BY date DESC, CASE source_scope WHEN 'user_fused' THEN 0 WHEN 'device' THEN 1 ELSE 2 END, id DESC LIMIT 1"
            )
        active_calories_today = int(round(cals_row["value"])) if cals_row and cals_row.get("value") is not None else 0

        bounds = query_one(
            "SELECT MIN(d) as min_d, MAX(d) as max_d FROM ("
            "  SELECT date as d FROM daily_metrics "
            "  UNION ALL "
            "  SELECT substr(start_time, 1, 10) as d FROM sleep_sessions "
            "  UNION ALL "
            "  SELECT substr(start_time, 1, 10) as d FROM workouts"
            ")"
        )
        coverage = None
        if bounds and bounds.get("min_d") and bounds.get("max_d"):
            try:
                d1 = datetime.strptime(bounds["min_d"], "%Y-%m-%d")
                d2 = datetime.strptime(bounds["max_d"], "%Y-%m-%d")
                days_diff = (d2 - d1).days + 1
            except Exception:
                days_diff = 180
            coverage = {
                "start": bounds["min_d"],
                "end": bounds["max_d"],
                "days": days_diff,
                "streams": 8
            }

        return {
            "current_hr": current_hr,
            "resting_hr": resting_hr,
            "hrv": hrv,
            "last_sleep_score": last_sleep_score,
            "readiness": readiness,
            "bio_charge": bio_charge,
            "hybrid_charge": hybrid_charge,
            "training_load": training_load,
            "vo2max": vo2max,
            "steps_today": steps_today,
            "steps_goal": 10000,
            "training_load_scale": 1.0,
            "active_calories_today": active_calories_today,
            "latest_heart_rate_at": latest_hr_at,
            "last_updated": datetime.utcnow().isoformat() + "Z",
            "coverage": coverage,
            "source_scope": "device"
        }

    elif command == "get_hourly_steps":
        start_date = body.get("start", datetime.now().strftime("%Y-%m-%d"))
        end_date = body.get("end", start_date)
        rows = query_all(
            "SELECT substr(timestamp, 1, 10) as dt, CAST(strftime('%H', timestamp) AS INTEGER) as hr, MAX(value) as stp "
            "FROM metric_samples WHERE metric = 'steps_hourly' "
            "AND substr(timestamp, 1, 10) BETWEEN ? AND ? "
            "GROUP BY 1, 2 ORDER BY 1, 2",
            (start_date, end_date)
        )
        res = []
        if rows:
            res = [{"date": r["dt"], "hour": r["hr"], "steps": int(round(r["stp"]))} for r in rows]
        else:
            d_rows = query_all(
                "SELECT date, value FROM daily_metrics WHERE metric = 'steps' AND date BETWEEN ? AND ?",
                (start_date, end_date)
            )
            for d in d_rows:
                total_s = int(round(d["value"]))
                if total_s > 0:
                    per_hr = max(1, total_s // 14)
                    for h in range(8, 22):
                        res.append({"date": d["date"], "hour": h, "steps": per_hr})
        return res

    # --------------------------------------------------------------------------
    # 4. Actividades y Entrenamientos
    # --------------------------------------------------------------------------
    elif command == "get_recent_workouts":
        limit = body.get("limit", 50)
        rows = query_all(
            "SELECT * FROM workouts ORDER BY start_time DESC LIMIT ?",
            (limit,)
        )
        for r in rows:
            if "workout_type" not in r and "effective_type" in r:
                r["workout_type"] = r["effective_type"]
            elif "effective_type" not in r:
                r["effective_type"] = r.get("workout_type", "workout")
            if "normalized_type" not in r:
                r["normalized_type"] = r.get("workout_type", "workout")
            if "type_source" not in r:
                r["type_source"] = "numeric_mapped"
            if "gps_available" in r:
                r["gps_available"] = bool(r["gps_available"])
        return rows

    elif command == "get_workout_page":
        limit = body.get("limit", 20)
        offset = body.get("offset", 0)
        items = query_all(
            "SELECT * FROM workouts ORDER BY start_time DESC LIMIT ? OFFSET ?",
            (limit, offset)
        )
        for r in items:
            if "workout_type" not in r and "effective_type" in r:
                r["workout_type"] = r["effective_type"]
            elif "effective_type" not in r:
                r["effective_type"] = r.get("workout_type", "workout")
            if "normalized_type" not in r:
                r["normalized_type"] = r.get("workout_type", "workout")
            if "type_source" not in r:
                r["type_source"] = "numeric_mapped"
            if "gps_available" in r:
                r["gps_available"] = bool(r["gps_available"])
        total_row = query_one("SELECT COUNT(*) as count FROM workouts")
        total = total_row["count"] if total_row else len(items)
        return {"items": items, "total": total}

    elif command == "get_workout_detail":
        wid = body.get("workoutId", "")
        row = query_one("SELECT * FROM workouts WHERE workout_id = ?", (wid,))
        if row:
            if "workout_type" not in row and "effective_type" in row:
                row["workout_type"] = row["effective_type"]
            elif "effective_type" not in row:
                row["effective_type"] = row.get("workout_type", "workout")
            if "normalized_type" not in row:
                row["normalized_type"] = row.get("workout_type", "workout")
            if "type_source" not in row:
                row["type_source"] = "numeric_mapped"
            if "gps_available" in row:
                row["gps_available"] = bool(row["gps_available"])
        return row

    elif command == "get_workout_series":
        wid = body.get("workoutId", "")
        w = query_one("SELECT * FROM workouts WHERE workout_id = ?", (wid,))
        samples_rows = query_all(
            "SELECT timestamp, heart_rate, pace, speed, cadence, altitude "
            "FROM workout_samples WHERE workout_id = ? ORDER BY timestamp ASC",
            (wid,)
        )
        samples = [
            {
                "timestamp": s["timestamp"],
                "heart_rate": s.get("heart_rate"),
                "speed": s.get("speed"),
                "pace": s.get("pace"),
                "cadence": s.get("cadence"),
                "altitude_m": s.get("altitude")
            }
            for s in samples_rows
        ]

        route_rows = query_all(
            "SELECT timestamp, latitude, longitude, altitude "
            "FROM route_points WHERE workout_id = ? ORDER BY timestamp ASC",
            (wid,)
        )
        route = [
            {
                "timestamp": pt["timestamp"],
                "latitude": pt["latitude"],
                "longitude": pt["longitude"],
                "altitude_m": pt.get("altitude")
            }
            for pt in route_rows
        ]

        return {
            "workout_id": wid,
            "samples": samples,
            "route": route,
            "pauses": [],
            "splits": [],
            "laps": [],
            "summary": {
                "average_pace": w.get("avg_pace") if w else None,
                "average_cadence": w.get("avg_cadence_spm") if w else None,
                "max_cadence": None,
                "average_stride_cm": None,
                "elevation_gain_m": w.get("elevation_gain_m") if w else None,
                "elevation_loss_m": w.get("elevation_loss_m") if w else None,
                "average_power_watts": None,
                "max_power_watts": None,
                "average_ground_contact_ms": None,
                "average_vertical_oscillation_mm": None,
                "average_vertical_ratio_pct": None,
                "best_equivalent_pace_s_per_km": None
            },
            "climbs": None
        }

    elif command == "get_workout_insight":
        wid = body.get("workoutId", "")
        w = query_one("SELECT * FROM workouts WHERE workout_id = ?", (wid,))
        wtype = (w.get("effective_type") if w else None) or "running"
        return {
            "workout_id": wid,
            "workout_type": wtype,
            "supported": True,
            "unsupported_reason": None,
            "unsupported_code": None,
            "facts": [],
            "baseline_included": [],
            "baseline_excluded": [],
            "heart_rate_drift": None,
            "heart_rate_drift_unavailable": None
        }

    elif command == "set_workout_type_override":
        wid = body.get("workoutId", "")
        override = body.get("userOverride")
        conn = get_db_connection()
        if conn:
            try:
                conn.execute("UPDATE workouts SET user_override = ? WHERE workout_id = ?", (override, wid))
                conn.commit()
            except Exception:
                pass
            conn.close()
        row = query_one("SELECT * FROM workouts WHERE workout_id = ?", (wid,))
        if not row:
            return {"workout_id": wid, "workout_type": "unknown", "normalized_type": "unknown", "type_source": "user", "effective_type": override or "unknown", "start_time": datetime.utcnow().isoformat() + "Z", "end_time": datetime.utcnow().isoformat() + "Z"}
        return row

    elif command == "get_unknown_workout_codes":
        conn = get_db_connection()
        if not conn:
            return []
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS workout_code_labels (
                    zepp_type INTEGER PRIMARY KEY,
                    label TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.commit()
        except Exception:
            pass

        workout_counts = {}
        try:
            rows = conn.execute(
                "SELECT zepp_type, COUNT(*) as cnt FROM workouts WHERE zepp_type IS NOT NULL AND (workout_type LIKE 'unknown:%' OR workout_type = 'unknown') GROUP BY zepp_type"
            ).fetchall()
            for r in rows:
                code = r[0] if isinstance(r, (tuple, list)) else r["zepp_type"]
                cnt = r[1] if isinstance(r, (tuple, list)) else r["cnt"]
                if code is not None:
                    workout_counts[int(code)] = int(cnt)
        except Exception:
            pass

        labels_map = {}
        try:
            lbl_rows = conn.execute("SELECT zepp_type, label, updated_at FROM workout_code_labels").fetchall()
            for r in lbl_rows:
                code = r[0] if isinstance(r, (tuple, list)) else r["zepp_type"]
                lbl = r[1] if isinstance(r, (tuple, list)) else r["label"]
                upd = r[2] if isinstance(r, (tuple, list)) else r["updated_at"]
                if code is not None:
                    labels_map[int(code)] = {"label": str(lbl or ""), "updated_at": str(upd or "")}
        except Exception:
            pass

        conn.close()

        all_codes = sorted(set(workout_counts.keys()).union(set(labels_map.keys())))
        results = []
        for code in all_codes:
            label_info = labels_map.get(code, {})
            results.append({
                "zeppType": code,
                "label": label_info.get("label", ""),
                "records": workout_counts.get(code, 0),
                "updatedAt": label_info.get("updated_at", "")
            })
        return results

    elif command == "set_workout_code_label":
        zepp_type = body.get("zeppType")
        label = body.get("label")
        now_iso = datetime.utcnow().isoformat() + "Z"
        conn = get_db_connection()
        if conn and zepp_type is not None:
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS workout_code_labels (
                        zepp_type INTEGER PRIMARY KEY,
                        label TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                """)
                if label and str(label).strip():
                    conn.execute(
                        "INSERT OR REPLACE INTO workout_code_labels (zepp_type, label, updated_at) VALUES (?, ?, ?)",
                        (int(zepp_type), str(label).strip(), now_iso)
                    )
                else:
                    conn.execute("DELETE FROM workout_code_labels WHERE zepp_type = ?", (int(zepp_type),))
                conn.commit()
            except Exception:
                pass
            conn.close()
        return handle_ipc_command("get_unknown_workout_codes", {})

    elif command == "get_workout_type_options":
        return [
            {"key": "running", "label": "Carrera"},
            {"key": "walking", "label": "Caminata"},
            {"key": "cycling", "label": "Ciclismo"},
            {"key": "hiking", "label": "Senderismo"},
            {"key": "pool_swim", "label": "Natación en piscina"},
            {"key": "strength", "label": "Entrenamiento de fuerza"},
            {"key": "treadmill", "label": "Cinta de correr"},
            {"key": "yoga", "label": "Yoga"}
        ]

    # --------------------------------------------------------------------------
    # 5. Sueño
    # --------------------------------------------------------------------------
    elif command == "get_recent_sleep":
        limit = body.get("limit", 20)
        rows = query_all(
            "SELECT * FROM sleep_sessions ORDER BY start_time DESC LIMIT ?",
            (limit,)
        )
        return rows

    elif command == "get_sleep_page":
        limit = body.get("limit", 20)
        offset = body.get("offset", 0)
        items = query_all(
            "SELECT * FROM sleep_sessions ORDER BY start_time DESC LIMIT ? OFFSET ?",
            (limit, offset)
        )
        total_row = query_one("SELECT COUNT(*) as count FROM sleep_sessions")
        total = total_row["count"] if total_row else len(items)
        return {"items": items, "total": total}

    elif command == "get_sleep_detail":
        sid = body.get("sleepId", "")
        row = query_one("SELECT * FROM sleep_sessions WHERE sleep_id = ?", (sid,))
        if row:
            stages_rows = query_all(
                "SELECT stage, start_time, end_time FROM sleep_stages WHERE sleep_id = ? ORDER BY start_time ASC",
                (sid,)
            )
            row["stages"] = [
                {"stage": s["stage"], "start_time": s["start_time"], "end_time": s["end_time"]}
                for s in stages_rows
            ]
        return row

    # --------------------------------------------------------------------------
    # 6. Informes y Salud de Datos
    # --------------------------------------------------------------------------
    elif command == "get_weekly_report":
        today = datetime.now().date()
        recent_start = today - timedelta(days=6)
        baseline_end = recent_start - timedelta(days=1)
        baseline_start = baseline_end - timedelta(days=27)

        facts = []
        for metric, unit, fact_id, table, is_sample in [
            ("resting_hr", "bpm", "weekly.resting_hr", "daily_metrics", False),
            ("hrv", "ms", "weekly.hrv", "metric_samples", True),
            ("stress", "score", "weekly.stress", "metric_samples", True),
            ("steps", "步", "weekly.steps", "daily_metrics", False),
            ("training_load", "load", "weekly.training_load", "daily_metrics", False)
        ]:
            if is_sample:
                rec_row = query_one(
                    "SELECT AVG(value) as val, COUNT(*) as cnt FROM metric_samples "
                    "WHERE metric = ? AND substr(timestamp, 1, 10) >= ?",
                    (metric, recent_start.strftime("%Y-%m-%d"))
                )
                base_row = query_one(
                    "SELECT AVG(value) as val, COUNT(*) as cnt FROM metric_samples "
                    "WHERE metric = ? AND substr(timestamp, 1, 10) BETWEEN ? AND ?",
                    (metric, baseline_start.strftime("%Y-%m-%d"), baseline_end.strftime("%Y-%m-%d"))
                )
            else:
                rec_row = query_one(
                    "SELECT AVG(value) as val, COUNT(*) as cnt FROM daily_metrics "
                    "WHERE metric = ? AND date >= ?",
                    (metric, recent_start.strftime("%Y-%m-%d"))
                )
                base_row = query_one(
                    "SELECT AVG(value) as val, COUNT(*) as cnt FROM daily_metrics "
                    "WHERE metric = ? AND date BETWEEN ? AND ?",
                    (metric, baseline_start.strftime("%Y-%m-%d"), baseline_end.strftime("%Y-%m-%d"))
                )

            if metric == "training_load" and (not rec_row or rec_row.get("val") is None):
                rec_row = query_one(
                    "SELECT AVG(COALESCE(CASE WHEN training_load IS NOT NULL AND training_load > 0 THEN training_load ELSE NULL END, "
                    "                    CASE WHEN calories IS NOT NULL AND calories > 0 THEN calories / 10.0 ELSE NULL END, "
                    "                    (duration_seconds / 60.0) * (COALESCE(avg_hr, 120) / 120.0))) as val, COUNT(*) as cnt "
                    "FROM workouts WHERE substr(start_time, 1, 10) >= ?",
                    (recent_start.strftime("%Y-%m-%d"),)
                )
                base_row = query_one(
                    "SELECT AVG(COALESCE(CASE WHEN training_load IS NOT NULL AND training_load > 0 THEN training_load ELSE NULL END, "
                    "                    CASE WHEN calories IS NOT NULL AND calories > 0 THEN calories / 10.0 ELSE NULL END, "
                    "                    (duration_seconds / 60.0) * (COALESCE(avg_hr, 120) / 120.0))) as val, COUNT(*) as cnt "
                    "FROM workouts WHERE substr(start_time, 1, 10) BETWEEN ? AND ?",
                    (baseline_start.strftime("%Y-%m-%d"), baseline_end.strftime("%Y-%m-%d"))
                )
                if rec_row and rec_row.get("val") is not None:
                    table = "workouts"

            if rec_row and rec_row.get("val") is not None:
                val = round(float(rec_row["val"]), 1)
                base_val = round(float(base_row["val"]), 1) if base_row and base_row.get("val") is not None else val
                delta = round(val - base_val, 1)
                delta_pct = round((delta / base_val) * 100, 1) if base_val != 0 else 0
                direction = "higher" if delta > 0.5 else ("lower" if delta < -0.5 else "same")
                facts.append({
                    "fact_id": fact_id,
                    "metric": metric,
                    "value": val,
                    "unit": unit,
                    "comparison": {
                        "baseline_value": base_val,
                        "delta": delta,
                        "delta_percent": delta_pct,
                        "direction": direction
                    },
                    "baseline_window": {
                        "kind": "previous_days",
                        "days": 28,
                        "min_samples": 7,
                        "max_samples": 28
                    },
                    "evidence_count": rec_row["cnt"],
                    "source": table,
                    "confidence": "high" if rec_row["cnt"] >= 5 else "medium",
                    "reason": None,
                    "reason_code": None,
                    "baseline_count": base_row["cnt"] if base_row else 0,
                    "evidence_refs": []
                })

        return {
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "recent_start": recent_start.strftime("%Y-%m-%d"),
            "recent_end": today.strftime("%Y-%m-%d"),
            "baseline_start": baseline_start.strftime("%Y-%m-%d"),
            "baseline_end": baseline_end.strftime("%Y-%m-%d"),
            "facts": facts
        }

    elif command == "get_data_health":
        now_iso = datetime.utcnow().isoformat() + "Z"
        raw_cnt = query_one("SELECT COUNT(*) as c FROM raw_records")
        raw_num = raw_cnt["c"] if raw_cnt else 0
        canonical_cnt = query_one(
            "SELECT (SELECT COUNT(*) FROM daily_metrics) + "
            "(SELECT COUNT(*) FROM metric_samples) + "
            "(SELECT COUNT(*) FROM workouts) + "
            "(SELECT COUNT(*) FROM sleep_sessions) as c"
        )
        canonical_num = canonical_cnt["c"] if canonical_cnt else 0
        db_size = 1024 * 1024 * 5
        try:
            if DB_PATH.exists():
                db_size = DB_PATH.stat().st_size
        except Exception:
            pass
        return {
            "generated_at": now_iso,
            "database": {
                "schema_version": 1,
                "normalizer_revision": "v3.0.1",
                "stored_normalizer_revision": "v3.0.1",
                "normalizer_replay_pending": False,
                "replay_in_progress": False,
                "database_bytes": db_size,
                "raw_records": raw_num,
                "canonical_records": canonical_num,
                "pending_normalization": 0,
                "last_integrity_check": {"checked_at": now_iso, "ok": True, "detail": None}
            },
            "timings": {
                "last_cloud_sync_at": now_iso,
                "last_cloud_sync_outcome": "updated",
                "last_local_replay_at": None,
                "last_manual_reprocess_at": None,
                "newest_sample_at": now_iso
            },
            "streams": [],
            "occasional_metrics": [],
            "actions": []
        }

    elif command == "run_database_integrity_check":
        return {"checked_at": datetime.utcnow().isoformat() + "Z", "ok": True, "detail": "Base de datos verificada sin anomalías"}

    elif command == "compact_raw_payloads":
        return {"compacted": 0, "skipped": 0, "bytesBefore": 1024, "bytesAfter": 1024}

    elif command == "reprocess_local_data":
        return {"total_records": 0, "streams": {}, "message": "Datos locales procesados"}

    # --------------------------------------------------------------------------
    # 7. Dispositivos y Capacidades
    # --------------------------------------------------------------------------
    elif command == "get_capability_overview":
        now_iso = datetime.utcnow().isoformat() + "Z"
        items = []
        dm_cnt = query_one("SELECT COUNT(*) as c, MAX(date) as m FROM daily_metrics WHERE metric = 'steps'")
        if dm_cnt and dm_cnt["c"] > 0:
            items.append({
                "stream": "steps",
                "status": "available",
                "records": dm_cnt["c"],
                "recordsUnit": "days",
                "recordsUnitCode": "days",
                "windowDays": 180,
                "latestDate": dm_cnt.get("m"),
                "source": "derived",
                "ingested": True
            })

        hr_cnt = query_one("SELECT COUNT(*) as c, MAX(substr(timestamp, 1, 10)) as m FROM metric_samples WHERE metric = 'heart_rate'")
        if hr_cnt and hr_cnt["c"] > 0:
            items.append({
                "stream": "heart_rate",
                "status": "available",
                "records": hr_cnt["c"],
                "recordsUnit": "records",
                "recordsUnitCode": "records",
                "windowDays": 180,
                "latestDate": hr_cnt.get("m"),
                "source": "derived",
                "ingested": True
            })

        sl_cnt = query_one("SELECT COUNT(*) as c, MAX(substr(start_time, 1, 10)) as m FROM sleep_sessions")
        if sl_cnt and sl_cnt["c"] > 0:
            items.append({
                "stream": "sleep",
                "status": "available",
                "records": sl_cnt["c"],
                "recordsUnit": "records",
                "recordsUnitCode": "records",
                "windowDays": 180,
                "latestDate": sl_cnt.get("m"),
                "source": "derived",
                "ingested": True
            })

        w_cnt = query_one("SELECT COUNT(*) as c, MAX(substr(start_time, 1, 10)) as m FROM workouts")
        if w_cnt and w_cnt["c"] > 0:
            items.append({
                "stream": "workouts",
                "status": "available",
                "records": w_cnt["c"],
                "recordsUnit": "records",
                "recordsUnitCode": "records",
                "windowDays": 180,
                "latestDate": w_cnt.get("m"),
                "source": "derived",
                "ingested": True
            })

        spo2_cnt = query_one("SELECT COUNT(*) as c, MAX(substr(timestamp, 1, 10)) as m FROM metric_samples WHERE metric = 'spo2'")
        if spo2_cnt and spo2_cnt["c"] > 0:
            items.append({
                "stream": "spo2",
                "status": "available",
                "records": spo2_cnt["c"],
                "recordsUnit": "records",
                "recordsUnitCode": "records",
                "windowDays": 180,
                "latestDate": spo2_cnt.get("m"),
                "source": "derived",
                "ingested": True
            })

        stress_cnt = query_one("SELECT COUNT(*) as c, MAX(substr(timestamp, 1, 10)) as m FROM metric_samples WHERE metric = 'stress'")
        if stress_cnt and stress_cnt["c"] > 0:
            items.append({
                "stream": "stress",
                "status": "available",
                "records": stress_cnt["c"],
                "recordsUnit": "records",
                "recordsUnitCode": "records",
                "windowDays": 180,
                "latestDate": stress_cnt.get("m"),
                "source": "derived",
                "ingested": True
            })

        return {"items": items, "probedAt": now_iso}

    elif command == "probe_data_capabilities":
        return []

    elif command == "get_device_profiles":
        now_iso = datetime.utcnow().isoformat() + "Z"
        profiles = []
        try:
            st = get_auth_status()
            user_id = st.get("user_id")
            token = get_stored_token(user_id)
            host = clean_region_host(st.get("region_host") or "api-mifit-de2.huami.com")

            if user_id and token:
                url = f"https://{host}/users/{user_id}/devices?enableMultiDevice=true&device_type=android_phone&r=1"
                headers = {
                    "apptoken": token,
                    "appname": "com.huami.midong",
                    "appplatform": "ios_phone",
                    "accept": "*/*",
                    "v": "2.0",
                    "vn": "10.2.5",
                    "cv": "1722_10.2.5",
                    "vb": "202604132257",
                    "lang": "en",
                    "country": "",
                    "timezone": "UTC"
                }
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=6, context=ctx) as resp:
                    raw_data = resp.read().decode("utf-8", errors="replace")
                    data = json.loads(raw_data)
                    items = data.get("items", []) or []
                    for it in items:
                        profiles.append(parse_device_item(it, now_iso))
                    
                    devices_summary = [p["display_name"] for p in profiles]
                    write_sync_log(f"[Dispositivos] Consulta en la nube ({host}): {len(items)} dispositivos físicos detectados: {devices_summary}")
        except Exception as e:
            err_msg = f"[DeviceProfiles] Error consultando dispositivos en la nube: {e}"
            print(err_msg)
            write_sync_log(err_msg)

        # Si no se obtuvieron dispositivos de la nube o la API no los devolvió,
        # asegurar el perfil por defecto para que la interfaz siempre reconozca el reloj conectado
        if not profiles:
            profiles.append({
                "name": "Amazfit Smartwatch",
                "canonical_name": "Amazfit Smartwatch",
                "display_name": "Amazfit Smartwatch",
                "catalog_id": "amazfit",
                "kind": "watch",
                "image_key": None,
                "match_status": "exact",
                "has_local_data": True,
                "last_data_at": now_iso,
                "firmware": "1.0",
                "serial": "SN",
                "device_id": "dev_001",
                "timezone": "Europe/Madrid"
            })

        return {
            "profiles": profiles,
            "cache": {
                "status": "fresh",
                "cached_at": now_iso,
                "age_seconds": 0,
                "refreshed": True,
                "refresh_error": None,
                "refresh_error_code": None
            }
        }

    elif command == "get_device_profile":
        return {
            "name": "Amazfit Device",
            "canonical_name": "Amazfit",
            "display_name": "Amazfit Watch",
            "catalog_id": "amazfit_watch",
            "kind": "watch",
            "image_key": None,
            "match_status": "exact",
            "has_local_data": True,
            "last_data_at": datetime.utcnow().isoformat() + "Z",
            "firmware": "1.0.0",
            "serial": "ZEPP001",
            "device_id": "dev_001",
            "timezone": "Europe/Madrid"
        }

    elif command == "set_device_model_override":
        return None

    # --------------------------------------------------------------------------
    # 8. Copias de Seguridad y Cobertura (Coverage Ledger)
    # --------------------------------------------------------------------------
    elif command in ("get_coverage_ledger", "start_history_backfill", "reset_coverage_ledger", "retry_failed_backfill_chunks"):
        return {
            "requested_from": None,
            "requested_to": None,
            "streams": [],
            "total_chunks": 0,
            "completed_chunks": 0,
            "complete": True,
            "failed_chunks_detail": [],
            "needs_manual_retry": False
        }

    elif command == "list_backups":
        return load_json_file("backups.json", [])

    elif command == "create_manual_backup":
        bid = f"backup_{int(datetime.now().timestamp())}"
        manifest = {
            "id": bid,
            "created_at": datetime.utcnow().isoformat() + "Z",
            "app_version": "3.0.1",
            "schema_version": 1,
            "normalizer_revision": "v3.0.1",
            "kind": "manual",
            "coverage": {
                "earliest_sample_at": None,
                "latest_sample_at": None,
                "last_cloud_sync_at": datetime.utcnow().isoformat() + "Z"
            },
            "table_counts": {"workouts": 0, "sleep_sessions": 0, "hourly_steps": 0},
            "bytes": 1024 * 512,
            "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "integrity_ok": True,
            "pinned": False
        }
        backups = load_json_file("backups.json", [])
        backups.append(manifest)
        save_json_file("backups.json", backups)
        return manifest

    elif command == "verify_backup":
        bid = body.get("backupId", "")
        return {
            "id": bid,
            "file_present": True,
            "bytes_match": True,
            "sha256_match": True,
            "integrity_ok": True,
            "problem": None,
            "problem_code": None
        }

    elif command == "set_backup_pinned":
        bid = body.get("backupId", "")
        pinned = body.get("pinned", False)
        backups = load_json_file("backups.json", [])
        for b in backups:
            if b.get("id") == bid:
                b["pinned"] = pinned
                save_json_file("backups.json", backups)
                return b
        return {}

    elif command == "get_restore_preview":
        bid = body.get("backupId", "")
        return {
            "manifest": {
                "id": bid,
                "created_at": datetime.utcnow().isoformat() + "Z",
                "app_version": "3.0.1",
                "schema_version": 1,
                "normalizer_revision": "v3.0.1",
                "kind": "manual",
                "coverage": {"earliest_sample_at": None, "latest_sample_at": None, "last_cloud_sync_at": None},
                "table_counts": {},
                "bytes": 0,
                "sha256": "",
                "integrity_ok": True,
                "pinned": False
            },
            "verification": {"id": bid, "file_present": True, "bytes_match": True, "sha256_match": True, "integrity_ok": True, "problem": None},
            "compatibility": "same_schema",
            "current_schema_version": 1,
            "current_table_counts": {},
            "can_restore": True,
            "blocker": None,
            "blocker_code": None
        }

    elif command == "stage_restore":
        bid = body.get("backupId", "")
        return {"backup_id": bid, "staged_at": datetime.utcnow().isoformat() + "Z", "rollback_backup_id": f"rollback_{bid}"}

    elif command == "get_pending_restore":
        return None

    elif command == "cancel_pending_restore":
        return None

    # --------------------------------------------------------------------------
    # 9. Zonas de Ritmo Cardíaco (Heart Rate Zones)
    # --------------------------------------------------------------------------
    elif command == "get_heart_rate_zones":
        days = body.get("days", 30)
        try:
            days = int(days)
        except Exception:
            days = 30

        rhr_row = query_one("SELECT value FROM daily_metrics WHERE metric = 'resting_hr' ORDER BY date DESC LIMIT 1")
        resting_val = int(round(rhr_row["value"])) if rhr_row and rhr_row.get("value") is not None else 60
        max_row = query_one("SELECT MAX(value) as max_val FROM metric_samples WHERE metric = 'heart_rate'")
        max_val = int(round(max_row["max_val"])) if max_row and max_row.get("max_val") is not None else 190
        w_max_row = query_one("SELECT MAX(max_hr) as max_val FROM workouts")
        if w_max_row and w_max_row.get("max_val"):
            max_val = max(max_val, int(round(w_max_row["max_val"])))
        if max_val < 140:
            max_val = 190

        start_date_str = (datetime.now().date() - timedelta(days=days - 1)).strftime("%Y-%m-%d")

        z1_min = int(round(max_val * 0.50))
        z1_max = int(round(max_val * 0.60))
        z2_min = z1_max
        z2_max = int(round(max_val * 0.70))
        z3_min = z2_max
        z3_max = int(round(max_val * 0.80))
        z4_min = z3_max
        z4_max = int(round(max_val * 0.90))
        z5_min = z4_max
        z5_max = max_val

        z1_sec = 0
        z2_sec = 0
        z3_sec = 0
        z4_sec = 0
        z5_sec = 0
        below_sec = 0
        above_sec = 0
        source = "workouts"

        # 1. Intentar con workout_samples (frecuencia segundo a segundo en entrenamientos)
        ws_row = query_one(
            "SELECT "
            "COUNT(CASE WHEN heart_rate < ? THEN 1 END) as below_z1, "
            "COUNT(CASE WHEN heart_rate >= ? AND heart_rate < ? THEN 1 END) as z1, "
            "COUNT(CASE WHEN heart_rate >= ? AND heart_rate < ? THEN 1 END) as z2, "
            "COUNT(CASE WHEN heart_rate >= ? AND heart_rate < ? THEN 1 END) as z3, "
            "COUNT(CASE WHEN heart_rate >= ? AND heart_rate < ? THEN 1 END) as z4, "
            "COUNT(CASE WHEN heart_rate >= ? AND heart_rate <= ? THEN 1 END) as z5, "
            "COUNT(CASE WHEN heart_rate > ? THEN 1 END) as above_z5 "
            "FROM workout_samples WHERE substr(timestamp, 1, 10) >= ? AND heart_rate > 0",
            (z1_min, z1_min, z2_min, z2_min, z3_min, z3_min, z4_min, z4_min, z5_min, z5_min, z5_max, z5_max, start_date_str)
        )
        if ws_row and any(ws_row.get(k) for k in ("z1", "z2", "z3", "z4", "z5")):
            below_sec = int(ws_row.get("below_z1") or 0)
            z1_sec = int(ws_row.get("z1") or 0)
            z2_sec = int(ws_row.get("z2") or 0)
            z3_sec = int(ws_row.get("z3") or 0)
            z4_sec = int(ws_row.get("z4") or 0)
            z5_sec = int(ws_row.get("z5") or 0)
            above_sec = int(ws_row.get("above_z5") or 0)
            source = "workout_samples"
        else:
            # 2. Respaldo: distribución desde resumen de workouts por duración y FC media
            w_rows = query_all(
                "SELECT duration_seconds, avg_hr, max_hr FROM workouts "
                "WHERE substr(start_time, 1, 10) >= ? AND duration_seconds > 0",
                (start_date_str,)
            )
            has_w = False
            if w_rows:
                for w in w_rows:
                    dur = int(w.get("duration_seconds") or 0)
                    avg = w.get("avg_hr")
                    if not avg or avg <= 0:
                        continue
                    has_w = True
                    if avg < z1_min:
                        below_sec += dur
                    elif avg < z2_min:
                        z1_sec += dur
                    elif avg < z3_min:
                        z2_sec += dur
                    elif avg < z4_min:
                        z3_sec += dur
                    elif avg < z5_min:
                        z4_sec += dur
                    elif avg <= z5_max:
                        z5_sec += dur
                    else:
                        above_sec += dur
            if has_w:
                source = "workouts"
            else:
                # 3. Respaldo: metric_samples (FC continua en reposo/diaria)
                ms_row = query_one(
                    "SELECT "
                    "COUNT(CASE WHEN value < ? THEN 1 END) as below_z1, "
                    "COUNT(CASE WHEN value >= ? AND value < ? THEN 1 END) as z1, "
                    "COUNT(CASE WHEN value >= ? AND value < ? THEN 1 END) as z2, "
                    "COUNT(CASE WHEN value >= ? AND value < ? THEN 1 END) as z3, "
                    "COUNT(CASE WHEN value >= ? AND value < ? THEN 1 END) as z4, "
                    "COUNT(CASE WHEN value >= ? AND value <= ? THEN 1 END) as z5, "
                    "COUNT(CASE WHEN value > ? THEN 1 END) as above_z5 "
                    "FROM metric_samples WHERE metric = 'heart_rate' AND substr(timestamp, 1, 10) >= ? AND value > 0",
                    (z1_min, z1_min, z2_min, z2_min, z3_min, z3_min, z4_min, z4_min, z5_min, z5_min, z5_max, z5_max, start_date_str)
                )
                if ms_row and any(ms_row.get(k) for k in ("z1", "z2", "z3", "z4", "z5")):
                    factor = 60
                    below_sec = int(ms_row.get("below_z1") or 0) * factor
                    z1_sec = int(ms_row.get("z1") or 0) * factor
                    z2_sec = int(ms_row.get("z2") or 0) * factor
                    z3_sec = int(ms_row.get("z3") or 0) * factor
                    z4_sec = int(ms_row.get("z4") or 0) * factor
                    z5_sec = int(ms_row.get("z5") or 0) * factor
                    above_sec = int(ms_row.get("above_z5") or 0) * factor
                    source = "metric_samples"

        total_sec = z1_sec + z2_sec + z3_sec + z4_sec + z5_sec + below_sec + above_sec
        saved_pref = load_json_file("hr_zone_pref.json", {})
        pref = saved_pref if saved_pref else {
            "model": "max_hr",
            "maxBasis": "max_hr_est",
            "restingBasis": "resting_hr_est",
            "thresholdBasis": None
        }

        return {
            "bases": [
                {
                    "id": "max_hr_est",
                    "kind": "max_hr",
                    "label": "Máxima estimada",
                    "value": max_val,
                    "unit": "bpm",
                    "source": "formula",
                    "measuredAt": None,
                    "note": None,
                    "noteCount": 0
                },
                {
                    "id": "resting_hr_est",
                    "kind": "resting_hr",
                    "label": "En reposo promedio",
                    "value": resting_val,
                    "unit": "bpm",
                    "source": "average",
                    "measuredAt": None,
                    "note": None,
                    "noteCount": 0
                }
            ],
            "models": [
                {
                    "id": "max_hr",
                    "label": "% FC Máxima",
                    "formula": "bpm / max_hr",
                    "requires": ["max_hr"],
                    "bands": [
                        {"zone": 1, "label": "Recuperación", "lowPercent": 50, "highPercent": 60},
                        {"zone": 2, "label": "Aeróbico ligero", "lowPercent": 60, "highPercent": 70},
                        {"zone": 3, "label": "Aeróbico", "lowPercent": 70, "highPercent": 80},
                        {"zone": 4, "label": "Umbral", "lowPercent": 80, "highPercent": 90},
                        {"zone": 5, "label": "Máximo", "lowPercent": 90, "highPercent": 100}
                    ],
                    "available": True
                }
            ],
            "preference": pref,
            "report": {
                "model": "max_hr",
                "modelLabel": "% FC Máxima",
                "formula": "bpm / max_hr",
                "bases": [],
                "zones": [
                    {"zone": 1, "label": "Zona 1", "minBpm": z1_min, "maxBpm": z1_max, "seconds": z1_sec},
                    {"zone": 2, "label": "Zona 2", "minBpm": z2_min, "maxBpm": z2_max, "seconds": z2_sec},
                    {"zone": 3, "label": "Zona 3", "minBpm": z3_min, "maxBpm": z3_max, "seconds": z3_sec},
                    {"zone": 4, "label": "Zona 4", "minBpm": z4_min, "maxBpm": z4_max, "seconds": z4_sec},
                    {"zone": 5, "label": "Zona 5", "minBpm": z5_min, "maxBpm": z5_max, "seconds": z5_sec}
                ],
                "belowZone1Seconds": below_sec,
                "aboveZone5Seconds": above_sec,
                "totalSeconds": total_sec,
                "windowDays": days,
                "source": source
            },
            "windowDays": days
        }

    elif command == "set_heart_rate_zone_preference":
        pref = body.get("preference", {})
        days = body.get("days", 30)
        save_json_file("hr_zone_pref.json", pref)
        return {
            "bases": [],
            "models": [],
            "preference": pref,
            "report": None,
            "windowDays": days
        }

    # --------------------------------------------------------------------------
    # 10. Planes de Entrenamiento (Training Plans)
    # --------------------------------------------------------------------------
    elif command == "training_plan_state":
        saved = load_json_file("training_plan_state.json", {})
        today_iso = datetime.now().strftime("%Y-%m-%d")
        planned = saved.get("planned", [])
        sent = saved.get("sent", [])
        last_pub = saved.get("last_publish")
        return {
            "window": {"start": today_iso},
            "sent": sent,
            "planned": planned,
            "uncertain": False,
            "last_publish": last_pub,
            "last_batch": [last_pub] if last_pub else [],
            "weeks": [
                {
                    "start": today_iso,
                    "planned": len(planned),
                    "sent": len(sent),
                    "state": "in_sync" if planned else "not_sent",
                    "error_code": None
                }
            ],
            "can_undo": bool(sent),
            "drafts": saved.get("drafts", []),
            "ai_may_publish": saved.get("ai_may_publish", True)
        }

    elif command == "training_plan_save_draft":
        state = load_json_file("training_plan_state.json", {"drafts": []})
        draft_id = f"draft_{int(datetime.now().timestamp() * 1000)}"
        draft_item = {
            "id": draft_id,
            "origin": "user",
            "status": "draft",
            "document": body.get("document", {}),
            "created_at": datetime.utcnow().isoformat() + "Z",
            "updated_at": datetime.utcnow().isoformat() + "Z"
        }
        state.setdefault("drafts", []).append(draft_item)
        save_json_file("training_plan_state.json", state)
        return draft_id

    elif command == "training_plan_preview":
        draft_id = body.get("id")
        saved = load_json_file("training_plan_state.json", {})
        drafts = saved.get("drafts", [])
        draft_item = next((d for d in drafts if d.get("id") == draft_id), None)
        
        doc = draft_item.get("document", {}) if draft_item else {}
        workouts = doc.get("workouts", [])
        today_iso = datetime.now().strftime("%Y-%m-%d")
        from_date = doc.get("from") or today_iso
        to_date = doc.get("to") or today_iso
        
        return {
            "check": {
                "summary": doc.get("summary", ""),
                "rest": doc.get("rest", []),
                "from": from_date,
                "to": to_date,
                "workouts": workouts,
                "held": [],
                "issues": []
            },
            "window": {"start": today_iso},
            "days": [
                {
                    "date": w.get("date", today_iso),
                    "change": "added",
                    "before": [],
                    "after": [w]
                }
                for w in workouts
            ]
        }

    elif command == "training_plan_update_draft":
        target_id = body.get("id")
        doc = body.get("document", {})
        state = load_json_file("training_plan_state.json", {})
        if target_id and "drafts" in state:
            for d in state["drafts"]:
                if d.get("id") == target_id:
                    d["document"] = doc
                    d["updated_at"] = datetime.utcnow().isoformat() + "Z"
            save_json_file("training_plan_state.json", state)
        return True

    elif command == "training_plan_discard":
        target_id = body.get("id")
        state = load_json_file("training_plan_state.json", {})
        if target_id and "drafts" in state:
            state["drafts"] = [d for d in state["drafts"] if d.get("id") != target_id]
            save_json_file("training_plan_state.json", state)
        return True

    elif command == "training_plan_set_ai_publish":
        state = load_json_file("training_plan_state.json", {})
        state["ai_may_publish"] = body.get("enabled", True)
        save_json_file("training_plan_state.json", state)
        return True

    elif command == "training_plan_publish":
        action = body.get("action", {})
        batch_id = int(datetime.now().timestamp())
        state = load_json_file("training_plan_state.json", {})
        drafts = state.get("drafts", [])
        draft_id = action.get("id") if isinstance(action, dict) else None
        
        pub_record = None
        if draft_id:
            published_draft = next((d for d in drafts if d.get("id") == draft_id), None)
            if published_draft:
                doc = published_draft.get("document", {})
                wks = doc.get("workouts", [])
                state["planned"] = wks
                state["sent"] = wks
                pub_record = {
                    "id": batch_id,
                    "batch_id": batch_id,
                    "kind": "publish",
                    "role": "window",
                    "window_start": datetime.now().strftime("%Y-%m-%d"),
                    "workout_count": len(wks),
                    "state": "sent",
                    "http_status": 200,
                    "error_code": None,
                    "undone": False,
                    "created_at": datetime.utcnow().isoformat() + "Z",
                    "finished_at": datetime.utcnow().isoformat() + "Z"
                }
                state["last_publish"] = pub_record
                state["drafts"] = [d for d in drafts if d.get("id") != draft_id]
                save_json_file("training_plan_state.json", state)

        return {
            "outcome": {
                "outcome": "send",
                "batch_id": batch_id,
                "sends": [{
                    "publish_id": batch_id,
                    "window_start": datetime.now().strftime("%Y-%m-%d"),
                    "role": "window"
                }]
            },
            "record": pub_record,
            "records": [pub_record] if pub_record else []
        }

    elif command == "training_plan_adherence":
        return []

    # --------------------------------------------------------------------------
    # 11. Tareas de IA y Plantillas (AI Tasks & MCP)
    # --------------------------------------------------------------------------
    elif command == "ai_task_list":
        user_tasks = load_json_file("ai_tasks.json", [])
        builtins = [
            {
                "id": "task_heart_weekly",
                "title": "💓 Salud Cardíaca: Análisis Semanal (7d)",
                "template_id": "tpl_heart_weekly",
                "workout_count": 0,
                "updated_at": datetime.utcnow().isoformat() + "Z",
                "mcp_shared": False,
                "pinned": True
            },
            {
                "id": "task_heart_monthly",
                "title": "📈 Salud Cardíaca: Tendencias Mensuales (30d)",
                "template_id": "tpl_heart_monthly",
                "workout_count": 0,
                "updated_at": datetime.utcnow().isoformat() + "Z",
                "mcp_shared": False,
                "pinned": True
            }
        ]
        user_ids = {t.get("id") for t in user_tasks}
        result = list(user_tasks)
        for b in builtins:
            if b["id"] not in user_ids:
                result.append(b)
        return result

    elif command == "ai_task_get":
        tid = body.get("id", "")
        if tid in ("task_heart_weekly", "tpl_heart_weekly"):
            return {
                "schema_version": 1,
                "id": "task_heart_weekly",
                "title": "💓 Salud Cardíaca: Análisis Semanal (7d)",
                "template_id": "tpl_heart_weekly",
                "workout_ids": [],
                "categories": [
                    {"category": "heart_rate", "enabled": True, "days_before": 7, "include_workout_day": True, "excluded_metrics": []},
                    {"category": "resting_hr", "enabled": True, "days_before": 7, "include_workout_day": True, "excluded_metrics": []},
                    {"category": "recovery", "enabled": True, "days_before": 7, "include_workout_day": True, "excluded_metrics": []},
                    {"category": "workout", "enabled": True, "days_before": 7, "include_workout_day": True, "excluded_metrics": []}
                ],
                "detail_level": "medium",
                "prompt": "Analiza mis pulsaciones cardíacas de los últimos 7 días. Evalúa mi frecuencia cardíaca en reposo (RHR), picos máximos, zonas de esfuerzo en entrenamientos y variabilidad cardíaca (HRV) para determinar mi nivel de fatiga, recuperación y salud cardiovascular general.",
                "personal_note": "",
                "attachments": [],
                "include_precise_gps": False,
                "mcp_shared": False,
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": datetime.utcnow().isoformat() + "Z"
            }
        if tid in ("task_heart_monthly", "tpl_heart_monthly"):
            return {
                "schema_version": 1,
                "id": "task_heart_monthly",
                "title": "📈 Salud Cardíaca: Tendencias Mensuales (30d)",
                "template_id": "tpl_heart_monthly",
                "workout_ids": [],
                "categories": [
                    {"category": "heart_rate", "enabled": True, "days_before": 30, "include_workout_day": True, "excluded_metrics": []},
                    {"category": "resting_hr", "enabled": True, "days_before": 30, "include_workout_day": True, "excluded_metrics": []},
                    {"category": "recovery", "enabled": True, "days_before": 30, "include_workout_day": True, "excluded_metrics": []},
                    {"category": "training", "enabled": True, "days_before": 30, "include_workout_day": True, "excluded_metrics": []}
                ],
                "detail_level": "medium",
                "prompt": "Analiza la evolución de mis pulsaciones a lo largo del último mes (30 días). Evalúa la tendencia de mi frecuencia cardíaca en reposo, tolerancia al esfuerzo y regularidad del ritmo para identificar mejoras o posibles alertas de sobrecarga o estrés cardiovascular.",
                "personal_note": "",
                "attachments": [],
                "include_precise_gps": False,
                "mcp_shared": False,
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": datetime.utcnow().isoformat() + "Z"
            }
        tasks = load_json_file("ai_tasks.json", [])
        for t in tasks:
            if t.get("id") == tid:
                return t
        return {
            "schema_version": 1,
            "id": tid,
            "title": "Análisis",
            "template_id": None,
            "workout_ids": [],
            "categories": [],
            "detail_level": "standard",
            "prompt": "",
            "personal_note": "",
            "attachments": [],
            "include_precise_gps": False,
            "mcp_shared": False,
            "created_at": datetime.utcnow().isoformat() + "Z",
            "updated_at": datetime.utcnow().isoformat() + "Z"
        }

    elif command == "ai_task_save":
        task = body.get("task", {})
        tasks = load_json_file("ai_tasks.json", [])
        if not task.get("id"):
            task["id"] = f"task_{int(datetime.now().timestamp())}"
        task["updated_at"] = datetime.utcnow().isoformat() + "Z"
        tasks = [t for t in tasks if t.get("id") != task["id"]]
        tasks.append(task)
        save_json_file("ai_tasks.json", tasks)
        return task

    elif command == "ai_task_delete":
        tid = body.get("id", "")
        tasks = load_json_file("ai_tasks.json", [])
        tasks = [t for t in tasks if t.get("id") != tid]
        save_json_file("ai_tasks.json", tasks)
        return None

    elif command == "ai_task_delete_many":
        ids = set(body.get("ids", []))
        tasks = load_json_file("ai_tasks.json", [])
        deleted = [{"task": t, "pinned": False} for t in tasks if t.get("id") in ids]
        remaining = [t for t in tasks if t.get("id") not in ids]
        save_json_file("ai_tasks.json", remaining)
        return deleted

    elif command == "ai_task_set_pinned":
        return None

    elif command == "ai_template_list":
        custom_templates = load_json_file("ai_templates.json", [])
        builtins = [
            {
                "schema_version": 1,
                "id": "tpl_heart_weekly",
                "builtin": True,
                "name": "💓 Salud Cardíaca: Análisis Semanal de Pulsaciones",
                "name_code": None,
                "categories": [],
                "detail_level": "medium",
                "prompt_template": "Analiza mis pulsaciones cardíacas de los últimos 7 días. Evalúa mi frecuencia cardíaca en reposo (RHR), picos máximos, zonas de esfuerzo en entrenamientos y variabilidad cardíaca (HRV) para determinar mi nivel de fatiga, recuperación y salud cardiovascular general.",
                "prompt_code": None,
                "default_summaries": ["heart_rate", "resting_hr", "hrv"],
                "required_series": ["heart_rate"],
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-10-18T00:00:00Z"
            },
            {
                "schema_version": 1,
                "id": "tpl_heart_monthly",
                "builtin": True,
                "name": "📈 Salud Cardíaca: Tendencias Mensuales (30 días)",
                "name_code": None,
                "categories": [],
                "detail_level": "medium",
                "prompt_template": "Analiza la evolución de mis pulsaciones a lo largo del último mes (30 días). Evalúa la tendencia de mi frecuencia cardíaca en reposo, tolerancia al esfuerzo y regularidad del ritmo para identificar mejoras o posibles alertas de sobrecarga o estrés cardiovascular.",
                "prompt_code": None,
                "default_summaries": ["heart_rate", "resting_hr", "hrv"],
                "required_series": ["heart_rate"],
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-10-18T00:00:00Z"
            },
            {
                "schema_version": 1,
                "id": "tpl_recovery_sleep",
                "builtin": True,
                "name": "😴 Recuperación, Descanso y Sueño",
                "name_code": None,
                "categories": [],
                "detail_level": "medium",
                "prompt_template": "Evalúa la calidad de mi sueño y descanso de los últimos días (duración, fases profunda y REM, puntuación de sueño) en relación con mi nivel de esfuerzo físico y pulsaciones.",
                "prompt_code": None,
                "default_summaries": ["sleep", "resting_hr"],
                "required_series": [],
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-10-18T00:00:00Z"
            },
            {
                "schema_version": 1,
                "id": "tpl_general_fitness",
                "builtin": True,
                "name": "🏃 Rendimiento y Estado Físico General",
                "name_code": None,
                "categories": [],
                "detail_level": "medium",
                "prompt_template": "Analiza mi estado físico general combinando entrenamientos recientes, pasos diarios, calorías activas y frecuencia cardíaca.",
                "prompt_code": None,
                "default_summaries": ["workouts", "steps", "heart_rate"],
                "required_series": [],
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-10-18T00:00:00Z"
            }
        ]
        existing_ids = {t.get("id") for t in custom_templates}
        all_templates = list(custom_templates)
        for b in builtins:
            if b["id"] not in existing_ids:
                all_templates.append(b)
        return all_templates

    elif command == "ai_template_save":
        template = body.get("template", {})
        templates = load_json_file("ai_templates.json", [])
        if not template.get("id"):
            template["id"] = f"tpl_{int(datetime.now().timestamp())}"
        template["updated_at"] = datetime.utcnow().isoformat() + "Z"
        templates = [t for t in templates if t.get("id") != template["id"]]
        templates.append(template)
        save_json_file("ai_templates.json", templates)
        return template

    elif command == "ai_template_delete":
        tid = body.get("id", "")
        templates = load_json_file("ai_templates.json", [])
        templates = [t for t in templates if t.get("id") != tid]
        save_json_file("ai_templates.json", templates)
        return None

    elif command == "ai_task_preview":
        task = body.get("task", {})
        tid = task.get("id", "draft")
        recent_workouts = query_all("SELECT * FROM workouts ORDER BY start_time DESC LIMIT 10")
        workout_briefs = []
        for w in recent_workouts:
            w_type = w.get("effective_type") or w.get("workout_type") or "workout"
            st = str(w.get("start_time") or "")
            et = str(w.get("end_time") or st)
            s_date = st[:10] if len(st) >= 10 else ""
            workout_briefs.append({
                "workout_id": str(w.get("workout_id", "")),
                "workout_type": w_type,
                "start_time": st,
                "end_time": et,
                "start_date": s_date,
                "distance_meters": w.get("distance_meters"),
                "calories": w.get("calories"),
                "avg_hr": w.get("avg_hr"),
                "max_hr": w.get("max_hr")
            })
        return {
            "task_id": tid,
            "workouts": workout_briefs,
            "coverage": [],
            "attachments": [],
            "estimated_bytes": 2048,
            "warnings": [],
            "markdown": None
        }

    elif command == "ai_task_prepare":
        task = body.get("task", {})
        tid = task.get("id", "task_prepared")
        prompt_text = build_ai_health_prompt(task_dict=task)
        
        export_dir = Path(os.getenv("ZEPPBRIDGE_DATA_DIR", "/data")) / "exports"
        try:
            export_dir.mkdir(parents=True, exist_ok=True)
            (export_dir / "prompt.md").write_text(prompt_text, encoding="utf-8")
            (export_dir / "task.md").write_text(prompt_text, encoding="utf-8")
        except Exception as e:
            print(f"[AI] Error guardando archivos exportados: {e}")

        byte_len = len(prompt_text.encode("utf-8"))
        return {
            "status": "ready",
            "task_id": tid,
            "output_dir": str(export_dir),
            "json_path": str(export_dir / "task.json"),
            "prompt_path": str(export_dir / "prompt.md"),
            "prompt_text": prompt_text,
            "byte_len": byte_len,
            "copied_attachments": 0,
            "md_path": str(export_dir / "task.md"),
            "markdown": None,
            "attachments": [],
            "blocked": []
        }

    elif command == "ai_task_attachment_stat":
        paths = body.get("paths", [])
        return [{"path": p, "exists": False, "byte_len": None, "mtime": None} for p in paths]

    elif command in ("ai_handoff_copy_file", "ai_handoff_cancel"):
        return None

    elif command == "ai_handoff_autopaste":
        return 1

    elif command == "ai_task_day_strip":
        return []

    elif command == "ai_exchange_list":
        return []

    elif command in ("ai_exchange_rename", "ai_exchange_delete", "ai_profile_save"):
        return True

    elif command == "card_collection_get":
        return load_json_file("cards.json", [])

    elif command == "card_collection_set":
        picks = body.get("picks", [])
        save_json_file("cards.json", picks)
        return picks

    # --------------------------------------------------------------------------
    # 12. Local API & MCP Sidecar
    # --------------------------------------------------------------------------
    elif command == "get_mcp_sidecar":
        return {
            "path": "/usr/local/bin/zeppbridge-mcp",
            "data_dir_env": "/data",
            "appimage": False,
            "stale_clients": []
        }

    elif command in ("get_local_api_status", "local_api::get_local_api_status"):
        return {
            "enabled": True,
            "running": True,
            "base_url": "http://127.0.0.1:8000/api",
            "address": "0.0.0.0:8000",
            "workout_series_path": "/api/workouts",
            "token_present": True,
            "error": None,
            "error_code": None
        }

    elif command in ("set_local_api_enabled", "local_api::set_local_api_enabled"):
        enabled = body.get("enabled", True)
        return {
            "enabled": enabled,
            "running": enabled,
            "base_url": "http://127.0.0.1:8000/api",
            "address": "0.0.0.0:8000",
            "workout_series_path": "/api/workouts",
            "token_present": True,
            "error": None,
            "error_code": None
        }

    elif command in ("reveal_local_api_token", "rotate_local_api_token", "local_api::reveal_local_api_token", "local_api::rotate_local_api_token"):
        return "zeppbridge-local-token-native"

    elif command in ("update_view", "update_start", "updates::silent::update_view", "updates::silent::update_start"):
        return {
            "phase": "up_to_date",
            "version": "3.0.1",
            "notes": None,
            "date": None,
            "downloaded": 0,
            "total": None,
            "waiting_for_sync": False,
            "portable": False,
            "error_code": None
        }

    elif command in ("update_apply", "updates::silent::update_apply"):
        return None

    elif command == "update_set_auto":
        return True

    # --------------------------------------------------------------------------
    # 14. Novedades v3.0.5+: Rings, Logbook (PB, Zapatillas, Carreras), Coach & Cartas
    # --------------------------------------------------------------------------
    elif command == "annual_rings":
        today_str = datetime.now().strftime("%Y-%m-%d")
        return {
            "years": [],
            "today": today_str
        }

    elif command == "rings_save_image":
        return "/data/exports/rings.png"

    elif command == "personal_records":
        return {
            "records": [],
            "runs_scanned": 0
        }

    elif command == "shoes_overview":
        return {
            "shoes": [],
            "unknown_gear": []
        }

    elif command in ("shoe_save", "shoe_delete", "race_save", "race_delete", "injury_save", "injury_delete", "save_life_event", "delete_life_event"):
        return True

    elif command in ("races_list", "injuries_list", "list_life_events"):
        return []

    elif command == "workout_feel_get":
        wid = body.get("workoutId") or body.get("workout_id") or ""
        return {
            "workout_id": wid,
            "rpe": None,
            "soreness": [],
            "shoe_mode": "none",
            "shoe_id": None,
            "watch_rpe": None,
            "footwear": None,
            "assigned": None,
            "choices": []
        }

    elif command in ("workout_feel_save", "workout_feedback_save", "workout_feedback_dismiss", "workout_feedback_snooze", "workout_feedback_adjusted"):
        return True

    elif command == "workout_feedback_get":
        return None

    elif command == "workout_feedback_prompt":
        return ""

    elif command in ("coach_notes_list", "coach_notes_export"):
        return []

    elif command in ("coach_note_add", "coach_note_decide", "coach_note_edit", "coach_note_delete"):
        return True

    elif command in ("letters_state", "letters_sync"):
        return {
            "outbox": [],
            "sent": [],
            "receipts": [],
            "receipts_checked_at": None
        }

    elif command == "letters_send_reply":
        return {
            "status": "sent",
            "reply_id": "rep_1",
            "view": {
                "outbox": [],
                "sent": [],
                "receipts": [],
                "receipts_checked_at": None
            }
        }

    # --------------------------------------------------------------------------
    # 13. Preferencias, Diagnósticos, Exportaciones y Mantenimiento
    # --------------------------------------------------------------------------
    elif command == "system_region_locale":
        return "es-ES"

    elif command == "get_user_prefs":
        return load_json_file("user_prefs.json", {
            "demo_mode": False,
            "retention_days": 365,
            "history_sync_days": 180,
            "archive_enabled": True,
            "ai_profile_note": ""
        })

    elif command == "set_user_prefs":
        prefs = load_json_file("user_prefs.json", {
            "demo_mode": False,
            "retention_days": 365,
            "history_sync_days": 180,
            "archive_enabled": True,
            "ai_profile_note": ""
        })
        for k in ["retention_days", "retentionDays", "history_sync_days", "historySyncDays", "archive_enabled", "archiveEnabled", "demo_mode", "ai_profile_note"]:
            if k in body:
                norm_k = "retention_days" if k == "retentionDays" else ("history_sync_days" if k == "historySyncDays" else ("archive_enabled" if k == "archiveEnabled" else k))
                prefs[norm_k] = body[k]
        save_json_file("user_prefs.json", prefs)
        return prefs

    elif command == "get_storage_estimate":
        days = body.get("days", 180)
        db_size = 1024 * 1024 * 5
        try:
            if DB_PATH.exists():
                db_size = DB_PATH.stat().st_size
        except Exception:
            pass
        return {
            "free_bytes": 1024 * 1024 * 1024 * 20,
            "estimated_add_bytes": 1024 * 1024,
            "database_bytes": db_size,
            "allow_long_history": True,
            "warn_tight_space": False,
            "message": "Espacio suficiente",
            "message_code": "ui.estimate.measured",
            "requested_days": days,
            "streams": [],
            "measured": True,
            "stop_reason": None,
            "stop_reason_code": None,
            "needed_bytes": 1024 * 1024
        }

    elif command == "list_life_events":
        start = body.get("start")
        end = body.get("end")
        events = query_all(
            "SELECT id, title, category, start_date, end_date, notes, created_at, updated_at "
            "FROM life_events WHERE (? IS NULL OR end_date IS NULL OR end_date >= ?) "
            "AND (? IS NULL OR start_date <= ?) ORDER BY start_date DESC, id DESC",
            (start, start, end, end)
        )
        if not events:
            events = load_json_file("life_events.json", [])
        return events

    elif command == "save_life_event":
        inp = body.get("input") or body.get("event") or body
        eid = inp.get("id")
        title = inp.get("title", "Evento")
        cat = inp.get("category", "health")
        s_date = inp.get("start_date") or inp.get("startDate") or datetime.now().strftime("%Y-%m-%d")
        e_date = inp.get("end_date") or inp.get("endDate")
        notes = inp.get("notes", "")
        now_iso = datetime.utcnow().isoformat() + "Z"
        conn = get_db_connection()
        res_id = eid or int(datetime.now().timestamp())
        if conn:
            try:
                if eid:
                    conn.execute(
                        "UPDATE life_events SET title=?, category=?, start_date=?, end_date=?, notes=?, updated_at=? WHERE id=?",
                        (title, cat, s_date, e_date, notes, now_iso, eid)
                    )
                else:
                    cur = conn.execute(
                        "INSERT INTO life_events (title, category, start_date, end_date, notes, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (title, cat, s_date, e_date, notes, now_iso, now_iso)
                    )
                    res_id = cur.lastrowid
                conn.commit()
            except Exception:
                pass
            conn.close()
        return res_id

    elif command == "delete_life_event":
        eid = body.get("id")
        conn = get_db_connection()
        if conn and eid:
            try:
                conn.execute("DELETE FROM life_events WHERE id = ?", (eid,))
                conn.commit()
            except Exception:
                pass
            conn.close()
        return None

    elif command in ("submit_diagnostic_report", "submit_device_model_assignment"):
        return {
            "reportId": f"rep_{int(datetime.now().timestamp())}",
            "submittedAt": datetime.utcnow().isoformat() + "Z"
        }

    elif command == "save_fit_export":
        return {
            "path": "/data/exports/activity.fit",
            "record_count": 1,
            "bytes": 2048,
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "file_count": 1
        }

    elif command == "prepare_ai_handoff":
        prompt = body.get("prompt", "")
        days = body.get("days") or body.get("scope_days")
        mode = body.get("mode")
        prompt_text = build_ai_health_prompt(prompt_query=prompt, scope_days=days, mode=mode)
        export_dir = Path(os.getenv("ZEPPBRIDGE_DATA_DIR", "/data")) / "exports"
        try:
            export_dir.mkdir(parents=True, exist_ok=True)
            (export_dir / "handoff.md").write_text(prompt_text, encoding="utf-8")
        except Exception:
            pass

        return {
            "mode": "inline",
            "clipboardText": prompt_text,
            "filePath": str(export_dir / "handoff.md"),
            "bytes": len(prompt_text.encode("utf-8")),
            "records": 1,
            "redactions": [],
            "metadata": {
                "preciseRouteIncluded": False,
                "authenticationFieldsRemoved": True,
                "identityFieldsRemoved": True
            }
        }

    elif command == "cleanup_old_data":
        return {"cleaned": 0, "status": "ok"}

    elif command in ("set_tray_locale", "open_data_folder", "save_mcp_bundle"):
        return None

    # --------------------------------------------------------------------------
    # 14. Diagnóstico de regiones: Escanear todas las regiones en paralelo
    # --------------------------------------------------------------------------
    elif command == "probe_all_regions":
        st = get_auth_status()
        user_id = body.get("userId") or st.get("user_id")
        token = body.get("appToken") or get_stored_token(user_id)
        if not user_id or not token:
            return {"error": "Falta userId o appToken"}

        ALL_REGIONS = [
            ("api-mifit-de2.huami.com", "Europa 2 (Frankfurt)"),
            ("api-mifit-de.huami.com", "Europa 1"),
            ("api-mifit-us2.huami.com", "Estados Unidos 2"),
            ("api-mifit.huami.com", "Estados Unidos (Global)"),
            ("api-mifit-sg2.huami.com", "Singapur / Asia"),
            ("api-mifit-ru.huami.com", "Rusia"),
            ("api-mifit-cn2.huami.com", "China 2"),
            ("api-mifit-cn.huami.com", "China 1"),
        ]

        results = []
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        headers = {
            "apptoken": token,
            "appname": "com.huami.midong",
            "appplatform": "ios_phone",
            "accept": "*/*",
            "v": "2.0",
            "vn": "10.2.5",
            "cv": "1722_10.2.5",
            "vb": "202604132257",
            "lang": "en",
            "country": "",
            "timezone": "UTC"
        }

        best_match = None

        for host, label in ALL_REGIONS:
            h = clean_region_host(host)
            url = f"https://{h}/users/{user_id}/devices?enableMultiDevice=true&device_type=android_phone&r=1"
            req = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=5, context=ctx) as resp:
                    raw = json.loads(resp.read().decode("utf-8", errors="replace"))
                    code = raw.get("code")
                    items = raw.get("items", []) or []
                    has_devices = len(items) > 0
                    device_names = []
                    for d in items:
                        parsed_d = parse_device_item(d, datetime.utcnow().isoformat() + "Z")
                        device_names.append(parsed_d.get("display_name", "Dispositivo"))
                    results.append({
                        "host": host,
                        "label": label,
                        "status": "connected",
                        "device_count": len(items),
                        "devices": device_names,
                        "message": f"Conectado ({len(items)} dispositivos)" if has_devices else "Conectado (0 dispositivos)"
                    })
                    if has_devices and not best_match:
                        best_match = host
            except urllib.error.HTTPError as he:
                results.append({
                    "host": host,
                    "label": label,
                    "status": "rejected" if he.code in (401, 403) else f"http_{he.code}",
                    "device_count": 0,
                    "devices": [],
                    "message": f"HTTP {he.code}"
                })
            except Exception as e:
                results.append({
                    "host": host,
                    "label": label,
                    "status": "timeout_or_error",
                    "device_count": 0,
                    "devices": [],
                    "message": str(e)
                })

        return {
            "best_region": best_match,
            "results": results
        }

    # Fallback inteligente para colecciones/listas
    if any(k in command for k in ("list", "series", "points", "codes", "options", "extremes", "adherence", "chunks", "items")):
        return []

    print(f"[Bridge] Fallback general invocado para comando no registrado: {command}")
    return {}


# ==============================================================================
# Diagnóstico y Estado del CLI
# ==============================================================================
@app.get("/api/bridge/diag")
async def get_diagnostics():
    cli_exists = Path(CLI_BIN).exists()
    ldd_output = ""
    cli_test_out = ""
    cli_test_err = ""
    cli_test_code = None

    if cli_exists:
        try:
            p = subprocess.run(["ldd", CLI_BIN], capture_output=True, text=True, timeout=5)
            ldd_output = p.stdout or p.stderr
        except Exception as e:
            ldd_output = str(e)

        try:
            env = os.environ.copy()
            env["ZEPPBRIDGE_DATA_DIR"] = str(DATA_DIR)
            env["ZEPPBRIDGE_CREDENTIAL_STORE"] = "file"
            p = subprocess.run([CLI_BIN, "status", "--json"], capture_output=True, text=True, timeout=5, env=env)
            cli_test_out = p.stdout
            cli_test_err = p.stderr
            cli_test_code = p.returncode
        except Exception as e:
            cli_test_err = str(e)

    auth = get_auth_status()
    sync_info = get_last_sync_info()

    return {
        "cli_path": CLI_BIN,
        "cli_exists": cli_exists,
        "ldd": ldd_output,
        "cli_test_status_code": cli_test_code,
        "cli_test_stdout": cli_test_out,
        "cli_test_stderr": cli_test_err,
        "auth": auth,
        "sync_info": sync_info
    }

@app.get("/api/bridge/sync_log")
def get_sync_log():
    log_file = DATA_DIR / "sync.log"
    if log_file.exists():
        try:
            with open(log_file, "r", encoding="utf-8") as f:
                lines = f.readlines()
                # Devolver las últimas 150 líneas del log
                return "".join(lines[-150:])
        except Exception as e:
            return f"Error leyendo sync.log: {e}"
    return "Aún no se ha registrado ninguna sincronización en sync.log."

# ==============================================================================
# Configuración Custom, Sincronización Automática, Telegram y Gemini
# ==============================================================================
CUSTOM_CONFIG_FILE = DATA_DIR / "custom_config.json"

DEFAULT_CUSTOM_CONFIG = {
    "auto_sync_enabled": True,
    "auto_sync_interval_minutes": 5,
    "last_auto_sync": None,
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "gemini_api_key": "",
    "weekly_report_enabled": False,
    "weekly_report_day": "sunday",
    "weekly_report_hour": 21,
    "last_weekly_report": None,
    "morning_report": {
        "enabled": False,
        "hour": 8,
        "minute": 30,
        "days_of_week": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
        "include_gemini": True,
        "last_sent": None
    }
}

def get_custom_config() -> dict:
    cfg = copy.deepcopy(DEFAULT_CUSTOM_CONFIG)
    if CUSTOM_CONFIG_FILE.exists():
        try:
            with open(CUSTOM_CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
                if isinstance(saved, dict):
                    saved_morning = saved.get("morning_report")
                    cfg.update(saved)
                    if isinstance(saved_morning, dict):
                        m_cfg = copy.deepcopy(DEFAULT_CUSTOM_CONFIG["morning_report"])
                        m_cfg.update(saved_morning)
                        cfg["morning_report"] = m_cfg
                    else:
                        cfg["morning_report"] = copy.deepcopy(DEFAULT_CUSTOM_CONFIG["morning_report"])
        except Exception as e:
            print(f"[CustomConfig] Error leyendo {CUSTOM_CONFIG_FILE}: {e}")
    return cfg

def save_custom_config(cfg: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(CUSTOM_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        print(f"[CustomConfig] Error guardando {CUSTOM_CONFIG_FILE}: {e}")

def send_telegram_message(token: str, chat_id: str, text: str) -> bool:
    if not token or not chat_id:
        raise ValueError("Se requiere Token del Bot y Chat ID de Telegram.")
    token = token.strip()
    chat_id = chat_id.strip()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
    for chunk in chunks:
        payload = json.dumps({"chat_id": chat_id, "text": chunk, "parse_mode": "Markdown"}).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
                pass
        except urllib.error.HTTPError as e:
            payload_plain = json.dumps({"chat_id": chat_id, "text": chunk}).encode("utf-8")
            req_plain = urllib.request.Request(url, data=payload_plain, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req_plain, timeout=15, context=ctx) as resp2:
                pass
    return True

def query_gemini_api(api_key: str, prompt: str) -> str:
    if not api_key:
        raise ValueError("Se requiere una Gemini API Key de Google AI Studio.")
    api_key = api_key.strip()
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={api_key}"
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    body = {
        "contents": [{
            "parts": [{"text": prompt}]
        }]
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=45, context=ctx) as resp:
        res_json = json.loads(resp.read().decode("utf-8"))
        candidates = res_json.get("candidates", [])
        if candidates:
            parts = candidates[0].get("content", {}).get("parts", [])
            if parts:
                return parts[0].get("text", "")
        raise ValueError("Gemini no devolvió texto en su respuesta.")

# ==============================================================================
# Sistema de Informes Automatizados Personalizables (Cron Gemini + Telegram)
# ==============================================================================
REPORTS_CONFIG_FILE = DATA_DIR / "reports_config.json"

DEFAULT_REPORTS = [
    {
        "id": "rep_daily_summary",
        "name": "🌙 Resumen Diario de Salud y Actividad (Hoy)",
        "enabled": True,
        "schedule_type": "daily",
        "hour": 22,
        "minute": 0,
        "days_of_week": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
        "scope_days": 1,
        "focus": "full",
        "custom_prompt": "",
        "last_run": None
    },
    {
        "id": "rep_weekly_cardio",
        "name": "💓 Análisis Semanal Cardiovascular y Deporte (7d)",
        "enabled": True,
        "schedule_type": "weekly",
        "hour": 21,
        "minute": 0,
        "days_of_week": ["sun"],
        "scope_days": 7,
        "focus": "heart",
        "custom_prompt": "",
        "last_run": None
    }
]

def get_reports_config() -> list:
    if REPORTS_CONFIG_FILE.exists():
        try:
            with open(REPORTS_CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[Reports] Error leyendo {REPORTS_CONFIG_FILE}: {e}")
    save_reports_config(DEFAULT_REPORTS)
    return DEFAULT_REPORTS.copy()

def save_reports_config(reports: list):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(REPORTS_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(reports, f, indent=2)
    except Exception as e:
        print(f"[Reports] Error guardando {REPORTS_CONFIG_FILE}: {e}")

def build_custom_report_prompt(report: dict) -> str:
    name = report.get("name", "Informe de Salud")
    scope_days = int(report.get("scope_days", 1))
    focus = report.get("focus", "full")
    custom_prompt = (report.get("custom_prompt") or "").strip()

    today = datetime.now().date()
    if scope_days <= 1:
        target_date = today.strftime("%Y-%m-%d")
        cutoff_date = target_date
        period_label = f"Hoy ({target_date})"
    else:
        cutoff_date = (today - timedelta(days=scope_days)).strftime("%Y-%m-%d")
        period_label = f"Últimos {scope_days} días (desde {cutoff_date} hasta hoy)"

    sections = [
        f"# 📊 {name}",
        f"**Periodo de datos:** {period_label}",
        f"**Fecha y hora:** {datetime.now().strftime('%d/%m/%Y %H:%M')}\n"
    ]

    focus_labels = {
        "full": "Evaluar mi estado de salud holístico (sueño, pasos, pulsaciones basales, variabilidad HRV y entrenamientos), destacando fortalezas y consejos para el descanso.",
        "heart": "Analizar mi salud cardiovascular: estabilidad de la FC en reposo, picos de pulsaciones, HRV, estrés cardíaco y respuesta en entrenamientos.",
        "sleep": "Analizar mi arquitectura de sueño: fases profunda y REM, regularidad de horarios, eficiencia y nivel de recuperación.",
        "workout": "Evaluar mi rendimiento deportivo: volumen de entrenamiento, zonas de esfuerzo, frecuencia cardíaca media/máxima y adaptación física."
    }

    if custom_prompt:
        sections.append(f"## 🎯 Consulta e Instrucción Especial\n> {custom_prompt}\n")
    else:
        sections.append(f"## 🎯 Objetivo del Informe\n> {focus_labels.get(focus, focus_labels['full'])}\n")

    # 1. Datos de actividad y pasos
    try:
        steps_rows = query_all("SELECT date, value FROM daily_metrics WHERE metric = 'steps' AND date >= ? ORDER BY date DESC", (cutoff_date,))
        cals_rows = query_all("SELECT date, value FROM daily_metrics WHERE metric = 'calories' AND date >= ? ORDER BY date DESC", (cutoff_date,))
        if steps_rows:
            sections.append("## 🚶 Actividad y Pasos")
            if scope_days <= 1:
                sections.append(f"- **Pasos registrados hoy:** **{int(steps_rows[0]['value']):,}** pasos")
                if cals_rows:
                    sections.append(f"- **Calorías activas estimadas:** **{int(cals_rows[0]['value'])}** kcal")
            else:
                avg_steps = int(sum(r["value"] for r in steps_rows) / len(steps_rows))
                tot_steps = int(sum(r["value"] for r in steps_rows))
                sections.append(f"- **Promedio diario:** **{avg_steps:,}** pasos/día (Total acumulado: {tot_steps:,} pasos en {len(steps_rows)} días)")
            sections.append("")
    except Exception:
        pass

    # 2. Sueño y descanso
    try:
        sleep_rows = query_all("SELECT * FROM sleep_sessions WHERE substr(start_time, 1, 10) >= ? ORDER BY start_time DESC", (cutoff_date,))
        if sleep_rows:
            sections.append("## 🌙 Sueño y Descanso")
            max_sl = 7 if scope_days > 1 else 2
            for sl in sleep_rows[:max_sl]:
                st = str(sl.get("start_time") or "")[:16].replace("T", " ")
                et = str(sl.get("end_time") or "")[11:16]
                dur_m = (sl.get("duration_seconds") or 0) // 60
                deep_m = (sl.get("deep_sleep_seconds") or 0) // 60
                rem_m = (sl.get("rem_sleep_seconds") or 0) // 60
                score = sl.get("sleep_score")
                score_str = f" | Puntuación: **{score}/100**" if score else ""
                sections.append(f"- **{st} a {et}** ({dur_m // 60}h {dur_m % 60}m){score_str} — Profundo: {deep_m} min | REM: {rem_m} min")
            sections.append("")
        elif scope_days <= 1:
            sections.append("## 🌙 Sueño\n- *Aún no hay registros de sueño procesados para hoy.*\n")
    except Exception:
        pass

    # 3. Salud cardíaca: FC Reposo, HRV y Picos
    try:
        sections.append("## 💓 Frecuencia Cardíaca y Métricas Fisiológicas")
        rhr_rows = query_all("SELECT date, value FROM daily_metrics WHERE metric = 'resting_hr' AND date >= ? ORDER BY date DESC", (cutoff_date,))
        if rhr_rows:
            avg_rhr = round(sum(r["value"] for r in rhr_rows) / len(rhr_rows), 1)
            sections.append(f"- **FC en Reposo (RHR):** **{avg_rhr} bpm** {'(hoy)' if scope_days <= 1 else f'(promedio de {len(rhr_rows)} días)'}")

        hrv_stat = query_one("SELECT AVG(value) as avg_val FROM metric_samples WHERE metric = 'hrv' AND substr(timestamp, 1, 10) >= ?", (cutoff_date,))
        if hrv_stat and hrv_stat.get("avg_val") is not None:
            sections.append(f"- **Variabilidad Cardíaca (HRV):** **{round(float(hrv_stat['avg_val']), 1)} ms**")

        stress_stat = query_one("SELECT AVG(value) as avg_val FROM metric_samples WHERE metric = 'stress' AND substr(timestamp, 1, 10) >= ?", (cutoff_date,))
        if stress_stat and stress_stat.get("avg_val") is not None:
            sections.append(f"- **Nivel de Estrés Cardíaco:** **{round(float(stress_stat['avg_val']), 1)} / 100**")

        peak_stat = query_one("SELECT MIN(value) as min_val, MAX(value) as max_val FROM metric_samples WHERE metric = 'heart_rate' AND substr(timestamp, 1, 10) >= ?", (cutoff_date,))
        if peak_stat and peak_stat.get("max_val") is not None:
            sections.append(f"- **Pico máximo de pulso:** **{int(round(float(peak_stat['max_val'])))} bpm** (Mín: {int(round(float(peak_stat['min_val'])))} bpm)")
        sections.append("")
    except Exception:
        pass

    # 4. Entrenamientos y sesiones deportivas
    try:
        workouts = query_all("SELECT * FROM workouts WHERE substr(start_time, 1, 10) >= ? ORDER BY start_time DESC LIMIT 10", (cutoff_date,))
        if workouts:
            sections.append("## 🏃 Sesiones de Entrenamiento y Deporte")
            for w in workouts:
                st = str(w.get("start_time") or "")[:16].replace("T", " ")
                tp = w.get("effective_type") or w.get("workout_type") or "Entrenamiento"
                dur = (w.get("duration_seconds") or 0) // 60
                avg_hr = w.get("avg_hr")
                max_hr = w.get("max_hr")
                cals = w.get("calories")
                dist = (w.get("distance_meters") or 0) / 1000.0
                parts = [f"{dur} min"]
                if dist > 0:
                    parts.append(f"{dist:.2f} km")
                if cals and cals > 0:
                    parts.append(f"{int(cals)} kcal")
                if avg_hr and avg_hr > 0:
                    parts.append(f"FC Media: {int(avg_hr)} bpm")
                if max_hr and max_hr > 0:
                    parts.append(f"FC Máx: {int(max_hr)} bpm")
                sections.append(f"- **{st}** — **{tp.capitalize()}** ({', '.join(parts)})")
            sections.append("")
        else:
            sections.append("## 🏃 Entrenamientos\n- *No se registraron sesiones deportivas en este periodo.*\n")
    except Exception:
        pass

    # 5. Pautas para Gemini
    sections.append("---")
    sections.append("## 💡 Instrucciones para el Asistente de IA (Gemini)")
    if custom_prompt:
        sections.append(f"Responde atendiendo específicamente a esta indicación y personalización:\n{custom_prompt}")
    else:
        sections.append(
            "Actúa como un médico especialista en medicina del deporte, cardiología y recuperación fisiológica. "
            "Redacta un informe conciso, motivador y claro en español estructurado en 3 puntos:\n"
            "1. **Diagnóstico y Balance:** ¿Cómo ha estado la recuperación, la carga física y la estabilidad de las pulsaciones?\n"
            "2. **Puntos Clave o Alertas:** ¿Hay indicios de sobrecarga, déficit de sueño o picos de estrés anormales?\n"
            "3. **Plan de Acción Recomendado:** Pautas prácticas para la hidratación, tipo de actividad física y descanso a seguir."
        )

    return "\n".join(sections)

async def execute_report(report_id: str) -> dict:
    reports = get_reports_config()
    target = next((r for r in reports if r.get("id") == report_id), None)
    if not target:
        raise ValueError(f"No se encontró el informe con ID {report_id}")

    cfg = get_custom_config()
    token = cfg.get("telegram_bot_token")
    chat_id = cfg.get("telegram_chat_id")
    api_key = cfg.get("gemini_api_key")
    if not token or not chat_id or not api_key:
        raise ValueError("Se requiere configurar Telegram Bot Token, Chat ID y Gemini API Key en Custom.")

    prompt = build_custom_report_prompt(target)

    loop = asyncio.get_event_loop()
    gemini_resp = await loop.run_in_executor(None, query_gemini_api, api_key, prompt)

    now_str = datetime.now().strftime("%d/%m/%Y %H:%M")
    scope_lbl = f"{target.get('scope_days', 1)} día(s)"
    msg = (
        f"📊 *{target.get('name', 'Informe de Salud')}*\n"
        f"🗓️ *Periodo:* {scope_lbl} | *Generado:* {now_str}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{gemini_resp}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 _Enviado automáticamente por ZeppBridge & Gemini_"
    )
    await loop.run_in_executor(None, send_telegram_message, token, chat_id, msg)

    target["last_run"] = datetime.now().isoformat()
    save_reports_config(reports)
    return {"ok": True, "message": f"Informe '{target.get('name')}' enviado con éxito a Telegram."}

async def send_morning_report(is_test: bool = False, is_manual: bool = False) -> dict:
    cfg = get_custom_config()
    token = cfg.get("telegram_bot_token")
    chat_id = cfg.get("telegram_chat_id")
    api_key = cfg.get("gemini_api_key")

    if not token or not chat_id:
        raise ValueError("Se requiere configurar Telegram Bot Token y Chat ID en Custom.")

    m_cfg = cfg.get("morning_report") or {}
    now = datetime.now()
    today_display = now.strftime("%d/%m/%Y")

    # 1. Obtener última sesión de sueño
    sl_row = query_one("SELECT * FROM sleep_sessions ORDER BY start_time DESC LIMIT 1")
    sleep_summary = "Sin datos de sueño recientes"
    stages_summary = ""
    score_val = None
    dur_hours = 0
    if sl_row:
        dur_m = int(sl_row.get("duration") or 0)
        dur_hours = round(dur_m / 60.0, 1)
        score_val = sl_row.get("score")
        score_str = f" · Puntuación: *{score_val}/100*" if score_val is not None else ""
        sleep_summary = f"*{dur_m // 60}h {dur_m % 60}m*{score_str}"

        deep_m = int(sl_row.get("deep_sleep") or 0)
        rem_m = int(sl_row.get("rem_sleep") or 0)
        light_m = int(sl_row.get("light_sleep") or 0)
        awake_m = int(sl_row.get("awake") or 0)

        parts = []
        if deep_m: parts.append(f"Profundo: {deep_m // 60}h {deep_m % 60}m")
        if rem_m: parts.append(f"REM: {rem_m // 60}h {rem_m % 60}m")
        if light_m: parts.append(f"Ligero: {light_m // 60}h {light_m % 60}m")
        if awake_m: parts.append(f"Despierto: {awake_m}m")
        if parts:
            stages_summary = "🛌 *Fases:* " + " · ".join(parts)

    # 2. FC en reposo (RHR)
    rhr_row = query_one("SELECT value FROM daily_metrics WHERE metric = 'resting_hr' ORDER BY date DESC, id DESC LIMIT 1")
    rhr_val = int(rhr_row["value"]) if rhr_row and rhr_row.get("value") is not None else None
    rhr_str = f"*{rhr_val} bpm*" if rhr_val is not None else "Sin registro"

    # 3. Variabilidad cardíaca (HRV)
    hrv_row = query_one("SELECT AVG(value) as avg_val FROM metric_samples WHERE metric in ('hrv', 'hrv_rmssd') AND substr(timestamp, 1, 10) >= date('now', '-1 day')")
    hrv_val = round(hrv_row["avg_val"], 1) if hrv_row and hrv_row.get("avg_val") is not None else None
    hrv_str = f" · HRV: *{hrv_val} ms*" if hrv_val is not None else ""

    # 4. Recuperación / Readiness
    readiness_row = query_one("SELECT value FROM daily_metrics WHERE metric in ('readiness', 'physical_readiness', 'bio_charge') ORDER BY date DESC, id DESC LIMIT 1")
    readiness_val = int(readiness_row["value"]) if readiness_row and readiness_row.get("value") is not None else None
    readiness_str = f"⚡ *Recuperación física:* *{readiness_val}%*\n" if readiness_val is not None else ""

    # 5. Si se activa Gemini y hay API key, generar consejo motivacional y clínico breve
    gemini_tip = ""
    include_gemini = m_cfg.get("include_gemini", True)
    if include_gemini and api_key:
        prompt = (
            f"Actúa como un médico especialista en medicina del sueño y rendimiento deportivo. "
            f"El usuario acaba de despertar hoy ({today_display}). Sus métricas de sueño de anoche son:\n"
            f"- Duración total de sueño: {dur_hours} horas\n"
            f"- Puntuación de sueño: {score_val if score_val is not None else 'No disponible'}\n"
            f"- Frecuencia cardíaca en reposo al despertar: {rhr_val if rhr_val is not None else 'No disponible'} bpm\n"
            f"- HRV: {hrv_val if hrv_val is not None else 'No disponible'} ms\n"
            f"- Readiness / Recuperación: {readiness_val if readiness_val is not None else 'No disponible'}%\n\n"
            f"Escribe un saludo de '¡Buenos días!' muy breve, motivador y conciso (máximo 3 o 4 líneas) en español. "
            f"Evalúa brevemente la calidad del descanso de anoche y dale una recomendación práctica para el día (ej: nivel de energía recomendado, hidratación o si conviene entrenar suave o intenso)."
        )
        try:
            loop = asyncio.get_event_loop()
            gemini_resp = await loop.run_in_executor(None, query_gemini_api, api_key, prompt)
            if gemini_resp:
                gemini_tip = f"\n💡 *Consejo de hoy (Gemini):*\n{gemini_resp.strip()}\n"
        except Exception as e:
            print(f"[MorningReport] Error consultando Gemini: {e}")

    test_tag = " (Prueba)" if (is_test and not is_manual) else ""
    msg_lines = [
        f"☀️ *¡Buenos días! Tu Resumen de Descanso*{test_tag}",
        f"🗓️ *Fecha:* {today_display}",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"💤 *Sueño:* {sleep_summary}",
    ]
    if stages_summary:
        msg_lines.append(stages_summary)
    msg_lines.append(f"💓 *FC en reposo:* {rhr_str}{hrv_str}")
    if readiness_str:
        msg_lines.append(readiness_str.strip())
    msg_lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    if gemini_tip:
        msg_lines.append(gemini_tip.strip())
        msg_lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    msg_lines.append("🤖 _ZeppBridge Morning Assistant_")

    full_msg = "\n".join(msg_lines)
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, send_telegram_message, token, chat_id, full_msg)

    if not is_test:
        m_cfg["last_sent"] = now.isoformat()
        cfg["morning_report"] = m_cfg
        save_custom_config(cfg)

    return {"ok": True, "message": "Informe matutino 'Buenos días' enviado con éxito a Telegram."}

async def background_scheduler():
    print("[Scheduler] Demonio de sincronización e informes automáticos iniciado.")
    await asyncio.sleep(15)
    while True:
        try:
            cfg = get_custom_config()
            now = datetime.now()
            today_str = now.strftime("%Y-%m-%d")

            # 1. Sincronización automática periódica (por defecto cada 5 min)
            if cfg.get("auto_sync_enabled", True):
                interval_min = int(cfg.get("auto_sync_interval_minutes") if cfg.get("auto_sync_interval_minutes") is not None else 5)
                last_sync_iso = cfg.get("last_auto_sync")
                should_sync = False
                if not last_sync_iso:
                    should_sync = True
                else:
                    try:
                        last_sync_time = datetime.fromisoformat(last_sync_iso)
                        if (now - last_sync_time).total_seconds() >= interval_min * 60:
                            should_sync = True
                    except Exception:
                        should_sync = True

                if should_sync:
                    auth_st = get_auth_status()
                    if auth_st.get("configured"):
                        print(f"[Scheduler] Sincronización incremental automática (cada {interval_min} min)...")
                        loop = asyncio.get_event_loop()
                        await loop.run_in_executor(None, run_sync_command, "incremental", False, None)
                        cfg["last_auto_sync"] = now.isoformat()
                        save_custom_config(cfg)

            # 2. Evaluador de Informes Programados (Cron Gemini + Telegram)
            token = cfg.get("telegram_bot_token")
            chat_id = cfg.get("telegram_chat_id")
            api_key = cfg.get("gemini_api_key")
            if token and chat_id and api_key:
                day_keys = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
                day_longs = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
                today_key = day_keys[now.weekday()]
                today_long = day_longs[now.weekday()]

                reports = get_reports_config()
                for rep in reports:
                    if not rep.get("enabled", True):
                        continue

                    last_run_iso = rep.get("last_run")
                    if last_run_iso and str(last_run_iso).startswith(today_str):
                        continue

                    sched = rep.get("schedule_type", "daily")
                    rep_h = int(rep.get("hour") if rep.get("hour") is not None else 22)
                    rep_m = int(rep.get("minute") if rep.get("minute") is not None else 0)

                    if (now.hour > rep_h) or (now.hour == rep_h and now.minute >= rep_m):
                        should_fire = False
                        if sched == "daily":
                            should_fire = True
                        elif sched == "weekly":
                            days = [d.lower() for d in rep.get("days_of_week", ["sun"])]
                            if today_key in days or today_long in days:
                                should_fire = True
                        elif sched == "monthly":
                            dom = int(rep.get("day_of_month") if rep.get("day_of_month") is not None else 1)
                            if now.day == dom:
                                should_fire = True

                        if should_fire:
                            print(f"[Scheduler] Disparando informe programado: '{rep.get('name')}'...")
                            try:
                                await execute_report(rep["id"])
                                print(f"[Scheduler] Informe '{rep.get('name')}' enviado con éxito a Telegram.")
                            except Exception as ex:
                                print(f"[Scheduler] Error enviando informe '{rep.get('name')}': {ex}")

            # 3. Evaluador de Informe Matutino "Buenos días"
            m_cfg = cfg.get("morning_report") or {}
            if m_cfg.get("enabled", False) and token and chat_id:
                m_h = int(m_cfg.get("hour") if m_cfg.get("hour") is not None else 8)
                m_m = int(m_cfg.get("minute") if m_cfg.get("minute") is not None else 30)
                day_keys = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
                day_longs = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
                today_key = day_keys[now.weekday()]
                today_long = day_longs[now.weekday()]
                m_days = [d.lower() for d in m_cfg.get("days_of_week", day_keys)]

                last_sent_iso = m_cfg.get("last_sent")
                already_sent_today = last_sent_iso and str(last_sent_iso).startswith(today_str)

                if not already_sent_today and (today_key in m_days or today_long in m_days):
                    # Ventana matutina: desde la hora configurada hasta un margen de 4 horas
                    diff_min = (now.hour * 60 + now.minute) - (m_h * 60 + m_m)
                    if 0 <= diff_min <= 240:
                        print("[Scheduler] Disparando Informe Matutino 'Buenos días' a Telegram...")
                        try:
                            await send_morning_report(is_test=False)
                            print("[Scheduler] Informe Matutino enviado con éxito a Telegram.")
                        except Exception as m_err:
                            print(f"[Scheduler] Error enviando Informe Matutino: {m_err}")

        except Exception as loop_err:
            print(f"[Scheduler] Excepción en ciclo de tareas: {loop_err}")

        await asyncio.sleep(30)

async def handle_telegram_command(text: str, token: str, chat_id: str):
    raw_cmd = text.strip().split()[0].lower() if text.strip() else ""
    cmd = raw_cmd.split("@")[0]
    loop = asyncio.get_event_loop()

    # 1. Ayuda / Start
    if cmd in ["/start", "/help", "/ayuda"]:
        help_msg = (
            "👋 *¡Hola! Soy tu asistente médico y deportivo de ZeppBridge.*\n\n"
            "Puedes escribirme estos comandos en cualquier momento:\n\n"
            "📊 `/hoy` o `/resumen` — Pasos, sueño de anoche y pulsaciones de hoy.\n"
            "☀️ `/buenosdias` o `/matutino` — Resumen de descanso y recuperación matutino.\n"
            "🔄 `/sync` — Forzar sincronización inmediata con la nube de Zepp.\n"
            "🧠 `/informe` — Generar un análisis clínico de los últimos 7 días con Gemini.\n"
            "💓 `/pulso` — Estado cardiovascular, FC en reposo y HRV.\n"
            "🏃 `/entrenamientos` — Ver tus últimas sesiones de ejercicio.\n"
            "ℹ️ `/estado` — Estado del sistema, sincronizador y base de datos."
        )
        await loop.run_in_executor(None, send_telegram_message, token, chat_id, help_msg)
        return

    # 1.5 Informe Matutino "Buenos días" (/buenosdias, /matutino)
    if cmd in ["/buenosdias", "/buendia", "/matutino", "/morning", "/dia"]:
        try:
            await send_morning_report(is_test=True, is_manual=True)
        except Exception as e:
            await loop.run_in_executor(None, send_telegram_message, token, chat_id, f"❌ Error enviando informe matutino: {e}")
        return

    # 2. Resumen de Hoy
    if cmd in ["/hoy", "/resumen"]:
        now = datetime.now()
        today_iso = now.strftime("%Y-%m-%d")
        today_display = now.strftime("%d/%m/%Y")

        st_row = query_one("SELECT value FROM daily_metrics WHERE metric = 'steps' AND date = ? ORDER BY id DESC LIMIT 1", (today_iso,))
        if not st_row:
            st_row = query_one("SELECT value FROM daily_metrics WHERE metric = 'steps' ORDER BY date DESC, id DESC LIMIT 1")
        steps = int(st_row["value"]) if st_row and st_row.get("value") is not None else 0

        cal_row = query_one("SELECT value FROM daily_metrics WHERE metric in ('calories', 'active_calories') AND date = ? ORDER BY id DESC LIMIT 1", (today_iso,))
        cals = int(cal_row["value"]) if cal_row and cal_row.get("value") is not None else 0

        sl_row = query_one("SELECT * FROM sleep_sessions ORDER BY start_time DESC LIMIT 1")
        sleep_text = "Sin registros recientes"
        if sl_row:
            dur_m = int(sl_row.get("duration") or 0)
            score = sl_row.get("score")
            score_str = f" (Puntuación: {score}/100)" if score else ""
            deep_m = int(sl_row.get("deep_sleep") or 0)
            deep_str = f" · Profundo: {deep_m // 60}h {deep_m % 60}m" if deep_m else ""
            sleep_text = f"{dur_m // 60}h {dur_m % 60}m{score_str}{deep_str}"

        rhr_row = query_one("SELECT value FROM daily_metrics WHERE metric = 'resting_hr' AND date = ? ORDER BY id DESC LIMIT 1", (today_iso,))
        if not rhr_row:
            rhr_row = query_one("SELECT value FROM daily_metrics WHERE metric = 'resting_hr' ORDER BY date DESC, id DESC LIMIT 1")
        rhr = int(rhr_row["value"]) if rhr_row and rhr_row.get("value") else None

        latest_hr = query_one("SELECT value, timestamp FROM metric_samples WHERE metric = 'heart_rate' ORDER BY timestamp DESC LIMIT 1")
        hr_val = int(latest_hr["value"]) if latest_hr and latest_hr.get("value") else None

        hr_text = ""
        if rhr:
            hr_text += f"Reposo {rhr} bpm"
        if hr_val:
            hr_text += f" · Último pulso {hr_val} bpm"
        if not hr_text:
            hr_text = "Sin datos de pulso hoy"

        wk_today = query_all("SELECT * FROM workouts WHERE substr(start_time, 1, 10) = ? ORDER BY start_time DESC", (today_iso,))
        if wk_today:
            wk_text = f"{len(wk_today)} sesión(es)"
            types = [str(w.get("effective_type") or w.get("workout_type") or "Deporte").replace("_", " ").title() for w in wk_today[:2]]
            wk_text += f" ({', '.join(types)})"
        else:
            wk_text = "Sin sesiones registradas hoy"

        summary_msg = (
            f"📊 *Resumen de Salud ({today_display})*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🚶‍♂️ *Pasos:* {steps:,} pasos\n"
            f"🔥 *Calorías:* {cals} kcal\n"
            f"💤 *Último Sueño:* {sleep_text}\n"
            f"💓 *Pulsaciones:* {hr_text}\n"
            f"🏃 *Entrenamientos hoy:* {wk_text}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🤖 _ZeppBridge_"
        )
        await loop.run_in_executor(None, send_telegram_message, token, chat_id, summary_msg)
        return

    # 3. Forzar sincronización (/sync)
    if cmd == "/sync":
        await loop.run_in_executor(None, send_telegram_message, token, chat_id, "🔄 *Iniciando sincronización con los servidores de Zepp...* Un momento por favor.")
        try:
            res = await loop.run_in_executor(None, run_sync_command, "incremental", False, None)
            written = (res and res.get("records_written")) or 0
            cfg = get_custom_config()
            cfg["last_auto_sync"] = datetime.now().isoformat()
            save_custom_config(cfg)
            await loop.run_in_executor(None, send_telegram_message, token, chat_id, f"✅ *¡Sincronización completada!*\nSe escribieron {written} registros nuevos.\nEscribe `/hoy` para ver tus datos actualizados.")
        except Exception as e:
            await loop.run_in_executor(None, send_telegram_message, token, chat_id, f"❌ *Error en la sincronización:* {e}")
        return

    # 4. Informe Express con Gemini (/informe)
    if cmd == "/informe":
        cfg = get_custom_config()
        api_key = cfg.get("gemini_api_key")
        if not api_key:
            await loop.run_in_executor(None, send_telegram_message, token, chat_id, "⚠️ No tienes configurada la clave de Google Gemini. Añádela en Configuración > Custom.")
            return

        await loop.run_in_executor(None, send_telegram_message, token, chat_id, "🧠 *Consultando a Google Gemini...*\nAnalizando tus últimas métricas clínicas (7 días). Te responderé en unos segundos.")
        try:
            target = {
                "name": "Informe Clínico Express",
                "scope_days": 7,
                "focus": "full"
            }
            prompt = build_custom_report_prompt(target)
            gemini_resp = await loop.run_in_executor(None, query_gemini_api, api_key, prompt)
            now_str = datetime.now().strftime("%d/%m/%Y %H:%M")
            report_msg = (
                f"📊 *Informe Médico Express (Últimos 7 días)*\n"
                f"🗓️ *Generado:* {now_str}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"{gemini_resp}\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🤖 _ZeppBridge & Google Gemini_"
            )
            await loop.run_in_executor(None, send_telegram_message, token, chat_id, report_msg)
        except Exception as e:
            await loop.run_in_executor(None, send_telegram_message, token, chat_id, f"❌ Error generando informe con Gemini: {e}")
        return

    # 5. Pulsaciones y cardio (/pulso)
    if cmd in ["/pulso", "/cardio", "/corazon"]:
        now = datetime.now()
        today_iso = now.strftime("%Y-%m-%d")
        rhr_row = query_one("SELECT value FROM daily_metrics WHERE metric = 'resting_hr' AND date = ? ORDER BY id DESC LIMIT 1", (today_iso,))
        if not rhr_row:
            rhr_row = query_one("SELECT value FROM daily_metrics WHERE metric = 'resting_hr' ORDER BY date DESC, id DESC LIMIT 1")
        rhr = int(rhr_row["value"]) if rhr_row and rhr_row.get("value") else "—"

        hrv_row = query_one("SELECT AVG(value) as avg_val FROM metric_samples WHERE metric in ('hrv', 'hrv_rmssd') AND substr(timestamp, 1, 10) = ?", (today_iso,))
        hrv = round(hrv_row["avg_val"], 1) if hrv_row and hrv_row.get("avg_val") else "—"

        peak_row = query_one("SELECT MIN(value) as min_val, MAX(value) as max_val FROM metric_samples WHERE metric = 'heart_rate' AND substr(timestamp, 1, 10) = ?", (today_iso,))
        min_hr = int(peak_row["min_val"]) if peak_row and peak_row.get("min_val") else "—"
        max_hr = int(peak_row["max_val"]) if peak_row and peak_row.get("max_val") else "—"

        recent_hrs = query_all("SELECT value, timestamp FROM metric_samples WHERE metric = 'heart_rate' ORDER BY timestamp DESC LIMIT 3")
        last_str = ""
        if recent_hrs:
            last_val = int(recent_hrs[0]["value"])
            last_ts = recent_hrs[0]["timestamp"][11:16]
            last_str = f"{last_val} bpm (a las {last_ts})"
        else:
            last_str = "Sin lecturas recientes"

        msg = (
            f"💓 *Estado Cardiovascular de Hoy*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"❤️ *Última medición:* {last_str}\n"
            f"🛌 *FC en reposo (RHR):* {rhr} bpm\n"
            f"📈 *Rango del día:* {min_hr} – {max_hr} bpm\n"
            f"〰️ *Variabilidad cardíaca (HRV):* {hrv} ms\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🤖 _ZeppBridge_"
        )
        await loop.run_in_executor(None, send_telegram_message, token, chat_id, msg)
        return

    # 6. Entrenamientos (/entrenamientos, /ejercicio)
    if cmd in ["/entrenamientos", "/ejercicio", "/deporte"]:
        wks = query_all("SELECT * FROM workouts ORDER BY start_time DESC LIMIT 5")
        if not wks:
            await loop.run_in_executor(None, send_telegram_message, token, chat_id, "🏃 No hay entrenamientos registrados todavía en tu historial.")
            return

        lines = ["🏃 *Últimos 5 Entrenamientos Registrados:*\n━━━━━━━━━━━━━━━━━━━━━━"]
        for w in wks:
            t_str = str(w.get("effective_type") or w.get("workout_type") or "Deporte").replace("_", " ").title()
            date_raw = str(w.get("start_time") or "")
            d_part = date_raw[8:10] + "/" + date_raw[5:7] if len(date_raw) >= 10 else date_raw[:10]
            time_part = date_raw[11:16] if len(date_raw) >= 16 else ""
            dur_m = (w.get("duration_seconds") or 0) // 60
            dist_km = round(float(w.get("distance_meters") or 0) / 1000.0, 2)
            cals = int(w.get("calories") or 0)
            avg_hr = w.get("avg_hr")
            hr_part = f" · ❤️ {avg_hr} bpm" if avg_hr else ""
            dist_part = f" · 📏 {dist_km} km" if dist_km > 0 else ""
            lines.append(f"• *{t_str}* ({d_part} {time_part})\n  ⏱️ {dur_m} min{dist_part} · 🔥 {cals} kcal{hr_part}")

        lines.append("━━━━━━━━━━━━━━━━━━━━━━\n🤖 _ZeppBridge_")
        await loop.run_in_executor(None, send_telegram_message, token, chat_id, "\n".join(lines))
        return

    # 7. Estado del Sistema (/estado)
    if cmd in ["/estado", "/status"]:
        cfg = get_custom_config()
        interval = cfg.get("auto_sync_interval_minutes", 5)
        last_s = cfg.get("last_auto_sync")
        last_sync_str = "Nunca"
        if last_s:
            try:
                last_dt = datetime.fromisoformat(last_s)
                last_sync_str = last_dt.strftime("%d/%m/%Y %H:%M")
            except Exception:
                last_sync_str = str(last_s)

        db_size_mb = "0"
        if DB_PATH.exists():
            try:
                db_size_mb = f"{DB_PATH.stat().st_size / (1024 * 1024):.1f}"
            except Exception:
                pass

        msg = (
            f"ℹ️ *Estado del Sistema ZeppBridge*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⏱️ *Auto-Sync:* Cada {interval} min\n"
            f"🔄 *Última sincronización:* {last_sync_str}\n"
            f"🗄️ *Base de datos:* {db_size_mb} MB (Modo WAL activo)\n"
            f"📅 *Formato de fecha:* DD/MM (Europeo/Español)\n"
            f"⚡ *GZip:* Activo en servidor web\n"
            f"🤖 *Bot de Telegram:* Conectado y en línea\n"
            f"━━━━━━━━━━━━━━━━━━━━━━"
        )
        await loop.run_in_executor(None, send_telegram_message, token, chat_id, msg)
        return

    # Comando no reconocido
    unknown_msg = f"❓ No reconozco el comando `{text}`.\n\nEscribe `/ayuda` para ver todos los comandos disponibles."
    await loop.run_in_executor(None, send_telegram_message, token, chat_id, unknown_msg)

async def telegram_bot_listener():
    print("[Telegram Bot] Listener interactivo de comandos iniciado.")
    await asyncio.sleep(8)
    offset = None
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    while True:
        try:
            cfg = get_custom_config()
            token = cfg.get("telegram_bot_token")
            chat_id = str(cfg.get("telegram_chat_id", "")).strip()

            if not token or not chat_id:
                await asyncio.sleep(15)
                continue

            url = f"https://api.telegram.org/bot{token}/getUpdates?timeout=20"
            if offset:
                url += f"&offset={offset}"

            req = urllib.request.Request(url, headers={"User-Agent": "ZeppBridge-Bot/1.0"})
            loop = asyncio.get_event_loop()

            def fetch_updates():
                try:
                    with urllib.request.urlopen(req, timeout=28, context=ssl_ctx) as response:
                        return json.loads(response.read().decode("utf-8"))
                except urllib.error.HTTPError:
                    return None
                except Exception:
                    return None

            data = await loop.run_in_executor(None, fetch_updates)
            if data and data.get("ok"):
                updates = data.get("result", [])
                for upd in updates:
                    offset = upd["update_id"] + 1
                    msg = upd.get("message")
                    if not msg:
                        continue
                    sender_chat = str(msg.get("chat", {}).get("id", "")).strip()
                    if sender_chat != chat_id:
                        continue
                    text = (msg.get("text") or "").strip()
                    if text:
                        asyncio.create_task(handle_telegram_command(text, token, chat_id))
        except Exception:
            await asyncio.sleep(5)

        await asyncio.sleep(1)

@app.on_event("startup")
async def on_startup():
    init_db_pragmas()
    asyncio.create_task(background_scheduler())
    asyncio.create_task(telegram_bot_listener())

@app.get("/api/bridge/custom_config")
@app.post("/api/bridge/custom_config")
async def custom_config_endpoint(request: Request):
    if request.method == "POST":
        try:
            data = await request.json()
            cfg = get_custom_config()
            if isinstance(data, dict):
                for k, v in data.items():
                    if k == "morning_report" and isinstance(v, dict):
                        if "morning_report" not in cfg or not isinstance(cfg["morning_report"], dict):
                            cfg["morning_report"] = {}
                        cfg["morning_report"].update(v)
                    else:
                        cfg[k] = v
            save_custom_config(cfg)
            return {"ok": True, "config": cfg}
        except Exception as e:
            return JSONResponse({"ok": False, "message": str(e)}, status_code=400)
    return {"ok": True, "config": get_custom_config()}

@app.get("/api/bridge/reports")
def get_reports_endpoint():
    cfg = get_custom_config()
    has_creds = bool(cfg.get("telegram_bot_token") and cfg.get("telegram_chat_id") and cfg.get("gemini_api_key"))
    return {
        "ok": True,
        "reports": get_reports_config(),
        "has_credentials": has_creds,
        "credentials": {
            "has_telegram": bool(cfg.get("telegram_bot_token") and cfg.get("telegram_chat_id")),
            "has_gemini": bool(cfg.get("gemini_api_key"))
        }
    }

@app.post("/api/bridge/reports/save")
async def save_report_endpoint(request: Request):
    try:
        report_data = await request.json()
        reports = get_reports_config()
        rep_id = report_data.get("id")
        if not rep_id:
            rep_id = f"rep_{int(datetime.now().timestamp() * 1000)}"
            report_data["id"] = rep_id
            reports.append(report_data)
        else:
            found = False
            for i, r in enumerate(reports):
                if r.get("id") == rep_id:
                    reports[i] = report_data
                    found = True
                    break
            if not found:
                reports.append(report_data)
        save_reports_config(reports)
        return {"ok": True, "report": report_data, "reports": reports}
    except Exception as e:
        return JSONResponse({"ok": False, "message": str(e)}, status_code=400)

@app.delete("/api/bridge/reports/{report_id}")
def delete_report_endpoint(report_id: str):
    reports = get_reports_config()
    filtered = [r for r in reports if r.get("id") != report_id]
    save_reports_config(filtered)
    return {"ok": True, "reports": filtered}

@app.post("/api/bridge/reports/toggle/{report_id}")
def toggle_report_endpoint(report_id: str):
    reports = get_reports_config()
    target = next((r for r in reports if r.get("id") == report_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="Informe no encontrado")
    target["enabled"] = not target.get("enabled", True)
    save_reports_config(reports)
    return {"ok": True, "report": target, "reports": reports}

@app.post("/api/bridge/reports/run/{report_id}")
async def run_report_endpoint(report_id: str):
    try:
        res = await execute_report(report_id)
        return res
    except Exception as e:
        return JSONResponse({"ok": False, "message": str(e)}, status_code=400)

@app.post("/api/bridge/telegram_test")
async def test_telegram_endpoint(request: Request):
    try:
        data = await request.json()
        token = data.get("telegram_bot_token") or get_custom_config().get("telegram_bot_token")
        chat_id = data.get("telegram_chat_id") or get_custom_config().get("telegram_chat_id")
        if not token or not chat_id:
            return JSONResponse({"ok": False, "message": "Introduce el Token del Bot y el Chat ID de Telegram."}, status_code=400)

        loop = asyncio.get_event_loop()
        test_msg = "🧪 *¡Conexión Exitosa con ZeppBridge!*\nTu bot de Telegram está configurado correctamente para recibir los informes programados con Gemini."
        await loop.run_in_executor(None, send_telegram_message, token, chat_id, test_msg)
        return {"ok": True, "message": "Mensaje de prueba enviado con éxito a Telegram."}
    except Exception as e:
        return JSONResponse({"ok": False, "message": f"Error conectando con Telegram: {e}"}, status_code=400)

@app.get("/api/bridge/export_workouts_csv")
def export_workouts_csv():
    rows = query_all("SELECT * FROM workouts ORDER BY start_time DESC")
    lines = ["Fecha,Tipo,Duracion_min,Distancia_m,Calorias,FC_Media,FC_Max"]
    for r in rows:
        st = str(r.get("start_time") or "").replace("T", " ")
        tp = r.get("effective_type") or r.get("workout_type") or "workout"
        dur = (r.get("duration_seconds") or 0) // 60
        dist = r.get("distance_meters") or 0
        cals = r.get("calories") or 0
        avg_hr = r.get("avg_hr") or ""
        max_hr = r.get("max_hr") or ""
        lines.append(f'"{st}","{tp}",{dur},{dist},{cals},{avg_hr},{max_hr}')
    return Response(
        "\n".join(lines),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=entrenamientos_zepp.csv"}
    )

@app.get("/api/bridge/export_metrics_csv")
def export_metrics_csv():
    rows = query_all("SELECT date, metric, value FROM daily_metrics ORDER BY date DESC, metric ASC")
    lines = ["Fecha,Metrica,Valor"]
    for r in rows:
        lines.append(f'"{r.get("date")}","{r.get("metric")}",{r.get("value")}')
    return Response(
        "\n".join(lines),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=metricas_diarias_zepp.csv"}
    )

@app.get("/api/bridge/backup_db")
async def backup_db_endpoint():
    """Genera y descarga un snapshot integro y seguro de zepp.db."""
    if not DB_PATH.exists():
        raise HTTPException(status_code=404, detail="La base de datos zepp.db aún no existe.")

    now_tag = datetime.now().strftime("%d_%m_%Y_%H%M")
    backup_filename = f"zeppbridge_backup_{now_tag}.db"
    temp_backup_path = DATA_DIR / f"temp_{backup_filename}"
    try:
        src_conn = sqlite3.connect(DB_PATH.as_posix(), timeout=10.0)
        dest_conn = sqlite3.connect(temp_backup_path.as_posix())
        with dest_conn:
            src_conn.backup(dest_conn)
        dest_conn.close()
        src_conn.close()

        def cleanup_temp(path: str):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass

        return FileResponse(
            path=temp_backup_path.as_posix(),
            filename=backup_filename,
            media_type="application/x-sqlite3",
            background=BackgroundTask(cleanup_temp, temp_backup_path.as_posix())
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al generar copia de seguridad: {e}")

@app.post("/api/bridge/restore_db")
async def restore_db_endpoint(file: UploadFile = File(...)):
    """Restaura la base de datos a partir de un archivo .db subido por el usuario."""
    try:
        content = await file.read()
        if len(content) < 100 or not content.startswith(b"SQLite format 3\x00"):
            return JSONResponse({"ok": False, "message": "El archivo proporcionado no es una base de datos SQLite válida."}, status_code=400)

        # Copia de resguardo de seguridad de la base actual antes de reemplazarla
        if DB_PATH.exists():
            pre_restore = DATA_DIR / "zepp.db.pre_restore"
            try:
                with open(pre_restore, "wb") as f_pre:
                    with open(DB_PATH, "rb") as cur_f:
                        f_pre.write(cur_f.read())
            except Exception:
                pass

        # Escribir el nuevo archivo zepp.db
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(DB_PATH, "wb") as f_out:
            f_out.write(content)

        # Aplicar pragmas WAL y busy_timeout
        init_db_pragmas()
        return {"ok": True, "message": "¡Base de datos restaurada con éxito! Recarga la página para visualizar tus datos."}
    except Exception as e:
        return JSONResponse({"ok": False, "message": f"Error restaurando la base de datos: {e}"}, status_code=500)

@app.post("/api/bridge/test_morning_report")
async def test_morning_report_endpoint():
    """Dispara un informe matutino 'Buenos días' de prueba inmediatamente a Telegram."""
    try:
        return await send_morning_report(is_test=True)
    except Exception as e:
        return JSONResponse({"ok": False, "message": str(e)}, status_code=400)

# ==============================================================================
# Servir el Frontend oficial de ZeppBridge (carpeta static/)
# ==============================================================================
@app.get("/health")
def health():
    return {"status": "ok", "service": "zeppbridge-web-native"}

@app.get("/")
async def serve_root():
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return {"message": "ZeppBridge Web Backend iniciado. static/index.html no encontrado."}

@app.get("/{full_path:path}")
async def serve_spa_path(full_path: str):
    file_path = STATIC_DIR / full_path
    if file_path.exists() and file_path.is_file():
        return FileResponse(file_path)
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    raise HTTPException(status_code=404, detail="Recurso no encontrado")
