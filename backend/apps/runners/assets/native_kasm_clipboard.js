/* Focus-scoped native clipboard bridge for pinned KasmVNC 1.3.3. */
(function () {
  'use strict';

  var ACTION = 'opencuria_clipboard';
  var VERSION = 1;
  var parentOrigin = window.location.origin;
  try {
    if (document.referrer) parentOrigin = new URL(document.referrer).origin;
  } catch (_) {
    return;
  }

  var state = {
    enabled: false,
    connected: false,
    visible: false,
    parentFocused: false,
    computerUseActive: true,
    focused: false,
    workspaceId: '',
    sequence: -1,
    generation: 0,
    panel: null,
    panelOpen: false,
    fallbackReason: '',
  };
  var pendingRead = null;
  var pendingVmWrites = Promise.resolve();

  function post(kind, extra) {
    if (window.parent !== window) {
      window.parent.postMessage(Object.assign({
        action: ACTION,
        version: VERSION,
        kind: kind,
      }, extra || {}), parentOrigin);
    }
  }

  function activeSurface(rfb) {
    rfb = rfb || (typeof UI !== 'undefined' ? UI.rfb : null);
    return Boolean(
      state.enabled && state.connected && state.visible && state.parentFocused &&
      !state.computerUseActive && state.focused &&
      document.visibilityState === 'visible' && document.hasFocus() && rfb &&
      rfb._rfbConnectionState === 'connected'
    );
  }

  function seamless(rfb) {
    return Boolean(activeSurface(rfb) && rfb.clipboardSeamless && !rfb.viewOnly);
  }

  function allowed(rfb, generation) {
    return Boolean(
      seamless(rfb) && rfb.clipboardUp && rfb.clipboardBinary &&
      (generation === undefined || generation === state.generation)
    );
  }

  function canReceive(rfb, generation) {
    return Boolean(
      activeSurface(rfb) && rfb.clipboardDown && !rfb.viewOnly &&
      (generation === undefined || generation === state.generation)
    );
  }

  function canWrite(rfb, generation) {
    return Boolean(
      canReceive(rfb, generation) && rfb.clipboardSeamless &&
      chromiumClipboardAvailable()
    );
  }

  function serializeVmWrite(callback) {
    pendingVmWrites = pendingVmWrites.catch(function () {}).then(callback);
    return pendingVmWrites;
  }

  function chromiumClipboardAvailable() {
    var ua = navigator.userAgent || '';
    var chromium = /(?:Chrome|Chromium|Edg)\//.test(ua) &&
      !/(?:OPR|Opera|EdgA|EdgiOS)\//.test(ua);
    return Boolean(
      chromium && window.isSecureContext && navigator.clipboard &&
      typeof navigator.clipboard.read === 'function' &&
      typeof navigator.clipboard.write === 'function'
    );
  }

  function fallback(reason) {
    if (reason !== 'unsupported' && reason !== 'permission' && reason !== 'unavailable') return;
    if (state.fallbackReason === reason) return;
    state.fallbackReason = reason;
    post('fallback', { reason: reason });
  }

  function writeFallback(reason, rfb, generation) {
    if (!canWrite(rfb, generation)) return;
    fallback(reason);
  }

  function clearNativeClipboard() {
    var textarea = document.getElementById('noVNC_clipboard_text');
    if (textarea) textarea.value = '';
  }

  function setFocused(value) {
    var focused = Boolean(
      value && document.visibilityState === 'visible' && document.hasFocus()
    );
    if (state.focused === focused) return;
    state.focused = focused;
    state.generation += 1;
    if (!focused) state.fallbackReason = '';
    pendingRead = null;
    if (typeof UI !== 'undefined' && UI.rfb) {
      UI.rfb._clipHash = 0;
      if (!focused && UI.rfb._keyboard) cancelPendingKeyboard(UI.rfb._keyboard);
    }
    if (!focused && state.panel) closeNativePanel();
    post('focus', { focused: focused });
  }

  function ensureNativePanel() {
    if (state.panel) return true;
    if (!document.body) return false;
    var nativePanel = document.getElementById('noVNC_clipboard');
    if (!nativePanel) return false;

    var style = document.createElement('style');
    style.setAttribute('data-opencuria-clipboard-style', '');
    style.textContent =
      '#noVNC_control_bar_anchor{display:none!important}' +
      '#opencuria-clipboard-panel{display:none;position:fixed;right:12px;top:12px;z-index:10001;' +
      'width:min(360px,90vw);padding:12px;background:#20242a;color:#fff;' +
      'border:1px solid #718096;border-radius:6px;box-shadow:0 6px 24px #0008;font:14px sans-serif}' +
      '#opencuria-clipboard-panel #noVNC_clipboard{display:block!important;visibility:visible!important;' +
      'opacity:1!important;transform:none!important;width:auto!important;max-height:none!important;' +
      'overflow:visible!important;padding:0!important;background:transparent!important}' +
      '#opencuria-clipboard-panel textarea{box-sizing:border-box;width:100%;min-height:110px;' +
      'color:#111;background:#fff;user-select:text;touch-action:auto}' +
      '#opencuria-clipboard-panel input{margin-top:8px}' +
      '#opencuria-clipboard-close{float:right;background:transparent;color:#fff;border:0;cursor:pointer}';
    document.head.appendChild(style);

    var panel = document.createElement('div');
    panel.id = 'opencuria-clipboard-panel';
    panel.setAttribute('role', 'region');
    panel.setAttribute('aria-label', 'KasmVNC text clipboard');
    var close = document.createElement('button');
    close.id = 'opencuria-clipboard-close';
    close.type = 'button';
    close.textContent = 'Close';
    close.setAttribute('aria-label', 'Close KasmVNC clipboard');
    panel.appendChild(close);
    panel.appendChild(nativePanel);
    document.body.appendChild(panel);
    state.panel = panel;
    close.addEventListener('click', closeNativePanel);
    return true;
  }

  function closeNativePanel() {
    if (!state.panel) return;
    state.panel.style.display = 'none';
    state.panelOpen = false;
  }

  function openNativePanel() {
    if (!ensureNativePanel() || !activeSurface()) return false;
    state.panel.style.display = 'block';
    state.panelOpen = true;
    var textarea = document.getElementById('noVNC_clipboard_text');
    if (textarea) textarea.focus();
    return true;
  }

  function readLocalClipboard(rfb) {
    if (!seamless(rfb) || !rfb.clipboardUp ||
        !rfb._resendClipboardNextUserDrivenEvent) return;
    if (!rfb.clipboardBinary) {
      if (pendingRead && pendingRead.generation === state.generation && pendingRead.promise)
        return pendingRead.promise;
      if (!navigator.clipboard || typeof navigator.clipboard.readText !== 'function') {
        return;
      }
      var textGeneration = state.generation;
      var textRead = { generation: textGeneration, failure: null, changed: false };
      pendingRead = textRead;
      try {
        textRead.promise = navigator.clipboard.readText().then(function (text) {
          if (pendingRead !== textRead || !allowed(rfb, textGeneration)) return false;
          if (!text) return false;
          textRead.changed = true;
          var result = rfb.clipboardPasteFrom(text);
          return Boolean(result) && allowed(rfb, textGeneration);
        }).catch(function () {
          textRead.failure = 'permission';
          if (activeSurface(rfb)) fallback('permission');
          return false;
        }).then(function (sent) {
          if (pendingRead === textRead) pendingRead = null;
          if (allowed(rfb, textGeneration)) rfb._resendClipboardNextUserDrivenEvent = false;
          if (!sent && allowed(rfb, textGeneration) && !textRead.failure && textRead.changed)
            fallback('unavailable');
          return sent;
        });
      } catch (_) {
        if (pendingRead === textRead) pendingRead = null;
      }
      return;
    }
    if (!chromiumClipboardAvailable()) return;
    uploadLocalClipboard(rfb, state.generation, false);
  }

  function uploadLocalClipboard(rfb, generation, explicitGesture) {
    if (!allowed(rfb, generation)) return Promise.resolve(false);
    if (pendingRead && pendingRead.generation === generation && pendingRead.promise)
      return pendingRead.promise;
    var read = { generation: generation, failure: null, noData: false };
    pendingRead = read;
    try {
      read.promise = navigator.clipboard.read().then(function (items) {
        if (pendingRead !== read || !allowed(rfb, generation)) return false;
        read.noData = !(items || []).some(function (item) {
          return Array.prototype.some.call(item.types || [], function (mime) {
            return mime === 'text/plain' || mime === 'text/html' || mime === 'image/png';
          });
        });
        return Promise.resolve(rfb.clipboardPasteDataFrom(items)).then(function (sent) {
          return Boolean(sent) && allowed(rfb, generation);
        });
      }).catch(function () {
        read.failure = 'permission';
        if (activeSurface(rfb)) fallback('permission');
        return false;
      }).then(function (sent) {
        if (pendingRead === read) pendingRead = null;
        if (allowed(rfb, generation)) rfb._resendClipboardNextUserDrivenEvent = false;
        if (!sent && explicitGesture && allowed(rfb, generation) &&
            !read.failure && !read.noData) fallback('unavailable');
        return sent;
      });
      return read.promise;
    } catch (_) {
      if (pendingRead === read) pendingRead = null;
      if (explicitGesture) fallback('unavailable');
      return Promise.resolve(false);
    }
  }

  function cancelPendingKeyboard(keyboard) {
    var pending = keyboard.__opencuriaPastePending;
    if (!pending) return;
    keyboard.__opencuriaPastePending = null;
    pending.queue.length = 0;
    // Release Ctrl/Meta/Shift already sent before the clipboard read started.
    if (typeof keyboard._allKeysUp === 'function') keyboard._allKeysUp();
  }

  function isPasteShortcut(event) {
    var key = String(event.key || '').toLowerCase();
    return (key === 'v' && (event.ctrlKey || event.metaKey) &&
      !event.altKey && !event.shiftKey) ||
      (key === 'insert' && event.shiftKey && !event.ctrlKey &&
      !event.metaKey && !event.altKey) ||
      (key === 'v' && event.ctrlKey && event.shiftKey && !event.metaKey &&
      !event.altKey);
  }

  function releaseQueuedState(keyboard) {
    // None of these queued key events were forwarded; release only physical
    // modifiers that had been sent before the clipboard read began.
    if (typeof keyboard._allKeysUp === 'function') keyboard._allKeysUp();
  }

  function installKeyboardQueue() {
    var prototype = keyboard_Keyboard.prototype;
    var originalDown = prototype._handleKeyDown;
    var originalUp = prototype._handleKeyUp;

    function replay(keyboard, rfb, pending, index) {
      if (keyboard.__opencuriaPastePending !== pending) return;
      if (!allowed(rfb, pending.generation)) {
        cancelPendingKeyboard(keyboard);
        return;
      }
      while (index < pending.queue.length) {
        var entry = pending.queue[index++];
        if (entry.type === 'keydown' && isPasteShortcut(entry.event)) {
          var next = {
            generation: state.generation,
            queue: pending.queue.slice(index),
            pasteEvent: entry.event,
          };
          keyboard.__opencuriaPastePending = next;
          readForPaste(keyboard, rfb, next);
          return;
        }
        if (entry.type === 'keydown') originalDown.call(keyboard, entry.event);
        else originalUp.call(keyboard, entry.event);
        if (!allowed(rfb, pending.generation)) {
          cancelPendingKeyboard(keyboard);
          return;
        }
      }
      keyboard.__opencuriaPastePending = null;
    }

    function readForPaste(keyboard, rfb, pending) {
      if (!chromiumClipboardAvailable()) {
        keyboard.__opencuriaPastePending = null;
        fallback('unsupported');
        releaseQueuedState(keyboard);
        return;
      }
      // This read is synchronous with the trusted paste key event.
      uploadLocalClipboard(rfb, pending.generation, true).catch(function () { return false; }).then(function (sent) {
        if (keyboard.__opencuriaPastePending !== pending) return;
        if (!sent || !allowed(rfb, pending.generation)) {
          keyboard.__opencuriaPastePending = null;
          if (rfb) rfb._clipHash = 0;
          releaseQueuedState(keyboard);
          return;
        }
        originalDown.call(keyboard, pending.pasteEvent);
        replay(keyboard, rfb, pending, 0);
      });
    }

    prototype._handleKeyDown = function (event) {
      var rfb = UI.rfb;
      if (this !== (rfb && rfb._keyboard) ||
          (event.target && event.target.id === 'noVNC_clipboard_text'))
        return originalDown.call(this, event);
      var pending = this.__opencuriaPastePending;
      if (pending) {
        event.preventDefault();
        event.stopImmediatePropagation();
        pending.queue.push({ type: 'keydown', event: event });
        return;
      }
      if (this === (rfb && rfb._keyboard) && isPasteShortcut(event) && allowed(rfb, state.generation)) {
        event.preventDefault();
        event.stopImmediatePropagation();
        var first = { generation: state.generation, queue: [], pasteEvent: event };
        this.__opencuriaPastePending = first;
        readForPaste(this, rfb, first);
        return;
      }
      return originalDown.call(this, event);
    };

    prototype._handleKeyUp = function (event) {
      var rfb = UI.rfb;
      if (this !== (rfb && rfb._keyboard) ||
          (event.target && event.target.id === 'noVNC_clipboard_text'))
        return originalUp.call(this, event);
      var pending = this.__opencuriaPastePending;
      if (pending) {
        event.preventDefault();
        event.stopImmediatePropagation();
        pending.queue.push({ type: 'keyup', event: event });
        return;
      }
      return originalUp.call(this, event);
    };
  }

  function installRfbGuards() {
    var prototype = rfb_RFB.prototype;
    var originalFocus = prototype._focusCanvas;
    prototype._focusCanvas = function (event) {
      setFocused(true);
      if (seamless(this) && this.clipboardBinary && !chromiumClipboardAvailable()) {
        fallback('unsupported');
      }
      // Stock _focusCanvas invokes the single guarded checkLocalClipboard read.
      return originalFocus.call(this, event);
    };

    prototype.checkLocalClipboard = function () {
      if (document.activeElement &&
          document.activeElement.id === 'noVNC_clipboard_text') return;
      readLocalClipboard(this);
    };

    var originalPasteDataFrom = prototype.clipboardPasteDataFrom;
    prototype.clipboardPasteDataFrom = function (items) {
      var rfb = this;
      var generation = state.generation;
      var hasSupportedType = Array.prototype.some.call(items || [], function (item) {
        return Array.prototype.some.call(item.types || [], function (mime) {
          return mime === 'text/plain' || mime === 'text/html' || mime === 'image/png';
        });
      });
      if (!hasSupportedType || !allowed(rfb, generation)) return Promise.resolve(false);
      var beforeHash = rfb._clipHash;
      return serializeVmWrite(function () {
        if (!allowed(rfb, generation)) return false;
        return Promise.resolve(originalPasteDataFrom.call(rfb, items)).then(function (sent) {
          var unchanged = rfb._clipHash === beforeHash && Boolean(beforeHash);
          return (Boolean(sent) || unchanged) && allowed(rfb, generation);
        });
      });
    };

    var originalPasteFrom = prototype.clipboardPasteFrom;
    prototype.clipboardPasteFrom = function (text) {
      // This synchronous text route also powers Kasm's native fallback panel.
      if (!activeSurface(this) || !this.clipboardUp || this.viewOnly || !text) return false;
      var beforeHash = this._clipHash;
      var textHash = hashUInt8Array(new TextEncoder().encode(text));
      originalPasteFrom.call(this, text);
      return Boolean(this._clipHash) &&
        (this._clipHash !== beforeHash || this._clipHash === textHash);
    };

    UI.clipboardRx = function () {};

    var originalReceiveMessage = UI.receiveMessage;
    UI.receiveMessage = function (event) {
      if (event && event.data && event.data.action === 'clipboardsnd') return;
      return originalReceiveMessage.apply(this, arguments);
    };

    var originalClipboardReceive = UI.clipboardReceive;
    UI.clipboardReceive = function (event) {
      if (!activeSurface(UI.rfb) || !UI.rfb.clipboardDown ||
          UI.rfb.viewOnly) return;
      clearNativeClipboard();
      return originalClipboardReceive.apply(this, arguments);
    };

    var originalClipboardSend = UI.clipboardSend;
    UI.clipboardSend = function () {
      if (!activeSurface(UI.rfb) || !UI.rfb.clipboardUp || UI.rfb.viewOnly) return;
      return originalClipboardSend.apply(this, arguments);
    };
  }

  window.addEventListener('message', function (event) {
    if (event.source !== window.parent || event.origin !== parentOrigin) return;
    var message = event.data;
    if (!message || message.action !== ACTION || message.version !== VERSION) return;
    if (message.kind === 'context') {
      if (!Number.isSafeInteger(message.sequence) || message.sequence <= state.sequence ||
          typeof message.enabled !== 'boolean' || typeof message.connected !== 'boolean' ||
          typeof message.visible !== 'boolean' || typeof message.parentFocused !== 'boolean' ||
          typeof message.computerUseActive !== 'boolean' || typeof message.workspaceId !== 'string') return;
      var wasPaused = !state.connected || !state.visible || !state.parentFocused ||
        state.computerUseActive;
      var previousComputerUse = state.computerUseActive;
      var previousWorkspace = state.workspaceId;
      var changed = state.enabled !== message.enabled || state.connected !== message.connected ||
        state.visible !== message.visible || state.parentFocused !== message.parentFocused ||
        state.computerUseActive !== message.computerUseActive ||
        state.workspaceId !== message.workspaceId;
      state.sequence = message.sequence;
      state.enabled = message.enabled;
      state.connected = message.connected;
      state.visible = message.visible;
      state.parentFocused = message.parentFocused;
      state.computerUseActive = message.computerUseActive;
      state.workspaceId = message.workspaceId;
      if (changed) {
        state.fallbackReason = '';
        state.generation += 1;
        pendingRead = null;
        if (typeof UI !== 'undefined' && UI.rfb) {
          UI.rfb._clipHash = 0;
          if (UI.rfb._keyboard &&
              (state.computerUseActive || !state.connected || !state.visible ||
               !state.parentFocused || previousComputerUse !== state.computerUseActive)) {
            cancelPendingKeyboard(UI.rfb._keyboard);
          }
        }
      }
      var isPaused = !state.connected || !state.visible || !state.parentFocused ||
        state.computerUseActive;
      if (wasPaused && !isPaused && typeof UI !== 'undefined' && UI.rfb) {
        UI.rfb._resendClipboardNextUserDrivenEvent = true;
      }
      if (changed && (previousComputerUse !== state.computerUseActive ||
          previousWorkspace !== state.workspaceId || wasPaused !== isPaused)) clearNativeClipboard();
      if (state.computerUseActive || !state.visible || !state.parentFocused || !state.connected) {
        closeNativePanel();
      } else if (state.panelOpen) {
        state.panel.style.display = 'block';
      }
      if (state.enabled && state.connected && state.visible && state.parentFocused &&
          !state.computerUseActive && !chromiumClipboardAvailable()) {
        fallback('unsupported');
      }
      return;
    }
    if (message.kind === 'open-panel' && state.visible && state.parentFocused &&
        state.connected && !state.computerUseActive &&
        document.visibilityState === 'visible' && document.hasFocus()) {
      setFocused(true);
      openNativePanel();
    }
  });

  window.addEventListener('focus', function () { setFocused(true); });
  window.addEventListener('blur', function () { setFocused(false); });
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState !== 'visible') setFocused(false);
  });
  document.addEventListener('pointerdown', function (event) {
    if (!event.target || event.target.closest('#opencuria-clipboard-panel')) return;
    setFocused(true);
  }, true);
  document.addEventListener('focusin', function (event) {
    if (event.target && event.target.id === 'noVNC_clipboard_text') setFocused(true);
  }, true);
  document.addEventListener('paste', function (event) {
    if (event.target && event.target.id === 'noVNC_clipboard_text') return;
    // Kasm's keyboard queue handles canvas paste gestures.
  }, true);

  window.__opencuriaClipboardCanRead = function () {
    return activeSurface() && chromiumClipboardAvailable();
  };
  window.__opencuriaClipboardCapture = function () { return state.generation; };
  window.__opencuriaClipboardAllowed = allowed;
  window.__opencuriaClipboardCanReceive = canReceive;
  window.__opencuriaClipboardCanWrite = canWrite;
  window.__opencuriaClipboardFallback = writeFallback;

  installRfbGuards();
  installKeyboardQueue();
  post('ready');
})();
