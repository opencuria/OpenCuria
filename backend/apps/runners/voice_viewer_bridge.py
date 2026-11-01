"""Passive recording bridge injected into the authenticated KasmVNC viewer."""

VOICE_VIEWER_BRIDGE = b"""
(function () {
  var parentOrigin;
  try { parentOrigin = new URL(document.referrer).origin; } catch (error) { return; }
  var busy = false;
  var sessionId = null;
  var expiresAt = 0;
  var lastMove = 0;
  var lastPoint = null;
  var pointerTimer = null;
  var cursorStyle = null;
  var cursorTarget = null;
  var frameWidth = 0;
  var frameHeight = 0;
  var previousFrame = null;
  var frameSequence = 0;
  var observationGeneration = 0;
  var now = function () { return performance.timeOrigin + performance.now(); };
  var canvas = function () {
    // KasmVNC creates auxiliary cursor canvases before the RFB framebuffer.
    // Only the canvas in its desktop container represents the displayed VM.
    return document.querySelector('#noVNC_container > div > canvas');
  };
  var post = function (value) { window.parent.postMessage(value, parentOrigin); };
  var inputSurface = function (element, target) {
    return element === target || (element && element.id === 'noVNC_mouse_capture_elem');
  };
  var showCursor = function (element) {
    if (cursorTarget === element) { return; }
    if (cursorTarget) { cursorTarget.removeAttribute('data-voice-pointing-cursor'); }
    cursorTarget = element;
    if (element) {
      element.setAttribute('data-voice-pointing-cursor', '');
      document.documentElement.setAttribute('data-voice-pointing-active', '');
    } else {
      document.documentElement.removeAttribute('data-voice-pointing-active');
    }
  };
  var sendPoint = function (kind) {
    if (!sessionId || !lastPoint || now() > expiresAt) { return; }
    var target = canvas();
    if (!target || !target.width || !target.height) { showCursor(null); return; }
    var rect = target.getBoundingClientRect();
    var x = (lastPoint.x - rect.left) / rect.width;
    var y = (lastPoint.y - rect.top) / rect.height;
    var inside = rect.width > 0 && rect.height > 0 &&
      x >= 0 && x < 1 && y >= 0 && y < 1;
    if (!inside) { kind = 'leave'; }
    var hit = document.elementFromPoint(lastPoint.x, lastPoint.y);
    showCursor(kind !== 'leave' && cursorStyle &&
      document.visibilityState !== 'hidden' && inputSurface(hit, target) &&
      frameWidth === target.width && frameHeight === target.height ? hit : null);
    post({ type: 'opencuria:voice-pointer', sessionId: sessionId,
      observed_at: now(), event: kind,
      x: kind === 'leave' ? null :
        Math.min(target.width - 1, Math.floor(x * target.width)),
      y: kind === 'leave' ? null :
        Math.min(target.height - 1, Math.floor(y * target.height)),
      width: target.width, height: target.height });
    if (kind === 'leave') { lastPoint = null; }
  };
  var point = function (event) {
    if (!sessionId || now() > expiresAt) { return; }
    var target = canvas();
    if (!target || !target.width || !target.height) { return; }
    if (event.type === 'pointerout') {
      // Kasm temporarily captures dragging on this full-document element.
      // Transfers between it and the canvas do not leave the VM desktop.
      if (!inputSurface(event.target, target) ||
          inputSurface(event.relatedTarget, target)) { return; }
      if (lastPoint) { sendPoint('leave'); }
      return;
    }
    if (!inputSurface(event.target, target)) {
      if (lastPoint) { sendPoint('leave'); }
      return;
    }
    // Retain every position before throttling. The final move in a burst may
    // be followed by seconds of stillness, especially just after mouseup.
    lastPoint = { x: event.clientX, y: event.clientY };
    // Cursor feedback follows every move, independently of recording throttling.
    var rect = target.getBoundingClientRect();
    var inside = lastPoint.x >= rect.left && lastPoint.x < rect.left + rect.width &&
      lastPoint.y >= rect.top && lastPoint.y < rect.top + rect.height;
    showCursor(cursorStyle && inside && document.visibilityState !== 'hidden' &&
      frameWidth === target.width && frameHeight === target.height ?
        event.target : null);
    var kind = event.type === 'pointerdown' ? 'down' :
      event.type === 'pointerup' ? 'up' : 'move';
    var observed = now();
    if (kind === 'move' && observed - lastMove < 100) { return; }
    if (kind === 'move') { lastMove = observed; }
    sendPoint(kind);
  };
  var stopObserver = function () {
    previousFrame = null;
    observationGeneration++;
    showCursor(null);
    if (cursorStyle) { cursorStyle.remove(); cursorStyle = null; }
    document.documentElement.style.removeProperty('--voice-pointing-cursor');
    frameWidth = 0; frameHeight = 0;
    sessionId = null; lastPoint = null;
    if (pointerTimer !== null) { window.clearInterval(pointerTimer); }
    pointerTimer = null;
  };
  var startObserver = function (id, data) {
    if (sessionId !== id) { stopObserver(); lastMove = 0; }
    sessionId = id; expiresAt = now() + 3000;
    frameWidth = data.frameWidth; frameHeight = data.frameHeight;
    if (typeof data.cursor === 'string' && data.cursor.length < 4096) {
      document.documentElement.style.setProperty(
        '--voice-pointing-cursor', data.cursor);
      if (!cursorStyle) {
        cursorStyle = document.createElement('style');
        cursorStyle.textContent = '[data-voice-pointing-cursor] {' +
          'cursor: var(--voice-pointing-cursor) !important; }' +
          // Hide Kasm's auxiliary fallback cursor without changing framebuffer pixels.
          'html[data-voice-pointing-active] body > canvas {' +
          'visibility: hidden !important; }';
        document.head.appendChild(cursorStyle);
      }
    }
    sendPoint('move');
    if (pointerTimer !== null) { return; }
    pointerTimer = window.setInterval(function () {
      if (!sessionId || now() > expiresAt) { stopObserver(); return; }
      sendPoint('move');
    }, 100);
  };
  ['pointermove', 'pointerdown', 'pointerup', 'pointerout'].forEach(function (kind) {
    document.addEventListener(kind, point, { capture: true, passive: true });
  });
  window.addEventListener('message', function (event) {
    var data = event.data;
    if (event.source !== window.parent || event.origin !== parentOrigin || !data) {
      return;
    }
    if (data.type === 'opencuria:voice-observe') {
      if (typeof data.sessionId !== 'string' ||
          !/^[a-f0-9-]{36}$/i.test(data.sessionId)) { return; }
      if (data.enabled === true) { startObserver(data.sessionId, data); }
      else if (data.sessionId === sessionId) { stopObserver(); }
      return;
    }
    if (data.type !== 'opencuria:voice-frame' || typeof data.requestId !== 'string' ||
        data.requestId.length > 80 || busy) { return; }
    var target = canvas();
    var observed = now();
    var width = target ? target.width : 0;
    var height = target ? target.height : 0;
    var generation = observationGeneration;
    var pixels = null;
    var reply = function (blob) {
      busy = false;
      var frameId;
      if (blob && generation === observationGeneration) {
        frameId = String(++frameSequence);
        previousFrame = { id: frameId, width: width, height: height, pixels: pixels };
      }
      post({ type: 'opencuria:voice-frame', requestId: data.requestId,
        blob: blob, width: width, height: height, captured_at: observed,
        frameId: frameId });
    };
    if (!target || !width || !height || width > 32768 || height > 32768) {
      reply(null); return;
    }
    busy = true;
    try {
      // Freeze pixels now. Encoding may run later while the live desktop changes.
      var snapshot = document.createElement('canvas');
      snapshot.width = width; snapshot.height = height;
      var context = snapshot.getContext('2d', { willReadFrequently: true });
      context.drawImage(target, 0, 0);
      pixels = context.getImageData(0, 0, width, height).data;
      // Compare framebuffer pixels, excluding the separate CSS/auxiliary cursor.
      // Only acknowledge the parent's last accepted frame, never a lost reply.
      if (previousFrame && data.previousFrameId === previousFrame.id &&
          width === previousFrame.width && height === previousFrame.height &&
          pixels.length === previousFrame.pixels.length) {
        var unchanged = true;
        for (var i = 0; i < pixels.length; i++) {
          if (pixels[i] !== previousFrame.pixels[i]) { unchanged = false; break; }
        }
        if (unchanged) {
          busy = false;
          post({ type: 'opencuria:voice-frame', requestId: data.requestId,
            unchanged: true, captured_at: observed });
          return;
        }
      }
      snapshot.toBlob(reply, 'image/jpeg', 0.85);
    } catch (error) { reply(null); }
  });
})();
"""
