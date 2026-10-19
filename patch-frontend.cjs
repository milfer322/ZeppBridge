const fs = require('fs');
const path = require('path');

console.log('[Patch] Iniciando personalización del frontend oficial de ZeppBridge...');

// 1. Inyectar conector web-ipc.js en index.html
if (fs.existsSync('index.html')) {
  let html = fs.readFileSync('index.html', 'utf8');
  if (!html.includes('/web-ipc.js')) {
    html = html.replace(
      '<script type="module" src="/src/main.ts"></script>',
      '<script src="/web-ipc.js"></script>\n    <script type="module" src="/src/main.ts"></script>'
    );
    fs.writeFileSync('index.html', html, 'utf8');
    console.log('[Patch] Conector web-ipc.js inyectado en index.html');
  }
}

// 2. Integrar opciones de Salud Cardíaca y Pulsaciones nativamente en FutureCard.vue (Pasar a la IA)
const futureCardPath = 'src/components/ai/stage/FutureCard.vue';
if (fs.existsSync(futureCardPath)) {
  let code = fs.readFileSync(futureCardPath, 'utf8');
  
  // Agregar heart_week y heart_month a los Intents
  code = code.replace(
    "type Intent = 'free' | 'sleep' | 'week' | 'workout' | 'next';",
    "type Intent = 'free' | 'heart_week' | 'heart_month' | 'sleep' | 'week' | 'workout' | 'next';"
  );

  // Mapear los templates con sus días (7 y 30 días)
  code = code.replace(
    "const TEMPLATE_OF: Record<Exclude<Intent, 'free'>, { id: string; days: number }> = {",
    "const TEMPLATE_OF: Record<Exclude<Intent, 'free'>, { id: string; days: number }> = {\n  heart_week: { id: 'tpl_heart_weekly', days: 7 },\n  heart_month: { id: 'tpl_heart_monthly', days: 30 },"
  );

  // Agregar los items en el carrusel de opciones
  code = code.replace(
    "{ value: 'free' as Intent, label: s.value.freeIntent },",
    "{ value: 'free' as Intent, label: s.value.freeIntent },\n  { value: 'heart_week' as Intent, label: '💓 Pulsaciones (7d)', title: 'Analizar pulsaciones cardíacas, FC en reposo y HRV de los últimos 7 días' },\n  { value: 'heart_month' as Intent, label: '📈 Cardio (30d)', title: 'Analizar tendencias de pulsaciones y evolución cardiovascular de 30 días' },"
  );

  // Agregar las preguntas predefinidas
  code = code.replace(
    "const questionsOf = (text: typeof t.value) => ({",
    "const questionsOf = (text: typeof t.value) => ({\n  heart_week: 'Analiza mis pulsaciones cardíacas de los últimos 7 días. Evalúa mi frecuencia cardíaca en reposo (RHR), picos máximos, zonas de esfuerzo en entrenamientos y variabilidad cardíaca (HRV) para determinar mi nivel de fatiga, recuperación y salud cardiovascular general.',\n  heart_month: 'Analiza la evolución de mis pulsaciones a lo largo del último mes (30 días). Evalúa la tendencia de mi frecuencia cardíaca en reposo, tolerancia al esfuerzo y regularidad del ritmo para identificar mejoras o posibles alertas de sobrecarga o estrés cardiovascular.',"
  );

  fs.writeFileSync(futureCardPath, code, 'utf8');
  console.log('[Patch] Opciones de Salud Cardíaca integradas en FutureCard.vue (Pasar a la IA)');
}

