'use strict';

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));

// The notification image (the event snapshot) is only used if it comes from our own origin.
function sameOrigin(value) {
  try {
    const url = new URL(value, self.registration.scope);
    return url.origin === self.location.origin ? url.href : undefined;
  } catch (error) { return undefined; }
}

self.addEventListener('push', event => {
  let data = {};
  try { data = event.data.json(); } catch (error) { /* no payload */ }
  const icon = new URL('static/icon-192.png', self.registration.scope).href;
  event.waitUntil(self.registration.showNotification(data.title || 'Videovigilància', {
    body: data.body || '',
    tag: data.tag,
    icon,
    badge: icon,
    image: sameOrigin(data.image),
    data: {url: data.url || self.registration.scope},
  }));
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  // Only URLs of our own origin are opened.
  let target = new URL(self.registration.scope);
  try {
    const url = new URL(event.notification.data.url, self.registration.scope);
    if (url.origin === self.location.origin) target = url;
  } catch (error) { /* fall back to the scope */ }
  event.waitUntil(self.clients.matchAll({type: 'window', includeUncontrolled: true}).then(windows => {
    for (const client of windows) {
      if (client.url.startsWith(self.registration.scope) && 'focus' in client) {
        return client.navigate(target.href).then(c => (c || client).focus());
      }
    }
    return self.clients.openWindow(target.href);
  }));
});
