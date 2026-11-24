// Copyright OpenCuria contributors. AGPL-3.0-only.
//
// Runs inside an installed app's JavaScript, before and again after its
// bundle. It reports the app's console output, uncaught errors and
// XMLHttpRequest/fetch requests (method, URL without query values, status,
// duration; never headers or bodies) to the app runtime, which keeps the
// latest ones for the app companion's recordings. Each hook installs once;
// the run before the bundle finds only the native logger.
(function inspect() {
  "use strict";
  var g = globalThis;
  var state = g.__opencuriaInspector;
  if (!state) {
    state = {
      second: 0,
      sent: 0,
      logs: false,
      xhr: false,
      fetch: false,
      errors: false,
    };
    Object.defineProperty(g, "__opencuriaInspector", { value: state });
  }
  var LEVELS = ["debug", "info", "warn", "error"];
  var MAX_PER_SECOND = 40;
  var MAX_MESSAGE = 1000;

  function sink(json) {
    if (typeof g.__opencuriaInspectorReport === "function") {
      g.__opencuriaInspectorReport(json);
      return;
    }
    var proxy = g.nativeModuleProxy;
    var module = proxy && proxy.OpenCuriaAppInspector;
    if (module && typeof module.report === "function") module.report(json);
  }

  function report(event) {
    try {
      var second = Math.floor(Date.now() / 1000);
      if (second !== state.second) {
        state.second = second;
        state.sent = 0;
      }
      if (state.sent++ >= MAX_PER_SECOND) return;
      event.t = event.t || Date.now();
      sink(JSON.stringify(event));
    } catch (error) {
      // Reporting never breaks the app.
    }
  }

  function text(value) {
    try {
      if (value instanceof Error) return value.message || String(value);
      return String(value);
    } catch (error) {
      return "";
    }
  }

  function withoutQueryValues(url) {
    var base = url.split("#")[0];
    var index = base.indexOf("?");
    if (index < 0) return base.slice(0, 500);
    var keys = base
      .slice(index + 1)
      .split("&")
      .map(function (pair) {
        return pair.split("=")[0];
      })
      .filter(Boolean)
      .map(function (key) {
        return key + "=…";
      });
    return (base.slice(0, index) + "?" + keys.join("&")).slice(0, 500);
  }

  // Console output: React Native's console calls this hook for every line.
  var logger = g.nativeLoggingHook;
  if (!state.logs && typeof logger === "function") {
    state.logs = true;
    g.nativeLoggingHook = function (message, level) {
      report({
        kind: "log",
        level: LEVELS[level] || "info",
        message: text(message).slice(0, MAX_MESSAGE),
      });
      return logger.apply(this, arguments);
    };
  }

  // Requests: fetch is built on XMLHttpRequest in React Native.
  var Request = g.XMLHttpRequest;
  if (!state.xhr && Request && Request.prototype) {
    state.xhr = true;
    var open = Request.prototype.open;
    var send = Request.prototype.send;
    Request.prototype.open = function (method, url) {
      this.__opencuria = { method: text(method || "GET"), url: text(url) };
      return open.apply(this, arguments);
    };
    Request.prototype.send = function () {
      var info = this.__opencuria;
      var request = this;
      if (info && typeof request.addEventListener === "function") {
        info.start = Date.now();
        request.addEventListener("loadend", function () {
          var status = request.status || 0;
          report({
            kind: "network",
            method: info.method.toUpperCase(),
            url: withoutQueryValues(info.url),
            status: status,
            durationMs: Date.now() - info.start,
            message: status ? "" : "Network request failed",
            t: info.start,
          });
        });
      }
      return send.apply(this, arguments);
    };
  }

  // Expo replaces fetch with its native implementation (expo/fetch); React
  // Native's own fetch polyfill runs on XMLHttpRequest, hooked above.
  var nativeFetch = g.fetch;
  if (
    !state.fetch &&
    typeof nativeFetch === "function" &&
    !nativeFetch.polyfill &&
    state.xhr
  ) {
    state.fetch = true;
    var wrapped = function (input, init) {
      var url =
        typeof input === "string" ? input : text((input && input.url) || input);
      var method = text(
        (init && init.method) || (input && input.method) || "GET",
      ).toUpperCase();
      var start = Date.now();
      var done = function (status, message) {
        report({
          kind: "network",
          method: method,
          url: withoutQueryValues(url),
          status: status,
          durationMs: Date.now() - start,
          message: message,
          t: start,
        });
      };
      return nativeFetch.apply(this, arguments).then(
        function (response) {
          done((response && response.status) || 0, "");
          return response;
        },
        function (error) {
          done(0, text(error).slice(0, MAX_MESSAGE) || "Network request failed");
          throw error;
        },
      );
    };
    try {
      Object.defineProperty(g, "fetch", {
        value: wrapped,
        writable: true,
        configurable: true,
        enumerable: true,
      });
    } catch (error) {
      state.fetch = false;
    }
  }

  // Uncaught errors, before React Native's own handler shows or reports them.
  var errors = g.ErrorUtils;
  if (!state.errors && errors && typeof errors.getGlobalHandler === "function") {
    state.errors = true;
    var previous = errors.getGlobalHandler();
    errors.setGlobalHandler(function (error, isFatal) {
      report({
        kind: "error",
        level: "error",
        message: ((isFatal ? "Fatal: " : "") + text(error)).slice(0, MAX_MESSAGE),
      });
      if (typeof previous === "function") return previous(error, isFatal);
    });
  }

  // Requests and errors exist only once the bundle ran: try again shortly.
  if (
    (!state.xhr || !state.fetch || !state.errors) &&
    typeof g.setTimeout === "function" &&
    (state.retries = (state.retries || 0) + 1) <= 100
  ) {
    g.setTimeout(inspect, 100);
  }
})();