// 3. Crear el componente CustomSection.vue en Configuración con Auto-Sync, Informes Cron (Gemini + Telegram) y Exportador
const customSectionDir = 'src/views/settings/sections';
if (fs.existsSync(customSectionDir)) {
  const customSectionVue = path.join(customSectionDir, 'CustomSection.vue');
  const customSectionContent = `<script setup lang="ts">
import { ref, computed, onMounted } from 'vue';
import Icon from '../../../components/Icon.vue';
import { dateOrder, setDateOrder, timeFormat, setTimeFormat, type DateOrder, type TimeFormat } from '../../../lib/dateTime';

const cfg = ref<any>({
  auto_sync_enabled: true,
  auto_sync_interval_minutes: 5,
  last_auto_sync: null,
  telegram_bot_token: '',
  telegram_chat_id: '',
  gemini_api_key: '',
  morning_report: {
    enabled: false,
    hour: 8,
    minute: 30,
    days_of_week: ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'],
    include_gemini: true,
    last_sent: null
  }
});

const hasTelegram = computed(() => Boolean(cfg.value?.telegram_bot_token?.trim() && cfg.value?.telegram_chat_id?.trim()));
const hasGemini = computed(() => Boolean(cfg.value?.gemini_api_key?.trim()));

const reports = ref<any[]>([]);
const hasCreds = ref(false);
const cfgSaving = ref(false);
const cfgSavedMsg = ref('');
const testingTelegram = ref(false);
const testResult = ref<{ ok: boolean; message: string } | null>(null);

const testingMorning = ref(false);
const testMorningResult = ref<{ ok: boolean; message: string } | null>(null);

const runningReportId = ref<string | null>(null);
const runResult = ref<{ id: string; ok: boolean; message: string } | null>(null);

// Modal para crear / editar informe
const showModal = ref(false);
const showDiffs = ref(true);
const editReport = ref<any>({
  id: '',
  name: '',
  enabled: true,
  schedule_type: 'daily',
  hour: 22,
  minute: 0,
  days_of_week: ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'],
  day_of_month: 1,
  scope_days: 1,
  focus: 'full',
  custom_prompt: ''
});

const syncing = ref(false);
const syncResult = ref<{ ok: boolean; message: string; count?: number } | null>(null);
const restoreFileInput = ref<HTMLInputElement | null>(null);
const restoring = ref(false);
const restoreResult = ref<{ ok: boolean; message: string } | null>(null);
const logOpen = ref(false);
const logLoading = ref(false);
const logText = ref('');
const copied = ref(false);

const unknownCodesList = ref<any[]>([]);
const loadingCodes = ref(false);
const codesChecked = ref(false);

const loadUnknownCodes = async () => {
  loadingCodes.value = true;
  try {
    const res = await fetch('/api/bridge/get_unknown_workout_codes', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({})
    });
    const data = await res.json();
    if (Array.isArray(data)) {
      unknownCodesList.value = data.map((item: any) => ({
        ...item,
        draftLabel: item.label || ''
      }));
    }
    codesChecked.value = true;
  } catch (e) {
  } finally {
    loadingCodes.value = false;
  }
};

const saveCodeLabel = async (zeppType: number, label: string) => {
  try {
    await fetch('/api/bridge/set_workout_code_label', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ zeppType, label })
    });
    await loadUnknownCodes();
  } catch (e) {}
};

const loadData = async () => {
  try {
    void loadUnknownCodes();
    const cRes = await fetch('/api/bridge/custom_config');
    const cData = await cRes.json();
    if (cData && cData.config) {
      const merged = { ...cfg.value, ...cData.config };
      merged.morning_report = {
        enabled: false,
        hour: 8,
        minute: 30,
        days_of_week: ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'],
        include_gemini: true,
        last_sent: null,
        ...(cData.config.morning_report || {})
      };
      cfg.value = merged;
    }

    const rRes = await fetch('/api/bridge/reports');
    const rData = await rRes.json();
    if (rData && rData.reports) {
      reports.value = rData.reports;
      hasCreds.value = rData.has_credentials;
    }
  } catch (e) {}
};

const saveConfig = async () => {
  cfgSaving.value = true;
  cfgSavedMsg.value = '';
  try {
    const res = await fetch('/api/bridge/custom_config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(cfg.value)
    });
    const data = await res.json();
    if (data.ok) {
      cfgSavedMsg.value = '¡Ajustes guardados con éxito!';
      await loadData();
      setTimeout(() => { cfgSavedMsg.value = ''; }, 3000);
    }
  } catch (e: any) {
    alert('Error guardando: ' + (e?.message || e));
  } finally {
    cfgSaving.value = false;
  }
};

const testTelegram = async () => {
  testingTelegram.value = true;
  testResult.value = null;
  try {
    await saveConfig();
    const res = await fetch('/api/bridge/telegram_test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        telegram_bot_token: cfg.value.telegram_bot_token,
        telegram_chat_id: cfg.value.telegram_chat_id
      })
    });
    const data = await res.json();
    testResult.value = data;
  } catch (e: any) {
    testResult.value = { ok: false, message: e?.message || String(e) };
  } finally {
    testingTelegram.value = false;
  }
};

const openCreateModal = () => {
  editReport.value = {
    id: '',
    name: 'Nuevo Informe de Salud',
    enabled: true,
    schedule_type: 'daily',
    hour: 22,
    minute: 0,
    days_of_week: ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'],
    day_of_month: 1,
    scope_days: 1,
    focus: 'full',
    custom_prompt: ''
  };
  showModal.value = true;
};

const openEditModal = (rep: any) => {
  editReport.value = JSON.parse(JSON.stringify(rep));
  showModal.value = true;
};

const saveReportForm = async () => {
  if (!editReport.value.name.trim()) {
    alert('Introduce un nombre para el informe.');
    return;
  }
  try {
    const res = await fetch('/api/bridge/reports/save', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(editReport.value)
    });
    const data = await res.json();
    if (data.ok) {
      reports.value = data.reports;
      showModal.value = false;
    }
  } catch (e: any) {
    alert('Error al guardar informe: ' + (e?.message || e));
  }
};

const toggleReport = async (rep: any) => {
  try {
    const res = await fetch('/api/bridge/reports/toggle/' + rep.id, { method: 'POST' });
    const data = await res.json();
    if (data.ok) reports.value = data.reports;
  } catch (e) {}
};

const deleteReport = async (id: string) => {
  if (!confirm('¿Seguro que deseas eliminar este informe programado?')) return;
  try {
    const res = await fetch('/api/bridge/reports/' + id, { method: 'DELETE' });
    const data = await res.json();
    if (data.ok) reports.value = data.reports;
  } catch (e) {}
};

const runReport = async (rep: any) => {
  runningReportId.value = rep.id;
  runResult.value = null;
  try {
    const res = await fetch('/api/bridge/reports/run/' + rep.id, { method: 'POST' });
    const data = await res.json();
    runResult.value = { id: rep.id, ok: data.ok, message: data.message };
    if (data.ok) loadData();
  } catch (e: any) {
    runResult.value = { id: rep.id, ok: false, message: e?.message || String(e) };
  } finally {
    runningReportId.value = null;
  }
};

const toggleDay = (d: string) => {
  const arr = editReport.value.days_of_week || [];
  const idx = arr.indexOf(d);
  if (idx > -1) {
    if (arr.length > 1) arr.splice(idx, 1);
  } else {
    arr.push(d);
  }
  editReport.value.days_of_week = arr;
};

const testMorningReport = async () => {
  testingMorning.value = true;
  testMorningResult.value = null;
  try {
    await saveConfig();
    const res = await fetch('/api/bridge/test_morning_report', { method: 'POST' });
    const data = await res.json();
    testMorningResult.value = data;
  } catch (e: any) {
    testMorningResult.value = { ok: false, message: e?.message || String(e) };
  } finally {
    testingMorning.value = false;
  }
};

const toggleMorningDay = (d: string) => {
  if (!cfg.value.morning_report) {
    cfg.value.morning_report = {
      enabled: false,
      hour: 8,
      minute: 30,
      days_of_week: ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'],
      include_gemini: true,
      last_sent: null
    };
  }
  const arr = [...(cfg.value.morning_report.days_of_week || [])];
  const idx = arr.indexOf(d);
  if (idx > -1) {
    if (arr.length > 1) arr.splice(idx, 1);
  } else {
    arr.push(d);
  }
  cfg.value.morning_report.days_of_week = arr;
};

const runHistorySync = async () => {
  syncing.value = true;
  syncResult.value = null;
  try {
    const res = await (window as any).__TAURI_INTERNALS__?.invoke('start_history_sync', { days: 180 });
    const count = (res && (res.records_written || res.total_records)) ?? 0;
    syncResult.value = {
      ok: true,
      message: 'Sincronización de 180 días completada con éxito (' + count + ' registros escritos).',
      count
    };
  } catch (err: any) {
    syncResult.value = { ok: false, message: 'Error: ' + (err?.message || err) };
  } finally {
    syncing.value = false;
  }
};

const toggleLog = async () => {
  logOpen.value = !logOpen.value;
  if (logOpen.value) {
    logLoading.value = true;
    try {
      const res = await fetch('/api/bridge/sync_log');
      logText.value = await res.text();
    } catch (e: any) {
      logText.value = 'Error leyendo log: ' + e.message;
    } finally {
      logLoading.value = false;
    }
  }
};

const copyLog = async () => {
  if (!logText.value) return;
  try {
    await navigator.clipboard.writeText(logText.value);
    copied.value = true;
    setTimeout(() => { copied.value = false; }, 2000);
  } catch (e) {}
};

const onTimeFormatChange = (val: TimeFormat) => {
  setTimeFormat(val);
};

const onDateOrderChange = (val: DateOrder) => {
  setDateOrder(val);
};

const handleRestoreFile = async (e: Event) => {
  const target = e.target as HTMLInputElement;
  if (!target.files || !target.files[0]) return;
  const file = target.files[0];
  if (!confirm("¿Estás seguro de restaurar '" + file.name + "'? Esto reemplazará la base de datos actual con la del archivo seleccionado.")) {
    target.value = '';
    return;
  }

  restoring.value = true;
  restoreResult.value = null;
  try {
    const formData = new FormData();
    formData.append('file', file);
    const res = await fetch('/api/bridge/restore_db', {
      method: 'POST',
      body: formData
    });
    const data = await res.json();
    restoreResult.value = { ok: res.ok && data.ok, message: data.message || 'Restauración completada.' };
  } catch (err: any) {
    restoreResult.value = { ok: false, message: 'Error de conexión: ' + err.message };
  } finally {
    restoring.value = false;
    target.value = '';
  }
};

onMounted(() => {
  loadData();
});
</script>

<template>
  <div class="custom-section">
    <!-- SECCIÓN DEPORTES Y PLANIFICADOR DE ENTRENAMIENTOS -->
    <section class="s-section">
      <div class="s-section-head">
        <h3>🏃 Entrenamientos, Deportes y Planificador Semanal</h3>
        <p class="s-row-sub" style="margin: 2px 0 0 0;">Acceso directo al diseñador de planes para tu reloj Amazfit y métricas deportivas avanzadas.</p>
      </div>
      <div class="s-list">
        <div class="s-row is-block">
          <div style="display: flex; flex-direction: column; gap: 12px; width: 100%;">
            <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 10px;">
              <a href="#/ai/plan" class="rep-card" style="text-decoration: none; display: flex; flex-direction: column; gap: 6px; cursor: pointer;">
                <div style="display: flex; align-items: center; gap: 8px;">
                  <span style="font-size: 20px;">📅</span>
                  <strong style="color: #6dd58c; font-size: 14px;">Planificador Semanal</strong>
                </div>
                <span style="font-size: 12px; color: #c4c6d0;">Diseña entrenamientos por series, intervalos, ritmos y zonas FC. Se sincronizan directo a tu reloj Amazfit.</span>
                <span style="font-size: 11px; color: var(--accent, #6dd58c); margin-top: 4px; font-weight: 600;">Abrir Planificador ➔</span>
              </a>

              <a href="#/workouts" class="rep-card" style="text-decoration: none; display: flex; flex-direction: column; gap: 6px; cursor: pointer;">
                <div style="display: flex; align-items: center; gap: 8px;">
                  <span style="font-size: 20px;">🏃</span>
                  <strong style="color: #6dd58c; font-size: 14px;">Historial de Entrenamientos</strong>
                </div>
                <span style="font-size: 12px; color: #c4c6d0;">Consulta mapas GPS, ritmos por km, desniveles, vueltas (splits), zonas cardíacas y cadencia de cada sesión.</span>
                <span style="font-size: 11px; color: var(--accent, #6dd58c); margin-top: 4px; font-weight: 600;">Ver Actividades GPS ➔</span>
              </a>

              <a href="#/training" class="rep-card" style="text-decoration: none; display: flex; flex-direction: column; gap: 6px; cursor: pointer;">
                <div style="display: flex; align-items: center; gap: 8px;">
                  <span style="font-size: 20px;">📈</span>
                  <strong style="color: #6dd58c; font-size: 14px;">Carga y Estado Físico</strong>
                </div>
                <span style="font-size: 12px; color: #c4c6d0;">Revisa tu equilibrio de carga aguda/crónica (TLB), VO2 Máx, tiempo de recuperación y umbral de lactato.</span>
                <span style="font-size: 11px; color: var(--accent, #6dd58c); margin-top: 4px; font-weight: 600;">Ver Estado Físico ➔</span>
              </a>
            </div>

            <!-- Panel de Deportes Desconocidos / Personalizados -->
            <div style="margin-top: 6px; background: #16181b; border: 1px solid #282a2e; border-radius: 8px; padding: 12px 14px;">
              <div style="display: flex; justify-content: space-between; align-items: center; gap: 8px; flex-wrap: wrap;">
                <div>
                  <strong style="font-size: 13px; color: #e2e2e6;">🏷️ Códigos de Deporte y Tipos de Ejercicio</strong>
                  <p style="font-size: 11px; color: #9da3af; margin: 2px 0 0 0;">Si tu reloj registra actividades con identificadores numéricos no estándar, puedes asignarles su nombre aquí o en Ajustes ➔ Avanzado.</p>
                </div>
                <button class="pill-button" type="button" @click="loadUnknownCodes" :disabled="loadingCodes" style="font-size: 12px;">
                  🔄 {{ loadingCodes ? 'Cargando...' : 'Comprobar Deportes' }}
                </button>
              </div>

              <div v-if="unknownCodesList.length > 0" style="margin-top: 10px; display: grid; gap: 8px;">
                <div v-for="c in unknownCodesList" :key="c.zeppType" style="display: flex; align-items: center; justify-content: space-between; background: #1e2024; padding: 8px 12px; border-radius: 6px; gap: 8px; flex-wrap: wrap;">
                  <div style="display: flex; align-items: center; gap: 8px;">
                    <span style="background: #2a2c32; padding: 2px 8px; border-radius: 4px; font-family: monospace; font-size: 11px; color: #6dd58c;">#{{ c.zeppType }}</span>
                    <span style="font-size: 12px; color: #e2e2e6;">{{ c.label || 'Sin etiqueta' }}</span>
                    <span style="font-size: 11px; color: #9da3af;">({{ c.records }} sesiones)</span>
                  </div>
                  <div style="display: flex; gap: 6px; align-items: center;">
                    <input type="text" v-model="c.draftLabel" placeholder="Nombre (ej: Pádel)" style="background: #111215; border: 1px solid #36383e; border-radius: 4px; color: #fff; padding: 4px 8px; font-size: 12px; width: 140px;">
                    <button class="pill-button" type="button" @click="saveCodeLabel(c.zeppType, c.draftLabel)" style="padding: 4px 10px; font-size: 11px;">Guardar</button>
                  </div>
                </div>
              </div>
              <div v-else-if="codesChecked" style="margin-top: 8px; font-size: 12px; color: #6dd58c;">
                ✓ Todos los deportes registrados están identificados.
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>

    <!-- SECCIÓN 1: INFORMES AUTOMATIZADOS PERSONALIZADOS (CRON GEMINI + TELEGRAM) -->
    <section class="s-section">
      <div class="s-section-head" style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px;">
        <div>
          <h3>📋 Informes Automatizados (Cron Gemini + Telegram)</h3>
          <p class="s-row-sub" style="margin: 2px 0 0 0;">Crea y programa múltiples informes periódicos para recibir tu análisis clínico en Telegram cuando quieras.</p>
        </div>
        <button class="pill-button" type="button" @click="openCreateModal" style="background: var(--accent, #6dd58c); color: #000; font-weight: 600; cursor: pointer;">
          <Icon name="plus" :size="14" /> + Nuevo Informe
        </button>
      </div>

      <!-- Alerta si faltan credenciales -->
      <div v-if="!hasTelegram || !hasGemini" class="alert danger" style="margin: 10px 0 16px 0;">
        <Icon name="warning" :size="16" />
        <span>Introduce tu <strong>Telegram Bot Token</strong>, <strong>Chat ID</strong> y <strong>Gemini API Key</strong> más abajo para que los informes puedan enviarse automáticamente.</span>
      </div>

      <!-- Banner de Bot de Telegram Interactivo -->
      <div v-if="hasTelegram" style="margin: 10px 0 16px 0; background: rgba(109, 213, 140, 0.08); border: 1px solid rgba(109, 213, 140, 0.25); border-radius: 8px; padding: 10px 14px; font-size: 12px; color: #c4c6d0;">
        <div style="display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 4px;">
          <strong style="color: #6dd58c; font-size: 13px;">🤖 Bot de Telegram Interactivo Activo</strong>
          <span class="rep-badge active">En línea</span>
        </div>
        <span>Escríbele desde tu móvil en Telegram: <code>/hoy</code> (resumen diario), <code>/buenosdias</code> (descanso matutino), <code>/sync</code> (sincronizar), <code>/informe</code> (análisis Gemini), <code>/pulso</code> (cardio), <code>/entrenamientos</code> o <code>/ayuda</code>.</span>
      </div>

      <!-- Listado de Informes -->
      <div style="display: grid; gap: 12px; margin-top: 10px;">
        <div v-for="rep in reports" :key="rep.id" class="rep-card" :style="{ opacity: rep.enabled ? '1' : '0.65' }">
          <div style="display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; flex-wrap: wrap;">
            <div>
              <div style="display: flex; align-items: center; gap: 8px;">
                <strong style="font-size: 15px; color: var(--ink, #e2e2e6);">{{ rep.name }}</strong>
                <span :class="['rep-badge', rep.enabled ? 'active' : 'paused']">{{ rep.enabled ? 'Activo' : 'Pausado' }}</span>
              </div>
              <div style="display: flex; gap: 6px; flex-wrap: wrap; margin-top: 6px;">
                <span class="rep-pill">
                  ⏱️ {{ rep.schedule_type === 'daily' ? 'Diario' : rep.schedule_type === 'weekly' ? 'Semanal' : 'Mensual' }} a las {{ String(rep.hour).padStart(2,'0') }}:{{ String(rep.minute).padStart(2,'0') }}
                </span>
                <span class="rep-pill">
                  📅 {{ rep.scope_days === 1 ? 'Datos de Hoy (1d)' : (rep.scope_days + ' días') }}
                </span>
                <span class="rep-pill">
                  🎯 Enfoque: {{ rep.focus === 'full' ? 'Completo' : rep.focus === 'heart' ? '💓 Cardio' : rep.focus === 'sleep' ? '🌙 Sueño' : rep.focus === 'workout' ? '🏃 Deporte' : '✍️ Personalizado' }}
                </span>
              </div>
              <div v-if="rep.last_run" style="font-size: 11px; color: var(--muted, #9da3af); margin-top: 6px;">
                Último envío con éxito: {{ new Date(rep.last_run).toLocaleString() }}
              </div>
            </div>

            <div style="display: flex; gap: 6px; align-items: center; flex-wrap: wrap;">
              <button class="pill-button" type="button" :disabled="runningReportId === rep.id || !hasTelegram || !hasGemini" @click="runReport(rep)" title="Generar con Gemini y enviar ahora a Telegram">
                <Icon name="spark" :size="13" /> {{ runningReportId === rep.id ? 'Generando...' : '🚀 Enviar Ahora' }}
              </button>
              <button class="pill-button" type="button" @click="toggleReport(rep)">
                {{ rep.enabled ? 'Pausar' : 'Activar' }}
              </button>
              <button class="pill-button" type="button" @click="openEditModal(rep)">
                <Icon name="edit" :size="13" /> Editar
              </button>
              <button class="pill-button" type="button" @click="deleteReport(rep.id)" style="color: #ffb4ab;">
                ✕
              </button>
            </div>
          </div>

          <div v-if="runResult && runResult.id === rep.id" :class="['alert', runResult.ok ? 'success' : 'danger']" style="margin-top: 8px;">
            <Icon :name="runResult.ok ? 'circle-check' : 'warning'" :size="14" />
            <span>{{ runResult.message }}</span>
          </div>
        </div>

        <div v-if="!reports.length" style="padding: 20px; text-align: center; color: var(--muted, #9da3af); font-size: 13px;">
          No tienes ningún informe creado todavía. Pulsa "+ Nuevo Informe" para crear el primero.
        </div>
      </div>
    </section>

    <!-- SECCIÓN: INFORME MATUTINO AUTOMÁTICO ("BUENOS DÍAS") -->
    <section class="s-section">
      <div class="s-section-head" style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px;">
        <div>
          <h3>☀️ Informe Matutino Automático ("Buenos días")</h3>
          <p class="s-row-sub" style="margin: 2px 0 0 0;">Recibe cada mañana en Telegram un resumen al despertar con tu calidad de descanso nocturno, fases de sueño, pulso en reposo, HRV y consejo clínico de Google Gemini.</p>
        </div>
        <label style="display: flex; align-items: center; gap: 8px; cursor: pointer; background: #26282c; padding: 6px 12px; border-radius: 8px; border: 1px solid #36383e;">
          <input type="checkbox" v-model="cfg.morning_report.enabled" @change="saveConfig" style="width: 18px; height: 18px; cursor: pointer;">
          <span :class="['rep-badge', cfg.morning_report?.enabled ? 'active' : 'paused']">{{ cfg.morning_report?.enabled ? 'Activado' : 'Pausado' }}</span>
        </label>
      </div>

      <div class="s-list" style="margin-top: 12px;">
        <!-- Configuración de Horario y Días cuando está activo -->
        <div class="s-row is-block" v-if="cfg.morning_report?.enabled">
          <div style="display: grid; gap: 14px; width: 100%;">
            <div style="display: flex; gap: 16px; flex-wrap: wrap; align-items: center;">
              <div style="flex: 1; min-width: 160px;">
                <label class="custom-label">⏰ Hora de envío matutino:</label>
                <div style="display: flex; gap: 6px; align-items: center;">
                  <select v-model.number="cfg.morning_report.hour" @change="saveConfig" class="custom-input-select" style="flex: 1;">
                    <option v-for="h in 24" :key="h-1" :value="h-1">{{ String(h-1).padStart(2,'0') }}h</option>
                  </select>
                  <span>:</span>
                  <select v-model.number="cfg.morning_report.minute" @change="saveConfig" class="custom-input-select" style="flex: 1;">
                    <option :value="0">00</option>
                    <option :value="15">15</option>
                    <option :value="30">30</option>
                    <option :value="45">45</option>
                  </select>
                </div>
              </div>

              <div style="flex: 2; min-width: 240px;">
                <label class="custom-label">📅 Días de la semana para recibirlo:</label>
                <div style="display: flex; gap: 6px; flex-wrap: wrap;">
                  <button
                    v-for="day in [{k:'mon',l:'Lun'},{k:'tue',l:'Mar'},{k:'wed',l:'Mié'},{k:'thu',l:'Jue'},{k:'fri',l:'Vie'},{k:'sat',l:'Sáb'},{k:'sun',l:'Dom'}]"
                    :key="day.k"
                    type="button"
                    :class="['day-btn', (cfg.morning_report?.days_of_week || []).includes(day.k) ? 'selected' : '']"
                    @click="toggleMorningDay(day.k); saveConfig();"
                  >
                    {{ day.l }}
                  </button>
                </div>
              </div>
            </div>

            <!-- Opción Gemini -->
            <div style="padding-top: 6px; border-top: 1px solid #282a2e;">
              <label style="display: flex; align-items: center; gap: 10px; cursor: pointer;">
                <input type="checkbox" v-model="cfg.morning_report.include_gemini" @change="saveConfig" style="width: 17px; height: 17px; cursor: pointer;">
                <span style="font-size: 13px; color: var(--ink, #e2e2e6);">
                  🧠 <strong>Incluir consejo clínico motivacional con Google Gemini</strong>
                  <span style="display: block; font-size: 11px; color: var(--muted, #9da3af);">Analiza el descanso de anoche para recomendarte nivel de energía y actividad para hoy.</span>
                </span>
              </label>
            </div>

            <div v-if="cfg.morning_report?.last_sent" style="font-size: 12px; color: var(--muted, #9da3af);">
              Último informe matutino enviado: {{ new Date(cfg.morning_report.last_sent).toLocaleString() }}
            </div>
          </div>
        </div>

        <!-- Botón de prueba instantánea -->
        <div class="s-row is-block">
          <div class="custom-row-head">
            <div class="s-row-main">
              <span class="s-row-title">Probar Envío de Informe Matutino en Telegram</span>
              <span class="s-row-sub">Genera y envía un informe de prueba de inmediato con los datos de tu último descanso nocturno.</span>
            </div>
            <div class="s-row-control">
              <button class="pill-button" type="button" :disabled="testingMorning || !hasTelegram" @click="testMorningReport" style="background: rgba(255, 180, 0, 0.15); color: #ffca28; border: 1px solid rgba(255, 202, 40, 0.4);">
                <Icon name="sun" :size="14" /> {{ testingMorning ? 'Generando y enviando...' : '☀️ Enviar Informe de Prueba' }}
              </button>
            </div>
          </div>

          <div v-if="testMorningResult" :class="['alert', testMorningResult.ok ? 'success' : 'danger']" style="margin-top: 10px;">
            <Icon :name="testMorningResult.ok ? 'circle-check' : 'warning'" :size="15" />
            <span>{{ testMorningResult.message }}</span>
          </div>
        </div>
      </div>
    </section>

    <!-- SECCIÓN 2: CREDENCIALES TELEGRAM Y GEMINI -->
    <section class="s-section">
      <div class="s-section-head">
        <h3>🔑 Credenciales de Conexión (Telegram y Gemini)</h3>
        <p class="s-row-sub" style="margin: 2px 0 0 0;">Configura aquí las claves necesarias para enviar los informes a tu móvil.</p>
      </div>
      <div class="s-list">
        <div class="s-row is-block">
          <div style="display: grid; gap: 12px; width: 100%;">
            <div>
              <label class="custom-label">Telegram Bot Token:</label>
              <input type="text" v-model="cfg.telegram_bot_token" placeholder="Ej: 123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ (creado en @BotFather)" class="custom-input-text">
            </div>
            <div>
              <label class="custom-label">Telegram Chat ID:</label>
              <input type="text" v-model="cfg.telegram_chat_id" placeholder="Ej: 987654321 (obtenlo hablándole a @userinfobot)" class="custom-input-text">
            </div>
            <div>
              <label class="custom-label">Google Gemini API Key:</label>
              <input type="password" v-model="cfg.gemini_api_key" placeholder="Pega tu clave gratuita de Google AI Studio (aistudio.google.com)" class="custom-input-text">
            </div>

            <div style="display: flex; gap: 8px; flex-wrap: wrap; margin-top: 4px;">
              <button class="pill-button" type="button" :disabled="cfgSaving" @click="saveConfig">
                <Icon name="check" :size="14" /> {{ cfgSaving ? 'Guardando...' : '💾 Guardar Credenciales' }}
              </button>
              <button class="pill-button" type="button" :disabled="testingTelegram || !cfg.telegram_bot_token || !cfg.telegram_chat_id" @click="testTelegram">
                <Icon name="send" :size="14" /> {{ testingTelegram ? 'Probando...' : '🧪 Probar Conexión con Telegram' }}
              </button>
            </div>

            <div v-if="cfgSavedMsg" class="alert success">{{ cfgSavedMsg }}</div>
            <div v-if="testResult" :class="['alert', testResult.ok ? 'success' : 'danger']">
              <Icon :name="testResult.ok ? 'circle-check' : 'warning'" :size="15" />
              <span>{{ testResult.message }}</span>
            </div>
          </div>
        </div>
      </div>
    </section>

    <!-- SECCIÓN 3: SINCRONIZACIÓN AUTOMÁTICA EN SEGUNDO PLANO -->
    <section class="s-section">
      <div class="s-section-head">
        <h3>🔄 Sincronización Automática en Segundo Plano</h3>
      </div>
      <div class="s-list">
        <div class="s-row is-block">
          <div class="custom-row-head">
            <div class="s-row-main">
              <span class="s-row-title">Activar Sincronización Automática</span>
              <span class="s-row-sub">Sincroniza de forma transparente con los servidores de Zepp para que tus datos estén siempre al día.</span>
            </div>
            <div class="s-row-control">
              <label style="display: flex; align-items: center; gap: 8px; cursor: pointer;">
                <input type="checkbox" v-model="cfg.auto_sync_enabled" @change="saveConfig" style="width: 18px; height: 18px; cursor: pointer;">
                <span style="font-size: 13px; font-weight: 650;">{{ cfg.auto_sync_enabled ? 'Activada' : 'Pausada' }}</span>
              </label>
            </div>
          </div>
        </div>

        <div class="s-row is-block" v-if="cfg.auto_sync_enabled">
          <div class="custom-row-head">
            <div class="s-row-main">
              <span class="s-row-title">Frecuencia de Sincronización</span>
              <span class="s-row-sub">Intervalo entre comprobaciones (por defecto: cada 5 minutos).</span>
            </div>
            <div class="s-row-control">
              <select v-model="cfg.auto_sync_interval_minutes" @change="saveConfig" class="custom-input-select">
                <option :value="5">⏱️ Cada 5 minutos (Recomendado)</option>
                <option :value="15">⏱️ Cada 15 minutos</option>
                <option :value="30">⏱️ Cada 30 minutos</option>
                <option :value="60">⏱️ Cada 1 hora</option>
                <option :value="120">⏱️ Cada 2 horas</option>
                <option :value="240">⏱️ Cada 4 horas</option>
              </select>
            </div>
          </div>
          <div v-if="cfg.last_auto_sync" style="font-size: 12px; color: var(--muted, #9da3af); margin-top: 6px;">
            Última sincronización automática: {{ new Date(cfg.last_auto_sync).toLocaleString() }}
          </div>
        </div>

        <div class="s-row is-block">
          <div class="custom-row-head">
            <div class="s-row-main">
              <span class="s-row-title">Sincronización Profunda de Historial (180 días)</span>
              <span class="s-row-sub">Descarga todo tu historial completo de entrenamientos, sueño y pulsaciones de 6 meses.</span>
            </div>
            <div class="s-row-control">
              <button class="pill-button" type="button" :disabled="syncing" @click="runHistorySync">
                <Icon name="cloud" :size="14" />
                {{ syncing ? 'Sincronizando 180 días...' : '🔄 Sincronizar 180 días' }}
              </button>
            </div>
          </div>
          <div v-if="syncResult" :class="['alert', syncResult.ok ? 'success' : 'danger']" style="margin-top: 10px;">
            <Icon :name="syncResult.ok ? 'circle-check' : 'warning'" :size="15" />
            <span>{{ syncResult.message }}</span>
          </div>
        </div>
      </div>
    </section>

    <!-- SECCIÓN: FORMATO DE HORA Y FECHA -->
    <section class="s-section">
      <div class="s-section-head">
        <h3>🕒 Formato de Hora y Fecha en la Aplicación</h3>
        <p class="s-row-sub" style="margin: 2px 0 0 0;">Configura cómo se presentan las horas y fechas en todas las gráficas, tendencias, métricas y tablas.</p>
      </div>
      <div class="s-list">
        <div class="s-row">
          <div class="s-row-main">
            <span class="s-row-title">Formato de Horario</span>
            <span class="s-row-sub">Configurado por defecto en <strong>24 horas (00:00 - 23:59)</strong> para evitar el formato con AM / PM.</span>
          </div>
          <div class="s-row-control">
            <select :value="timeFormat" @change="onTimeFormatChange(($event.target as HTMLSelectElement).value as TimeFormat)" class="custom-input-select">
              <option value="24h">🕒 24 horas (24h - Por defecto)</option>
              <option value="12h">⏰ 12 horas con AM / PM (12h)</option>
              <option value="regional">⚙️ Seguir región del sistema</option>
            </select>
          </div>
        </div>

        <div class="s-row">
          <div class="s-row-main">
            <span class="s-row-title">Orden y Formato de Fecha</span>
            <span class="s-row-sub">Configurado por defecto en formato europeo / español <strong>Día/Mes (DD/MM)</strong> para evitar confusiones con el formato americano.</span>
          </div>
          <div class="s-row-control">
            <select :value="dateOrder" @change="onDateOrderChange(($event.target as HTMLSelectElement).value as DateOrder)" class="custom-input-select">
              <option value="dmy">🇪🇸 Día / Mes (DD/MM, Europeo / Español - Por defecto)</option>
              <option value="mdy">🇺🇸 Mes / Día (MM/DD, Americano)</option>
              <option value="ymd">🌐 Año / Mes / Día (YYYY/MM/DD)</option>
              <option value="regional">⚙️ Seguir región del sistema</option>
            </select>
          </div>
        </div>
      </div>
    </section>

    <!-- SECCIÓN 4: EXPORTACIÓN DE DATOS (CSV) -->
    <section class="s-section">
      <div class="s-section-head">
        <h3>📤 Exportar Datos de Salud y Entrenamientos</h3>
      </div>
      <div class="s-list">
        <div class="s-row">
          <div class="s-row-main">
            <span class="s-row-title">Entrenamientos en CSV</span>
            <span class="s-row-sub">Descarga tus sesiones deportivas registradas con tipo de deporte, duración, distancia, calorías y pulsaciones.</span>
          </div>
          <div class="s-row-control">
            <a href="/api/bridge/export_workouts_csv" download="entrenamientos_zepp.csv" class="pill-button" style="text-decoration: none; display: inline-flex; align-items: center; gap: 6px;">
              <Icon name="export" :size="14" /> Descargar CSV
            </a>
          </div>
        </div>
        <div class="s-row">
          <div class="s-row-main">
            <span class="s-row-title">Métricas Diarias y Pulsaciones en CSV</span>
            <span class="s-row-sub">Descarga las mediciones día a día de FC en reposo, sueño, pasos y estrés para abrirlos en Excel o Google Sheets.</span>
          </div>
          <div class="s-row-control">
            <a href="/api/bridge/export_metrics_csv" download="metricas_diarias_zepp.csv" class="pill-button" style="text-decoration: none; display: inline-flex; align-items: center; gap: 6px;">
              <Icon name="export" :size="14" /> Descargar CSV
            </a>
          </div>
        </div>
      </div>
    </section>

    <!-- SECCIÓN: COPIA DE SEGURIDAD Y RESTAURACIÓN DE BASE DE DATOS -->
    <section class="s-section">
      <div class="s-section-head">
        <h3>💾 Copia de Seguridad y Restauración de Base de Datos</h3>
        <p class="s-row-sub" style="margin: 2px 0 0 0;">Respalda o restaura todo tu historial médico, entrenamientos y métricas en formato SQLite íntegro.</p>
      </div>
      <div class="s-list">
        <div class="s-row">
          <div class="s-row-main">
            <span class="s-row-title">Descargar Copia de Seguridad Completa (.db)</span>
            <span class="s-row-sub">Genera una instantánea segura de tu base de datos SQLite con todos tus datos intactos.</span>
          </div>
          <div class="s-row-control">
            <a href="/api/bridge/backup_db" download class="pill-button" style="text-decoration: none; display: inline-flex; align-items: center; gap: 6px;">
              <Icon name="download" :size="14" /> 💾 Descargar Backup (.db)
            </a>
          </div>
        </div>

        <div class="s-row is-block">
          <div class="custom-row-head">
            <div class="s-row-main">
              <span class="s-row-title">Restaurar Copia de Seguridad</span>
              <span class="s-row-sub">Sube un archivo <code>.db</code> previamente respaldado para restaurar tu historial deportivo y de salud.</span>
            </div>
            <div class="s-row-control">
              <input type="file" ref="restoreFileInput" accept=".db,application/octet-stream" @change="handleRestoreFile" style="display: none;">
              <button class="pill-button" type="button" :disabled="restoring" @click="restoreFileInput?.click()">
                <Icon name="upload" :size="14" /> {{ restoring ? 'Restaurando...' : '📥 Restaurar Base de Datos (.db)' }}
              </button>
            </div>
          </div>
          <div v-if="restoreResult" :class="['alert', restoreResult.ok ? 'success' : 'danger']" style="margin-top: 10px;">
            <Icon :name="restoreResult.ok ? 'circle-check' : 'warning'" :size="15" />
            <span>{{ restoreResult.message }}</span>
          </div>
        </div>
      </div>
    </section>

    <!-- SECCIÓN 5: REGISTRO DEL SINCRONIZADOR (sync.log) -->
    <section class="s-section">
      <div class="s-section-head">
        <h3>📋 Diagnóstico y Registro del Sistema</h3>
      </div>
      <div class="s-list">
        <div class="s-row is-block">
          <div class="custom-row-head">
            <div class="s-row-main">
              <span class="s-row-title">Registro del Sincronizador Oficial (sync.log)</span>
              <span class="s-row-sub">Inspecciona los eventos detallados de comunicación del CLI oficial de Rust y respuestas de Huami.</span>
            </div>
            <div class="s-row-control">
              <button class="pill-button" type="button" @click="toggleLog">
                <Icon name="file" :size="14" /> {{ logOpen ? 'Ocultar Registro' : 'Ver Registro' }}
              </button>
            </div>
          </div>

          <div v-if="logOpen" class="log-panel" style="margin-top: 12px;">
            <div class="log-panel-actions">
              <span class="log-status">{{ logLoading ? 'Cargando...' : 'Salida de sync.log (últimas 150 líneas):' }}</span>
              <div style="display: flex; gap: 8px;">
                <button class="pill-button" type="button" :disabled="logLoading" @click="toggleLog">
                  <Icon name="clock" :size="12" /> Actualizar
                </button>
                <button class="pill-button" type="button" :disabled="!logText" @click="copyLog">
                  <Icon name="copy" :size="12" /> {{ copied ? '✅ ¡Copiado!' : 'Copiar' }}
                </button>
              </div>
            </div>
            <pre class="log-terminal">{{ logText || 'No hay eventos registrados.' }}</pre>
          </div>
        </div>
      </div>
    </section>

    <!-- SECCIÓN: COMPARATIVA Y DIFERENCIAS RESPECTO AL ORIGINAL -->
    <section class="s-section">
      <div class="s-section-head" style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px;">
        <div>
          <h3>✨ Diferencias y Mejoras frente a la Versión Original</h3>
          <p class="s-row-sub" style="margin: 2px 0 0 0;">Comparativa de arquitectura, funciones añadidas y mejoras de este fork respecto al ZeppBridge oficial.</p>
        </div>
        <button class="pill-button" type="button" @click="showDiffs = !showDiffs">
          <Icon name="spark" :size="13" /> {{ showDiffs ? 'Ocultar Comparativa' : 'Ver Comparativa' }}
        </button>
      </div>

      <div v-if="showDiffs" class="s-list" style="margin-top: 12px; overflow-x: auto;">
        <table class="diff-table">
          <thead>
            <tr>
              <th style="min-width: 140px;">Módulo / Característica</th>
              <th style="min-width: 220px;">ZeppBridge Original (Tauri Desktop)</th>
              <th style="min-width: 260px;">ZeppBridge Web (Este Fork Docker)</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td><strong>🌐 Plataforma y Acceso</strong></td>
              <td>Aplicación nativa de escritorio (Rust / Tauri). Requiere instalación en local en cada PC/Mac cliente.</td>
              <td><span class="diff-tag plus">Web SPA</span> 100% en navegador web para cualquier dispositivo (PC, móvil, tablet) corriendo en Docker.</td>
            </tr>
            <tr>
              <td><strong>🔄 Sincronización Automática</strong></td>
              <td>Solo sincroniza cuando la aplicación de escritorio está abierta en primer/segundo plano en el PC.</td>
              <td><span class="diff-tag plus">Servicio 24/7</span> Sincronización continua en segundo plano en el servidor con intervalo configurable (5m por defecto).</td>
            </tr>
            <tr>
              <td><strong>🤖 Bot de Telegram Interactivo</strong></td>
              <td>No disponible.</td>
              <td><span class="diff-tag plus">Interactivo</span> Control bidireccional desde Telegram en tu móvil: <code>/hoy</code>, <code>/buenosdias</code>, <code>/sync</code>, <code>/pulso</code>, <code>/informe</code>.</td>
            </tr>
            <tr>
              <td><strong>📋 Informes Automáticos (Cron + IA)</strong></td>
              <td>Solo consultas puntuales manuales en pantalla.</td>
              <td><span class="diff-tag plus">Motor Cron</span> Informes programables periódicos (diarios, semanales) y reporte matutino al despertar con Google Gemini.</td>
            </tr>
            <tr>
              <td><strong>🕒 Horario y Formato de Fechas</strong></td>
              <td>Horario de 12 horas (AM/PM) y formato de fecha americano (MM/DD) por defecto.</td>
              <td><span class="diff-tag plus">Adaptación Europea</span> <strong>24 horas (00:00 - 23:59)</strong> y formato <strong>Día/Mes (DD/MM)</strong> fijados por defecto en toda la app y gráficas.</td>
            </tr>
            <tr>
              <td><strong>💓 Métricas Cardíacas con IA</strong></td>
              <td>Plantillas genéricas para sueño, semana y workout.</td>
              <td><span class="diff-tag plus">Intents Especiales</span> Análisis cardiovascular dedicado de 7d y 30d (HRV, pulso en reposo y zonas de esfuerzo).</td>
            </tr>
            <tr>
              <td><strong>💾 Respaldos y Exportación</strong></td>
              <td>Archivos SQLite en directorios internos del sistema operativo.</td>
              <td><span class="diff-tag plus">Descarga en 1 Clic</span> Backups directos de la base de datos (<code>.db</code>), restauración y exportador a CSV para Excel.</td>
            </tr>
            <tr>
              <td><strong>🏃 Entrenamientos y Actividades GPS</strong></td>
              <td>Integrado en la última versión v3.0.3.</td>
              <td><span class="diff-tag same">100% Sincronizado</span> Mantiene el planificador de entrenamientos semanales, intervalos y carga TLB en la web.</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <!-- MODAL PARA CREAR O EDITAR INFORME -->
    <div v-if="showModal" class="modal-overlay">
      <div class="modal-box">
        <div class="modal-head">
          <strong>{{ editReport.id ? '✏️ Editar Informe' : '➕ Crear Nuevo Informe Personalizado' }}</strong>
          <button type="button" class="modal-close" @click="showModal = false">✕</button>
        </div>

        <div class="modal-body">
          <div>
            <label class="custom-label">Nombre del Informe:</label>
            <input type="text" v-model="editReport.name" placeholder="Ej: 🌙 Resumen Diario de Salud (Hoy)" class="custom-input-text">
          </div>

          <div style="display: flex; gap: 12px; flex-wrap: wrap;">
            <div style="flex: 1; min-width: 140px;">
              <label class="custom-label">Frecuencia / Programación:</label>
              <select v-model="editReport.schedule_type" class="custom-input-select">
                <option value="daily">📅 Diario (cada día)</option>
                <option value="weekly">🗓️ Semanal (días específicos)</option>
                <option value="monthly">📆 Mensual (un día del mes)</option>
              </select>
            </div>

            <div style="flex: 1; min-width: 140px;">
              <label class="custom-label">Hora de Envío:</label>
              <div style="display: flex; gap: 6px; align-items: center;">
                <select v-model="editReport.hour" class="custom-input-select" style="flex: 1;">
                  <option v-for="h in 24" :key="h-1" :value="h-1">{{ String(h-1).padStart(2,'0') }}h</option>
                </select>
                <span>:</span>
                <select v-model="editReport.minute" class="custom-input-select" style="flex: 1;">
                  <option :value="0">00</option>
                  <option :value="15">15</option>
                  <option :value="30">30</option>
                  <option :value="45">45</option>
                </select>
              </div>
            </div>
          </div>

          <!-- Días de la semana si es semanal -->
          <div v-if="editReport.schedule_type === 'weekly'">
            <label class="custom-label">Días de la semana:</label>
            <div style="display: flex; gap: 6px; flex-wrap: wrap;">
              <button
                v-for="day in [{k:'mon',l:'Lun'},{k:'tue',l:'Mar'},{k:'wed',l:'Mié'},{k:'thu',l:'Jue'},{k:'fri',l:'Vie'},{k:'sat',l:'Sáb'},{k:'sun',l:'Dom'}]"
                :key="day.k"
                type="button"
                :class="['day-btn', (editReport.days_of_week || []).includes(day.k) ? 'selected' : '']"
                @click="toggleDay(day.k)"
              >
                {{ day.l }}
              </button>
            </div>
          </div>

          <!-- Día del mes si es mensual -->
          <div v-if="editReport.schedule_type === 'monthly'">
            <label class="custom-label">Día del mes (1 - 31):</label>
            <input type="number" min="1" max="31" v-model.number="editReport.day_of_month" class="custom-input-text">
          </div>

          <!-- Alcance de datos -->
          <div style="display: flex; gap: 12px; flex-wrap: wrap;">
            <div style="flex: 1; min-width: 140px;">
              <label class="custom-label">Datos a recopilar (Ventana):</label>
              <select v-model.number="editReport.scope_days" class="custom-input-select">
                <option :value="1">📅 Datos de Hoy (1 día)</option>
                <option :value="7">📅 Últimos 7 días (Semana)</option>
                <option :value="14">📅 Últimos 14 días (2 Semanas)</option>
                <option :value="30">📅 Últimos 30 días (Mes)</option>
              </select>
            </div>

            <div style="flex: 1; min-width: 140px;">
              <label class="custom-label">Enfoque para Gemini:</label>
              <select v-model="editReport.focus" class="custom-input-select">
                <option value="full">📊 Salud Completa (Sueño, Pasos, Cardio y Deporte)</option>
                <option value="heart">💓 Salud Cardíaca y Pulsaciones</option>
                <option value="sleep">🌙 Sueño y Recuperación Nocturna</option>
                <option value="workout">🏃 Rendimiento y Entrenamientos</option>
                <option value="custom">✍️ Prompt Personalizado...</option>
              </select>
            </div>
          </div>

          <!-- Prompt personalizado -->
          <div v-if="editReport.focus === 'custom'">
            <label class="custom-label">Instrucción Especial / Pregunta a Gemini:</label>
            <textarea v-model="editReport.custom_prompt" rows="3" placeholder="Escribe aquí tu consulta o instrucción específica para Gemini..." class="custom-input-text" style="resize: vertical;"></textarea>
          </div>

          <div>
            <label style="display: flex; align-items: center; gap: 8px; cursor: pointer;">
              <input type="checkbox" v-model="editReport.enabled" style="width: 16px; height: 16px; cursor: pointer;">
              <span style="font-size: 13px;">Informe activado (se ejecutará según el horario programado)</span>
            </label>
          </div>
        </div>

        <div class="modal-foot">
          <button class="pill-button" type="button" @click="showModal = false">Cancelar</button>
          <button class="pill-button" type="button" @click="saveReportForm" style="background: var(--accent, #6dd58c); color: #000; font-weight: 600;">
            💾 Guardar Informe
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped src="../settings-local.css"></style>
<style scoped>
.custom-section { display: grid; gap: 22px; min-width: 0; }
.custom-row-head { display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
.custom-label { display: block; font-size: 12px; font-weight: 600; color: var(--ink, #e2e2e6); margin-bottom: 4px; }
.custom-input-text, .custom-input-select { width: 100%; box-sizing: border-box; background: #141517; color: #e2e2e6; border: 1px solid var(--mat-line, #333538); border-radius: 6px; padding: 8px 10px; font-size: 12px; outline: none; }
.custom-input-text:focus, .custom-input-select:focus { border-color: var(--accent, #6dd58c); }

.rep-card { background: #1a1c1f; border: 1px solid var(--mat-line, #333538); border-radius: 10px; padding: 14px 16px; transition: border-color var(--dur-base, 0.2s) ease; }
.rep-card:hover { border-color: var(--accent, #6dd58c); }
.rep-badge { font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: 12px; }
.rep-badge.active { background: rgba(109, 213, 140, 0.2); color: #6dd58c; }
.rep-badge.paused { background: rgba(255, 180, 171, 0.2); color: #ffb4ab; }
.rep-pill { font-size: 11px; background: #26282c; color: #c4c6d0; padding: 3px 8px; border-radius: 6px; border: 1px solid #36383e; }

.day-btn { background: #26282c; color: #c4c6d0; border: 1px solid #36383e; border-radius: 6px; padding: 6px 12px; font-size: 12px; cursor: pointer; }
.day-btn.selected { background: var(--accent, #6dd58c); color: #000; font-weight: 600; border-color: var(--accent, #6dd58c); }

.log-panel { background: var(--mat-card, #1e1f22); border: 1px solid var(--mat-line, #333538); border-radius: 8px; padding: 12px; }
.log-panel-actions { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
.log-status { font-size: 12px; color: var(--muted, #9da3af); }
.log-terminal { background: #0e0f11; color: #6dd58c; padding: 12px; border-radius: 6px; font-family: var(--font-mono, monospace); font-size: 11px; max-height: 280px; overflow-y: auto; white-space: pre-wrap; word-break: break-all; margin: 0; line-height: 1.4; }

.modal-overlay { position: fixed; top: 0; left: 0; width: 100vw; height: 100vh; background: rgba(0,0,0,0.75); display: flex; align-items: center; justify-content: center; z-index: 99999; padding: 16px; box-sizing: border-box; }
.modal-box { background: #1e1f22; border: 1px solid #44474e; border-radius: 12px; width: 100%; max-width: 580px; max-height: 90vh; display: flex; flex-direction: column; overflow: hidden; box-shadow: 0 16px 40px rgba(0,0,0,0.8); }
.modal-head { display: flex; justify-content: space-between; align-items: center; padding: 14px 18px; border-bottom: 1px solid #333538; font-size: 15px; color: #e2e2e6; }
.modal-close { background: transparent; border: 0; color: #9da3af; font-size: 18px; cursor: pointer; }
.modal-body { padding: 16px 18px; overflow-y: auto; display: grid; gap: 14px; }
.modal-foot { padding: 12px 18px; border-top: 1px solid #333538; display: flex; justify-content: flex-end; gap: 8px; }

.alert { display: flex; align-items: center; gap: 8px; padding: 8px 12px; border-radius: 6px; font-size: 13px; margin-top: 8px; }
.alert.success { background: rgba(109, 213, 140, 0.12); color: #6dd58c; border: 1px solid rgba(109, 213, 140, 0.3); }
.diff-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.diff-table th, .diff-table td { padding: 10px 12px; text-align: left; border-bottom: 1px solid var(--mat-line, #333538); vertical-align: middle; }
.diff-table th { background: #141517; color: var(--ink, #e2e2e6); font-weight: 600; font-size: 12px; }
.diff-table tr:hover { background: rgba(255, 255, 255, 0.02); }
.diff-tag { display: inline-block; font-size: 10px; font-weight: 700; padding: 2px 6px; border-radius: 4px; margin-right: 6px; }
.diff-tag.plus { background: rgba(109, 213, 140, 0.2); color: #6dd58c; border: 1px solid rgba(109, 213, 140, 0.4); }
.diff-tag.same { background: rgba(147, 197, 253, 0.2); color: #93c5fd; border: 1px solid rgba(147, 197, 253, 0.4); }
</style>
`;
  fs.writeFileSync(customSectionVue, customSectionContent, 'utf8');
  console.log('[Patch] Creado componente CustomSection.vue en views/settings/sections/');
}

