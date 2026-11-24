// The tablet page's one way to reach the hub: app.js builds its `api` here,
// and every call the page makes, the tap flow's, the conversation
// controller's and the startup view's, goes through it. No DOM of its own;
// the bearer token stays in app.js, read per call through `token()`.
(function (root) {
  "use strict";

  function NetworkError(message) { this.message = message; }

  // o: {token(), session(), onStatus(status, opts, session)}. `onStatus`
  // hears each call's status line the moment it arrives, before and
  // whatever becomes of the body, with the call's own opts and the
  // session() it started in.
  //
  // api(method, path, body, opts) resolves {status, data} and rejects with
  // NetworkError when no response came. The status line is the hub's answer
  // even if the body never arrives: a body that can't be read or parsed
  // resolves with data null (answered, body unavailable), never as no answer.
  function create(o) {
    return function api(method, path, body, opts) {
      opts = opts || {};
      var headers = {};
      var token = o.token();
      if (opts.auth !== false && token) headers.Authorization = "Bearer " + token;
      if (body !== undefined) headers["Content-Type"] = "application/json";
      var init = { method: method, headers: headers, cache: "no-store", credentials: "omit" };
      if (body !== undefined) init.body = JSON.stringify(body);
      if (opts.signal) init.signal = opts.signal;
      var started = o.session();
      return fetch(path, init).then(function (res) {
        o.onStatus(res.status, opts, started);
        return res.text().then(function (raw) {
          try { return raw ? JSON.parse(raw) : null; } catch (e) { return null; }
        }, function () { return null; }).then(function (data) {
          return { status: res.status, data: data };
        });
      }, function () {
        throw new NetworkError("network");
      });
    };
  }

  root.DeskHubApi = { create: create, NetworkError: NetworkError };
})(typeof window !== "undefined" ? window : globalThis);
