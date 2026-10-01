const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const bridgeSource = fs.readFileSync(process.argv[2], 'utf8');

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function createEnvironment() {
  const events = new Map();
  const sent = [];
  const apiReads = [];
  const fallbacks = [];
  const nativeClipboard = { value: '' };
  const parent = { postMessage() {} };
  const elements = new Map();
  function makeElement(tag) {
    return {
      tagName: tag.toUpperCase(), style: {}, children: [], listeners: {}, value: '',
      setAttribute() {}, appendChild(child) { this.children.push(child); child.parentNode = this; },
      addEventListener(name, fn) { this.listeners[name] = fn; },
      focus() { document.activeElement = this; },
      closest() { return null; },
    };
  }
  const document = {
    visibilityState: 'visible', hasFocus: () => true, referrer: 'https://app.example/',
    body: { appendChild(node) { node.parentNode = this; } },
    head: { appendChild(node) { node.parentNode = this; } }, activeElement: null,
    getElementById(id) { return elements.get(id) || null; },
    createElement(tag) { return makeElement(tag); },
    addEventListener(name, fn) { events.set(`doc:${name}`, fn); },
  };
  const canvas = makeElement('canvas'); canvas.id = 'noVNC_container';
  const panel = makeElement('div'); panel.id = 'noVNC_clipboard';
  const textarea = makeElement('textarea'); textarea.id = 'noVNC_clipboard_text';
  elements.set('noVNC_container', canvas); elements.set('noVNC_clipboard', panel);
  elements.set('noVNC_clipboard_text', textarea);
  const navigator = {
    userAgent: 'Chrome/140.0',
    clipboard: {
      read() { const item = deferred(); apiReads.push(item); return item.promise; },
      readText: async () => nativeClipboard.value,
      write: async () => {}, writeText: async text => { nativeClipboard.value = text; },
    },
  };
  const window = {
    parent, location: { origin: 'https://vnc.example' }, isSecureContext: true, listeners: {},
    addEventListener(name, fn) { events.set(`window:${name}`, fn); this.listeners[name] = fn; },
    setTimeout,
  };
  const rfbMessages = { sendBinaryClipboard(_sock, dataset, mimes) {
    sent.push({ data: dataset.map(value => Array.from(value)), mimes: mimes.slice() });
  } };
  function RFB() {}
  RFB.messages = rfbMessages;
  RFB.prototype._focusCanvas = function () {
    if (this._resendClipboardNextUserDrivenEvent) this.checkLocalClipboard();
  };
  RFB.prototype.checkLocalClipboard = function () {};
  RFB.prototype.clipboardPasteFrom = function (text) {
    if (this._rfbConnectionState !== 'connected' || this.viewOnly || !text) return;
    const data = new TextEncoder().encode(text);
    this._clipHash = hash(data);
    RFB.messages.sendBinaryClipboard(this._sock, [data], ['text/plain']);
  };
  RFB.prototype.clipboardPasteDataFrom = async function (items) {
    const generation = window.__opencuriaClipboardCapture();
    this._clipHash = 123;
    const dataset = []; const mimes = [];
    for (const item of items) for (const mime of item.types) {
      if (!['image/png', 'text/plain', 'text/html'].includes(mime)) continue;
      const blob = await item.getType(mime);
      const buffer = await blob.arrayBuffer();
      dataset.push(new Uint8Array(buffer)); mimes.push(mime);
    }
    if (!window.__opencuriaClipboardAllowed(this, generation)) { this._clipHash = 0; return false; }
    if (dataset.length) RFB.messages.sendBinaryClipboard(this._sock, dataset, mimes);
    return dataset.length > 0;
  };
  RFB.prototype._write_binary_clipboard = function () {};
  RFB.prototype._proxyRFBMessage = function () {};
  function Keyboard() {}
  Keyboard.prototype._handleKeyDown = function (event) {
    this.keyEvents.push(`down:${event.code}`);
    if (event.code && event.key !== 'Unidentified') this._keyDownList[event.code] = event.key;
  };
  Keyboard.prototype._handleKeyUp = function (event) {
    this.keyEvents.push(`up:${event.code}`); delete this._keyDownList[event.code];
  };
  Keyboard.prototype._allKeysUp = function () {
    for (const code of Object.keys(this._keyDownList)) {
      this.keyEvents.push(`up:${code}`); delete this._keyDownList[code];
    }
  };
  const nativePanelText = { value: '' };
  const UI = { rfb: null,
    clipboardReceive(event) { nativePanelText.value = event.detail.text; },
    receiveMessage() {}, clipboardRx() {}, clipboardSend() { this.rfb.clipboardPasteFrom(textarea.value); },
  };
  function hash(bytes) { return Array.from(bytes).reduce((sum, byte) => sum + byte, 0) || 1; }
  const context = { window, document, navigator, URL, Promise, ClipboardItem: function () {},
    Uint8Array, TextEncoder, rfb_RFB: RFB, keyboard_Keyboard: Keyboard, UI, hashUInt8Array: hash };
  vm.runInNewContext(bridgeSource, context, { filename: 'native_kasm_clipboard.js' });
  const originalPost = parent.postMessage;
  parent.postMessage = message => {
    if (message?.kind === 'fallback') fallbacks.push(message.reason);
    originalPost.call(parent, message);
  };
  window.listeners.focus();
  const keyboard = new Keyboard(); keyboard.keyEvents = []; keyboard._keyDownList = {};
  const rfb = Object.create(RFB.prototype);
  Object.assign(rfb, { _rfbConnectionState: 'connected', clipboardSeamless: true, clipboardUp: true,
    clipboardDown: true, clipboardBinary: true, _resendClipboardNextUserDrivenEvent: true,
    _clipHash: 0, _isPrimaryDisplay: true, viewOnly: false, _sock: {}, _keyboard: keyboard });
  UI.rfb = rfb;
  window.isSecureContext = true;
  function contextMessage(sequence, changes = {}) {
    events.get('window:message')({ source: parent, origin: 'https://app.example', data: Object.assign({
      action: 'opencuria_clipboard', version: 1, kind: 'context', sequence,
      workspaceId: 'workspace-a', enabled: true, connected: true, visible: true,
      parentFocused: true, computerUseActive: false,
    }, changes) });
  }
  function message(kind, sequence = 1) {
    events.get('window:message')({ source: parent, origin: 'https://app.example',
      data: { action: 'opencuria_clipboard', version: 1, kind, sequence } });
  }
  function key(key, code, modifiers = {}) {
    let prevented = false;
    return { key, code, ...modifiers, target: canvas, preventDefault() { prevented = true; },
      stopImmediatePropagation() {}, get prevented() { return prevented; } };
  }
  return { events, apiReads, sent, nativeClipboard, nativePanelText, textarea, panel, elements,
    window, document, RFB, Keyboard, UI, rfb, keyboard, contextMessage, message, key, fallbacks };
}