// 4. Registrar la tarjeta 'custom' en cards.ts
const cardsTsPath = 'src/views/settings/cards.ts';
if (fs.existsSync(cardsTsPath)) {
  let cardsCode = fs.readFileSync(cardsTsPath, 'utf8');
  cardsCode = cardsCode.replace(
    "export const SETTINGS_CARD_IDS = ['account', 'sync',",
    "export const SETTINGS_CARD_IDS = ['account', 'sync', 'custom',"
  );
  cardsCode = cardsCode.replace(
    "sync: 'auto-sync',",
    "sync: 'auto-sync',\n  custom: 'maintenance',"
  );
  cardsCode = cardsCode.replace(
    "sync: 'training',",
    "sync: 'training',\n  custom: 'accent',"
  );
  fs.writeFileSync(cardsTsPath, cardsCode, 'utf8');
  console.log('[Patch] Tarjeta custom registrada en src/views/settings/cards.ts');
}

// 5. Registrar título y resumen de 'custom' en Settings.vue
const settingsVuePath = 'src/views/Settings.vue';
if (fs.existsSync(settingsVuePath)) {
  let settingsCode = fs.readFileSync(settingsVuePath, 'utf8');
  settingsCode = settingsCode.replace(
    "sync: syncCard.value.cardTitle,",
    "sync: syncCard.value.cardTitle,\n  custom: 'Custom e Informes',"
  );
  settingsCode = settingsCode.replace(
    "sync: autoSyncEnabled.value ? d.value.sumSyncOn(autoSyncInterval.value) : d.value.sumSyncOff,",
    "sync: autoSyncEnabled.value ? d.value.sumSyncOn(autoSyncInterval.value) : d.value.sumSyncOff,\n    custom: 'Informes programados (cron), Telegram + Gemini, auto-sync (5m) y CSV',"
  );
  fs.writeFileSync(settingsVuePath, settingsCode, 'utf8');
  console.log('[Patch] Título y descripción de tarjeta custom integrados en src/views/Settings.vue');
}

