import os
import time
import json
import secrets
import hashlib
import hmac
import struct
import base64
import urllib.parse
from pathlib import Path
from typing import Dict, Any, Optional, Tuple
from fastapi.responses import HTMLResponse

try:
    import pyotp
except ImportError:
    pyotp = None

try:
    import segno
except ImportError:
    segno = None

# Directorio de persistencia
DATA_DIR = Path(os.getenv("ZEPPBRIDGE_DATA_DIR", "/data"))
AUTH_SECURITY_FILE = DATA_DIR / "auth_security.json"

# Almacenes temporales en memoria para flujos pendientes y control de fuerza bruta
PENDING_SETUPS: Dict[str, Dict[str, Any]] = {}
PENDING_LOGINS: Dict[str, Dict[str, Any]] = {}
FAILED_ATTEMPTS: Dict[str, list] = {}  # ip -> list of timestamps

# ------------------------------------------------------------------------------
# 1. Carga y Guardado de Configuración de Seguridad
# ------------------------------------------------------------------------------
def load_security_config() -> Dict[str, Any]:
    if AUTH_SECURITY_FILE.exists():
        try:
            with open(AUTH_SECURITY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    if "sessions" not in data or not isinstance(data["sessions"], dict):
                        data["sessions"] = {}
                    return data
        except Exception:
            pass
    return {
        "initialized": False,
        "email": "",
        "password_hash": "",
        "totp_secret": "",
        "sessions": {}
    }

def save_security_config(cfg: Dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temp_file = DATA_DIR / "auth_security.tmp"
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    temp_file.replace(AUTH_SECURITY_FILE)
    try:
        os.chmod(AUTH_SECURITY_FILE, 0o600)
    except Exception:
        pass

def is_security_initialized() -> bool:
    cfg = load_security_config()
    return bool(cfg.get("initialized") and cfg.get("email") and cfg.get("password_hash") and cfg.get("totp_secret"))

# ------------------------------------------------------------------------------
# 2. Hashing y Verificación de Contraseñas (PBKDF2-HMAC-SHA256, 100k iteraciones)
# ------------------------------------------------------------------------------
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    pw_hash = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000)
    return f"{salt.hex()}${pw_hash.hex()}"

def verify_password(password: str, stored_hash: str) -> bool:
    try:
        parts = stored_hash.split("$")
        if len(parts) != 2:
            return False
        salt = bytes.fromhex(parts[0])
        expected_hash = bytes.fromhex(parts[1])
        calc_hash = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000)
        return hmac.compare_digest(calc_hash, expected_hash)
    except Exception:
        return False

# ------------------------------------------------------------------------------
# 3. Autenticación de Doble Factor (TOTP - RFC 6238)
# ------------------------------------------------------------------------------
def generate_totp_secret() -> str:
    if pyotp:
        return pyotp.random_base32()
    raw = secrets.token_bytes(20)
    return base64.b32encode(raw).decode("ascii").rstrip("=")

def get_totp_uri(email: str, secret: str, issuer: str = "ZeppBridge") -> str:
    if pyotp:
        return pyotp.totp.TOTP(secret).provisioning_uri(name=email, issuer_name=issuer)
    label = urllib.parse.quote(f"{issuer}:{email}")
    issuer_q = urllib.parse.quote(issuer)
    return f"otpauth://totp/{label}?secret={secret}&issuer={issuer_q}&algorithm=SHA1&digits=6&period=30"

def generate_qr_svg(uri: str) -> str:
    if segno:
        try:
            qr = segno.make_qr(uri)
            return qr.svg_inline(scale=5, border=2)
        except Exception:
            pass
    return ""

def verify_totp(secret: str, code: str, window: int = 1) -> bool:
    clean_code = str(code).strip().replace(" ", "").replace("-", "")
    if len(clean_code) != 6 or not clean_code.isdigit():
        return False
    if pyotp:
        try:
            totp = pyotp.TOTP(secret)
            return bool(totp.verify(clean_code, valid_window=window))
        except Exception:
            pass
    # Implementación pura en caso de no estar disponible pyotp
    try:
        key = base64.b32decode(secret, True)
        curr_counter = int(time.time() / 30)
        for offset in range(-window, window + 1):
            msg = struct.pack(">Q", curr_counter + offset)
            h = hmac.new(key, msg, hashlib.sha1).digest()
            o = h[19] & 15
            calc_code = (struct.unpack(">I", h[o:o+4])[0] & 0x7fffffff) % 1_000_000
            if f"{calc_code:06d}" == clean_code:
                return True
        return False
    except Exception:
        return False