function item(mime, value, getTypeGate) {
  const bytes = typeof value === 'string' ? new TextEncoder().encode(value) : value;
  return { types: [mime], async getType() {
    if (getTypeGate) await getTypeGate.promise;
    return { size: bytes.length, async arrayBuffer() {
      return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
    } };
  } };
}
async function settle() { await new Promise(resolve => setImmediate(resolve)); }

async function testPendingReadFencesPauseResumeAndDeduplicates() {
  const env = createEnvironment(); env.rfb.checkLocalClipboard();
  assert.equal(env.apiReads.length, 0, 'must not read before an active surface');
  env.contextMessage(1); env.rfb._focusCanvas({}); env.rfb.checkLocalClipboard();
  assert.equal(env.apiReads.length, 1, 'focus resend must start one read');
  const mimeGate = deferred();
  env.apiReads[0].resolve([item('text/plain', 'stale', mimeGate)]); await settle();
  env.contextMessage(2, { enabled: false, visible: false, parentFocused: false });
  env.contextMessage(3, { enabled: true, visible: true, parentFocused: true });
  mimeGate.resolve(); await settle();
  assert.equal(env.sent.length, 0, 'stale generator cannot send after pause/resume');
  assert.equal(env.rfb._clipHash, 0, 'stale generator resets hash');
  env.rfb._resendClipboardNextUserDrivenEvent = true; env.rfb.checkLocalClipboard();
  env.apiReads[1].resolve([item('text/plain', 'fresh')]); await settle(); await settle();
  assert.equal(env.sent.length, 1); assert.equal(Buffer.from(env.sent[0].data[0]).toString(), 'fresh');
}