// 6. Conectar CustomSection en CardBody.vue
const cardBodyPath = 'src/views/settings/CardBody.vue';
if (fs.existsSync(cardBodyPath)) {
  let cardBodyCode = fs.readFileSync(cardBodyPath, 'utf8');
  cardBodyCode = cardBodyCode.replace(
    "import AutoSyncSection from './sections/AutoSyncSection.vue';",
    "import AutoSyncSection from './sections/AutoSyncSection.vue';\nimport CustomSection from './sections/CustomSection.vue';"
  );
  cardBodyCode = cardBodyCode.replace(
    "<AutoSyncSection v-else-if=\"id === 'sync'\" />",
    "<AutoSyncSection v-else-if=\"id === 'sync'\" />\n    <CustomSection v-else-if=\"id === 'custom'\" />"
  );
  fs.writeFileSync(cardBodyPath, cardBodyCode, 'utf8');
  console.log('[Patch] Componente CustomSection enlazado en src/views/settings/CardBody.vue');
}

// 7. Configurar orden de fecha Día/Mes (DD/MM) europeo/español por defecto en dateTime.ts
const dateTimeTsPath = 'src/lib/dateTime.ts';
if (fs.existsSync(dateTimeTsPath)) {
  let dtCode = fs.readFileSync(dateTimeTsPath, 'utf8');

  // Fallback por defecto a '24h' y 'dmy' en lugar de 'regional'
  dtCode = dtCode.replace(
    "if (choices.includes(value)) return value;",
    `if (key === 'zeppbridge-time-format' && (!value || value === 'regional')) return ('24h' as T);
    if (key === 'zeppbridge-date-order' && (!value || value === 'regional')) return ('dmy' as T);
    if (choices.includes(value)) return value;`
  );

  dtCode = dtCode.replace(
    "return choices[0]!;",
    "if (key === 'zeppbridge-time-format') return ('24h' as T);\n  if (key === 'zeppbridge-date-order') return ('dmy' as T);\n  return choices[0]!;"
  );

  // Perfeccionar displayDateTimeFormatter para que 'dmy' formatee siempre día/mes respetando el idioma español
  dtCode = dtCode.replace(
    /region = \{ ymd: 'sv-SE', dmy: 'en-GB', mdy: 'en-US' \}\[(?:order\.value|effectiveOrder)\];/,
    `const activeOrder = typeof effectiveOrder !== 'undefined' ? effectiveOrder : order.value;
    if (activeOrder === 'dmy') {
      region = (adjusted.month === 'short' || adjusted.month === 'long')
        ? (intlLocale().startsWith('es') ? 'es-ES' : 'en-GB')
        : 'en-GB';
    } else if (activeOrder === 'ymd') {
      region = 'sv-SE';
    } else if (activeOrder === 'mdy') {
      region = 'en-US';
    }`
  );

  // Evitar sobreescribir 'short' o 'long' con '2-digit'
  dtCode = dtCode.replace(
    /if \(!adjusted\.dateStyle\) \{\s+adjusted\.month = '2-digit';\s+adjusted\.day = '2-digit';\s+\}/,
    `if (!adjusted.dateStyle && adjusted.month !== 'short' && adjusted.month !== 'long') {
      adjusted.month = '2-digit';
      adjusted.day = '2-digit';
    }`
  );

  fs.writeFileSync(dateTimeTsPath, dtCode, 'utf8');
  console.log('[Patch] Formato europeo DD/MM configurado por defecto en src/lib/dateTime.ts');
}

