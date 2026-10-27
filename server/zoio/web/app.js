// Zóio: app web (PWA). Sem build, sem dependências: o player vem do go2rtc via /live/.

const $ = (sel) => document.querySelector(sel);
const state = { config: null, cameras: [], liveEls: new Map(), alertsUnread: 0, oldestAlert: null };

const store = {
  get(key, fallback) { try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; } },
  set(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* modo privado */ } },
};

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: opts.body ? { 'Content-Type': 'application/json' } : undefined,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) throw new Error((await res.text()) || res.statusText);
  return res.json();
}

function toast(text, ms = 3500) {
  const el = $('#toast');
  el.textContent = text;
  el.hidden = false;
  clearTimeout(toast.t);
  toast.t = setTimeout(() => { el.hidden = true; }, ms);
}

const pad = (n) => String(n).padStart(2, '0');
const today = () => { const d = new Date(); return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`; };
const hhmm = (dvrTime) => dvrTime.slice(11, 16);
const mb = (bytes) => bytes ? `${(bytes / 1048576).toFixed(bytes > 1e8 ? 0 : 1)} MB` : '';
const camName = (n) => state.cameras.find((c) => c.number === n)?.name ?? `Câmera ${n}`;
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null && v !== false) node.setAttribute(k, v === true ? '' : v);
  }
  node.append(...children.filter((c) => c !== null && c !== undefined));
  return node;
}

// ------------------------------------------------------------------ ao vivo

let playerReady = null;
function loadPlayer() {
  playerReady ??= import('/live/video-stream.js').catch((err) => {
    toast('Não consegui carregar o player ao vivo. O go2rtc está rodando no Pi?');
    throw err;
  });
  return playerReady;
}

function makeStream(src, { controls }) {
  const v = document.createElement('video-stream');
  // MSE e HLS passam pelo mesmo endereço HTTPS do Zóio; nada de portas extras.
  v.mode = 'mse,hls';
  v.background = false;
  v.visibilityCheck = true; // para de puxar vídeo quando a câmera sai da tela
  queueMicrotask(() => {
    if (v.video) { v.video.controls = controls; v.video.muted = true; }
    v.src = `/live/ws?src=${encodeURIComponent(src)}`;
  });
  return v;
}

async function renderGrid() {
  await loadPlayer();
  const grid = $('#grid');
  if (grid.childElementCount) return;
  for (const cam of state.cameras) {
    const tile = el('div', { class: 'tile', 'data-cam': cam.number, onclick: () => { location.hash = `#camera/${cam.number}`; } },
      el('span', { class: 'label' }, cam.name));
    grid.append(tile);
    tile.prepend(makeStream(cam.sub, { controls: false }));
    state.liveEls.set(cam.number, tile);
  }
}

function setCols(cols) {
  document.documentElement.style.setProperty('--cols', cols);
  for (const b of document.querySelectorAll('#layout-picker button')) b.classList.toggle('on', b.dataset.cols === String(cols));
  store.set('cols', cols);
}

async function renderSingle(number) {
  await loadPlayer();
  const cam = state.cameras.find((c) => c.number === number);
  if (!cam) { location.hash = '#ao-vivo'; return; }
  $('#single-name').textContent = cam.name;
  $('#single-rec').onclick = () => { location.hash = `#gravacoes?cam=${cam.number}`; };
  const box = $('#single-video');
  box.replaceChildren(makeStream(cam.main, { controls: true }));
}

function flashMotion(camera) {
  const tile = state.liveEls.get(camera);
  if (!tile) return;
  tile.classList.add('motion');
  clearTimeout(tile._t);
  tile._t = setTimeout(() => tile.classList.remove('motion'), 15000);
}

// ------------------------------------------------------------------ gravações

function fillCameraSelect(select, selected) {
  select.replaceChildren(...state.cameras.map((c) => el('option', { value: c.number, selected: c.number === selected }, c.name)));
}

