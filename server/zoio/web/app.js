// Zóio: app web (PWA). Sem build, sem dependências: o player vem do go2rtc via /live/.

import * as device from '/web/device.js';

const $ = (sel) => document.querySelector(sel);
const state = { config: null, me: null, cameras: [], liveEls: new Map(), alertsUnread: 0, oldestAlert: null, pendingDevices: 0 };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const store = {
  get(key, fallback) { try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; } },
  set(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* modo privado */ } },
};

async function api(path, opts = {}, retry = true) {
  const res = await fetch(path, {
    ...opts,
    headers: opts.body ? { 'Content-Type': 'application/json' } : undefined,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  // Sessão vencida ou aparelho revogado: entra de novo com a chave do aparelho.
  if (res.status === 401 && retry) {
    await ensureSession();
    return api(path, opts, false);
  }
  if (!res.ok) throw new Error((await res.text()) || res.statusText);
  return res.json();
}

// ------------------------------------------------------------------ aparelho

let sessionPromise = null;
function ensureSession() {
  sessionPromise ??= authenticate().finally(() => { sessionPromise = null; });
  return sessionPromise;
}

function showPair(mode, rec) {
  $('#pair').hidden = false;
  $('#pair-wait').hidden = mode !== 'wait';
  $('#pair-revoked').hidden = mode !== 'revoked';
  $('#pair-unsupported').hidden = mode !== 'unsupported';
  if (mode === 'wait' && rec) {
    $('#pair-code').textContent = rec.code ?? '····-····';
    $('#pair-cmd').textContent = `sudo zoio aparelhos aprovar ${rec.code ?? ''}`;
    const name = $('#pair-name');
    if (document.activeElement !== name) name.value = rec.name ?? '';
    $('#pair-save').onclick = async () => {
      const value = name.value.trim();
      if (!value) return;
      await device.rename(rec, value);
      const res = await device.register(rec, value);
      $('#pair-status').textContent = res.error ? 'Não consegui salvar o nome.' : 'Nome salvo. Aguardando aprovação…';
    };
    $('#pair-ios').hidden = !(/iPhone|iPad/.test(navigator.userAgent));
  }
}

async function authenticate() {
  const me = await (await fetch('/api/devices/me')).json();
  if (!me.required || me.device) {
    state.me = me;
    return;
  }
  if (!device.supported()) {
    showPair('unsupported');
    return new Promise(() => {}); // sem chave, não há como seguir
  }
  const rec = await device.identity();
  for (;;) {
    try {
      if (!rec.id) {
        const reg = await device.register(rec);
        if (reg.status === 'revoked') {
          showPair('revoked');
          await sleep(30000);
          continue;
        }
        if (reg.error) throw new Error(reg.error.error ?? 'cadastro recusado');
      }
      const res = await device.login(rec);
      if (res.status === 'approved') {
        $('#pair').hidden = true;
        state.me = { required: true, device: res.device };
        return;
      }
      if (res.status === 'unknown') { // excluído da lista: pede acesso de novo
        rec.id = null;
        continue;
      }
      if (res.status === 'revoked') {
        showPair('revoked');
        await sleep(30000);
        continue;
      }
      if (res.status === 'pending') {
        showPair('wait', rec);
        $('#pair-status').textContent = 'Aguardando aprovação…';
      } else {
        throw new Error(res.status);
      }
    } catch (err) {
      showPair('wait', rec);
      $('#pair-status').textContent = `Sem resposta do Pi (${err.message}). Tentando de novo…`;
    }
    await sleep(4000);
  }
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
  v.visibilityCheck = true; // para quando o app vai para segundo plano
  v.visibilityThreshold = 0.01; // e quando a câmera sai da tela (rolagem)
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

function showPhoto(eventId) {
  const box = $('#lightbox');
  box.querySelector('img').src = `/api/gate/photos/${eventId}.jpg`;
  box.hidden = false;
}

function gateItem(ev, when, date) {
  const d = ev.detail ?? {};
  const ok = ev.status === 'Aberto';
  return el('li', {},
    d.photo ? el('img', { class: 'thumb', src: `/api/gate/photos/${ev.id}.jpg`, alt: 'Foto do portão', loading: 'lazy', onclick: () => showPhoto(ev.id) }) : null,
    el('div', { class: 'main' },
      el('div', {}, ok ? 'Portão aberto ' : 'Portão não abriu ', el('span', { class: ok ? 'tag gate' : 'tag' }, 'portão')),
      el('div', { class: 'sub' }, [`por ${d.by ?? '?'}`, when.toLocaleString('pt-BR', { dateStyle: 'short', timeStyle: 'medium' }), ok ? null : d.error].filter(Boolean).join(' · '))),
    ev.camera ? el('a', { class: 'ghost', href: `#gravacoes?cam=${ev.camera}&date=${date}` }, 'Gravação') : null);
}

function alertItem(ev) {
  const when = new Date(ev.received_at * 1000);
  const date = `${when.getFullYear()}-${pad(when.getMonth() + 1)}-${pad(when.getDate())}`;
  if (ev.kind === 'Portao') return gateItem(ev, when, date);
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
    if (ev.type === 'device') {
      if (ev.status === 'pending') toast(`Aparelho novo pedindo acesso: ${ev.name}. Aprove em Ajustes.`, 6000);
      refreshDevices();
      return;
    }
    if (ev.type === 'gate') {
      const list = $('#alert-list');
      list.querySelector('.empty')?.remove();
      if (list.childElementCount) list.prepend(alertItem(ev));
      if (ev.detail?.device_id !== state.me?.device?.id) {
        toast(ev.status === 'Aberto' ? `Portão aberto por ${ev.detail?.by}.` : `Tentativa de abrir o portão falhou (${ev.detail?.by}).`, 5000);
      }
      return;
    }
    if (ev.type !== 'motion') return;
    if (ev.status === 'Start') {
      flashMotion(ev.camera);
      const list = $('#alert-list');
      list.querySelector('.empty')?.remove();
      if (list.childElementCount) list.prepend(alertItem(ev));
      if (currentView !== 'alerts') setBadge(state.alertsUnread + 1);
    }
  };
  src.onerror = () => {
    updateStatus();
    // Com 401 o navegador desiste da conexão; entra de novo e reabre.
    if (src.readyState === EventSource.CLOSED) {
      setTimeout(() => ensureSession().catch(() => {}).finally(listenEvents), 5000);
    }
  };
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

// ------------------------------------------------------------------ portão

const HOLD_MS = 1200;

function setupGate() {
  const gate = state.config.gate;
  $('#gate-btn').hidden = !(gate?.enabled && gate.allowed);
}

async function openGateSheet() {
  $('#gate').hidden = false;
  resetHold('Segure para abrir', 'Mantenha o botão apertado até completar o círculo.');
  const cam = state.cameras.find((c) => c.number === state.config.gate?.camera);
  const box = $('#gate-video');
  box.replaceChildren();
  if (cam) {
    await loadPlayer().catch(() => {});
    if (!$('#gate').hidden) box.replaceChildren(makeStream(cam.sub, { controls: false }));
  }
}

function closeGateSheet() {
  $('#gate').hidden = true;
  $('#gate-video').replaceChildren(); // desliga o vídeo
}

function resetHold(label, status) {
  const btn = $('#gate-hold');
  btn.classList.remove('done', 'failed');
  btn.style.setProperty('--p', 0);
  btn.disabled = false;
  $('#gate-hold-label').textContent = label;
  if (status !== undefined) $('#gate-status').textContent = status;
}

// Segurar evita abrir o portão com um toque sem querer (no bolso, por exemplo).
function bindHold(btn, action) {
  let start = 0;
  let raf = 0;
  const stop = () => {
    if (!raf) return;
    cancelAnimationFrame(raf);
    raf = 0;
    btn.style.setProperty('--p', 0);
  };
  const tick = (t) => {
    const p = Math.min(1, (t - start) / HOLD_MS);
    btn.style.setProperty('--p', p);
    if (p < 1) { raf = requestAnimationFrame(tick); return; }
    raf = 0;
    action();
  };
  const begin = (e) => {
    if (btn.disabled || raf) return;
    e.preventDefault();
    start = performance.now();
    raf = requestAnimationFrame(tick);
  };
  btn.addEventListener('pointerdown', (e) => { btn.setPointerCapture?.(e.pointerId); begin(e); });
  for (const name of ['pointerup', 'pointercancel', 'lostpointercapture']) btn.addEventListener(name, stop);
  btn.addEventListener('keydown', (e) => { if ((e.key === ' ' || e.key === 'Enter') && !e.repeat) begin(e); });
  btn.addEventListener('keyup', (e) => { if (e.key === ' ' || e.key === 'Enter') stop(); });
  btn.addEventListener('contextmenu', (e) => e.preventDefault());
}

async function doOpenGate() {
  const btn = $('#gate-hold');
  btn.disabled = true;
  $('#gate-hold-label').textContent = 'Abrindo…';
  $('#gate-status').textContent = '';
  let res;
  try {
    const rec = await device.identity();
    res = await device.openGate(rec);
    if (res.status === 401 && res.data?.error === 'aparelho_nao_autorizado') { // sessão vencida
      await ensureSession();
      res = await device.openGate(rec);
    }
  } catch (err) {
    res = { status: 0, data: { error: err.message } };
  }
  if (res.status === 200) {
    btn.classList.add('done');
    $('#gate-hold-label').textContent = 'Aberto';
    const at = new Date(res.data.received_at * 1000).toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' });
    $('#gate-status').textContent = `Portão aberto às ${at}.`;
    navigator.vibrate?.(80);
    setTimeout(() => { if (!$('#gate').hidden) resetHold('Segure para abrir'); }, 6000);
  } else {
    btn.classList.add('failed');
    $('#gate-hold-label').textContent = 'Não abriu';
    $('#gate-status').textContent = `Não consegui abrir: ${res.data?.detail?.error ?? res.data?.error ?? `erro ${res.status}`}`;
    setTimeout(() => { if (!$('#gate').hidden) resetHold('Tentar de novo'); }, 3000);
  }
}

// ------------------------------------------------------------------ ajustes

function hevcSupport() {
  const mse = window.ManagedMediaSource || window.MediaSource;
  return !!mse && mse.isTypeSupported('video/mp4; codecs="hvc1.1.6.L153.B0"');
}

const kv = (k, v) => el('li', {}, el('span', {}, k), el('span', {}, v));
const when = (ts) => ts ? new Date(ts * 1000).toLocaleString('pt-BR', { dateStyle: 'short', timeStyle: 'short' }) : '—';

function setSettingsBadge(n) {
  state.pendingDevices = n;
  const b = $('#settings-badge');
  b.hidden = n === 0;
  b.textContent = n;
}

function shortUA(ua) {
  const os = /iPhone/.test(ua) ? 'iPhone' : /iPad/.test(ua) ? 'iPad' : /Android/.test(ua) ? 'Android'
    : /Windows/.test(ua) ? 'Windows' : /Mac OS X/.test(ua) ? 'Mac' : /Linux/.test(ua) ? 'Linux' : '';
  const nav = /Edg\//.test(ua) ? 'Edge' : /Firefox\//.test(ua) ? 'Firefox' : /Chrome\//.test(ua) ? 'Chrome' : /Safari\//.test(ua) ? 'Safari' : '';
  return [os, nav].filter(Boolean).join(' ');
}

function deviceItem(d) {
  const [cls, label] = {
    pending: ['wait', 'aguardando'],
    approved: ['ok', d.admin ? 'administrador' : 'aprovado'],
    revoked: ['off', 'revogado'],
  }[d.status] ?? ['off', d.status];
  const details = [
    d.status === 'pending' ? `pediu em ${when(d.created_at)}` : `visto em ${when(d.last_seen)}`,
    d.status === 'pending' ? null : `código ${d.code}`, // o código do aparelho novo é conferido na tela dele
    d.user,
    d.user_agent ? shortUA(d.user_agent) : null,
  ].filter(Boolean).join(' · ');
  const act = async (path, question) => {
    if (question && !confirm(question)) return;
    try {
      await api(`/api/devices/${d.id}/${path}`, { method: 'POST', body: {} });
    } catch (err) {
      toast(err.message);
    }
    refreshDevices();
  };
  const actions = [];
  if (d.status === 'pending') actions.push(el('button', { class: 'ghost', onclick: () => act('revoke', `Recusar o pedido de "${d.name}"?`) }, 'Recusar'));
  if (d.status === 'approved' && !d.current) actions.push(el('button', { class: 'ghost', onclick: () => act('revoke', `Revogar "${d.name}"? Ele perde o acesso na hora.`) }, 'Revogar'));
  if (d.status === 'revoked') actions.push(el('button', { class: 'ghost', onclick: () => act('delete', `Excluir "${d.name}"? Ele poderá pedir acesso de novo.`) }, 'Excluir'));
  const gateOn = state.config.gate?.enabled;
  if (gateOn && d.status === 'approved') {
    actions.push(el('button', { class: 'ghost', onclick: () => setGatePermission(d, !d.gate) }, d.gate ? 'Bloquear portão' : 'Liberar portão'));
  }
  return el('li', {},
    el('div', { class: 'main' },
      el('div', {}, d.name, d.current ? ' (este aparelho) ' : ' ', el('span', { class: `tag ${cls}` }, label),
        gateOn && d.status === 'approved' && d.gate ? el('span', { class: 'tag gate' }, 'portão') : null),
      el('div', { class: 'sub' }, details)),
    el('div', { class: 'dev-actions' }, ...actions));
}

async function setGatePermission(d, allow) {
  const question = allow ? `Deixar "${d.name}" abrir o portão?` : `Tirar de "${d.name}" a permissão de abrir o portão?`;
  if (!confirm(question)) return;
  try {
    await api(`/api/devices/${d.id}/gate`, { method: 'POST', body: { allow } });
    if (d.current) {
      state.config = await api('/api/config');
      state.me.device = state.config.device;
      setupGate();
    }
  } catch (err) {
    toast(err.message);
  }
  refreshDevices();
}

function approveForm() {
  const code = el('input', { type: 'text', placeholder: 'ABCD-EFGH', maxlength: 9, autocomplete: 'off', 'aria-label': 'Código do aparelho novo' });
  const admin = el('input', { type: 'checkbox' });
  const gate = el('input', { type: 'checkbox' });
  const gateOn = state.config.gate?.enabled;
  const submit = async (e) => {
    e.preventDefault();
    const body = { code: code.value, admin: admin.checked };
    if (gateOn) body.gate = gate.checked;
    try {
      const d = await api('/api/devices/approve', { method: 'POST', body });
      toast(`Aprovado: ${d.name}.`);
      code.value = '';
    } catch (err) {
      toast(err.message);
    }
    refreshDevices();
  };
  return el('li', { class: 'column' },
    el('form', { onsubmit: submit },
      el('div', {}, 'Aprovar aparelho novo'),
      el('div', { class: 'sub' }, 'Digite o código que aparece na tela dele.'),
      el('div', { class: 'dev-approve' }, code, el('button', { type: 'submit' }, 'Aprovar')),
      el('label', { class: 'check' }, admin, 'pode aprovar outros aparelhos'),
      gateOn ? el('label', { class: 'check' }, gate, 'pode abrir o portão') : null));
}

async function refreshDevices() {
  if (!state.me?.device?.admin) return;
  let items;
  try {
    items = await api('/api/devices');
  } catch (err) {
    $('#device-list').replaceChildren(el('li', { class: 'empty' }, err.message));
    return;
  }
  const pending = items.filter((d) => d.status === 'pending');
  setSettingsBadge(pending.length);
  if (currentView !== 'settings') return;
  $('#device-list').replaceChildren(approveForm(), ...items.map(deviceItem));
}

async function renderSettings() {
  renderPush();
  const me = state.me?.device;
  $('#device-info').replaceChildren(
    ...(me ? [kv('Nome', me.name), kv('Código', me.code), kv('Permissão', me.admin ? 'administrador' : 'comum')] : []),
    ...(me && state.config.gate?.enabled ? [kv('Portão', me.gate ? 'pode abrir' : 'sem permissão')] : []),
    kv('Vídeo H.265 ao vivo', hevcSupport() ? 'suportado' : 'não suportado (peça H.264 no fluxo secundário do DVR)'),
    kv('Usuário Tailscale', state.config.user ?? '—'));
  $('#device-rename').hidden = !me;
  if (me) {
    $('#device-name').value = me.name;
    $('#device-name-save').onclick = async () => {
      const name = $('#device-name').value.trim();
      if (!name) return;
      try {
        const r = await api('/api/devices/rename', { method: 'POST', body: { name } });
        me.name = r.name;
        toast('Nome salvo.');
        renderSettings();
      } catch (err) {
        toast(err.message);
      }
    };
  }
  $('#devices-card').hidden = !me?.admin;
  if (me?.admin) refreshDevices();
  try {
    const s = await api('/api/status');
    $('#server-info').replaceChildren(
      kv('Ao vivo (go2rtc)', s.live ? 'ok' : 'parado'),
      kv('Alertas do DVR', s.alerts ? 'conectado' : (s.alerts_error ?? 'desconectado')),
      kv('Último alerta', s.last_event_at ? new Date(s.last_event_at * 1000).toLocaleString('pt-BR') : '—'),
      kv('Portão', s.gate ? 'configurado' : (s.gate_error ?? 'não configurado')),
      kv('Versão', state.config.version));
    $('#gate-test').hidden = !(s.gate && me?.admin);
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
    await ensureSession();
    state.config = await api('/api/config');
    if (state.config.device) state.me = { required: true, device: state.config.device };
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
  setupGate();
  $('#gate-btn').onclick = openGateSheet;
  $('#gate-close').onclick = closeGateSheet;
  $('#gate').addEventListener('click', (e) => { if (e.target.id === 'gate') closeGateSheet(); });
  bindHold($('#gate-hold'), doOpenGate);
  $('#lightbox').onclick = () => { $('#lightbox').hidden = true; };
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (!$('#lightbox').hidden) $('#lightbox').hidden = true;
    else if (!$('#gate').hidden) closeGateSheet();
  });
  $('#gate-test').onclick = async () => {
    try {
      const r = await api('/api/gate/check', { method: 'POST', body: {} });
      toast(r.button_ok ? `Placa ${r.name} respondeu. Tudo certo.` : `Placa ${r.name} respondeu, mas sem o botão esperado (achei: ${r.buttons.join(', ') || 'nenhum'}).`, 6000);
    } catch (err) {
      toast(`Placa do portão: ${err.message}`, 6000);
    }
  };
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
  refreshDevices();
  // Reenvia a inscrição de push deste navegador: liga a inscrição ao aparelho
  // aprovado e acompanha uma eventual troca de endereço pelo serviço de push.
  pushSubscription().then((sub) => {
    if (sub && state.config.push_key) {
      api('/api/push/subscribe', { method: 'POST', body: { subscription: sub.toJSON(), cameras: store.get('pushCameras', []) } }).catch(() => {});
    }
  }).catch(() => {});
  setInterval(updateStatus, 30000);
}

init();
