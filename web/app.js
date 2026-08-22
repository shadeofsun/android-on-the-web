/*
 * Pixel 6 Console - vanilla JS, no build step, no framework, no CDN.
 *
 * The API token lives in sessionStorage (never localStorage) so it dies with
 * the tab. It is sent as an Authorization header everywhere except the SSE
 * logcat stream, where EventSource cannot set headers and a query parameter is
 * used instead.
 */
'use strict';

const TOKEN_KEY = 'android-console-token';

const state = {
  token: null,
  device: null,          // last /api/device payload
  pollMs: 1000,
  pollTimer: null,
  polling: false,
  screenObjectUrl: null,
  history: [],
  historyIndex: -1,
  logcatSource: null,
  drag: null,
  lastFrameAt: 0,
  consecutiveFailures: 0,
};

/* ───────────────────────────── tiny helpers ───────────────────────────── */
const $  = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function toast(message, kind = 'info', ttl = 4200) {
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.textContent = message;
  $('#toasts').appendChild(el);
  setTimeout(() => el.remove(), ttl);
}

function authHeaders(extra = {}) {
  return Object.assign({ Authorization: `Bearer ${state.token}` }, extra);
}

async function api(path, options = {}) {
  const res = await fetch(path, Object.assign({}, options, {
    headers: authHeaders(options.headers || {}),
  }));

  if (res.status === 401) {
    lock('Session rejected: the token is no longer valid.');
    throw new Error('unauthorised');
  }

  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      if (body && body.detail) detail = typeof body.detail === 'string'
        ? body.detail
        : JSON.stringify(body.detail);
    } catch (_) { /* non-JSON error body */ }
    throw new Error(detail);
  }

  return res;
}

const apiJson = (path, options) => api(path, options).then((r) => r.json());

/* ───────────────────────────── auth ───────────────────────────── */
async function connect(token) {
  const res = await fetch('/api/device', { headers: { Authorization: `Bearer ${token}` } });
  if (res.status === 401) throw new Error('Invalid token.');
  if (!res.ok) throw new Error(`Server responded ${res.status}. Is the device booted?`);
  return res.json();
}

function unlock(token, device, persist) {
  state.token = token;
  state.device = device;
  if (persist) {
    try { sessionStorage.setItem(TOKEN_KEY, token); } catch (_) { /* private mode */ }
  }
  $('#login').hidden = true;
  $('#app').hidden = false;
  renderDevice(device);
  startPolling();
  pollHealth();
  setInterval(pollHealth, 5000);
}

function lock(reason) {
  stopPolling();
  stopLogcat();
  state.token = null;
  try { sessionStorage.removeItem(TOKEN_KEY); } catch (_) { /* ignore */ }
  $('#app').hidden = true;
  $('#login').hidden = false;
  const err = $('#login-error');
  if (reason) { err.textContent = reason; err.hidden = false; } else { err.hidden = true; }
  $('#token-input').value = '';
}

$('#login-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = $('#login-form button[type="submit"]');
  const token = $('#token-input').value.trim();
  const err = $('#login-error');
  err.hidden = true;
  if (!token) return;

  button.disabled = true;
  button.textContent = 'Connecting…';
  try {
    const device = await connect(token);
    unlock(token, device, $('#remember-session').checked);
  } catch (e) {
    err.textContent = e.message;
    err.hidden = false;
  } finally {
    button.disabled = false;
    button.textContent = 'Connect';
  }
});

$('#btn-logout').addEventListener('click', () => lock(''));

/* ───────────────────────────── device meta ───────────────────────────── */
function renderDevice(device) {
  state.device = device;
  $('#chip-model').textContent   = device.model || 'unknown';
  $('#chip-android').textContent = `Android ${device.android_version || '?'} (API ${device.sdk_int ?? '?'})`;
  $('#chip-res').textContent     = device.screen_width
    ? `${device.screen_width}×${device.screen_height} @${device.density || '?'}dpi`
    : 'resolution ?';
  $('#chip-serial').textContent  = device.serial || '—';
}

async function pollHealth() {
  if (!state.token) return;
  try {
    const health = await (await fetch('/api/health')).json();
    const dot = $('#status-dot');
    dot.className = 'dot ' + (health.boot_completed ? 'ok' : (health.device_state === 'device' ? 'booting' : 'down'));
    dot.title = `${health.status} · ${health.device_state} · up ${health.api_uptime_seconds}s`;

    const chip = $('#chip-shellmode');
    if (health.shell_mode === 'unrestricted') {
      chip.hidden = false;
      chip.textContent = 'SHELL: UNRESTRICTED';
    } else {
      chip.hidden = true;
    }
  } catch (_) {
    $('#status-dot').className = 'dot down';
  }
}