# ------------------------------------------------------------------------------
# 4. Gestión de Sesiones y Protección contra Fuerza Bruta
# ------------------------------------------------------------------------------
def create_session(email: str, remember: bool = True, user_agent: str = "") -> str:
    cfg = load_security_config()
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    # 30 días si es recordado, 24 horas si no
    duration = 30 * 86400 if remember else 86400
    expires_at = now + duration

    # Limpiar sesiones expiradas
    valid_sessions = {}
    for tok, sess in cfg.get("sessions", {}).items():
        if isinstance(sess, dict) and sess.get("expires_at", 0) > now:
            valid_sessions[tok] = sess

    valid_sessions[token] = {
        "email": email,
        "created_at": now,
        "expires_at": expires_at,
        "user_agent": (user_agent or "")[:150]
    }
    cfg["sessions"] = valid_sessions
    save_security_config(cfg)
    return token

def verify_session(token: Optional[str]) -> Optional[Dict[str, Any]]:
    if not token or not isinstance(token, str):
        return None
    cfg = load_security_config()
    sessions = cfg.get("sessions", {})
    sess = sessions.get(token)
    if not sess or not isinstance(sess, dict):
        return None
    now = int(time.time())
    if sess.get("expires_at", 0) < now:
        # Expirada
        del sessions[token]
        save_security_config(cfg)
        return None
    return sess

def revoke_session(token: Optional[str]) -> None:
    if not token:
        return
    cfg = load_security_config()
    if "sessions" in cfg and token in cfg["sessions"]:
        del cfg["sessions"][token]
        save_security_config(cfg)

def check_rate_limit(ip: str) -> bool:
    """Retorna True si la IP puede intentar loguearse; False si excede 5 fallos en 3 minutos."""
    now = time.time()
    attempts = [t for t in FAILED_ATTEMPTS.get(ip, []) if now - t < 180]
    FAILED_ATTEMPTS[ip] = attempts
    return len(attempts) < 5

def record_failed_attempt(ip: str) -> None:
    now = time.time()
    attempts = [t for t in FAILED_ATTEMPTS.get(ip, []) if now - t < 180]
    attempts.append(now)
    FAILED_ATTEMPTS[ip] = attempts

def reset_failed_attempts(ip: str) -> None:
    if ip in FAILED_ATTEMPTS:
        del FAILED_ATTEMPTS[ip]