async function searchRecordings() {
  const cam = Number($('#rec-camera').value);
  const date = $('#rec-date').value || today();
  const list = $('#rec-list');
  list.replaceChildren(el('li', { class: 'empty' }, 'Buscando no DVR…'));
  try {
    let items = await api(`/api/recordings?camera=${cam}&date=${date}`);
    if ($('#rec-motion').checked) items = items.filter((i) => i.motion);
    if (!items.length) { list.replaceChildren(el('li', { class: 'empty' }, 'Nenhuma gravação nesse dia.')); return; }
    list.replaceChildren(...items.map((it) => el('li', {},
      el('div', { class: 'main' },
        el('div', {}, `${hhmm(it.begin)} – ${hhmm(it.end)} `, it.motion ? el('span', { class: 'tag' }, 'movimento') : null),
        el('div', { class: 'sub' }, mb(it.size))),
      el('button', { class: 'ghost', onclick: () => openClip(it) }, 'Assistir'))));
  } catch (err) {
    list.replaceChildren(el('li', { class: 'empty' }, `Erro: ${err.message}`));
  }
}

let clipPoll = null;
async function openClip(item) {
  clearTimeout(clipPoll);
  const box = $('#player');
  const status = $('#player-status');
  const video = $('#player-video');
  const link = $('#player-download');
  box.hidden = false;
  video.hidden = true;
  link.hidden = true;
  video.removeAttribute('src');
  box.scrollIntoView({ behavior: 'smooth', block: 'start' });
  const bar = el('div', {});
  status.replaceChildren(el('div', {}, `${camName(item.camera)} · ${item.begin.slice(0, 16)}`), el('div', { class: 'sub muted' }, 'Pedindo ao DVR…'), el('div', { class: 'progress' }, bar));

  const show = (job) => {
    const pct = job.total ? Math.min(100, Math.round((job.received / job.total) * 100)) : null;
    bar.style.width = `${pct ?? 5}%`;
    status.children[1].textContent = {
      'na fila': 'Na fila (outro download em andamento)…',
      baixando: `Baixando do DVR… ${pct !== null ? pct + '%' : mb(job.received)}`,
      convertendo: 'Preparando o vídeo…',
      pronto: 'Pronto',
      erro: `Erro: ${job.error}`,
    }[job.state] ?? job.state;
    if (job.state === 'pronto') {
      status.querySelector('.progress')?.remove();
      video.src = job.url;
      video.hidden = false;
      link.href = `${job.url}?download=1`;
      link.hidden = false;
      video.play().catch(() => {});
      video.onerror = () => { status.children[1].textContent = 'Este navegador não toca H.265. Use o botão Baixar e abra no VLC.'; };
    } else if (job.state !== 'erro') {
      clipPoll = setTimeout(async () => show(await api(`/api/clips/${job.id}`)), 1000);
    }
  };
  try {
    show(await api('/api/clips', { method: 'POST', body: { camera: item.camera, file: item.file, begin: item.begin, end: item.end, size: item.size } }));
  } catch (err) {
    status.children[1].textContent = `Erro: ${err.message}`;
  }
}

function renderRecordings(params) {
  const cam = Number(params.get('cam')) || Number($('#rec-camera').value) || state.cameras[0]?.number;
  fillCameraSelect($('#rec-camera'), cam);
  if (params.get('date')) $('#rec-date').value = params.get('date');
  if (!$('#rec-date').value) $('#rec-date').value = today();
  if (params.get('cam') || !$('#rec-list').childElementCount) searchRecordings();
}

// ------------------------------------------------------------------ alertas

function alertItem(ev) {
  const when = new Date(ev.received_at * 1000);
  const date = `${when.getFullYear()}-${pad(when.getMonth() + 1)}-${pad(when.getDate())}`;
  return el('li', {},
    el('div', { class: 'main' },
      el('div', {}, ev.name ?? camName(ev.camera), ' ', el('span', { class: 'tag' }, ev.kind === 'VideoMotion' ? 'movimento' : ev.kind)),
      el('div', { class: 'sub' }, when.toLocaleString('pt-BR', { dateStyle: 'short', timeStyle: 'medium' }))),
    el('a', { class: 'ghost', href: `#gravacoes?cam=${ev.camera}&date=${date}` }, 'Gravação'));
}

