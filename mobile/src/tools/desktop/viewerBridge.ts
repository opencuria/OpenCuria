/**
 * Bridge between the native desktop panel and the KasmVNC client page that
 * runs in the WebView. The page shows only the framebuffer; all input comes
 * from native gestures and is replayed as ordinary DOM events on KasmVNC's
 * canvas, so KasmVNC keeps owning the RFB protocol, idle tracking and key
 * state exactly as for a desktop browser.
 *
 * Page → native messages are JSON strings (see `DesktopViewerMessage`).
 * Native → page commands are small `window.__opencuriaDesktop.*` calls.
 */

/** Modifier keys held for the next input, in X11 terms. */
export interface DesktopModifiers {
  ctrl: boolean;
  alt: boolean;
  shift: boolean;
  meta: boolean;
}

export const NO_MODIFIERS: DesktopModifiers = Object.freeze({
  ctrl: false,
  alt: false,
  shift: false,
  meta: false,
});

/** A DOM key as KasmVNC's keyboard handler reads it (`key` and `code`). */
export interface DesktopKey {
  key: string;
  code: string;
  keyCode: number;
}

export type DesktopViewerMessage =
  | {
      type: "state";
      state: "connecting" | "connected" | "disconnecting" | "disconnected";
    }
  | { type: "framebuffer"; width: number; height: number }
  | {
      type: "cursor";
      image: string;
      width: number;
      height: number;
      hotX: number;
      hotY: number;
    }
  | { type: "cursor"; image: null };

/** Parse a page message; anything unexpected is ignored. */
export function parseDesktopViewerMessage(
  data: string,
): DesktopViewerMessage | null {
  let message: unknown;
  try {
    message = JSON.parse(data);
  } catch {
    return null;
  }
  if (!message || typeof message !== "object") return null;
  const value = message as Record<string, unknown>;
  if (value.type === "state") {
    return value.state === "connecting" ||
      value.state === "connected" ||
      value.state === "disconnecting" ||
      value.state === "disconnected"
      ? { type: "state", state: value.state }
      : null;
  }
  if (value.type === "framebuffer") {
    const width = Number(value.width);
    const height = Number(value.height);
    return width > 0 && height > 0
      ? { type: "framebuffer", width, height }
      : null;
  }
  if (value.type === "cursor") {
    if (value.image === null) return { type: "cursor", image: null };
    if (
      typeof value.image !== "string" ||
      !value.image.startsWith("data:image/png;base64,") ||
      value.image.length > 200_000
    )
      return null;
    const width = Number(value.width);
    const height = Number(value.height);
    if (!(width > 0 && height > 0)) return null;
    return {
      type: "cursor",
      image: value.image,
      width,
      height,
      hotX: Math.max(0, Number(value.hotX) || 0),
      hotY: Math.max(0, Number(value.hotY) || 0),
    };
  }
  return null;
}

function call(method: string, ...args: unknown[]): string {
  return `window.__opencuriaDesktop&&window.__opencuriaDesktop.${method}(${args
    .map((arg) => JSON.stringify(arg))
    .join(",")});true;`;
}

/** Move the pointer to framebuffer pixel (x, y), holding `buttons`. */
export function pointerCommand(
  x: number,
  y: number,
  buttons: number,
  modifiers: DesktopModifiers = NO_MODIFIERS,
): string {
  return call("pointer", Math.round(x), Math.round(y), buttons, modifiers);
}

/** Scroll by pixel deltas at framebuffer pixel (x, y). */
export function scrollCommand(
  x: number,
  y: number,
  deltaX: number,
  deltaY: number,
  modifiers: DesktopModifiers = NO_MODIFIERS,
): string {
  return call(
    "scroll",
    Math.round(x),
    Math.round(y),
    Math.round(deltaX),
    Math.round(deltaY),
    modifiers,
  );
}

/** Press and release one key while holding `modifiers`. */
export function keyCommand(
  key: DesktopKey,
  modifiers: DesktopModifiers = NO_MODIFIERS,
): string {
  return call("key", key, modifiers);
}

/** Type text; each character is sent as its keysym. */
export function textCommand(
  text: string,
  modifiers: DesktopModifiers = NO_MODIFIERS,
): string {
  return call("text", text, modifiers);
}

/**
 * Runs before KasmVNC's scripts. It reduces the page to the framebuffer,
 * reports connection state, framebuffer size and the remote cursor image,
 * and exposes `window.__opencuriaDesktop` for native input.
 */