$('#btn-refresh').addEventListener('click', async () => {
  try {
    renderDevice(await apiJson('/api/device'));
    toast('Device info refreshed.', 'ok');
  } catch (e) { toast(e.message, 'err'); }
});

$('#btn-reboot').addEventListener('click', async () => {
  if (!window.confirm('Reboot the emulator? The device will be unavailable for a few minutes.')) return;
  try {
    await api('/api/reboot', { method: 'POST' });
    toast('Reboot requested. Watch the status dot.', 'info', 7000);
    setScreenOverlay(true, 'rebooting…');
  } catch (e) { toast(e.message, 'err'); }
});

/* ───────────────────────────── screen polling ───────────────────────────── */
function setScreenOverlay(visible, text) {
  const overlay = $('#screen-overlay');
  overlay.hidden = !visible;
  if (text) $('#screen-overlay-text').textContent = text;
}

async function grabFrame() {
  const res = await fetch('/api/screenshot', { headers: authHeaders(), cache: 'no-store' });
  if (res.status === 401) { lock('Session rejected.'); return; }
  if (!res.ok) throw new Error(`screenshot HTTP ${res.status}`);

  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const img = $('#screen');
  const previous = state.screenObjectUrl;

  await new Promise((resolve) => {
    img.onload = resolve;
    img.onerror = resolve;
    img.src = url;
  });

  state.screenObjectUrl = url;
  if (previous) URL.revokeObjectURL(previous);

  const now = performance.now();
  if (state.lastFrameAt) {
    const fps = 1000 / Math.max(1, now - state.lastFrameAt);
    $('#fps-label').textContent = `${fps.toFixed(1)} fps`;
  }
  state.lastFrameAt = now;
  setScreenOverlay(false);
}

async function pollLoop() {
  if (!state.polling || !state.token) return;
  try {
    await grabFrame();
    state.consecutiveFailures = 0;
  } catch (e) {
    state.consecutiveFailures += 1;
    if (state.consecutiveFailures === 1 || state.consecutiveFailures % 10 === 0) {
      setScreenOverlay(true, `no frame (${e.message})`);
    }
  } finally {
    if (state.polling) {
      // Back off while the device is unreachable instead of hammering adb.
      const delay = state.consecutiveFailures > 3
        ? Math.min(10000, state.pollMs * 4)
        : state.pollMs;
      state.pollTimer = setTimeout(pollLoop, delay);
    }
  }
}

function startPolling() {
  stopPolling();
  if (state.pollMs <= 0) { $('#fps-label').textContent = 'paused'; return; }
  state.polling = true;
  setScreenOverlay(true, 'waiting for first frame…');
  pollLoop();
}

function stopPolling() {
  state.polling = false;
  if (state.pollTimer) clearTimeout(state.pollTimer);
  state.pollTimer = null;
  state.lastFrameAt = 0;
}

$('#poll-interval').addEventListener('change', (event) => {
  state.pollMs = Number(event.target.value);
  if (state.pollMs === 0) { stopPolling(); $('#fps-label').textContent = 'paused'; }
  else startPolling();
});

document.addEventListener('visibilitychange', () => {
  if (!state.token) return;
  if (document.hidden) stopPolling();
  else if (state.pollMs > 0) startPolling();
});

/* ─────────────────────── coordinate mapping + gestures ─────────────────────
 * The <img> uses object-fit: contain, so the rendered bitmap is letterboxed
 * inside the element. Map client coords -> bitmap coords -> device pixels.
 */
function toDeviceCoords(event) {
  const img = $('#screen');
  const natW = img.naturalWidth;
  const natH = img.naturalHeight;
  if (!natW || !natH) return null;

  const rect = img.getBoundingClientRect();
  const scale = Math.min(rect.width / natW, rect.height / natH);
  const drawnW = natW * scale;
  const drawnH = natH * scale;
  const offsetX = (rect.width - drawnW) / 2;
  const offsetY = (rect.height - drawnH) / 2;

  const localX = event.clientX - rect.left - offsetX;
  const localY = event.clientY - rect.top - offsetY;
  if (localX < 0 || localY < 0 || localX > drawnW || localY > drawnH) return null;

  // The screenshot bitmap is already in device pixels, so bitmap == device
  // space. Fall back to /api/device dimensions only if they disagree.
  const devW = state.device?.screen_width || natW;
  const devH = state.device?.screen_height || natH;

  return {
    x: Math.round((localX / drawnW) * devW),
    y: Math.round((localY / drawnH) * devH),
  };
}

