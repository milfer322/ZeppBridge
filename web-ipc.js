// ==============================================================================
// ZeppBridge Web-IPC Bridge: Emulador completo del ecosistema Tauri para Web
// ==============================================================================
(function() {
  // Asegurar formato de hora 24h por defecto en toda la app
  try {
    const currentTimeFormat = localStorage.getItem('zeppbridge-time-format');
    if (!currentTimeFormat || currentTimeFormat === 'regional') {
      localStorage.setItem('zeppbridge-time-format', '24h');
    }
  } catch (e) {}

  // Asegurar formato de fecha europeo (día/mes - DD/MM) por defecto en toda la app
  try {
    const currentOrder = localStorage.getItem('zeppbridge-date-order');
    if (!currentOrder || currentOrder === 'regional') {
      localStorage.setItem('zeppbridge-date-order', 'dmy');
    }
  } catch (e) {}

  const eventListeners = new Map();
  const callbackRegistry = new Map();
  let nextCallbackId = 1;

  function emitClientEvent(event, payload) {
    const list = eventListeners.get(event);
    if (list) {
      list.forEach((cb) => {
        try {
          cb({ event, payload });
        } catch (e) {
          console.error('[Web IPC] Error en listener de evento:', event, e);
        }
      });
    }
  }

  window.__ZB_EMIT__ = emitClientEvent;

  window.__ZB_IPC__ = {
    __zbDemo: false,

    transformCallback(cb) {
      const id = ++nextCallbackId;
      callbackRegistry.set(id, cb);
      return id;
    },

    unregisterCallback(id) {
      callbackRegistry.delete(id);
    },

    metadata: {
      currentWindow: { label: 'main' },
      currentWebview: { label: 'main', windowLabel: 'main' }
    },

    async invoke(command, args = {}) {
      // 1. Manejador de eventos de Tauri (plugin:event|listen)
      if (command === 'plugin:event|listen') {
        const { event, handler } = args;
        if (!eventListeners.has(event)) {
          eventListeners.set(event, new Set());
        }
        const cb = callbackRegistry.get(handler);
        if (cb) {
          eventListeners.get(event).add(cb);
        }

        if (event === 'app://ready' && cb) {
          setTimeout(() => {
            try { cb({ event: 'app://ready', payload: true }); } catch (e) {}
          }, 10);
        }
        return handler;
      }

      if (command === 'plugin:event|unlisten') {
        const { event, eventId } = args;
        const cb = callbackRegistry.get(eventId);
        if (cb && eventListeners.has(event)) {
          eventListeners.get(event).delete(cb);
        }
        return null;
      }

      // 2. Manejo de Clipboard nativo del navegador
      if (command === 'plugin:clipboard-manager|read_text') {
        try {
          return (await navigator.clipboard.readText()) || '';
        } catch (e) {
          return '';
        }
      }
      if (command === 'plugin:clipboard-manager|write_text') {
        try {
          await navigator.clipboard.writeText(String(args.text || ''));
          return true;
        } catch (e) {
          return true;
        }
      }

      // 3. Manejo de URLs y Opener
      if (command.includes('opener') || command === 'open_url') {
        const url = args.url || args.path;
        if (url && typeof url === 'string' && url.startsWith('http')) {
          console.log('[Web IPC] Abriendo enlace solicitado:', url);
          window.open(url, '_blank', 'noopener,noreferrer');
          if (/gemini\.google\.com|chatgpt\.com|claude\.ai|kimi\.moonshot|deepseek\.com/i.test(url)) {
            showAiHandoffNotice(url);
          }
          return null;
        }
      }

      // 4. Manejo de diálogos
      if (command === 'plugin:dialog|message') {
        window.alert(args.message || '');
        return true;
      }
      if (command === 'plugin:dialog|open') {
        return '/data/exports';
      }
      if (command === 'plugin:dialog|save') {
        return '/data/exports/export.json';
      }

      // 5. Manejo de ventanas, procesos y updater
      if (command.includes('plugin:window')) {
        return false;
      }
      if (command.includes('plugin:process')) {
        location.reload();
        return null;
      }
      if (command.includes('plugin:updater')) {
        return { shouldUpdate: false };
      }
      if (command === 'system_region_locale') {
        return navigator.language || 'es-ES';
      }

      // 6. Consultas al Backend FastAPI en el contenedor
      try {
        if (command === 'manual_auth') {
          const sel = document.querySelector('select.mat-field');
          if (sel && sel.value && sel.value !== 'custom') {
            args.regionHost = sel.value;
          }
        }

        if (command === 'prepare_ai_handoff') {
          const pText = (args && (args.prompt || (args.task && args.task.prompt))) || '';
          const tId = (args && (args.templateId || (args.task && args.task.id))) || '';
          if (/tpl_heart|task_heart/i.test(tId) || /pulsaci|cardio|coraz[oó]n|heart|rhr|frecuencia/i.test(pText)) {
            args.mode = 'heart';
            if (!args.scope_days && !args.days) {
              args.scope_days = (/mes|30\s*d/i.test(pText) || /30/.test(tId)) ? 30 : 7;
            }
          }
        }

        const res = await fetch('/api/bridge/' + encodeURIComponent(command), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(args)
        });

        if (!res.ok) {
          const err = await res.json().catch(() => ({ message: res.statusText }));
          throw new Error(err.message || 'Error en ' + command);
        }
        const data = await res.json();

        // IMPORTANTE: Únicamente abrir la ventana externa si el usuario pulsó explícitamente
        // el botón de inicio de sesión (start_official_login o start_web_login). NUNCA en consultas de estado.
        if (command === 'start_official_login' || command === 'start_web_login') {
          const targetUrl = data && (data.authorize_url || data.page_url);
          if (targetUrl) {
            console.log('[Web IPC] Abriendo portal oficial de Zepp tras pulsar Iniciar Sesión:', targetUrl);
            window.open(targetUrl, '_blank', 'noopener,noreferrer');
          }
        }

        // Si es una sincronización completada, emitir evento para refrescar la interfaz
        if (command.includes('sync') && data && data.success) {
          emitClientEvent('sync://progress', {
            completed: true,
            stream: 'all',
            current: 100,
            total: 100,
            message: 'Sincronización completada'
          });
        }

        // Si es una preparación de IA, almacenar y copiar el prompt enriquecido al portapapeles
        if (command === 'prepare_ai_handoff' || command === 'ai_task_prepare') {
          const pt = (data && (data.prompt_text || data.clipboardText)) || '';
          if (pt) {
            window.__LAST_AI_PROMPT__ = pt;
            if (navigator.clipboard && navigator.clipboard.writeText) {
              navigator.clipboard.writeText(pt).catch(() => {});
            }
          }
        }

        return data;
      } catch (err) {
        console.warn('[Web IPC] Error invocando:', command, err);
        throw err;
      }
    }
  };

  try {
    Object.assign(window, { __TAURI_INTERNALS__: window.__ZB_IPC__ });
    Object.assign(window, {
      __TAURI_EVENT_PLUGIN_INTERNALS__: {
        unregisterListener: (id) => {
          callbackRegistry.delete(id);
        }
      }
    });
  } catch (e) {}

  // ==============================================================================
  // UI Enhancer: Inyectar un selector desplegable de Región/Servidor Zepp
  // ==============================================================================
  const REGION_PRESETS = [
    { label: '🇪🇺 Europa / Frankfurt (api-mifit-de2.huami.com)', value: 'api-mifit-de2.huami.com' },
    { label: '🇺🇸 Estados Unidos (api-mifit.huami.com)', value: 'api-mifit.huami.com' },
    { label: '🇺🇸 Estados Unidos 2 (api-mifit-us2.huami.com)', value: 'api-mifit-us2.huami.com' },
    { label: '🇸🇬 Singapur / Global (api-mifit-sg2.huami.com)', value: 'api-mifit-sg2.huami.com' },
    { label: '🇷🇺 Rusia (api-mifit-ru.huami.com)', value: 'api-mifit-ru.huami.com' },
    { label: '🇨🇳 China (api-mifit-cn.huami.com)', value: 'api-mifit-cn.huami.com' },
    { label: '✏️ Servidor personalizado...', value: 'custom' }
  ];

  function setupRegionSelect() {
    const input = document.getElementById('manual-host');
    if (!input || input.dataset.hasRegionPicker) return;

    input.dataset.hasRegionPicker = 'true';

    const select = document.createElement('select');
    select.className = 'mat-field';
    select.style.marginBottom = '6px';
    select.style.cursor = 'pointer';
    select.style.background = 'var(--mat-surface-container-high, #25282d)';
    select.style.color = 'var(--mat-on-surface, #e2e2e6)';
    select.style.border = '1px solid var(--mat-outline-variant, #44474e)';
    select.style.borderRadius = '8px';
    select.style.padding = '8px 12px';
    select.style.width = '100%';
    select.style.fontSize = '13px';

    const currentVal = (input.value || '').trim();
    let matched = false;

    REGION_PRESETS.forEach(p => {
      const opt = document.createElement('option');
      opt.value = p.value;
      opt.textContent = p.label;
      if (currentVal && (currentVal === p.value || currentVal.includes(p.value))) {
        opt.selected = true;
        matched = true;
      }
      select.appendChild(opt);
    });

    // Si no tiene valor previo o es el de US por defecto y estamos en Europa, marcar Europa
    if (!currentVal || (!matched && currentVal.includes('api-mifit.huami.com'))) {
      select.value = 'api-mifit-de2.huami.com';
      input.value = 'api-mifit-de2.huami.com';
      input.dispatchEvent(new Event('input', { bubbles: true }));
    } else if (!matched && currentVal) {
      select.value = 'custom';
    }

    if (select.value !== 'custom') {
      input.style.display = 'none';
    }

    select.addEventListener('change', () => {
      if (select.value === 'custom') {
        input.style.display = 'block';
        input.focus();
      } else {
        input.style.display = 'none';
        input.value = select.value;
        input.dispatchEvent(new Event('input', { bubbles: true }));
      }
    });

    input.parentNode.insertBefore(select, input);

    // Botón de autodetección de región
    const probeBtn = document.createElement('button');
    probeBtn.type = 'button';
    probeBtn.className = 'button secondary';
    probeBtn.style.marginTop = '4px';
    probeBtn.style.fontSize = '12px';
    probeBtn.style.padding = '4px 10px';
    probeBtn.textContent = '🔍 Auto-detectar mi región (probar todos los servidores)';
    
    const probeMsg = document.createElement('div');
    probeMsg.style.fontSize = '12px';
    probeMsg.style.marginTop = '4px';
    probeMsg.style.color = 'var(--mat-on-surface-variant, #8e9099)';

    probeBtn.addEventListener('click', async () => {
      const tokenInput = document.getElementById('manual-apptoken');
      const userInput = document.getElementById('manual-userid');
      const token = tokenInput ? tokenInput.value.trim() : '';
      const uid = userInput ? userInput.value.trim() : '';
      if (!token || !uid) {
        probeMsg.textContent = '⚠️ Introduce primero tu App Token y tu User ID arriba.';
        probeMsg.style.color = '#ffb4ab';
        return;
      }
      probeBtn.disabled = true;
      probeBtn.textContent = '⏳ Comprobando servidores de Europa, US, Asia...';
      probeMsg.textContent = '';

      try {
        const res = await fetch('/api/bridge/probe_all_regions', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ userId: uid, appToken: token })
        });
        const data = await res.json();
        probeBtn.disabled = false;
        probeBtn.textContent = '🔍 Auto-detectar mi región (probar todos los servidores)';

        if (data.best_region) {
          select.value = data.best_region;
          input.value = data.best_region;
          input.dispatchEvent(new Event('input', { bubbles: true }));
          const matchedItem = data.results.find(r => r.host === data.best_region);
          const devList = matchedItem && matchedItem.devices && matchedItem.devices.length ? ` (${matchedItem.devices.join(', ')})` : '';
          probeMsg.textContent = `✅ ¡Encontrado! Tus datos y reloj están en: ${data.best_region}${devList}`;
          probeMsg.style.color = '#6dd58c';
        } else {
          const connected = data.results.filter(r => r.status === 'connected');
          if (connected.length > 0) {
            select.value = connected[0].host;
            input.value = connected[0].host;
            input.dispatchEvent(new Event('input', { bubbles: true }));
            probeMsg.textContent = `ℹ️ Cuenta válida en: ${connected.map(c => c.label).join(', ')}. No hay relojes vinculados actualmente en la nube.`;
            probeMsg.style.color = '#e2e2e6';
          } else {
            probeMsg.textContent = '❌ Ningún servidor reconoció este token. Verifica que el token no haya caducado.';
            probeMsg.style.color = '#ffb4ab';
          }
        }
      } catch (e) {
        probeBtn.disabled = false;
        probeBtn.textContent = '🔍 Auto-detectar mi región (probar todos los servidores)';
        probeMsg.textContent = 'Error al escanear: ' + e.message;
        probeMsg.style.color = '#ffb4ab';
      }
    });

    input.parentNode.appendChild(probeBtn);
    input.parentNode.appendChild(probeMsg);
  }

  function showLogModal(text) {
    let existing = document.getElementById('zb-log-modal');
    if (existing) existing.remove();

    const overlay = document.createElement('div');
    overlay.id = 'zb-log-modal';
    overlay.style.position = 'fixed';
    overlay.style.top = '0';
    overlay.style.left = '0';
    overlay.style.width = '100vw';
    overlay.style.height = '100vh';
    overlay.style.backgroundColor = 'rgba(0, 0, 0, 0.75)';
    overlay.style.backdropFilter = 'blur(4px)';
    overlay.style.zIndex = '99999';
    overlay.style.display = 'flex';
    overlay.style.alignItems = 'center';
    overlay.style.justifyContent = 'center';
    overlay.style.padding = '20px';

    const box = document.createElement('div');
    box.style.background = '#1e1f22';
    box.style.border = '1px solid #44474e';
    box.style.borderRadius = '12px';
    box.style.width = '90%';
    box.style.maxWidth = '750px';
    box.style.maxHeight = '80vh';
    box.style.display = 'flex';
    box.style.flexDirection = 'column';
    box.style.overflow = 'hidden';
    box.style.boxShadow = '0 12px 36px rgba(0,0,0,0.6)';

    const head = document.createElement('div');
    head.style.display = 'flex';
    head.style.justifyContent = 'space-between';
    head.style.alignItems = 'center';
    head.style.padding = '12px 16px';
    head.style.borderBottom = '1px solid #333538';
    head.innerHTML = '<span style="font-weight:600; font-size:14px; color:#e2e2e6;">📋 Diagnóstico y Registro de Sincronización</span>';

    const closeBtn = document.createElement('button');
    closeBtn.textContent = '✕';
    closeBtn.style.background = 'transparent';
    closeBtn.style.border = 'none';
    closeBtn.style.color = '#c4c6d0';
    closeBtn.style.fontSize = '18px';
    closeBtn.style.cursor = 'pointer';
    closeBtn.onclick = () => overlay.remove();
    head.appendChild(closeBtn);

    const pre = document.createElement('pre');
    pre.style.flex = '1';
    pre.style.margin = '0';
    pre.style.padding = '14px';
    pre.style.overflowY = 'auto';
    pre.style.fontFamily = 'monospace';
    pre.style.fontSize = '12px';
    pre.style.color = '#c4c6d0';
    pre.style.background = '#141517';
    pre.style.whiteSpace = 'pre-wrap';
    pre.textContent = text || 'No hay datos en el registro aún.';

    const foot = document.createElement('div');
    foot.style.padding = '10px 16px';
    foot.style.borderTop = '1px solid #333538';
    foot.style.display = 'flex';
    foot.style.justifyContent = 'flex-end';
    foot.style.gap = '8px';

    const copyBtn = document.createElement('button');
    copyBtn.className = 'button secondary';
    copyBtn.textContent = '📄 Copiar Log';
    copyBtn.style.fontSize = '12px';
    copyBtn.onclick = async () => {
      try {
        await navigator.clipboard.writeText(text);
        copyBtn.textContent = '✅ Copiado';
        setTimeout(() => copyBtn.textContent = '📄 Copiar Log', 2000);
      } catch (e) {
        copyBtn.textContent = 'Error al copiar';
      }
    };
    foot.appendChild(copyBtn);

    const okBtn = document.createElement('button');
    okBtn.className = 'button primary';
    okBtn.textContent = 'Cerrar';
    okBtn.style.fontSize = '12px';
    okBtn.onclick = () => overlay.remove();
    foot.appendChild(okBtn);

    box.appendChild(head);
    box.appendChild(pre);
    box.appendChild(foot);
    overlay.appendChild(box);
    document.body.appendChild(overlay);
  }

  function showAiHandoffNotice(url) {
    if (document.getElementById('zb-ai-notice-modal')) return;
    
    let targetProvider = 'Gemini';
    if (/chatgpt/i.test(url)) targetProvider = 'ChatGPT';
    else if (/claude/i.test(url)) targetProvider = 'Claude';
    else if (/deepseek/i.test(url)) targetProvider = 'DeepSeek';
    else if (/kimi/i.test(url)) targetProvider = 'Kimi';

    const overlay = document.createElement('div');
    overlay.id = 'zb-ai-notice-modal';
    overlay.style.position = 'fixed';
    overlay.style.top = '0';
    overlay.style.left = '0';
    overlay.style.width = '100vw';
    overlay.style.height = '100vh';
    overlay.style.backgroundColor = 'rgba(0,0,0,0.75)';
    overlay.style.zIndex = '999999';
    overlay.style.display = 'flex';
    overlay.style.alignItems = 'center';
    overlay.style.justifyContent = 'center';
    overlay.style.padding = '20px';

    const box = document.createElement('div');
    box.style.background = '#1e1f22';
    box.style.border = '1px solid #44474e';
    box.style.borderRadius = '12px';
    box.style.width = '90%';
    box.style.maxWidth = '640px';
    box.style.maxHeight = '85vh';
    box.style.display = 'flex';
    box.style.flexDirection = 'column';
    box.style.overflow = 'hidden';
    box.style.boxShadow = '0 12px 36px rgba(0,0,0,0.75)';

    const head = document.createElement('div');
    head.style.display = 'flex';
    head.style.justifyContent = 'space-between';
    head.style.alignItems = 'center';
    head.style.padding = '14px 18px';
    head.style.borderBottom = '1px solid #333538';
    head.innerHTML = `<span style="font-weight:600; font-size:15px; color:#e2e2e6;">🤖 Entrega a ${targetProvider} (AI Handoff)</span>`;

    const closeBtn = document.createElement('button');
    closeBtn.textContent = '✕';
    closeBtn.style.background = 'transparent';
    closeBtn.style.border = 'none';
    closeBtn.style.color = '#c4c6d0';
    closeBtn.style.fontSize = '18px';
    closeBtn.style.cursor = 'pointer';
    closeBtn.onclick = () => overlay.remove();
    head.appendChild(closeBtn);

    const bodyDiv = document.createElement('div');
    bodyDiv.style.flex = '1';
    bodyDiv.style.padding = '16px 18px';
    bodyDiv.style.overflowY = 'auto';
    bodyDiv.style.fontSize = '13px';
    bodyDiv.style.color = '#c4c6d0';
    bodyDiv.style.lineHeight = '1.5';

    bodyDiv.innerHTML = `
      <div style="background: rgba(103, 80, 164, 0.15); border: 1px solid rgba(142, 107, 240, 0.35); border-radius: 8px; padding: 12px 14px; margin-bottom: 14px; color: #d0bcff;">
        <strong>🚀 Se ha abierto la web de ${targetProvider} en una nueva pestaña.</strong><br>
        Por privacidad, ZeppBridge <strong>no envía tus datos biométricos directamente a través de servidores externos</strong>. En su lugar, prepara tu consulta con todas tus métricas de salud (pasos, sueño, pulsaciones y entrenamientos) y las guarda automáticamente en tu portapapeles.
      </div>
      <div style="font-weight: 600; color: #e2e2e6; margin-bottom: 8px;">👉 ¿Cómo hacer que ${targetProvider} responda?</div>
      <ol style="margin: 0 0 14px 20px; padding: 0;">
        <li style="margin-bottom: 6px;">Ve a la pestaña de <strong>${targetProvider}</strong> que se acaba de abrir en tu navegador.</li>
        <li style="margin-bottom: 6px;">Haz clic en el cuadro de texto del chat.</li>
        <li style="margin-bottom: 6px;">Pulsa <strong>Ctrl + V</strong> (o clic derecho &gt; <em>Pegar</em>) y presiona <strong>Enter</strong>.</li>
      </ol>
      <div id="zb-prompt-preview-container" style="display: none; margin-top: 10px;">
        <div style="font-size: 11px; color: #a4a7b5; margin-bottom: 4px;">Vista previa de los datos copiados:</div>
        <pre id="zb-prompt-preview" style="background: #141517; padding: 10px; border-radius: 6px; font-size: 11px; white-space: pre-wrap; max-height: 180px; overflow-y: auto; color: #9da3af; font-family: monospace;"></pre>
      </div>
    `;

    const foot = document.createElement('div');
    foot.style.padding = '10px 18px';
    foot.style.borderTop = '1px solid #333538';
    foot.style.display = 'flex';
    foot.style.justifyContent = 'space-between';
    foot.style.alignItems = 'center';
    foot.style.gap = '8px';

    const leftFoot = document.createElement('div');
    leftFoot.style.display = 'flex';
    leftFoot.style.gap = '8px';

    const copyBtn = document.createElement('button');
    copyBtn.className = 'button secondary';
    copyBtn.style.fontSize = '12px';
    copyBtn.style.padding = '6px 12px';
    copyBtn.style.cursor = 'pointer';
    copyBtn.textContent = '📋 Copiar Prompt de nuevo';
    copyBtn.onclick = async () => {
      const textToCopy = window.__LAST_AI_PROMPT__ || '';
      if (textToCopy) {
        try {
          await navigator.clipboard.writeText(textToCopy);
          copyBtn.textContent = '✅ ¡Copiado!';
          setTimeout(() => copyBtn.textContent = '📋 Copiar Prompt de nuevo', 2500);
        } catch (e) {
          copyBtn.textContent = 'Error al copiar';
        }
      }
    };
    leftFoot.appendChild(copyBtn);

    const toggleViewBtn = document.createElement('button');
    toggleViewBtn.className = 'button secondary';
    toggleViewBtn.style.fontSize = '12px';
    toggleViewBtn.style.padding = '6px 12px';
    toggleViewBtn.style.cursor = 'pointer';
    toggleViewBtn.textContent = '👁️ Ver datos / Prompt';
    toggleViewBtn.onclick = () => {
      const container = document.getElementById('zb-prompt-preview-container');
      const pre = document.getElementById('zb-prompt-preview');
      if (container && pre) {
        if (container.style.display === 'none') {
          container.style.display = 'block';
          pre.textContent = window.__LAST_AI_PROMPT__ || 'Cargando datos de salud...';
          toggleViewBtn.textContent = '👁️ Ocultar datos';
        } else {
          container.style.display = 'none';
          toggleViewBtn.textContent = '👁️ Ver datos / Prompt';
        }
      }
    };
    leftFoot.appendChild(toggleViewBtn);

    const okBtn = document.createElement('button');
    okBtn.className = 'button primary';
    okBtn.textContent = 'Entendido';
    okBtn.style.fontSize = '12px';
    okBtn.style.padding = '6px 16px';
    okBtn.style.cursor = 'pointer';
    okBtn.onclick = () => overlay.remove();

    foot.appendChild(leftFoot);
    foot.appendChild(okBtn);

    box.appendChild(head);
    box.appendChild(bodyDiv);
    box.appendChild(foot);
    overlay.appendChild(box);
    document.body.appendChild(overlay);

    // Si aún no tenemos el prompt cargado en memoria, solicitarlo al backend y copiarlo
    if (!window.__LAST_AI_PROMPT__) {
      fetch('/api/bridge/prepare_ai_handoff', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({})
      }).then(r => r.json()).then(data => {
        const text = (data && (data.clipboardText || data.prompt_text)) || '';
        if (text) {
          window.__LAST_AI_PROMPT__ = text;
          navigator.clipboard?.writeText(text).catch(() => {});
          const pre = document.getElementById('zb-prompt-preview');
          if (pre && pre.textContent === 'Cargando datos de salud...') {
            pre.textContent = text;
          }
        }
      }).catch(() => {});
    }
  }

  function setupAiComposerHeartOptions() {
    const isAiPage = window.location.hash.includes('/ai') || !!document.querySelector('.future, .fcard, .card-future');
    if (!isAiPage) return;

    if (document.getElementById('zb-heart-composer-presets')) return;

    // Buscar el contenedor de templates o el bloque de preguntas de FutureCard
    const target = document.querySelector('.future .templates, .fcard .templates, .templates, .question, .fcard');
    if (!target) return;

    // Comprobar si ya existe una opción nativa de pulsaciones compilada en Vue
    const hasNativeHeart = Array.from(document.querySelectorAll('button, .capsule, .segment')).some(
      el => el.textContent && (el.textContent.includes('Pulsaciones') || el.textContent.includes('Cardio (30d)'))
    );
    if (hasNativeHeart) return;

    const presetRow = document.createElement('div');
    presetRow.id = 'zb-heart-composer-presets';
    presetRow.style.display = 'flex';
    presetRow.style.alignItems = 'center';
    presetRow.style.gap = '8px';
    presetRow.style.margin = '10px 0 6px 0';
    presetRow.style.flexWrap = 'wrap';

    const label = document.createElement('span');
    label.style.fontSize = '12px';
    label.style.color = '#c4c6d0';
    label.style.fontWeight = '500';
    label.textContent = '💓 Salud Cardíaca:';
    presetRow.appendChild(label);

    const heart7Btn = document.createElement('button');
    heart7Btn.type = 'button';
    heart7Btn.className = 'button secondary zb-heart-pill';
    heart7Btn.style.fontSize = '12px';
    heart7Btn.style.padding = '4px 10px';
    heart7Btn.style.borderRadius = '20px';
    heart7Btn.style.cursor = 'pointer';
    heart7Btn.style.display = 'inline-flex';
    heart7Btn.style.alignItems = 'center';
    heart7Btn.style.gap = '4px';
    heart7Btn.textContent = '💓 Pulsaciones (7d)';
    heart7Btn.title = 'Analizar pulsaciones cardíacas, FC en reposo y HRV de los últimos 7 días';

    const heart30Btn = document.createElement('button');
    heart30Btn.type = 'button';
    heart30Btn.className = 'button secondary zb-heart-pill';
    heart30Btn.style.fontSize = '12px';
    heart30Btn.style.padding = '4px 10px';
    heart30Btn.style.borderRadius = '20px';
    heart30Btn.style.cursor = 'pointer';
    heart30Btn.style.display = 'inline-flex';
    heart30Btn.style.alignItems = 'center';
    heart30Btn.style.gap = '4px';
    heart30Btn.textContent = '📈 Cardio (30d)';
    heart30Btn.title = 'Analizar tendencias de pulsaciones y evolución cardiovascular de 30 días';

    function applyHeartPrompt(days, promptText, activeBtn) {
      presetRow.querySelectorAll('.zb-heart-pill').forEach(b => {
        b.style.borderColor = '';
        b.style.background = '';
        b.style.color = '';
      });
      activeBtn.style.borderColor = '#d0bcff';
      activeBtn.style.background = 'rgba(103, 80, 164, 0.35)';
      activeBtn.style.color = '#ffffff';

      // Localizar el textarea del compositor de IA
      const textarea = document.querySelector('.future textarea, .fcard textarea, .question textarea, textarea');
      if (textarea) {
        textarea.value = promptText;
        textarea.dispatchEvent(new Event('input', { bubbles: true }));
        textarea.dispatchEvent(new Event('change', { bubbles: true }));
        textarea.focus();
      }

      window.__LAST_AI_HEART_SCOPE__ = days;
    }

    heart7Btn.onclick = () => {
      applyHeartPrompt(
        7,
        'Analiza mis pulsaciones cardíacas de los últimos 7 días. Evalúa mi frecuencia cardíaca en reposo (RHR), picos máximos, zonas de esfuerzo en entrenamientos y variabilidad cardíaca (HRV) para determinar mi nivel de fatiga, recuperación y salud cardiovascular general.',
        heart7Btn
      );
    };

    heart30Btn.onclick = () => {
      applyHeartPrompt(
        30,
        'Analiza la evolución de mis pulsaciones a lo largo del último mes (30 días). Evalúa la tendencia de mi frecuencia cardíaca en reposo, tolerancia al esfuerzo y regularidad del ritmo para identificar mejoras o posibles alertas de sobrecarga o estrés cardiovascular.',
        heart30Btn
      );
    };

    presetRow.appendChild(heart7Btn);
    presetRow.appendChild(heart30Btn);

    target.parentNode.insertBefore(presetRow, target.nextSibling);
  }

  // Función de apoyo para la tarjeta Custom en Configuración (cuando se visite antes de recompilar)
  function setupSettingsCustomCard() {
    const isSettings = window.location.pathname.startsWith('/settings') || window.location.hash.includes('/settings');
    if (!isSettings) return;

    // Si ya existe la tarjeta compilada en Vue, no hacer nada
    const existing = document.querySelector('.card-title, .face-title');
    const hasVueCustom = Array.from(document.querySelectorAll('.card-title, .face-title')).some(el => el.textContent && el.textContent.includes('Custom'));
    if (hasVueCustom) return;

    // Si el usuario abrió /settings/custom directamente y no hay contenido renderizado
    if (window.location.hash.includes('/settings/custom') || window.location.pathname.endsWith('/custom')) {
      const deckBody = document.querySelector('.card-deck-expanded .card-body, .deck-body');
      if (deckBody && !document.getElementById('zb-custom-fallback-body')) {
        const wrap = document.createElement('div');
        wrap.id = 'zb-custom-fallback-body';
        wrap.className = 'custom-section';
        wrap.innerHTML = `
          <section class="s-section" style="padding: 16px; background: #1e1f22; border: 1px solid #333538; border-radius: 12px; margin-top: 16px;">
            <div style="font-weight: 600; font-size: 16px; color: #e2e2e6; margin-bottom: 12px;">Herramientas Custom del Sistema</div>
            <div style="display: flex; flex-direction: column; gap: 14px;">
              <div style="display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #2a2c30; padding-bottom: 12px;">
                <div>
                  <div style="font-weight: 600; color: #e2e2e6;">Sincronización Profunda de Historial (180 días)</div>
                  <div style="font-size: 12px; color: #9da3af;">Descarga y normaliza todo tu historial de 6 meses desde Huami / Zepp.</div>
                </div>
                <button id="zb-custom-sync-btn" class="pill-button button primary" style="cursor: pointer; padding: 6px 14px; font-size: 12px;">🔄 Sincronizar 180 días</button>
              </div>
              <div style="display: flex; justify-content: space-between; align-items: center;">
                <div>
                  <div style="font-weight: 600; color: #e2e2e6;">Registro del Sincronizador (sync.log)</div>
                  <div style="font-size: 12px; color: #9da3af;">Ver la salida del proceso de sincronización oficial.</div>
                </div>
                <button id="zb-custom-log-btn" class="pill-button button secondary" style="cursor: pointer; padding: 6px 14px; font-size: 12px;">📋 Ver Registro</button>
              </div>
            </div>
            <div id="zb-custom-sync-result" style="margin-top: 10px; font-size: 12px; display: none;"></div>
          </section>
        `;
        const syncBtn = wrap.querySelector('#zb-custom-sync-btn');
        const resDiv = wrap.querySelector('#zb-custom-sync-result');
        syncBtn.onclick = async () => {
          syncBtn.disabled = true;
          syncBtn.textContent = '⏳ Sincronizando...';
          resDiv.style.display = 'none';
          try {
            const res = await window.__TAURI_INTERNALS__.invoke('start_history_sync', { days: 180 });
            const count = (res && (res.records_written || res.total_records)) || 0;
            resDiv.textContent = `✅ Sincronización completada con éxito. Se escribieron ${count} registros.`;
            resDiv.style.color = '#6dd58c';
            resDiv.style.display = 'block';
            syncBtn.textContent = '🔄 Sincronizar 180 días';
            syncBtn.disabled = false;
          } catch (e) {
            resDiv.textContent = `❌ Error: ${e.message || e}`;
            resDiv.style.color = '#ffb4ab';
            resDiv.style.display = 'block';
            syncBtn.textContent = '🔄 Sincronizar 180 días';
            syncBtn.disabled = false;
          }
        };

        const logBtn = wrap.querySelector('#zb-custom-log-btn');
        logBtn.onclick = async () => {
          try {
            const res = await fetch('/api/bridge/sync_log');
            const text = await res.text();
            showLogModal(text);
          } catch (e) {
            alert('Error leyendo log: ' + e.message);
          }
        };

        deckBody.appendChild(wrap);
      }
    }
  }

  // Observar el DOM para cuando el usuario interactúe con la app
  const observer = new MutationObserver(() => {
    if (document.getElementById('manual-host')) {
      setupRegionSelect();
    }
    setupAiComposerHeartOptions();
    setupSettingsCustomCard();
  });

  window.addEventListener('popstate', () => {
    setupAiComposerHeartOptions();
    setupSettingsCustomCard();
  });
  window.addEventListener('hashchange', () => {
    setupAiComposerHeartOptions();
    setupSettingsCustomCard();
  });

  if (document.body) {
    observer.observe(document.body, { childList: true, subtree: true });
  } else {
    window.addEventListener('DOMContentLoaded', () => {
      observer.observe(document.body, { childList: true, subtree: true });
    });
  }
})();