export const DESKTOP_VIEWER_BRIDGE = String.raw`(function () {
  if (window.__opencuriaDesktop) { return; }
  var post = function (message) {
    if (window.ReactNativeWebView) {
      window.ReactNativeWebView.postMessage(JSON.stringify(message));
    }
  };
  // KasmVNC remaps keys for macOS/iOS clients. Native code sends X11 keys, so
  // present one stable platform to the viewer.
  try {
    Object.defineProperty(Navigator.prototype, 'platform', {
      configurable: true, get: function () { return 'Linux x86_64'; }
    });
  } catch (error) {}

  var css = 'html,body{margin:0!important;padding:0!important;background:#000!important;' +
    'overflow:hidden!important;touch-action:none!important;' +
    '-webkit-user-select:none!important;user-select:none!important}' +
    'body>*:not(#noVNC_container){display:none!important}' +
    '#noVNC_container{position:fixed!important;left:0!important;top:0!important;' +
    'width:100%!important;height:100%!important;background:#000!important}' +
    '#noVNC_container>div{background:#000!important}' +
    // A shrinking page must not be held wide by the previous canvas size.
    '#noVNC_container canvas{max-width:100%!important;max-height:100%!important}' +
    '#noVNC_keyboardinput{display:none!important}';
  var styled = false;
  var addStyle = function () {
    var root = document.head || document.documentElement;
    if (styled || !root) { return; }
    var style = document.createElement('style');
    style.setAttribute('data-opencuria-mobile', '');
    style.textContent = css;
    root.appendChild(style);
    styled = true;
  };
  addStyle();
  document.addEventListener('DOMContentLoaded', addStyle);
  // Without shrink-to-fit=no WebKit widens the layout viewport to content
  // that overflows while the view shrinks, and the page stays enlarged.
  var fitViewport = function () {
    var meta = document.querySelector('meta[name="viewport"]');
    if (meta && meta.content.indexOf('shrink-to-fit') === -1) {
      meta.content = meta.content + ',shrink-to-fit=no';
    }
  };
  fitViewport();
  document.addEventListener('DOMContentLoaded', fitViewport);

  var screen = function () {
    return document.querySelector('#noVNC_container > div > canvas');
  };

  // --- Connection state and framebuffer size ---
  var lastState = null;
  var lastWidth = 0;
  var lastHeight = 0;
  var reportState = function (repeat) {
    var root = document.documentElement;
    if (!root) { return; }
    var list = root.classList;
    var state = list.contains('noVNC_connected') ? 'connected' :
      list.contains('noVNC_disconnecting') ? 'disconnecting' :
      list.contains('noVNC_disconnected') ? 'disconnected' :
      list.contains('noVNC_connecting') ? 'connecting' : null;
    if (state && (state !== lastState || repeat === true)) {
      lastState = state;
      post({ type: 'state', state: state });
    }
    var canvas = screen();
    if (canvas && canvas.width > 1 && canvas.height > 1 &&
        (canvas.width !== lastWidth || canvas.height !== lastHeight)) {
      lastWidth = canvas.width;
      lastHeight = canvas.height;
      post({ type: 'framebuffer', width: lastWidth, height: lastHeight });
    }
  };

  // --- Remote cursor image (KasmVNC draws it on a body-level canvas) ---
  var cursorCanvas = null;
  var cursorTimer = null;
  var cursorHot = { x: 0, y: 0 };
  var lastClient = null;
  var sendCursor = function () {
    cursorTimer = null;
    if (!cursorCanvas) { return; }
    if (!cursorCanvas.width || !cursorCanvas.height) {
      post({ type: 'cursor', image: null });
      return;
    }
    try {
      post({ type: 'cursor', image: cursorCanvas.toDataURL('image/png'),
        width: cursorCanvas.width, height: cursorCanvas.height,
        hotX: cursorHot.x, hotY: cursorHot.y });
    } catch (error) {}
  };
  var scheduleCursor = function () {
    if (cursorTimer === null) { cursorTimer = setTimeout(sendCursor, 16); }
  };
  var watchCursor = function (canvas) {
    if (cursorCanvas === canvas) { return; }
    cursorCanvas = canvas;
    try {
      var context = canvas.getContext('2d');
      var put = context.putImageData;
      context.putImageData = function () {
        var result = put.apply(this, arguments);
        scheduleCursor();
        return result;
      };
    } catch (error) {}
    scheduleCursor();
  };
  var readHotSpot = function () {
    if (!cursorCanvas || !lastClient) { return; }
    var left = parseFloat(cursorCanvas.style.left);
    var top = parseFloat(cursorCanvas.style.top);
    if (!isFinite(left) || !isFinite(top)) { return; }
    var x = Math.round(lastClient.x - left);
    var y = Math.round(lastClient.y - top);
    if (x < 0 || y < 0 || x > cursorCanvas.width || y > cursorCanvas.height) { return; }
    if (x !== cursorHot.x || y !== cursorHot.y) {
      cursorHot = { x: x, y: y };
      scheduleCursor();
    }
  };

  var observe = function () {
    var root = document.documentElement;
    if (!root) { return false; }
    new MutationObserver(function () { reportState(false); }).observe(root, {
      attributes: true, attributeFilter: ['class'], childList: true, subtree: true
    });
    var findCursor = function () {
      if (!document.body) { return; }
      var children = document.body.children;
      for (var index = 0; index < children.length; index++) {
        if (children[index].tagName === 'CANVAS') { watchCursor(children[index]); }
      }
    };
    new MutationObserver(findCursor).observe(root, { childList: true, subtree: true });
    findCursor();
    reportState(false);
    return true;
  };
  if (!observe()) { document.addEventListener('DOMContentLoaded', observe); }
  // Heartbeat: a remounted or restarted native view learns the state again.
  setInterval(function () { reportState(true); }, 2000);

  // --- Input: ordinary DOM events on KasmVNC's canvas ---
  var buttons = 0;
  var held = { ctrl: false, alt: false, shift: false, meta: false };
  var MODIFIER_KEYS = {
    ctrl: { key: 'Control', code: 'ControlLeft', keyCode: 17 },
    alt: { key: 'Alt', code: 'AltLeft', keyCode: 18 },
    shift: { key: 'Shift', code: 'ShiftLeft', keyCode: 16 },
    meta: { key: 'Meta', code: 'MetaLeft', keyCode: 91 }
  };
  var flags = function () {
    return { ctrlKey: held.ctrl, altKey: held.alt, shiftKey: held.shift, metaKey: held.meta };
  };
  var assign = function (target, source) {
    for (var key in source) { target[key] = source[key]; }
    return target;
  };
  var keyEvent = function (type, key) {
    var canvas = screen();
    if (!canvas) { return; }
    canvas.dispatchEvent(new KeyboardEvent(type, assign({
      key: key.key, code: key.code, keyCode: key.keyCode, which: key.keyCode,
      bubbles: true, cancelable: true
    }, flags())));
  };
  // Press requested modifiers, release the rest (sticky keys end here).
  var setModifiers = function (wanted) {
    wanted = wanted || {};
    ['ctrl', 'alt', 'shift', 'meta'].forEach(function (name) {
      var want = Boolean(wanted[name]);
      if (want === held[name]) { return; }
      held[name] = want;
      keyEvent(want ? 'keydown' : 'keyup', MODIFIER_KEYS[name]);
    });
  };
  var point = function (x, y) {
    var canvas = screen();
    if (!canvas || !canvas.width || !canvas.height) { return null; }
    var rect = canvas.getBoundingClientRect();
    var fx = Math.max(0, Math.min(canvas.width - 1, x));
    var fy = Math.max(0, Math.min(canvas.height - 1, y));
    // Mouse events carry whole CSS pixels (one screen point); round to the
    // nearest instead of letting the browser truncate.
    return {
      canvas: canvas,
      x: Math.round(rect.left + (fx + 0.5) * rect.width / canvas.width),
      y: Math.round(rect.top + (fy + 0.5) * rect.height / canvas.height)
    };
  };
  var BUTTON_INDEX = { 1: 0, 4: 1, 2: 2 };
  var mouse = function (type, at, button) {
    // While KasmVNC captures a drag, a real pointer would hit its capture layer.
    var capture = document.captureElement &&
      document.getElementById('noVNC_mouse_capture_elem');
    var target = type !== 'mousedown' && capture ? capture : at.canvas;
    lastClient = { x: at.x, y: at.y };
    target.dispatchEvent(new MouseEvent(type, assign({
      bubbles: true, cancelable: true,
      clientX: at.x, clientY: at.y, screenX: at.x, screenY: at.y,
      button: button, buttons: buttons
    }, flags())));
  };

  window.__opencuriaDesktop = {
    pointer: function (x, y, nextButtons, modifiers) {
      var at = point(x, y);
      if (!at) { return; }
      setModifiers(modifiers);
      mouse('mousemove', at, 0);
      readHotSpot();
      [1, 2, 4].forEach(function (mask) {
        var was = (buttons & mask) !== 0;
        var now = (nextButtons & mask) !== 0;
        if (was === now) { return; }
        buttons = now ? buttons | mask : buttons & ~mask;
        mouse(now ? 'mousedown' : 'mouseup', at, BUTTON_INDEX[mask]);
      });
      if (!buttons) { setModifiers(null); }
    },
    scroll: function (x, y, deltaX, deltaY, modifiers) {
      var at = point(x, y);
      if (!at) { return; }
      setModifiers(modifiers);
      lastClient = { x: at.x, y: at.y };
      at.canvas.dispatchEvent(new WheelEvent('wheel', assign({
        bubbles: true, cancelable: true,
        clientX: at.x, clientY: at.y, deltaX: deltaX, deltaY: deltaY, deltaMode: 0
      }, flags())));
      setModifiers(null);
    },
    key: function (key, modifiers) {
      setModifiers(modifiers);
      keyEvent('keydown', key);
      keyEvent('keyup', key);
      setModifiers(null);
    },
    text: function (text, modifiers) {
      setModifiers(modifiers);
      Array.from(String(text)).forEach(function (character) {
        // Virtual-keyboard path: KasmVNC sends the character's keysym.
        var key = { key: character === '\n' ? 'Enter' : character,
          code: character === '\n' ? 'Enter' : 'Unidentified', keyCode: 0 };
        keyEvent('keydown', key);
        keyEvent('keyup', key);
      });
      setModifiers(null);
    }
  };
})();
true;`;
