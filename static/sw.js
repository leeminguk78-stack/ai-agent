// Service worker пульта.
// Задача одна: если агент в Termux не запущен, показать подсказку вместо ошибки браузера.
// Запросы к API не кэшируются — пульт всегда показывает живые данные.
const CACHE = 'agent-v3';
const OFFLINE = ['/offline.html', '/icon-192.png'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(OFFLINE)));
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const r = e.request;
  if (r.method !== 'GET') return;
  if (r.mode === 'navigate') {
    e.respondWith(fetch(r).catch(() => caches.match('/offline.html')));
    return;
  }
  if (OFFLINE.includes(new URL(r.url).pathname)) {
    e.respondWith(fetch(r).catch(() => caches.match(r)));
  }
});