# ------------------------------------------------------------------------------
# 5. Plantilla HTML Única y Responsiva: ZeppBridge Shield (Login y Setup)
# ------------------------------------------------------------------------------
def render_auth_page(mode: str = "login", initial_message: str = "") -> HTMLResponse:
    is_setup = (mode == "setup")
    title_text = "Configuración de Acceso Seguro" if is_setup else "Acceso Protegido"
    sub_text = (
        "Configura tu email, contraseña y vincula tu Authenticator 2FA para blindar ZeppBridge."
        if is_setup else
        "Introduce tus credenciales y el código de tu Authenticator para acceder."
    )

    html_content = f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <title>ZeppBridge Shield · {title_text}</title>
  <style>
    :root {{
      --bg: #090d16;
      --card-bg: rgba(17, 24, 39, 0.85);
      --card-border: rgba(255, 255, 255, 0.1);
      --card-border-focus: rgba(6, 182, 212, 0.5);
      --text-main: #f3f4f6;
      --text-muted: #9ca3af;
      --cyan: #06b6d4;
      --cyan-hover: #0891b2;
      --cyan-glow: rgba(6, 182, 212, 0.25);
      --red: #ef4444;
      --green: #10b981;
      --input-bg: rgba(15, 23, 42, 0.7);
      --radius-lg: 20px;
      --radius-md: 12px;
      --font: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background-color: var(--bg);
      background-image: 
        radial-gradient(circle at 15% 20%, rgba(6, 182, 212, 0.12) 0%, transparent 40%),
        radial-gradient(circle at 85% 80%, rgba(59, 130, 246, 0.10) 0%, transparent 45%);
      color: var(--text-main);
      font-family: var(--font);
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 20px;
      -webkit-font-smoothing: antialiased;
    }}
    .shield-card {{
      width: 100%;
      max-width: 440px;
      background: var(--card-bg);
      backdrop-filter: blur(20px);
      -webkit-backdrop-filter: blur(20px);
      border: 1px solid var(--card-border);
      border-radius: var(--radius-lg);
      padding: 36px 32px;
      box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.6), 0 0 0 1px rgba(255, 255, 255, 0.05);
      animation: fadeIn 0.4s cubic-bezier(0.16, 1, 0.3, 1);
    }}
    @keyframes fadeIn {{
      from {{ opacity: 0; transform: translateY(16px) scale(0.98); }}
      to {{ opacity: 1; transform: translateY(0) scale(1); }}
    }}
    .brand-header {{
      text-align: center;
      margin-bottom: 28px;
    }}
    .logo-container {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 58px;
      height: 58px;
      border-radius: 16px;
      background: linear-gradient(135deg, rgba(6, 182, 212, 0.2), rgba(59, 130, 246, 0.2));
      border: 1px solid rgba(6, 182, 212, 0.35);
      box-shadow: 0 8px 24px var(--cyan-glow);
      margin-bottom: 14px;
    }}
    .logo-icon {{
      width: 30px;
      height: 30px;
      fill: var(--cyan);
    }}
    .brand-title {{
      font-size: 24px;
      font-weight: 700;
      letter-spacing: -0.02em;
      color: #fff;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
    }}
    .brand-badge {{
      font-size: 11px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      padding: 3px 8px;
      border-radius: 999px;
      background: rgba(6, 182, 212, 0.15);
      color: var(--cyan);
      border: 1px solid rgba(6, 182, 212, 0.3);
    }}
    .brand-subtitle {{
      font-size: 13.5px;
      color: var(--text-muted);
      margin-top: 6px;
      line-height: 1.45;
    }}
    .form-group {{
      margin-bottom: 20px;
    }}
    .form-label {{
      display: block;
      font-size: 12.5px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      color: var(--text-muted);
      margin-bottom: 8px;
    }}
    .input-wrap {{
      position: relative;
    }}
    .form-input {{
      width: 100%;
      background: var(--input-bg);
      border: 1px solid var(--card-border);
      border-radius: var(--radius-md);
      padding: 13px 15px;
      color: #fff;
      font-size: 15px;
      outline: none;
      transition: all 0.2s ease;
    }}
    .form-input:focus {{
      border-color: var(--cyan);
      box-shadow: 0 0 0 3px var(--cyan-glow);
    }}
    .pw-toggle {{
      position: absolute;
      right: 12px;
      top: 50%;
      transform: translateY(-50%);
      background: none;
      border: none;
      color: var(--text-muted);
      cursor: pointer;
      font-size: 13px;
      padding: 4px;
    }}
    .code-input {{
      letter-spacing: 8px;
      font-size: 26px;
      font-weight: 700;
      text-align: center;
      font-family: monospace;
      padding: 14px;
    }}
    .checkbox-wrap {{
      display: flex;
      align-items: center;
      gap: 9px;
      margin-bottom: 24px;
      font-size: 13px;
      color: var(--text-muted);
      cursor: pointer;
      user-select: none;
    }}
    .checkbox-wrap input {{
      width: 16px;
      height: 16px;
      accent-color: var(--cyan);
      cursor: pointer;
    }}
    .btn-submit {{
      width: 100%;
      background: linear-gradient(135deg, var(--cyan), var(--cyan-hover));
      color: #fff;
      font-size: 15px;
      font-weight: 600;
      border: none;
      border-radius: var(--radius-md);
      padding: 14px;
      cursor: pointer;
      transition: all 0.2s ease;
      box-shadow: 0 4px 15px var(--cyan-glow);
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
    }}
    .btn-submit:hover {{
      transform: translateY(-1px);
      box-shadow: 0 6px 20px rgba(6, 182, 212, 0.4);
    }}
    .btn-submit:active {{
      transform: translateY(1px);
    }}
    .btn-submit:disabled {{
      opacity: 0.6;
      cursor: not-allowed;
      transform: none;
    }}
    .alert-box {{
      padding: 12px 14px;
      border-radius: var(--radius-md);
      font-size: 13px;
      margin-bottom: 20px;
      display: none;
      animation: fadeIn 0.2s ease;
      line-height: 1.4;
    }}
    .alert-box.error {{
      display: block;
      background: rgba(239, 68, 68, 0.12);
      border: 1px solid rgba(239, 68, 68, 0.35);
      color: #fca5a5;
    }}
    .alert-box.success {{
      display: block;
      background: rgba(16, 185, 129, 0.12);
      border: 1px solid rgba(16, 185, 129, 0.35);
      color: #86efac;
    }}
    .qr-box {{
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 14px;
      margin: 20px 0;
      padding: 20px;
      background: rgba(15, 23, 42, 0.85);
      border: 1px solid var(--card-border);
      border-radius: var(--radius-md);
    }}
    .qr-svg-holder {{
      background: #ffffff;
      padding: 12px;
      border-radius: 12px;
      box-shadow: 0 4px 16px rgba(0, 0, 0, 0.4);
      display: flex;
      align-items: center;
      justify-content: center;
    }}
    .qr-svg-holder svg {{
      display: block;
      max-width: 190px;
      max-height: 190px;
      width: 100%;
      height: auto;
    }}
    .secret-key-badge {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      width: 100%;
      background: rgba(0, 0, 0, 0.3);
      padding: 8px 12px;
      border-radius: 8px;
      border: 1px dashed rgba(255, 255, 255, 0.15);
      font-family: monospace;
      font-size: 13px;
      color: var(--cyan);
    }}
    .btn-copy {{
      background: rgba(255, 255, 255, 0.1);
      border: none;
      color: #fff;
      font-size: 11px;
      font-weight: 600;
      padding: 4px 8px;
      border-radius: 6px;
      cursor: pointer;
      transition: background 0.2s;
    }}
    .btn-copy:hover {{
      background: rgba(255, 255, 255, 0.2);
    }}
    .step-indicator {{
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      margin-bottom: 22px;
    }}
    .step-pill {{
      width: 32px;
      height: 4px;
      border-radius: 999px;
      background: rgba(255, 255, 255, 0.15);
      transition: all 0.3s ease;
    }}
    .step-pill.active {{
      background: var(--cyan);
      box-shadow: 0 0 10px var(--cyan-glow);
    }}
    .footer-note {{
      text-align: center;
      font-size: 12px;
      color: var(--text-muted);
      margin-top: 26px;
    }}
    .spinner {{
      display: inline-block;
      width: 16px;
      height: 16px;
      border: 2px solid rgba(255, 255, 255, 0.3);
      border-radius: 50%;
      border-top-color: #fff;
      animation: spin 0.7s linear infinite;
    }}
    @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
  </style>