const screenEl = $('#screen');

screenEl.addEventListener('pointerdown', (event) => {
  const point = toDeviceCoords(event);
  if (!point) return;
  screenEl.setPointerCapture(event.pointerId);
  state.drag = { start: point, startedAt: performance.now() };
});

screenEl.addEventListener('pointerup', async (event) => {
  const drag = state.drag;
  state.drag = null;
  if (!drag) return;

  const end = toDeviceCoords(event) || drag.start;
  const dx = end.x - drag.start.x;
  const dy = end.y - drag.start.y;
  const distance = Math.hypot(dx, dy);
  const elapsed = Math.round(performance.now() - drag.startedAt);

  try {
    if (distance < 12) {
      await api('/api/input/tap', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ x: drag.start.x, y: drag.start.y }),
      });
    } else {
      await api('/api/input/swipe', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          x1: drag.start.x, y1: drag.start.y,
          x2: end.x, y2: end.y,
          ms: Math.min(2000, Math.max(60, elapsed)),
        }),
      });
    }
    if (state.pollMs > 0) setTimeout(() => { grabFrame().catch(() => {}); }, 180);
  } catch (e) {
    toast(e.message, 'err');
  }
});

screenEl.addEventListener('dragstart', (event) => event.preventDefault());
screenEl.addEventListener('contextmenu', (event) => event.preventDefault());

/* ───────────────────────────── keys and text ───────────────────────────── */
async function sendKey(keycode) {
  try {
    await api('/api/input/key', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ keycode }),
    });
    if (state.pollMs > 0) setTimeout(() => { grabFrame().catch(() => {}); }, 180);
  } catch (e) { toast(e.message, 'err'); }
}

$$('.navbtn').forEach((btn) => btn.addEventListener('click', () => sendKey(btn.dataset.key)));
$('#btn-enter').addEventListener('click', () => sendKey('KEYCODE_ENTER'));
$('#btn-del').addEventListener('click', () => sendKey('KEYCODE_DEL'));

$('#type-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const input = $('#type-input');
  const text = input.value;
  if (!text) return;
  try {
    await api('/api/input/text', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    });
    input.value = '';
    if (state.pollMs > 0) setTimeout(() => { grabFrame().catch(() => {}); }, 250);
  } catch (e) { toast(e.message, 'err'); }
});

/* ───────────────────────────── tabs ───────────────────────────── */
$$('.tab').forEach((tab) => {
  tab.addEventListener('click', () => {
    $$('.tab').forEach((t) => t.classList.toggle('active', t === tab));
    $$('.tabpanel').forEach((p) => p.classList.toggle('active', p.dataset.panel === tab.dataset.tab));
  });
});

/* ───────────────────────────── shell terminal ───────────────────────────── */
const term = $('#term');

function termWrite(text, kind = 'out') {
  const line = document.createElement('span');
  line.className = `term-line ${kind}`;
  line.textContent = text;
  term.appendChild(line);
  term.scrollTop = term.scrollHeight;
}

termWrite('Connected. Type a command; ↑/↓ walks the history.', 'meta');

$('#btn-clear-term').addEventListener('click', () => { term.textContent = ''; });

$('#shell-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const input = $('#shell-input');
  const cmd = input.value.trim();
  if (!cmd) return;

  state.history.push(cmd);
  state.historyIndex = state.history.length;
  input.value = '';
  termWrite(`$ ${cmd}`, 'cmd');

  try {
    const result = await apiJson('/api/shell', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ cmd }),
    });
    if (result.stdout) termWrite(result.stdout.replace(/\n$/, ''), 'out');
    if (result.stderr) termWrite(result.stderr.replace(/\n$/, ''), 'err');
    termWrite(`[exit ${result.exit_code} · ${result.duration_ms}ms]`, 'meta');
  } catch (e) {
    termWrite(e.message, 'err');
  }
});

$('#shell-input').addEventListener('keydown', (event) => {
  if (event.key !== 'ArrowUp' && event.key !== 'ArrowDown') return;
  if (state.history.length === 0) return;
  event.preventDefault();

  if (event.key === 'ArrowUp') {
    state.historyIndex = Math.max(0, state.historyIndex - 1);
  } else {
    state.historyIndex = Math.min(state.history.length, state.historyIndex + 1);
  }
  event.target.value = state.history[state.historyIndex] ?? '';
  requestAnimationFrame(() => {
    event.target.setSelectionRange(event.target.value.length, event.target.value.length);
  });
});