// 8. Corregir formato de fecha en HeartRateDetail.vue (Frecuencia cardíaca máxima diaria)
const hrDetailPath = 'src/views/HeartRateDetail.vue';
if (fs.existsSync(hrDetailPath)) {
  let hrCode = fs.readFileSync(hrDetailPath, 'utf8');

  // Asegurar import de parseDisplayDate
  if (!hrCode.includes('parseDisplayDate')) {
    hrCode = hrCode.replace(
      "import { displayDateTimeFormatter } from '../lib/dateTime';",
      "import { displayDateTimeFormatter, parseDisplayDate } from '../lib/dateTime';"
    );
  }

  // Quitar el .slice(5) hardcodeado que forzaba formato americano MM-DD
  hrCode = hrCode.replace(
    "data: rows.map((row) => row.date.slice(5)),",
    "data: rows.map((row) => row.date),"
  );

  // Formatear el eje X con el orden de fecha configurado (ej: DD/MM)
  hrCode = hrCode.replace(
    "axisLabel: { color: chartPalette.value.axis, fontSize: 14.5, hideOverlap: true },",
    "axisLabel: { color: chartPalette.value.axis, fontSize: 14.5, hideOverlap: true, formatter: (val: string) => displayDateTimeFormatter({ month: 'numeric', day: 'numeric' }).format(parseDisplayDate(val)) },"
  );

  // Formatear la fecha del tooltip de la gráfica de FC máxima diaria
  hrCode = hrCode.replace(
    "return t.value.dailyMaxTooltip(row.date, row.max, row.average, row.samples);",
    "const dateFormatted = displayDateTimeFormatter({ month: 'short', day: 'numeric', weekday: 'short' }).format(parseDisplayDate(row.date));\n        return t.value.dailyMaxTooltip(dateFormatted, row.max, row.average, row.samples);"
  );

  fs.writeFileSync(hrDetailPath, hrCode, 'utf8');
  console.log('[Patch] Formato de fecha corregido en HeartRateDetail.vue (FC máxima diaria)');
}