async function loadAlerts(more = false) {
  const list = $('#alert-list');
  const qs = more && state.oldestAlert ? `?before=${state.oldestAlert}` : '';
  const rows = await api(`/api/events${qs}`);
  if (!more) list.replaceChildren();
  if (!rows.length && !more) list.append(el('li', { class: 'empty' }, 'Nenhum alerta ainda.'));
  list.append(...rows.map(alertItem));
  if (rows.length) state.oldestAlert = rows[rows.length - 1].id;
  $('#alert-more').hidden = rows.length < 100;
}

function setBadge(n) {
  state.alertsUnread = n;
  const b = $('#alert-badge');
  b.hidden = n === 0;
  b.textContent = n > 99 ? '99+' : n;
}

function listenEvents() {
  const src = new EventSource('/api/events/stream');
  src.onmessage = (msg) => {
    const ev = JSON.parse(msg.data);
    if (ev.type !== 'motion') return;
    if (ev.status === 'Start') {
      flashMotion(ev.camera);
      const list = $('#alert-list');
      list.querySelector('.empty')?.remove();
      if (list.childElementCount) list.prepend(alertItem(ev));
      if (currentView !== 'alerts') setBadge(state.alertsUnread + 1);
    }
  };
  src.onerror = () => updateStatus();
}

// ------------------------------------------------------------------ notificações

function b64ToBytes(b64) {
  const s = atob(b64.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - (b64.length % 4)) % 4));
  return Uint8Array.from(s, (c) => c.charCodeAt(0));
}

async function pushSubscription() {
  if (!('serviceWorker' in navigator) || !('PushManager' in window)) return null;
  const reg = await navigator.serviceWorker.ready;
  return reg.pushManager.getSubscription();
}

async function renderPush() {
  const info = $('#push-info');
  const supported = 'serviceWorker' in navigator && 'PushManager' in window && state.config.push_key;
  const sub = supported ? await pushSubscription() : null;
  const isIOS = /iPhone|iPad/.test(navigator.userAgent);
  const standalone = matchMedia('(display-mode: standalone)').matches || navigator.standalone;
  if (!supported) {
    info.textContent = isIOS && !standalone
      ? 'No iPhone, primeiro adicione o Zóio à Tela de Início (Compartilhar › Adicionar à Tela de Início) e abra por lá.'
      : 'Este navegador não aceita notificações (é preciso abrir pelo endereço https do Tailscale).';
    $('#push-on').hidden = true;
    return;
  }
  const chosen = new Set(store.get('pushCameras', state.cameras.map((c) => c.number)));
  $('#push-cameras').replaceChildren(...state.cameras.map((c) => el('label', {},
    el('input', { type: 'checkbox', value: c.number, checked: chosen.has(c.number) }), c.name)));
  info.textContent = sub ? 'Ativas neste aparelho. Marque as câmeras que devem avisar.' : 'Receba um aviso quando uma câmera detectar movimento.';
  $('#push-on').textContent = sub ? 'Salvar câmeras' : 'Ativar neste aparelho';
  $('#push-test').hidden = !sub;
  $('#push-off').hidden = !sub;
}

async function enablePush() {
  try {
    const perm = await Notification.requestPermission();
    if (perm !== 'granted') { toast('Permissão de notificação negada.'); return; }
    const reg = await navigator.serviceWorker.ready;
    const sub = (await reg.pushManager.getSubscription())
      ?? await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToBytes(state.config.push_key) });
    const cameras = [...document.querySelectorAll('#push-cameras input:checked')].map((i) => Number(i.value));
    store.set('pushCameras', cameras);
    await api('/api/push/subscribe', { method: 'POST', body: { subscription: sub.toJSON(), cameras } });
    toast('Notificações ativadas.');
  } catch (err) {
    toast(`Não deu para ativar: ${err.message}`);
  }
  renderPush();
}

async function disablePush() {
  const sub = await pushSubscription();
  if (sub) {
    await api('/api/push/unsubscribe', { method: 'POST', body: { endpoint: sub.endpoint } }).catch(() => {});
    await sub.unsubscribe();
  }
  toast('Notificações desativadas.');
  renderPush();
}

// ------------------------------------------------------------------ ajustes