/* ───────────────────────────── apps ───────────────────────────── */
let loadedPackages = [];

function renderApps() {
  const needle = $('#app-filter').value.trim().toLowerCase();
  const list = $('#app-list');
  list.textContent = '';

  const matches = loadedPackages.filter((p) => !needle || p.toLowerCase().includes(needle));
  if (matches.length === 0) {
    const li = document.createElement('li');
    li.className = 'muted';
    li.textContent = loadedPackages.length ? 'No package matches the filter.' : 'No packages loaded yet.';
    list.appendChild(li);
    return;
  }

  matches.forEach((pkg) => {
    const li = document.createElement('li');

    const name = document.createElement('span');
    name.className = 'pkg';
    name.textContent = pkg;
    name.title = pkg;

    const actions = document.createElement('span');
    actions.className = 'actions';

    const launch = document.createElement('button');
    launch.className = 'ghost small';
    launch.textContent = 'Launch';
    launch.addEventListener('click', () => runShellQuiet(
      `monkey -p ${pkg} -c android.intent.category.LAUNCHER 1`, `Launched ${pkg}`));

    const stop = document.createElement('button');
    stop.className = 'ghost small';
    stop.textContent = 'Stop';
    stop.addEventListener('click', () => runShellQuiet(`am force-stop ${pkg}`, `Stopped ${pkg}`));

    const remove = document.createElement('button');
    remove.className = 'ghost small danger';
    remove.textContent = 'Uninstall';
    remove.addEventListener('click', async () => {
      if (!window.confirm(`Uninstall ${pkg}?`)) return;
      try {
        await api(`/api/app/${encodeURIComponent(pkg)}`, { method: 'DELETE' });
        toast(`Uninstalled ${pkg}`, 'ok');
        loadedPackages = loadedPackages.filter((p) => p !== pkg);
        renderApps();
      } catch (e) { toast(e.message, 'err'); }
    });

    actions.append(launch, stop, remove);
    li.append(name, actions);
    list.appendChild(li);
  });
}

async function runShellQuiet(cmd, okMessage) {
  try {
    const result = await apiJson('/api/shell', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ cmd }),
    });
    toast(result.exit_code === 0 ? okMessage : (result.stderr || 'Command failed'),
          result.exit_code === 0 ? 'ok' : 'err');
  } catch (e) { toast(e.message, 'err'); }
}

$('#btn-load-apps').addEventListener('click', async () => {
  const btn = $('#btn-load-apps');
  btn.disabled = true;
  try {
    const data = await apiJson(`/api/apps?include_system=${$('#include-system').checked}`);
    loadedPackages = data.packages;
    renderApps();
    toast(`${data.count} packages loaded.`, 'ok');
  } catch (e) {
    toast(e.message, 'err');
  } finally {
    btn.disabled = false;
  }
});

$('#app-filter').addEventListener('input', renderApps);

/* ───────────────────────────── APK install ───────────────────────────── */
const dropzone = $('#dropzone');

['dragenter', 'dragover'].forEach((type) => {
  dropzone.addEventListener(type, (event) => {
    event.preventDefault();
    dropzone.classList.add('hover');
  });
});

['dragleave', 'drop'].forEach((type) => {
  dropzone.addEventListener(type, (event) => {
    event.preventDefault();
    dropzone.classList.remove('hover');
  });
});

dropzone.addEventListener('drop', (event) => {
  const file = event.dataTransfer?.files?.[0];
  if (file) uploadApk(file);
});

$('#btn-browse').addEventListener('click', () => $('#apk-input').click());
$('#apk-input').addEventListener('change', (event) => {
  const file = event.target.files?.[0];
  if (file) uploadApk(file);
  event.target.value = '';
});