// 9. Corregir formato de fecha en TrainingStatus.vue (Umbral de lactato y equilibrio de carga)
const trainingStatusPath = 'src/views/TrainingStatus.vue';
if (fs.existsSync(trainingStatusPath)) {
  let tsCode = fs.readFileSync(trainingStatusPath, 'utf8');

  // Importar displayDateTimeFormatter y parseDisplayDate
  if (!tsCode.includes('parseDisplayDate')) {
    tsCode = tsCode.replace(
      "<script setup lang=\"ts\">",
      "<script setup lang=\"ts\">\nimport { displayDateTimeFormatter, parseDisplayDate } from '../lib/dateTime';"
    );
  }

  // Formatear eje X del gráfico de umbral de lactato
  tsCode = tsCode.replace(
    "xAxis: { type: 'category', data: dates, boundaryGap: false, axisLabel: { fontSize: 12, hideOverlap: true } },",
    "xAxis: { type: 'category', data: dates, boundaryGap: false, axisLabel: { fontSize: 12, hideOverlap: true, formatter: (val: string) => displayDateTimeFormatter({ month: 'numeric', day: 'numeric' }).format(parseDisplayDate(val)) } },"
  );

  // Formatear fecha del tooltip del gráfico de umbral
  tsCode = tsCode.replace(
    "return [params[0].axisValue, ...lines].join('<br>');",
    "const axisDate = displayDateTimeFormatter({ month: 'short', day: 'numeric', weekday: 'short' }).format(parseDisplayDate(params[0].axisValue));\n        return [axisDate, ...lines].join('<br>');"
  );

  // Formatear eje X del gráfico de equilibrio de carga
  tsCode = tsCode.replace(
    "xAxis: { type: 'category', data: dates, boundaryGap: false, axisLabel: { fontSize: 14.5, hideOverlap: true } },",
    "xAxis: { type: 'category', data: dates, boundaryGap: false, axisLabel: { fontSize: 14.5, hideOverlap: true, formatter: (val: string) => displayDateTimeFormatter({ month: 'numeric', day: 'numeric' }).format(parseDisplayDate(val)) } },"
  );

  // Formatear fecha del tooltip de equilibrio de carga
  tsCode = tsCode.replace(
    "return [\n          point.date,",
    "const pointDate = displayDateTimeFormatter({ month: 'short', day: 'numeric', weekday: 'short' }).format(parseDisplayDate(point.date));\n        return [\n          pointDate,"
  );

  fs.writeFileSync(trainingStatusPath, tsCode, 'utf8');
  console.log('[Patch] Formato de fecha corregido en TrainingStatus.vue (Umbrales y Carga)');
}

