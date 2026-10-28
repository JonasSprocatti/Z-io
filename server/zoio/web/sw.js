// Service worker do Zóio: notificações (movimento, campainha) e abertura rápida do app.
const CACHE = 'zoio-v5';
const SHELL = ['/', '/web/app.js', '/web/device.js', '/web/app.css', '/web/icons/icon.svg', '/manifest.webmanifest'];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

// Só a "casca" do app vem do cache, e mesmo assim tenta a rede primeiro.
// Vídeo, API e ao vivo nunca são guardados.
self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.origin !== location.origin || !SHELL.includes(url.pathname)) return;
  event.respondWith(fetch(event.request)
    .then((res) => { const copy = res.clone(); caches.open(CACHE).then((c) => c.put(event.request, copy)); return res; })
    .catch(() => caches.match(event.request)));
});

self.addEventListener('push', (event) => {
  let data = {};
  try { data = event.data.json(); } catch { data = { title: 'Zóio', body: event.data?.text() }; }
  event.waitUntil(self.registration.showNotification(data.title || 'Zóio', {
    body: data.body,
    tag: data.tag,
    renotify: true,
    requireInteraction: !!data.urgent, // campainha fica na tela até alguém ver
    vibrate: data.urgent ? [300, 150, 300, 150, 600] : undefined,
    icon: '/web/icons/icon-192.png',
    badge: '/web/icons/icon-192.png',
    data: { url: data.url || (data.camera ? `/#camera/${data.camera}` : '/#alertas') },
  }));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const target = event.notification.data?.url || '/';
  event.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((wins) => {
    for (const w of wins) {
      if (new URL(w.url).origin === location.origin) { w.navigate(target); return w.focus(); }
    }
    return self.clients.openWindow(target);
  }));
});