// XHR rather than fetch: it is the only way to get real upload progress.
function uploadApk(file) {
  if (!file.name.toLowerCase().endsWith('.apk')) {
    toast('Only .apk files can be installed.', 'err');
    return;
  }

  const box = $('#upload-progress');
  const bar = $('#upload-bar');
  const log = $('#install-log');

  box.hidden = false;
  log.hidden = true;
  bar.className = 'progress-bar';
  bar.style.width = '0%';
  $('#upload-name').textContent = `${file.name} (${(file.size / 1048576).toFixed(1)} MB)`;
  $('#upload-pct').textContent = '0%';

  const form = new FormData();
  form.append('file', file, file.name);

  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/api/install');
  xhr.setRequestHeader('Authorization', `Bearer ${state.token}`);

  xhr.upload.addEventListener('progress', (event) => {
    if (!event.lengthComputable) return;
    const pct = Math.round((event.loaded / event.total) * 100);
    bar.style.width = `${pct}%`;
    $('#upload-pct').textContent = pct < 100 ? `${pct}%` : 'installing…';
  });

  xhr.addEventListener('load', () => {
    let payload = {};
    try { payload = JSON.parse(xhr.responseText); } catch (_) { /* ignore */ }

    if (xhr.status >= 200 && xhr.status < 300) {
      bar.className = 'progress-bar done';
      bar.style.width = '100%';
      $('#upload-pct').textContent = 'done';
      log.hidden = false;
      log.textContent = payload.output || 'Success';
      toast(`Installed ${file.name}`, 'ok');
      $('#btn-load-apps').click();
    } else {
      bar.className = 'progress-bar fail';
      $('#upload-pct').textContent = 'failed';
      log.hidden = false;
      log.textContent = payload.detail || `HTTP ${xhr.status}`;
      toast(payload.detail || `Install failed (HTTP ${xhr.status})`, 'err', 8000);
      if (xhr.status === 401) lock('Session rejected.');
    }
  });

  xhr.addEventListener('error', () => {
    bar.className = 'progress-bar fail';
    $('#upload-pct').textContent = 'network error';
    toast('Upload failed: network error.', 'err');
  });

  xhr.send(form);
}

/* ───────────────────────────── logcat (SSE) ───────────────────────────── */
const logcatBox = $('#logcat-box');
const LOGCAT_MAX_NODES = 2000;

function logcatAppend(text, cssClass) {
  const line = document.createElement('div');
  line.className = cssClass || '';
  line.textContent = text;

  const pinned = logcatBox.scrollTop + logcatBox.clientHeight >= logcatBox.scrollHeight - 24;
  logcatBox.appendChild(line);

  while (logcatBox.childElementCount > LOGCAT_MAX_NODES) {
    logcatBox.removeChild(logcatBox.firstElementChild);
  }
  if (pinned) logcatBox.scrollTop = logcatBox.scrollHeight;
}

// threadtime format: "MM-DD HH:MM:SS.mmm  PID  TID L TAG: message"
function logcatLevel(line) {
  const match = /^\d\d-\d\d \d\d:\d\d:\d\d\.\d+\s+\d+\s+\d+\s+([VDIWEF])\s/.exec(line);
  return match ? `lg-${match[1]}` : '';
}

function stopLogcat() {
  if (state.logcatSource) {
    state.logcatSource.close();
    state.logcatSource = null;
  }
  $('#btn-logcat').textContent = 'Start stream';
  $('#btn-logcat').classList.remove('ghost');
  $('#btn-logcat').classList.add('primary');
}

function startLogcat() {
  const filters = $('#logcat-filter').value.trim();
  const params = new URLSearchParams({ token: state.token });
  if (filters) params.set('filters', filters);
  if ($('#logcat-clear').checked) params.set('clear', 'true');

  const source = new EventSource(`/api/logcat?${params.toString()}`);
  state.logcatSource = source;

  source.onmessage = (event) => logcatAppend(event.data, logcatLevel(event.data));
  source.addEventListener('status', (event) => logcatAppend(`— ${event.data} —`, 'lg-D'));
  source.addEventListener('error', (event) => logcatAppend(`— error: ${event.data} —`, 'lg-E'));
  source.onerror = () => {
    logcatAppend('— stream disconnected —', 'lg-W');
    stopLogcat();
  };

  $('#btn-logcat').textContent = 'Stop stream';
  $('#btn-logcat').classList.remove('primary');
  $('#btn-logcat').classList.add('ghost');
}

$('#btn-logcat').addEventListener('click', () => {
  if (state.logcatSource) stopLogcat(); else startLogcat();
});

$('#btn-logcat-clear').addEventListener('click', () => { logcatBox.textContent = ''; });

window.addEventListener('beforeunload', () => { stopLogcat(); stopPolling(); });

/* ───────────────────────────── boot ───────────────────────────── */
(async function init() {
  let stored = null;
  try { stored = sessionStorage.getItem(TOKEN_KEY); } catch (_) { /* private mode */ }
  if (!stored) return;

  try {
    const device = await connect(stored);
    unlock(stored, device, true);
  } catch (_) {
    try { sessionStorage.removeItem(TOKEN_KEY); } catch (_) { /* ignore */ }
  }
})();