// 10. Agregar etiquetas de navegación en App.i18n.ts
const appI18nPath = 'src/App.i18n.ts';
if (fs.existsSync(appI18nPath)) {
  let appI18nCode = fs.readFileSync(appI18nPath, 'utf8');
  appI18nCode = appI18nCode.replace(
    "navOverview: '概览',\n    navHandoff: '交给 AI',",
    "navOverview: '概览',\n    navWorkouts: '运动记录',\n    navPlan: '训练计划',\n    navHandoff: '交给 AI',"
  );
  appI18nCode = appI18nCode.replace(
    "navOverview: 'Overview',\n    navHandoff: 'Send to AI',",
    "navOverview: 'Overview',\n    navWorkouts: 'Workouts',\n    navPlan: 'Planner',\n    navHandoff: 'Send to AI',"
  );
  appI18nCode = appI18nCode.replace(
    "navOverview: 'Resumen',\n    navHandoff: 'Pasar a la IA',",
    "navOverview: 'Resumen',\n    navWorkouts: 'Entrenamientos',\n    navPlan: 'Planificador',\n    navHandoff: 'Pasar a la IA',"
  );
  fs.writeFileSync(appI18nPath, appI18nCode, 'utf8');
  console.log('[Patch] Traducciones de navegación agregadas en src/App.i18n.ts');
}