async function testKeyboardQueueAndFallback() {
  const env = createEnvironment(); env.contextMessage(1); const keyboard = env.keyboard;
  keyboard._handleKeyDown(env.key('Control', 'ControlLeft', { ctrlKey: true }));
  const paste = env.key('v', 'KeyV', { ctrlKey: true }); keyboard._handleKeyDown(paste);
  keyboard._handleKeyDown(env.key('x', 'KeyX', { ctrlKey: true }));
  keyboard._handleKeyUp(env.key('x', 'KeyX', { ctrlKey: true }));
  keyboard._handleKeyUp(env.key('Control', 'ControlLeft'));
  assert.equal(paste.prevented, true); assert.deepEqual(keyboard.keyEvents, ['down:ControlLeft']);
  env.apiReads[0].resolve([item('text/plain', 'native key paste')]); await settle(); await settle();
  assert.deepEqual(keyboard.keyEvents, ['down:ControlLeft', 'down:KeyV', 'down:KeyX', 'up:KeyX', 'up:ControlLeft']);
  keyboard._handleKeyDown(env.key('Control', 'ControlLeft', { ctrlKey: true }));
  keyboard._handleKeyDown(env.key('v', 'KeyV', { ctrlKey: true }));
  keyboard._handleKeyUp(env.key('Control', 'ControlLeft'));
  env.apiReads[1].reject(new Error('denied')); await settle(); await settle();
  assert.ok(env.fallbacks.includes('permission'), 'a native read denial offers manual panel fallback');
  assert.equal(keyboard._keyDownList.ControlLeft, undefined);
  assert.equal(env.events.has('window:message'), true);
  env.message('open-panel'); env.textarea.value = 'manual'; env.UI.clipboardSend();
  assert.equal(env.sent.length, 2); assert.equal(Buffer.from(env.sent[1].data[0]).toString(), 'manual');
}

async function testEmptyUnsupportedAndNativeClipboardPanelKeyboard() {
  const env = createEnvironment(); env.contextMessage(1); env.window.listeners.focus();
  env.rfb._resendClipboardNextUserDrivenEvent = true;
  const unsupported = await env.rfb.clipboardPasteDataFrom([{ types: ['application/octet-stream'], getType: async () => ({}) }]);
  assert.equal(unsupported, false, 'unsupported local MIME cannot reuse/send stale clipboard data');
  env.keyboard._handleKeyDown({ key: 'v', code: 'KeyV', ctrlKey: true, target: env.textarea,
    preventDefault() { throw new Error('native clipboard text entry was intercepted'); },
    stopImmediatePropagation() { throw new Error('native clipboard text entry was intercepted'); } });
  env.keyboard._handleKeyUp({ key: 'v', code: 'KeyV', ctrlKey: true, target: env.textarea,
    preventDefault() { throw new Error('native clipboard text entry was intercepted'); },
    stopImmediatePropagation() { throw new Error('native clipboard text entry was intercepted'); } });
  env.document.activeElement = env.canvas;
  assert.equal(env.window.__opencuriaClipboardCanRead(), true, 'active Chromium clipboard path is ready');
}

(async () => {
  await testPendingReadFencesPauseResumeAndDeduplicates();
  await testKeyboardQueueAndFallback();
  await testEmptyUnsupportedAndNativeClipboardPanelKeyboard();
  console.log('native clipboard ordering, fallback, empty MIME and generation fences passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
