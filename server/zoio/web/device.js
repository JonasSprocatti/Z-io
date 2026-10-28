// Identidade do aparelho: um par de chaves ECDSA P-256 criado aqui mesmo.
// A chave privada é "não exportável": fica guardada pelo navegador (IndexedDB)
// e só serve para assinar. Nem este código consegue ler o conteúdo dela.

const DB_NAME = 'zoio-aparelho';
const STORE = 'identidade';
const KEY = 'atual';

function openDb() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, 1);
    req.onupgradeneeded = () => req.result.createObjectStore(STORE);
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function idb(mode, fn) {
  const db = await openDb();
  try {
    return await new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, mode);
      const req = fn(tx.objectStore(STORE));
      tx.oncomplete = () => resolve(req?.result);
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error);
    });
  } finally {
    db.close();
  }
}

const load = () => idb('readonly', (s) => s.get(KEY));
const save = (rec) => idb('readwrite', (s) => s.put(rec, KEY));

const b64u = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');

export function supported() {
  return window.isSecureContext && !!window.crypto?.subtle && 'indexedDB' in window;
}

export function defaultName() {
  const ua = navigator.userAgent;
  const os = /iPhone/.test(ua) ? 'iPhone' : /iPad/.test(ua) ? 'iPad' : /Android/.test(ua) ? 'Android'
    : /Windows/.test(ua) ? 'Windows' : /Mac OS X/.test(ua) ? 'Mac' : /Linux/.test(ua) ? 'Linux' : 'Aparelho';
  const standalone = matchMedia('(display-mode: standalone)').matches || navigator.standalone;
  const app = standalone ? 'app' : /Edg\//.test(ua) ? 'Edge' : /Firefox\//.test(ua) ? 'Firefox'
    : /Chrome\//.test(ua) ? 'Chrome' : /Safari\//.test(ua) ? 'Safari' : 'navegador';
  return `${os} (${app})`;
}

export async function identity() {
  let rec = await load();
  if (!rec?.keyPair) {
    const keyPair = await crypto.subtle.generateKey({ name: 'ECDSA', namedCurve: 'P-256' }, false, ['sign', 'verify']);
    rec = { keyPair, id: null, code: null, name: defaultName() };
    await save(rec);
  }
  return rec;
}

async function post(path, body) {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body ?? {}),
    credentials: 'same-origin',
  });
  const text = await res.text();
  let data = {};
  try { data = text ? JSON.parse(text) : {}; } catch { data = { error: text }; }
  return { status: res.status, data };
}

export async function register(rec, name) {
  const jwk = await crypto.subtle.exportKey('jwk', rec.keyPair.publicKey);
  const { status, data } = await post('/api/devices/register', {
    public_key: { kty: jwk.kty, crv: jwk.crv, x: jwk.x, y: jwk.y },
    name: name ?? rec.name,
  });
  if (status === 200) {
    Object.assign(rec, { id: data.id, code: data.code, name: name ?? rec.name });
    await save(rec);
  }
  return { status: data.status ?? (status === 200 ? 'pending' : 'error'), code: data.code, error: status >= 400 ? data : null };
}

// Assina um desafio de uso único. Se o aparelho estiver aprovado, o servidor
// devolve um cookie de sessão (HttpOnly, invisível para o JavaScript).
export async function login(rec) {
  const ch = await post('/api/devices/challenge');
  if (ch.status !== 200) return { status: 'error' };
  const message = new TextEncoder().encode(`zoio-login\n${ch.data.nonce}\n${rec.id}`);
  const signature = await crypto.subtle.sign({ name: 'ECDSA', hash: 'SHA-256' }, rec.keyPair.privateKey, message);
  const { status, data } = await post('/api/devices/login', { id: rec.id, nonce: ch.data.nonce, signature: b64u(signature) });
  if (status === 200 && data.status === 'approved') return { status: 'approved', device: data.device };
  return { status: data.status ?? 'error', code: data.code };
}

export async function rename(rec, name) {
  rec.name = name;
  await save(rec);
}

// Cada comando da portaria pede uma assinatura nova, além da sessão: só o
// aparelho que guarda a chave consegue, cada desafio vale uma vez, e a porta e
// o comando entram no que é assinado.
export async function challenge() {
  const ch = await post('/api/portaria/challenge');
  return ch.status === 200 ? ch.data.nonce : null;
}

export async function portaria(rec, door, action, nonce) {
  nonce = nonce ?? await challenge();
  if (!nonce) return { status: 403, data: { error: 'sem permissão de portaria' } };
  const message = new TextEncoder().encode(`zoio-portaria\n${nonce}\n${rec.id}\n${door}\n${action}`);
  const signature = await crypto.subtle.sign({ name: 'ECDSA', hash: 'SHA-256' }, rec.keyPair.privateKey, message);
  return post('/api/portaria/act', { door, action, nonce, signature: b64u(signature) });
}