// 11. Agregar Entrenamientos y Planificador en la barra de navegación de AppShell.vue
const appShellPath = 'src/AppShell.vue';
if (fs.existsSync(appShellPath)) {
  let shellCode = fs.readFileSync(appShellPath, 'utf8');
  shellCode = shellCode.replace(
    `const navigation = computed(() => [
  { to: '/', label: t.value.navOverview, icon: 'overview' as DesignIconName },
  { to: '/ai', label: t.value.navHandoff, icon: 'handoff' as DesignIconName },
  { to: '/settings', label: t.value.navSettings, icon: 'settings' as DesignIconName },
]);`,
    `const navigation = computed(() => [
  { to: '/', label: t.value.navOverview, icon: 'overview' as DesignIconName },
  { to: '/workouts', label: (t.value as any).navWorkouts || 'Entrenamientos', icon: 'pace' as DesignIconName },
  { to: '/ai/plan', label: (t.value as any).navPlan || 'Planificador', icon: 'outdoor-run' as DesignIconName },
  { to: '/ai', label: t.value.navHandoff, icon: 'handoff' as DesignIconName },
  { to: '/settings', label: t.value.navSettings, icon: 'settings' as DesignIconName },
]);`
  );
  fs.writeFileSync(appShellPath, shellCode, 'utf8');
  console.log('[Patch] Entrenamientos y Planificador integrados en barra de navegación de AppShell.vue');
}

// 12. Actualizar navigationBranch y TAB_ORDER en navigation.ts
const navTsPath = 'src/lib/navigation.ts';
if (fs.existsSync(navTsPath)) {
  let navCode = fs.readFileSync(navTsPath, 'utf8');
  navCode = navCode.replace(
    "export const navigationBranch = (path: string): string => {\n  if (path === '/ai' || path.startsWith('/ai/')) return '/ai';",
    "export const navigationBranch = (path: string): string => {\n  if (path === '/ai/plan' || path.startsWith('/ai/plan/')) return '/ai/plan';\n  if (path === '/workouts' || path.startsWith('/workouts/')) return '/workouts';\n  if (path === '/ai' || path.startsWith('/ai/')) return '/ai';"
  );
  navCode = navCode.replace(
    "export const TAB_ORDER = ['/', '/ai', '/settings'] as const;",
    "export const TAB_ORDER = ['/', '/workouts', '/ai/plan', '/ai', '/settings'] as const;"
  );
  fs.writeFileSync(navTsPath, navCode, 'utf8');
  console.log('[Patch] Rutas de navegación y orden de pestañas actualizados en src/lib/navigation.ts');
}

// 13. Mejorar WorkoutCodesSection.vue para mostrar siempre estado visible
const workoutCodesPath = 'src/views/settings/sections/WorkoutCodesSection.vue';
if (fs.existsSync(workoutCodesPath)) {
  let wcCode = fs.readFileSync(workoutCodesPath, 'utf8');
  wcCode = wcCode.replace(
    '<section v-if="unknownCodes.length" id="codes" class="s-section" aria-labelledby="codes-title">',
    '<section id="codes" class="s-section" aria-labelledby="codes-title">'
  );
  wcCode = wcCode.replace(
    '<div class="s-list">',
    '<div v-if="unknownCodes.length" class="s-list">'
  );
  wcCode = wcCode.replace(
    '<p v-if="codeError" class="api-error" role="alert">{{ codeError }}</p>',
    '<div v-else class="s-note" style="padding: 10px 0; color: var(--accent);">✓ Todos los tipos de deporte registrados están actualmente identificados.</div>\n    <p v-if="codeError" class="api-error" role="alert">{{ codeError }}</p>'
  );
  fs.writeFileSync(workoutCodesPath, wcCode, 'utf8');
  console.log('[Patch] Sección de Códigos de Deporte mejorada en WorkoutCodesSection.vue');
}

console.log('[Patch] Proceso de parcheo finalizado con éxito.');