</head>
<body>
  <div class="shield-card">
    <div class="brand-header">
      <div class="logo-container">
        <svg class="logo-icon" viewBox="0 0 24 24">
          <path d="M12 2L4 5v6.09c0 5.05 3.41 9.76 8 10.91 4.59-1.15 8-5.86 8-10.91V5l-8-3zm1 14h-2v-2h2v2zm0-4h-2V7h2v5z"/>
        </svg>
      </div>
      <h1 class="brand-title">
        ZeppBridge <span class="brand-badge">Shield 2FA</span>
      </h1>
      <p id="page-subtitle" class="brand-subtitle">{sub_text}</p>
    </div>

    <!-- Indicador de pasos (solo en setup) -->
    <div id="step-indicator" class="step-indicator" style="{'display: flex;' if is_setup else 'display: none;'}">
      <div id="pill-step-1" class="step-pill active"></div>
      <div id="pill-step-2" class="step-pill"></div>
    </div>

    <!-- Caja de alertas / errores -->
    <div id="alert-box" class="alert-box {'error' if initial_message else ''}">{initial_message}</div>

    <!-- ===================================================================== -->
    <!-- FORMULARIO 1: CREDENCIALES (Email + Contraseña)                       -->
    <!-- ===================================================================== -->
    <form id="form-credentials" onsubmit="handleCredentialsSubmit(event)">
      <div class="form-group">
        <label class="form-label">Correo Electrónico (Usuario)</label>
        <div class="input-wrap">
          <input id="input-email" type="email" class="form-input" placeholder="tu@email.com" required autocomplete="username">
        </div>
      </div>

      <div class="form-group">
        <label class="form-label">Contraseña</label>
        <div class="input-wrap">
          <input id="input-password" type="password" class="form-input" placeholder="••••••••••••" required autocomplete="{'new-password' if is_setup else 'current-password'}">
          <button type="button" class="pw-toggle" onclick="togglePasswordVisibility('input-password')">👁️</button>
        </div>
      </div>

      {'<div class="form-group"><label class="form-label">Confirmar Contraseña</label><div class="input-wrap"><input id="input-password-confirm" type="password" class="form-input" placeholder="••••••••••••" required autocomplete="new-password"></div></div>' if is_setup else ''}

      {'<label class="checkbox-wrap"><input id="remember-me" type="checkbox" checked> <span>Recordar este dispositivo durante 30 días</span></label>' if not is_setup else ''}

      <button id="btn-cred-submit" type="submit" class="btn-submit">
        <span>{'Continuar a Verificación 2FA →' if is_setup else 'Siguiente paso →'}</span>
      </button>
    </form>

    <!-- ===================================================================== -->
    <!-- FORMULARIO 2: VERIFICACIÓN 2FA / TOTP                                 -->
    <!-- ===================================================================== -->
    <form id="form-2fa" style="display: none;" onsubmit="handle2faSubmit(event)">
      <!-- Vista previa QR (solo en setup) -->
      <div id="qr-container" class="qr-box" style="display: none;">
        <span style="font-size: 13px; font-weight: 600; color: #fff;">1. Escanea este código con tu app Authenticator</span>
        <div id="qr-svg-holder" class="qr-svg-holder"></div>
        <div class="secret-key-badge">
          <span id="secret-key-text">CLAVE-SECRETA</span>
          <button type="button" class="btn-copy" onclick="copySecretKey()">Copiar</button>
        </div>
        <span style="font-size: 11.5px; color: var(--text-muted);">Compatible con Google Authenticator, Authy, Apple Passwords y 1Password.</span>
      </div>

      <div class="form-group">
        <label id="code-label" class="form-label">Código de Seguridad (6 dígitos)</label>
        <div class="input-wrap">
          <input id="input-code" type="text" inputmode="numeric" pattern="[0-9]*" maxlength="6" class="form-input code-input" placeholder="000000" autocomplete="one-time-code" required>
        </div>
        <p style="font-size: 12px; color: var(--text-muted); margin-top: 8px; text-align: center;">Introduce el código que aparece en tu teléfono</p>
      </div>

      <button id="btn-2fa-submit" type="submit" class="btn-submit">
        <span>{'Verificar y Activar Seguridad' if is_setup else 'Verificar y Entrar'}</span>
      </button>

      <button type="button" class="btn-submit" style="background: transparent; box-shadow: none; border: 1px solid var(--card-border); color: var(--text-muted); margin-top: 10px;" onclick="backToStep1()">
        ← Volver
      </button>
    </form>

    <p class="footer-note">ZeppBridge · Autenticación local segura de extremo a extremo</p>
  </div>

  <script>
    const IS_SETUP = {'true' if is_setup else 'false'};
    let pendingToken = null;
    let pendingSecret = null;

    function showAlert(msg, isSuccess = false) {{
      const el = document.getElementById('alert-box');
      el.className = 'alert-box ' + (isSuccess ? 'success' : 'error');
      el.textContent = msg;
    }}

    function hideAlert() {{
      const el = document.getElementById('alert-box');
      el.className = 'alert-box';
      el.textContent = '';
    }}

    function togglePasswordVisibility(id) {{
      const input = document.getElementById(id);
      input.type = input.type === 'password' ? 'text' : 'password';
    }}

    function copySecretKey() {{
      const txt = document.getElementById('secret-key-text').innerText;
      navigator.clipboard.writeText(txt).then(() => {{
        showAlert('Clave secreta copiada al portapapeles', true);
      }}).catch(() => {{
        showAlert('No se pudo copiar automáticamente', false);
      }});
    }}

    function backToStep1() {{
      hideAlert();
      document.getElementById('form-2fa').style.display = 'none';
      document.getElementById('form-credentials').style.display = 'block';
      if (IS_SETUP) {{
        document.getElementById('pill-step-1').className = 'step-pill active';
        document.getElementById('pill-step-2').className = 'step-pill';
        document.getElementById('page-subtitle').innerText = 'Configura tu email, contraseña y vincula tu Authenticator 2FA para blindar ZeppBridge.';
      }} else {{
        document.getElementById('page-subtitle').innerText = 'Introduce tus credenciales y el código de tu Authenticator para acceder.';
      }}
    }}

    async function handleCredentialsSubmit(e) {{
      e.preventDefault();
      hideAlert();

      const email = document.getElementById('input-email').value.trim();
      const password = document.getElementById('input-password').value;

      if (!email || !email.includes('@')) {{
        showAlert('Introduce una dirección de correo electrónico válida');
        return;
      }}

      if (password.length < 6) {{
        showAlert('La contraseña debe tener al menos 6 caracteres');
        return;
      }}

      if (IS_SETUP) {{
        const confirm = document.getElementById('input-password-confirm').value;
        if (password !== confirm) {{
          showAlert('Las contraseñas no coinciden');
          return;
        }}
      }}

      const btn = document.getElementById('btn-cred-submit');
      btn.disabled = true;
      btn.innerHTML = '<span class="spinner"></span> Comprobando...';

      try {{
        if (IS_SETUP) {{
          const res = await fetch('/api/auth/setup-credentials', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{ email, password }})
          }});
          const data = await res.json();
          if (!res.ok || !data.ok) {{
            showAlert(data.message || 'Error al iniciar la configuración');
            return;
          }}

          pendingToken = data.pending_token;
          pendingSecret = data.totp_secret;

          // Mostrar QR y clave
          document.getElementById('qr-container').style.display = 'flex';
          document.getElementById('qr-svg-holder').innerHTML = data.qr_svg || '';
          document.getElementById('secret-key-text').innerText = data.totp_secret;

          // Cambiar a paso 2
          document.getElementById('form-credentials').style.display = 'none';
          document.getElementById('form-2fa').style.display = 'block';
          document.getElementById('pill-step-1').className = 'step-pill';
          document.getElementById('pill-step-2').className = 'step-pill active';
          document.getElementById('page-subtitle').innerText = 'Escanea el código con tu app de autenticación y confirma el código.';
          setTimeout(() => document.getElementById('input-code').focus(), 150);
        }} else {{
          const res = await fetch('/api/auth/login-credentials', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{ email, password }})
          }});
          const data = await res.json();
          if (!res.ok || !data.ok) {{
            showAlert(data.message || 'Credenciales incorrectas');
            return;
          }}

          pendingToken = data.temp_token;

          // Cambiar a paso 2 (2FA)
          document.getElementById('form-credentials').style.display = 'none';
          document.getElementById('form-2fa').style.display = 'block';
          document.getElementById('page-subtitle').innerText = 'Introduce el código de 6 dígitos de tu app de autenticación (2FA).';
          setTimeout(() => document.getElementById('input-code').focus(), 150);
        }}
      }} catch (err) {{
        showAlert('Error de conexión con el servidor: ' + err.message);
      }} finally {{
        btn.disabled = false;
        btn.innerHTML = '<span>' + (IS_SETUP ? 'Continuar a Verificación 2FA →' : 'Siguiente paso →') + '</span>';
      }}
    }}

    async function handle2faSubmit(e) {{
      e.preventDefault();
      hideAlert();

      const code = document.getElementById('input-code').value.trim();
      if (!code || code.length !== 6) {{
        showAlert('Introduce el código de 6 dígitos');
        return;
      }}

      const btn = document.getElementById('btn-2fa-submit');
      btn.disabled = true;
      btn.innerHTML = '<span class="spinner"></span> Verificando...';

      try {{
        if (IS_SETUP) {{
          const res = await fetch('/api/auth/verify-setup', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{ pending_token: pendingToken, code: code }})
          }});
          const data = await res.json();
          if (!res.ok || !data.ok) {{
            showAlert(data.message || 'Código incorrecto. Comprueba la hora en tu móvil.');
            return;
          }}

          showAlert('¡Seguridad 2FA activada con éxito! Redirigiendo...', true);
          setTimeout(() => {{ window.location.href = '/'; }}, 900);
        }} else {{
          const remember = document.getElementById('remember-me')?.checked ?? true;
          const res = await fetch('/api/auth/verify-login-2fa', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{ temp_token: pendingToken, code: code, remember: remember }})
          }});
          const data = await res.json();
          if (!res.ok || !data.ok) {{
            showAlert(data.message || 'Código incorrecto o caducado. Vuelve a intentarlo.');
            return;
          }}

          showAlert('Autenticado correctamente. Redirigiendo...', true);
          setTimeout(() => {{ window.location.href = '/'; }}, 600);
        }}
      }} catch (err) {{
        showAlert('Error de conexión con el servidor: ' + err.message);
      }} finally {{
        btn.disabled = false;
        btn.innerHTML = '<span>' + (IS_SETUP ? 'Verificar y Activar Seguridad' : 'Verificar y Entrar') + '</span>';
      }}
    }}

    // Auto-enviar cuando se completen 6 dígitos en el código
    document.getElementById('input-code').addEventListener('input', (e) => {{
      const val = e.target.value.replace(/[^0-9]/g, '');
      e.target.value = val;
      if (val.length === 6) {{
        document.getElementById('form-2fa').dispatchEvent(new Event('submit'));
      }}
    }});
  </script>
</body>
</html>"""
    return HTMLResponse(content=html_content)
