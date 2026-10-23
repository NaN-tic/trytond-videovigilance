'use strict';
// Live view: draws the latest frame of a camera and sends the movement commands.
(() => {
  const root = document.getElementById('live');
  if (!root) return;
  const canvas = document.getElementById('live-canvas');
  const status = document.getElementById('live-status');
  const message = document.getElementById('live-message');
  const context = canvas.getContext('2d');
  const {csrf, frame, move, stop} = root.dataset;
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

  // Video. One request at a time, each for the newest frame only: it never queues up old
  // images, so the picture stays close to real time even on a slow connection.
  let sequence = 0, watching = false;

  async function watch() {
    if (watching) return;
    watching = true;
    while (!document.hidden) {
      try {
        const response = await fetch(`${frame}?after=${sequence}`,
          {cache: 'no-store', credentials: 'same-origin'});
        // A movement the camera did not carry out is reported by the service afterwards.
        const note = response.headers.get('X-Live-Message');
        if (note) message.textContent = decodeURIComponent(note);
        if (response.status === 200) {
          sequence = Number(response.headers.get('X-Frame-Sequence'));
          const bitmap = await createImageBitmap(await response.blob());
          if (canvas.width !== bitmap.width || canvas.height !== bitmap.height) {
            canvas.width = bitmap.width;
            canvas.height = bitmap.height;
          }
          context.drawImage(bitmap, 0, 0);
          bitmap.close();
          status.hidden = true;
        } else if (response.status === 204) {
          await sleep(120);
        } else if (response.status === 503) {
          status.hidden = false;
          status.textContent = (await response.json()).error || 'Sin señal';
          await sleep(2500);
        } else {
          status.hidden = false;
          status.textContent = response.status === 401 ? 'Sesión caducada: vuelve a entrar'
            : 'Sin acceso a la cámara';
          break;
        }
      } catch (error) {
        status.hidden = false;
        status.textContent = 'Sin conexión';
        await sleep(1500);
      }
    }
    watching = false;
  }

  // Movement. The camera stops by itself a couple of seconds after the last command, so
  // while a button is held the command is renewed, and releasing it sends a stop.
  const speed = document.getElementById('live-speed');
  let renewal = null, held = null;

  async function post(url, values) {
    try {
      const response = await fetch(url, {method: 'POST', credentials: 'same-origin',
        body: new URLSearchParams({csrf, ...values})});
      if (!response.ok) {
        let reason = 'Error';
        try { reason = (await response.json()).error || reason; } catch (error) { /* no body */ }
        message.textContent = reason;
      } else {
        message.textContent = '';
      }
    } catch (error) {
      message.textContent = 'No se pudo enviar la orden';
    }
  }

  function start(dx, dy) {
    if (held) return;
    held = {dx, dy};
    const send = () => post(move, {dx, dy, speed: speed ? speed.value : '0.6'});
    send();
    renewal = setInterval(send, 700);
  }

  function release() {
    if (!held) return;
    held = null;
    clearInterval(renewal);
    post(stop, {});
  }

  for (const control of root.querySelectorAll('button[data-dx]')) {
    const dx = Number(control.dataset.dx), dy = Number(control.dataset.dy);
    control.addEventListener('pointerdown', event => {
      control.setPointerCapture(event.pointerId);
      start(dx, dy);
    });
    for (const name of ['pointerup', 'pointercancel', 'lostpointercapture']) {
      control.addEventListener(name, release);
    }
    control.addEventListener('contextmenu', event => event.preventDefault());
  }
  const stopButton = root.querySelector('button[data-halt]');
  if (stopButton) stopButton.addEventListener('click', () => post(stop, {}));

  const KEYS = {ArrowUp: [0, 1], ArrowDown: [0, -1], ArrowLeft: [-1, 0], ArrowRight: [1, 0]};
  document.addEventListener('keydown', event => {
    if (!(event.key in KEYS) || event.repeat || event.target.tagName === 'SELECT') return;
    event.preventDefault();
    start(...KEYS[event.key]);
  });
  document.addEventListener('keyup', event => {
    if (event.key in KEYS) release();
  });
  // Never leave the camera running if the page loses focus or is hidden.
  window.addEventListener('blur', release);
  window.addEventListener('pagehide', release);
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) release(); else watch();
  });

  watch();
})();
