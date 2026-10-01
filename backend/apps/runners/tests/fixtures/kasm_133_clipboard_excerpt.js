/*
 * Copyright (C) 2019 The noVNC Authors.
 *
 * This fixture excerpt is copied from the KasmVNC 1.3.3 bundled noVNC client,
 * whose upstream core JavaScript is licensed under the Mozilla Public License
 * 2.0 (MPL-2.0). Source: https://github.com/novnc/noVNC; see
 * THIRD_PARTY_NOTICES.md and the upstream KasmVNC LICENSE.txt. The patched
 * stock clipboard writer string in backend/apps/runners/desktop_proxy.py also
 * comes from that KasmVNC/noVNC bundle.
 */
}; // Set up translations

key: "clipboardPasteDataFrom",
    value: function () {
      var _clipboardPasteDataFrom = rfb_asyncToGenerator( /*#__PURE__*/regeneratorRuntime.mark(function _callee(clipdata) {
        var dataset, mimes, h, i, ti, mime, blob, buff, data, _i2, _i3;

        return regeneratorRuntime.wrap(function _callee$(_context) {
          while (1) {
            switch (_context.prev = _context.next) {
              case 0:
                if (!(this._rfbConnectionState !== 'connected' || this._viewOnly)) {
                  _context.next = 2;
                  break;
                }

                return _context.abrupt("return");

              case 2:
                dataset = [];
                mimes = [];
                h = 0;
                i = 0;

              case 6:
                if (!(i < clipdata.length)) {
                  _context.next = 43;
                  break;
                }

                ti = 0;

              case 8:
                if (!(ti < clipdata[i].types.length)) {
                  _context.next = 40;
                  break;
                }

                mime = clipdata[i].types[ti];
                _context.t0 = mime;
                _context.next = _context.t0 === 'image/png' ? 13 : _context.t0 === 'text/plain' ? 13 : _context.t0 === 'text/html' ? 13 : 36;
                break;

              case 13:
                _context.next = 15;
                return clipdata[i].getType(mime);

              case 15:
                blob = _context.sent;

                if (blob) {
                  _context.next = 18;
                  break;
                }

                return _context.abrupt("continue", 37);

              case 18:
                _context.next = 20;
                return blob.arrayBuffer();

              case 20:
                buff = _context.sent;
                data = new Uint8Array(buff);

                if (h) {
                  _context.next = 30;
                  break;
                }

                h = hashUInt8Array(data); // avoid resending the same data if larger than 64k

                if (!(h === this._clipHash)) {
                  _context.next = 29;
                  break;
                }

                Debug('No clipboard changes');
                return _context.abrupt("return");

              case 29:
                this._clipHash = h;

              case 30:
                if (!mimes.includes(mime)) {
                  _context.next = 32;
                  break;
                }

                return _context.abrupt("continue", 37);

              case 32:
                mimes.push(mime);
                dataset.push(data);
                Debug('Sending mime type: ' + mime);
                return _context.abrupt("break", 37);

              case 36:
                Info('skipping clip send mime type: ' + mime);

              case 37:
                ti++;
                _context.next = 8;
                break;

              case 40:
                i++;
                _context.next = 6;
                break;

              case 43:
                //if png is present and  text/plain is not, remove other variations of images to save bandwidth
                //if png is present with text/plain, then remove png. Word will put in a png of copied text
                if (mimes.includes('image/png') && !mimes.includes('text/plain')) {
                  _i2 = mimes.indexOf('image/png');
                  mimes = mimes.slice(_i2, _i2 + 1);
                  dataset = dataset.slice(_i2, _i2 + 1);
                } else if (mimes.includes('image/png') && mimes.includes('text/plain')) {
                  _i3 = mimes.indexOf('image/png');
                  mimes.splice(_i3, 1);
                  dataset.splice(_i3, 1);
                }

                if (dataset.length > 0) {
                  if (this._isPrimaryDisplay) {
                    RFB.messages.sendBinaryClipboard(this._sock, dataset, mimes);
                  } else {
                    this._proxyRFBMessage('sendBinaryClipboard', [dataset, mimes]);
                  }
                }

              case 45:
              case "end":
                return _context.stop();
            }
          }
        }, _callee, this);
      }));

      function clipboardPasteDataFrom(_x) {
        return _clipboardPasteDataFrom.apply(this, arguments);
      }

      return clipboardPasteDataFrom;
    }()
  }, {

key: "requestBottleneckStats"
    key: "_focusCanvas",
    value: function _focusCanvas(event) {
      // Hack:
      // On most mobile phones it's possible to play audio
      // only if it's triggered by user action. It's also
      // impossible to listen for touch events on child frames (on mobile phones)
      // so we catch those events here but forward the audio unlocking to the parent window
      window.parent.postMessage({
        action: "enable_audio",
        value: null
      }, "*"); // Re-enable pointerLock if relative cursor is enabled
      // pointerLock must come from user initiated event

      if (!this._pointerLock && this._pointerRelativeEnabled) {
        this.pointerLock = true;
      }

      if (this._resendClipboardNextUserDrivenEvent) {
        this.checkLocalClipboard();
      }

      if (!this.focusOnClick) {
        return;
      }

      this.focus();
    }
  }, {

key: "_setDesktopName"
  clipboardReceive: function clipboardReceive(e) {
    if (UI.rfb.clipboardDown) {
      var curvalue = document.getElementById('noVNC_clipboard_text').value;

      if (curvalue != e.detail.text) {
        Debug(">> UI.clipboardReceive: " + e.detail.text.substr(0, 40) + "...");
        document.getElementById('noVNC_clipboard_text').value = e.detail.text;
        Debug("<< UI.clipboardReceive");
      }
    }
  },
  //recieved bottleneck stats

  bottleneckStatsRecieve: function bottleneckStatsRecieve(e) {
key: "_write_binary_clipboard",
    value: function _write_binary_clipboard(clipItemData, textdata) {
      var _this8 = this;

      navigator.clipboard.write([new ClipboardItem(clipItemData)]).then(function () {
        if (textdata) {
          _this8._clipHash = hashUInt8Array(textdata);
        }
      }, function (err) {
        logging_Error("Error writing to client clipboard: " + err); // Lets try writeText

        if (textdata.length > 0) {
          navigator.clipboard.writeText(textdata).then(function () {
            _this8._clipHash = hashUInt8Array(textdata);
          }, function (err) {
            logging_Error("Error writing text to client clipboard: " + err);
          });
        }
      });
    }
  }, {
    key: "_handle_server_stats_msg"