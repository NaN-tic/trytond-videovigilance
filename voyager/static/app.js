'use strict';
// Only what HTMX cannot do: register the service worker and the push subscription.
(() => {
  const card = document.getElementById('device');
  if (!card) return;
  const base = card.dataset.base, csrf = card.dataset.csrf, key = card.dataset.key;
  // URLs built by Voyager on the server (Endpoint.url()).
  const urls = {subscribe: card.dataset.subscribe, unsubscribe: card.dataset.unsubscribe, test: card.dataset.test};
  const state = document.getElementById('device-state');
  const toggle = document.getElementById('toggle');
  const test = document.getElementById('test');
  let registration = null;

  const supported = 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;

  async function post(url, data) {
    const response = await fetch(url, {
      method: 'POST', credentials: 'same-origin', cache: 'no-store',
      body: new URLSearchParams({...data, csrf})});
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.error || 'Error ' + response.status);
    return body;
  }

  function keyBytes(text) {
    const padded = (text + '='.repeat((4 - text.length % 4) % 4)).replace(/-/g, '+').replace(/_/g, '/');
    return Uint8Array.from(atob(padded), c => c.charCodeAt(0));
  }

  function params(subscription) {
    const json = subscription.toJSON();
    return {endpoint: json.endpoint, p256dh: json.keys.p256dh, auth: json.keys.auth};
  }

  async function refresh() {
    test.hidden = true;
    toggle.disabled = false;
    if (!supported) {
      state.textContent = 'Este navegador no admite avisos. En iPhone o iPad usa «Compartir → Añadir a pantalla de inicio» y abre la web desde ese icono.';
      toggle.hidden = true;
      return;
    }
    if (card.dataset.enabled !== '1') {
      state.textContent = 'El envío de avisos no está activado en el servidor.';
      toggle.hidden = true;
      return;
    }
    const subscription = await registration.pushManager.getSubscription();
    if (subscription && Notification.permission === 'granted') {
      // Send the subscription again in case the server disabled or reassigned it.
      await post(urls.subscribe, params(subscription)).catch(() => {});
      state.textContent = 'Avisos activados en este dispositivo.';
      toggle.textContent = 'Desactivar avisos';
      test.hidden = false;
    } else if (Notification.permission === 'denied') {
      state.textContent = 'Has bloqueado las notificaciones de esta web: actívalas en los ajustes del navegador.';
      toggle.disabled = true;
    } else {
      state.textContent = 'Avisos desactivados en este dispositivo.';
      toggle.textContent = 'Activar avisos';
    }
  }

  async function switchDevice() {
    const existing = await registration.pushManager.getSubscription();
    if (existing) {
      await post(urls.unsubscribe, {endpoint: existing.endpoint});
      await existing.unsubscribe();
    } else {
      if (await Notification.requestPermission() !== 'granted') return refresh();
      const subscription = await registration.pushManager.subscribe({
        userVisibleOnly: true, applicationServerKey: keyBytes(key)});
      await post(urls.subscribe, params(subscription));
    }
    await refresh();
  }

  const fail = error => { state.textContent = error.message; };
  toggle.onclick = () => switchDevice().catch(fail);
  test.onclick = () => post(urls.test, {})
    .then(() => { state.textContent = 'Prueba enviada: debería llegar en unos segundos.'; })
    .catch(fail);

  (async () => {
    try {
      if (supported) registration = await navigator.serviceWorker.register(card.dataset.sw, {scope: base});
      await refresh();
    } catch (error) { fail(error); }
  })();
})();