function hevcSupport() {
  const mse = window.ManagedMediaSource || window.MediaSource;
  return !!mse && mse.isTypeSupported('video/mp4; codecs="hvc1.1.6.L153.B0"');
}

async function renderSettings() {
  renderPush();
  const kv = (k, v) => el('li', {}, el('span', {}, k), el('span', {}, v));
  $('#device-info').replaceChildren(
    kv('Vídeo H.265 ao vivo', hevcSupport() ? 'suportado' : 'não suportado (peça H.264 no fluxo secundário do DVR)'),
    kv('Usuário Tailscale', state.config.user ?? '—'));
  try {
    const s = await api('/api/status');
    $('#server-info').replaceChildren(
      kv('Ao vivo (go2rtc)', s.live ? 'ok' : 'parado'),
      kv('Alertas do DVR', s.alerts ? 'conectado' : (s.alerts_error ?? 'desconectado')),
      kv('Último alerta', s.last_event_at ? new Date(s.last_event_at * 1000).toLocaleString('pt-BR') : '—'),
      kv('Versão', state.config.version));
  } catch (err) {
    $('#server-info').replaceChildren(kv('Servidor', `erro: ${err.message}`));
  }
}

async function updateStatus() {
  const dot = $('#status');
  try {
    const s = await api('/api/status');
    dot.className = `status ${s.live && s.alerts ? 'ok' : s.live ? 'warn' : 'bad'}`;
    dot.title = s.live ? (s.alerts ? 'Tudo ok' : `Alertas: ${s.alerts_error}`) : 'Ao vivo parado';
  } catch {
    dot.className = 'status bad';
    dot.title = 'Sem conexão com o Pi';
  }
}

// ------------------------------------------------------------------ navegação

let currentView = null;
function route() {
  const [path, query = ''] = location.hash.slice(1).split('?');
  const params = new URLSearchParams(query);
  let view = { 'ao-vivo': 'live', gravacoes: 'recordings', alertas: 'alerts', ajustes: 'settings' }[path] ?? 'live';
  if (path.startsWith('camera/')) view = 'single';

  // Sair da tela cheia desliga o fluxo principal (o mais pesado).
  if (currentView === 'single' && view !== 'single') $('#single-video').replaceChildren();
  currentView = view;
  for (const sec of document.querySelectorAll('.view')) sec.hidden = sec.id !== `view-${view}`;
  for (const a of document.querySelectorAll('.tabs a')) a.classList.toggle('on', a.dataset.view === view || (view === 'single' && a.dataset.view === 'live'));
  document.body.classList.toggle('live', view === 'live');

  if (view === 'live') renderGrid();
  if (view === 'single') renderSingle(Number(path.split('/')[1]));
  if (view === 'recordings') renderRecordings(params);
  if (view === 'alerts') { setBadge(0); loadAlerts().catch((e) => toast(e.message)); }
  if (view === 'settings') renderSettings();
}

async function init() {
  if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});
  try {
    state.config = await api('/api/config');
  } catch (err) {
    document.body.innerHTML = `<p style="padding:24px">Não consegui falar com o Zóio: ${err.message}</p>`;
    return;
  }
  state.cameras = state.config.cameras;
  setCols(store.get('cols', window.innerWidth >= 900 ? 4 : 2));
  $('#layout-picker').addEventListener('click', (e) => { if (e.target.dataset.cols) setCols(Number(e.target.dataset.cols)); });
  $('#single-back').onclick = () => { history.length > 1 ? history.back() : (location.hash = '#ao-vivo'); };
  $('#rec-form').addEventListener('submit', (e) => { e.preventDefault(); searchRecordings(); });
  $('#alert-more').onclick = () => loadAlerts(true);
  $('#push-on').onclick = enablePush;
  $('#push-off').onclick = disablePush;
  $('#push-test').onclick = async () => {
    const sub = await pushSubscription();
    if (sub) api('/api/push/test', { method: 'POST', body: { subscription: { endpoint: sub.endpoint } } }).then((r) => toast(r.ok ? 'Enviado.' : 'Falhou.'));
  };
  window.addEventListener('hashchange', route);
  route();
  listenEvents();
  updateStatus();
  setInterval(updateStatus, 30000);
}

init();
