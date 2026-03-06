const CACHE_NAME = 'p189-dav-v2';
const STATIC_ASSETS = [
  '/static/manifest.json',
  '/static/icon-192.svg',
  '/static/icon-512.svg',
  'https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css',
  'https://cdn.jsdelivr.net/npm/qrcode-generator@1.4.4/qrcode.min.js'
];

// 安装 Service Worker
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => cache.addAll(STATIC_ASSETS))
      .then(() => self.skipWaiting())
  );
});

// 激活 Service Worker
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(cacheNames => {
      return Promise.all(
        cacheNames
          .filter(name => name !== CACHE_NAME)
          .map(name => caches.delete(name))
      );
    }).then(() => self.clients.claim())
  );
});

// 拦截请求
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin) return;

  // API 和直链：直连网络，不缓存
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/d/')) {
    return event.respondWith(fetch(event.request));
  }

  // 主文档（/、/login）始终走网络，不缓存也不读缓存，避免页面被缓存导致刷新时请求被卸载取消
  if (event.request.mode === 'navigate' && (url.pathname === '/' || url.pathname === '/login')) {
    return event.respondWith(fetch(event.request));
  }

  // 静态资源：缓存优先
  event.respondWith(
    caches.match(event.request)
      .then(response => {
        if (response) {
          fetch(event.request).then(freshResponse => {
            if (freshResponse.ok) {
              caches.open(CACHE_NAME).then(cache => cache.put(event.request, freshResponse));
            }
          }).catch(() => {});
          return response;
        }
        return fetch(event.request).then(response => {
          if (response.ok && event.request.method === 'GET') {
            const cloned = response.clone();
            caches.open(CACHE_NAME).then(cache => cache.put(event.request, cloned));
          }
          return response;
        });
      })
      .catch(() => {
        if (event.request.mode === 'navigate') {
          return caches.match('/').catch(() => fetch(event.request));
        }
        return fetch(event.request);
      })
  );
});
