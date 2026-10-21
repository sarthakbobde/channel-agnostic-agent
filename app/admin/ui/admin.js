/* Channel Agnostic Agent admin page. Talks only to /admin/api. Builds the page with DOM calls and
   textContent (never HTML strings), so text from the API or the audit log cannot inject markup. */
(function () {
  "use strict";

  var API = "/admin/api";
  var csrf = null;
  var me = null;
  var state = { tab: "channels", channels: [], selected: null, flash: null, perm: null };

  // ---- tiny DOM helpers -------------------------------------------------------
  function add(el, kid) {
    if (kid === null || kid === undefined || kid === false) return;
    if (Array.isArray(kid)) { kid.forEach(function (k) { add(el, k); }); return; }
    el.appendChild(typeof kid === "object" ? kid : document.createTextNode(String(kid)));
  }
  function h(tag, attrs) {
    var el = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      var v = attrs[k];
      if (v === null || v === undefined || v === false) return;
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = v;
      else if (k === "value") el.value = v;
      else if (k === "checked") el.checked = true;
      else if (k === "disabled") el.disabled = true;
      else if (k.slice(0, 2) === "on") el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? "" : v);
    });
    for (var i = 2; i < arguments.length; i++) add(el, arguments[i]);
    return el;
  }
  function mount(node) {
    var root = document.getElementById("root");
    while (root.firstChild) root.removeChild(root.firstChild);
    root.appendChild(node);
  }
  function when(s) {
    if (!s) return "never";
    var iso = /[zZ]|[+-]\d\d:?\d\d$/.test(s) ? s : s + "Z";       // server timestamps are UTC
    var d = new Date(iso);
    return isNaN(d) ? String(s) : d.toLocaleString();
  }
  function msg(kind, text) { return h("div", { class: "msg " + kind, role: "status" }, text); }

  // ---- API ---------------------------------------------------------------------
  function ApiError(status, message) { this.status = status; this.message = message; }

  async function api(method, path, body) {
    var opts = { method: method, credentials: "same-origin", headers: {} };
    if (body instanceof FormData) opts.body = body;
    else if (body !== undefined) { opts.body = JSON.stringify(body); opts.headers["Content-Type"] = "application/json"; }
    if (method !== "GET" && csrf) opts.headers["X-CSRF-Token"] = csrf;
    var res = await fetch(API + path, opts);
    var data = null;
    try { data = await res.json(); } catch (e) { /* no body */ }
    if (!res.ok) {
      var d = data && data.detail;
      if (Array.isArray(d)) d = d.map(function (x) { return x.msg; }).join("; ");
      if (res.status === 401 && me) { me = null; csrf = null; renderLogin("Your session ended. Sign in again."); }
      throw new ApiError(res.status, d || "Request failed (" + res.status + ")");
    }
    return data;
  }

  // ---- boot / login ---------------------------------------------------------------
  function renderBoot(text) { mount(h("p", { class: "boot" }, text)); }

  function renderLogin(notice) {
    var email = h("input", { type: "email", id: "email", autocomplete: "username", required: true });
    var pw = h("input", { type: "password", id: "pw", autocomplete: "current-password", required: true });
    var out = h("div", {}, notice ? msg("bad", notice) : null);
    var form = h("form", {
      onsubmit: async function (e) {
        e.preventDefault();
        out.replaceChildren(msg("wait", "Signing in…"));
        try {
          var r = await api("POST", "/auth/login", { email: email.value, password: pw.value });
          me = r.email; csrf = r.csrf;
          await enter();
        } catch (err) { out.replaceChildren(msg("bad", err.message)); pw.value = ""; }
      }
    },
      h("div", { class: "field" }, h("label", { class: "l", for: "email" }, "Email"), email),
      h("div", { class: "field" }, h("label", { class: "l", for: "pw" }, "Password"), pw),
      h("div", { class: "actions" }, h("button", { class: "btn primary", type: "submit" }, "Sign in")),
      out);
    mount(h("div", { class: "login" }, h("h1", {}, "Channel Agnostic Agent"), h("p", {}, "Admin sign-in"), form));
    email.focus();
  }

  async function boot() {
    var m = /^#setpw=(.+)$/.exec(location.hash);
    if (m) {                                              // opened from a one-time password link
      history.replaceState(null, "", location.pathname);   // do not leave the token in the address bar
      renderSetPassword(m[1]);
      return;
    }
    try {
      var r = await api("GET", "/auth/me");
      me = r.email; csrf = r.csrf;
      await enter();
    } catch (e) {
      if (e.status === 503) renderBoot(e.message + " Set APP_SECRET_KEY in .env and restart the server.");
      else if (e.status === 403) renderBoot(e.message);
      else renderLogin();
    }
  }

  async function enter() {
    state.perm = await api("GET", "/me/permissions");
    state.tab = state.perm.screens[0] || "account";
    if (state.perm.screens.indexOf("channels") >= 0) await loadChannels();
    renderShell();
  }

  async function loadChannels() {
    state.channels = await api("GET", "/channels");
    if (!state.channels.some(function (c) { return c.type === state.selected; })) {
      state.selected = state.channels.length ? state.channels[0].type : null;
    }
  }

  // ---- shell ----------------------------------------------------------------------
  var TABS = [["channels", "Channels"], ["settings", "Settings"], ["providers", "Providers"], ["knowledge", "Knowledge bases"], ["groups", "Groups"], ["prompts", "Prompts"], ["guardrails", "Guardrails"], ["conversations", "Conversations"], ["usage", "Usage and cost"], ["reports", "Reports"], ["departments", "Departments"], ["users", "Users"], ["audit", "Audit log"], ["account", "Account"]];
  function renderShell() {
    var tabs = h("div", { class: "tabs" });
    var allowed = state.perm ? state.perm.screens : [];
    TABS.filter(function (t) { return allowed.indexOf(t[0]) >= 0; }).forEach(function (t) {
      tabs.appendChild(h("button", {
        type: "button", class: state.tab === t[0] ? "on" : "", "aria-current": state.tab === t[0] ? "page" : null,
        onclick: function () { state.tab = t[0]; renderShell(); }
      }, t[1]));
    });
    var nav = h("nav", { class: "side", "aria-label": "Admin sections" },
      h("h1", {}, "Channel Agnostic Agent"),
      h("div", { class: "who" }, h("b", {}, me), h("span", {}, (state.perm ? ROLE_LABEL[state.perm.role] : "") + " · local sign-in"),
        h("button", { type: "button", onclick: signOut }, "Sign out")),
      tabs);
    var main = h("main", {});
    mount(h("div", { class: "app" }, nav, main));
    var view = { channels: viewChannels, settings: viewSettings, providers: viewProviders, knowledge: viewKnowledge, groups: viewGroups, prompts: viewPrompts, guardrails: viewGuardrails, conversations: viewConversations, usage: viewUsage, reports: viewReports, departments: viewDepartments, users: viewUsers, audit: viewAudit, account: viewAccount }[state.tab];
    view(main);
    loadAlerts(main);
  }

  async function signOut() {
    try { await api("POST", "/auth/logout", {}); } catch (e) { /* already out */ }
    me = null; csrf = null;
    renderLogin();
  }

  // ---- channels -------------------------------------------------------------------
  function status(ch) {
    if (!ch.configured) return ["off", "Not set up"];
    if (!ch.enabled) return ["off", "Off"];
    return ch.running ? ["ok", "Running"] : ["bad", "Not running"];
  }

  function viewChannels(main) {
    main.appendChild(h("h2", {}, "Channels"));
    main.appendChild(h("p", { class: "sub" }, "Connect the assistant to the chat apps your team uses. One entry per platform; all of them are answered by the same assistant."));
    var grid = h("div", { class: "grid" });
    state.channels.forEach(function (ch) {
      var s = status(ch);
      grid.appendChild(h("button", {
        type: "button", class: "card" + (ch.type === state.selected ? " sel" : ""),
        onclick: function () { state.selected = ch.type; state.flash = null; renderShell(); }
      }, h("div", { class: "top" }, h("b", {}, ch.title), h("span", { class: "pill " + s[0] }, s[1])),
         h("div", { class: "meta" }, ch.configured ? "Updated " + when(ch.updated_at) : "Not configured yet")));
    });
    main.appendChild(grid);
    var ch = state.channels.filter(function (c) { return c.type === state.selected; })[0];
    if (ch) main.appendChild(channelPanel(ch));
  }

  // ---- spec-driven form -----------------------------------------------------------
  // Draws a form from a list of field definitions (name, label, kind, secret, help, ...), the shape
  // the API returns for channels and for settings. Kinds: text, list, bool, choice, number, plus
  // write-only secret fields. Returns { el, collect }; collect() gives only the values that changed.
  // Providers, embedding models and vector stores will reuse it unchanged.
  function sameValue(a, b) { return JSON.stringify(a) === JSON.stringify(b); }
  function normalize(v, f) {
    if (f.kind === "bool") return !!v;
    if (f.kind === "list") return Array.isArray(v) ? v : [];
    if (f.kind === "kbs") return (Array.isArray(v) ? v.slice() : []).sort(function (a, b) { return a - b; });
    if (f.kind === "number") return v === null || v === undefined ? null : Number(v);
    return v === null || v === undefined ? "" : String(v);
  }

  function specField(f, inputs) {
    var id = "f_" + f.name;
    var wrap = h("div", { class: "field" + (f.kind === "list" ? " wide" : "") });
    var control;
    if (f.secret) {
      control = h("div", { class: "secret" });
      var showSet = function () {
        control.replaceChildren(h("div", { class: "dots" }, f.is_set ? "•••••••••••• (set)" : "not set"),
          h("button", { type: "button", class: "btn small", onclick: showInput }, f.is_set ? "Replace" : "Set value"));
        delete inputs[f.name];
      };
      var showInput = function () {
        var inp = h("input", { type: "password", id: id, autocomplete: "new-password", placeholder: "Paste the new value" });
        inputs[f.name] = inp;
        var kids = [inp];
        if (f.is_set) kids.push(h("button", { type: "button", class: "btn small", onclick: showSet }, "Cancel"));
        control.replaceChildren.apply(control, kids);
        inp.focus();
      };
      showSet();
    } else {
      var inp;
      if (f.kind === "kbs") {
        var picked = Array.isArray(f.value) ? f.value : [];
        inp = h("div", { class: "kbpick", role: "group", "aria-label": f.label });
        inp.boxes = (f.choices || []).map(function (c) {
          var cb = h("input", { type: "checkbox", id: id + "_" + c.id, checked: picked.indexOf(c.id) >= 0 });
          inp.appendChild(h("label", { class: "check", for: id + "_" + c.id }, cb, c.name));
          return { id: c.id, cb: cb };
        });
        control = inp;
      } else if (f.kind === "bool") {
        inp = h("input", { type: "checkbox", id: id, checked: !!f.value });
        control = h("label", { class: "check", for: id }, inp, "On");
      } else if (f.kind === "choice") {
        inp = h("select", { id: id }, (f.choices || []).map(function (c) { return h("option", { value: c, text: c }); }));
        inp.value = f.value || "";
        control = inp;
      } else if (f.kind === "number") {
        inp = h("input", { type: "number", id: id, value: f.value, min: f.min, max: f.max, step: f.step, inputmode: "decimal" });
        control = inp;
      } else {
        inp = h("input", { type: "text", id: id, value: f.kind === "list" ? (f.value || []).join(", ") : (f.value || ""),
                           placeholder: f.kind === "list" ? "comma-separated" : null, autocomplete: "off" });
        control = inp;
      }
      inputs[f.name] = inp;
    }
    wrap.appendChild(h("label", { class: "l", for: id }, f.label + (f.required ? " *" : "")));
    wrap.appendChild(control);
    if (f.help) wrap.appendChild(h("div", { class: "help" }, f.help));
    return wrap;
  }

  function specForm(fields) {
    var inputs = {};
    var form = h("div", { class: "form" });
    fields.forEach(function (f) { form.appendChild(specField(f, inputs)); });

    function collect() {
      var out = {};
      fields.forEach(function (f) {
        var inp = inputs[f.name];
        if (f.secret) { if (inp && inp.value) out[f.name] = inp.value; return; }
        var v;
        if (f.kind === "kbs") v = inp.boxes.filter(function (b) { return b.cb.checked; }).map(function (b) { return b.id; }).sort(function (a, b) { return a - b; });
        else if (f.kind === "bool") v = inp.checked;
        else if (f.kind === "list") v = inp.value.split(",").map(function (s) { return s.trim(); }).filter(Boolean);
        else if (f.kind === "number") {
          var t = inp.value.trim();
          v = t === "" ? null : Number(t);
          if (v !== null && isNaN(v)) v = t;                       // let the server explain
        } else v = inp.value.trim();
        if (!sameValue(v, normalize(f.value, f))) out[f.name] = (v === "" ? null : v);
      });
      return out;
    }
    return { el: form, collect: collect };
  }

  function channelPanel(ch) {
    var result = h("div", { "aria-live": "polite" });
    if (state.flash) { result.appendChild(msg(state.flash.kind, state.flash.text)); state.flash = null; }

    var enabled = h("input", { type: "checkbox", id: "enabled", checked: ch.enabled });
    var sf = specForm(ch.fields);
    var form = sf.el;

    if (ch.webhook_path) {
      var full = "https://<your-public-host>" + ch.webhook_path;
      form.appendChild(h("div", { class: "field wide" },
        h("span", { class: "l" }, "Endpoint to enter at the provider"),
        h("div", { class: "url" }, full),
        h("div", { class: "help" }, "Needs a public HTTPS address (for example a tunnel). Replace the placeholder with your real host."),
        h("div", {}, h("button", { type: "button", class: "btn small", onclick: function () {
          try { navigator.clipboard.writeText(ch.webhook_path).then(function () { flash(result, "ok", "Path copied."); },
                function () { flash(result, "bad", "Copy is blocked here; select the text instead."); }); }
          catch (e) { flash(result, "bad", "Copy is blocked here; select the text instead."); }
        } }, "Copy path"))));
    }

    var saveBtn = h("button", { type: "button", class: "btn primary", onclick: async function () {
      result.replaceChildren(msg("wait", "Saving…"));
      try {
        var updated = await api("PUT", "/channels/" + ch.type, { enabled: enabled.checked, values: sf.collect() });
        await loadChannels();
        state.flash = { kind: "ok", text: updated.enabled ? "Saved. The channel is running with the new settings." : "Saved. The channel is off." };
        renderShell();
      } catch (err) { if (me) result.replaceChildren(msg("bad", err.message)); }
    } }, "Save changes");

    var testBtn = h("button", { type: "button", class: "btn", onclick: async function () {
      result.replaceChildren(msg("wait", "Testing…"));
      try {
        var r = await api("POST", "/channels/" + ch.type + "/test", { values: sf.collect() });
        result.replaceChildren(msg(r.ok ? "ok" : "bad", r.message));
      } catch (err) { if (me) result.replaceChildren(msg("bad", err.message)); }
    } }, "Test connection");

    var actions = h("div", { class: "actions" }, saveBtn, testBtn, confirmButton("Delete", "Confirm delete", async function () {
      try { await api("DELETE", "/channels/" + ch.type); await loadChannels(); state.flash = null; renderShell(); }
      catch (err) { if (me) result.replaceChildren(msg("bad", err.message)); }
    }, ch.configured));

    return h("div", { class: "panel" },
      h("div", { class: "row" },
        h("div", {}, h("h3", {}, ch.title), h("div", { class: "meta" }, status(ch)[1])),
        h("label", { class: "switch", for: "enabled" }, enabled, "Enabled")),
      form, actions, result,
      h("p", { class: "note" }, "Secrets are stored encrypted and are never shown again. Saving checks the new settings first and applies them with no restart; if they fail, the working connection stays. Leave a secret alone to keep its current value."));
  }

  // ---- settings -------------------------------------------------------------------
  async function viewSettings(main) {
    main.appendChild(h("h2", {}, "Settings"));
    main.appendChild(h("p", { class: "sub" }, "Values the assistant reads while it runs. Changes apply within a few seconds, with no restart."));
    var holder = h("div", {}, msg("wait", "Loading…"));
    main.appendChild(holder);

    function show(data, notice) {
      var sf = specForm(data.fields);
      var result = h("div", { "aria-live": "polite" }, notice || null);
      var save = h("button", { type: "button", class: "btn primary", onclick: async function () {
        result.replaceChildren(msg("wait", "Saving…"));
        try {
          var updated = await api("PUT", "/settings", { values: sf.collect() });
          show(updated, msg("ok", "Saved. The new values are in use within a few seconds."));
        } catch (err) { if (me) result.replaceChildren(msg("bad", err.message)); }
      } }, "Save changes");
      holder.replaceChildren(h("div", { class: "panel" },
        h("div", { class: "meta" }, "Last changed " + when(data.updated_at) + " by " + data.updated_by),
        sf.el, h("div", { class: "actions" }, save), result,
        h("p", { class: "note" }, "Chunk size and overlap apply the next time documents are indexed; the other values apply to the next question.")));
    }
    try { show(await api("GET", "/settings")); }
    catch (err) { if (me) holder.replaceChildren(msg("bad", err.message)); }

    // optional notifications: where new alerts are also sent (off by default)
    var sinkBox = h("div", { class: "panel" });
    async function loadSinks(notice) {
      try {
        var c = await api("GET", "/alert-sinks");
        var wOn = h("input", { type: "checkbox", id: "as_w", checked: c.webhook_enabled });
        var wUrl = h("input", { type: "password", id: "as_wurl", autocomplete: "off", placeholder: c.secrets_set.indexOf("webhook_url") >= 0 ? "set (type a new one to replace)" : "https://hooks.slack.com/…" });
        var mOn = h("input", { type: "checkbox", id: "as_m", checked: c.smtp_enabled });
        var host = h("input", { type: "text", id: "as_host", value: c.smtp_host, autocomplete: "off" });
        var port = h("input", { type: "number", id: "as_port", value: c.smtp_port, min: 1, max: 65535 });
        var user = h("input", { type: "text", id: "as_user", value: c.smtp_user, autocomplete: "off" });
        var pw = h("input", { type: "password", id: "as_pw", autocomplete: "new-password", placeholder: c.secrets_set.indexOf("smtp_password") >= 0 ? "set (type a new one to replace)" : "" });
        var from = h("input", { type: "email", id: "as_from", value: c.smtp_from, autocomplete: "off" });
        var to = h("input", { type: "text", id: "as_to", value: (c.smtp_to || []).join(", "), placeholder: "comma-separated", autocomplete: "off" });
        var sev = selectOf([["warning", "Warnings and errors"], ["error", "Errors only"]], c.min_severity, null, "Send");
        sev.id = "as_sev";
        var out = h("div", { "aria-live": "polite" });
        var save = h("button", { type: "button", class: "btn primary", onclick: async function () {
          out.replaceChildren(msg("wait", "Saving…"));
          try {
            var secrets = {};
            if (wUrl.value) secrets.webhook_url = wUrl.value;
            if (pw.value) secrets.smtp_password = pw.value;
            await api("PUT", "/alert-sinks", { values: { webhook_enabled: wOn.checked, smtp_enabled: mOn.checked, smtp_host: host.value, smtp_port: Number(port.value), smtp_user: user.value,
              smtp_from: from.value, smtp_to: to.value.split(",").map(function (x) { return x.trim(); }).filter(Boolean), min_severity: sev.value }, secrets: secrets });
            await loadSinks(msg("ok", "Saved."));
          } catch (err) { if (me) out.replaceChildren(msg("bad", err.message)); }
        } }, "Save");
        var test = h("button", { type: "button", class: "btn", onclick: async function () {
          out.replaceChildren(msg("wait", "Sending a test…"));
          try { var r = await api("POST", "/alert-sinks/test", {}); out.replaceChildren(msg(Object.keys(r.results).every(function (k) { return r.results[k] === "sent"; }) && Object.keys(r.results).length ? "ok" : "bad",
            r.message || Object.keys(r.results).map(function (k) { return k + ": " + r.results[k]; }).join(", "))); }
          catch (err) { if (me) out.replaceChildren(msg("bad", err.message)); }
        } }, "Send a test");
        sinkBox.replaceChildren(h("h3", {}, "Alert notifications"),
          h("p", { class: "note" }, "Alerts always appear as banners on this page. You can also have each NEW alert sent to a chat webhook (Slack-compatible) and/or by email. Off by default; addresses and passwords are stored encrypted."),
          h("div", { class: "form" },
            h("div", { class: "field" }, h("label", { class: "check", for: "as_w" }, wOn, "Send to a webhook")),
            h("div", { class: "field wide" }, h("label", { class: "l", for: "as_wurl" }, "Webhook address"), wUrl),
            h("div", { class: "field" }, h("label", { class: "check", for: "as_m" }, mOn, "Send by email")),
            h("div", { class: "field" }, h("label", { class: "l", for: "as_host" }, "Mail server"), host),
            h("div", { class: "field" }, h("label", { class: "l", for: "as_port" }, "Port"), port),
            h("div", { class: "field" }, h("label", { class: "l", for: "as_user" }, "User name"), user),
            h("div", { class: "field" }, h("label", { class: "l", for: "as_pw" }, "Password"), pw),
            h("div", { class: "field" }, h("label", { class: "l", for: "as_from" }, "From"), from),
            h("div", { class: "field wide" }, h("label", { class: "l", for: "as_to" }, "To"), to),
            h("div", { class: "field" }, h("label", { class: "l", for: "as_sev" }, "Send"), sev)),
          h("div", { class: "actions" }, save, test), out, notice || null);
      } catch (err) { if (me) sinkBox.replaceChildren(msg("bad", err.message)); }
    }
    main.appendChild(sinkBox);
    loadSinks();
  }

  function flash(el, kind, text) { el.replaceChildren(msg(kind, text)); }

  // two-step button: first click arms it for 5 seconds (no browser dialogs)
  function confirmButton(label, armedLabel, action, visible) {
    if (!visible) return null;
    var timer = null;
    var b = h("button", { type: "button", class: "btn danger" }, label);
    b.addEventListener("click", function () {
      if (timer) { clearTimeout(timer); timer = null; action(); return; }
      b.textContent = armedLabel;
      timer = setTimeout(function () { b.textContent = label; timer = null; }, 5000);
    });
    return b;
  }

  // ---- alerts ---------------------------------------------------------------------
  // Open alerts (a provider out of credit, a fallback in use, ...) shown above every screen.
  async function loadAlerts(main) {
    try {
      var list = await api("GET", "/me/alerts");
      if (!list.length) return;
      var box = h("div", { class: "banners", role: "region", "aria-label": "Alerts" });
      list.forEach(function (a) {
        var row = h("div", { class: "banner " + a.severity },
          h("span", {}, a.message + (a.count > 1 ? " (" + a.count + " times)" : "")),
          h("button", { type: "button", class: "btn small", onclick: async function () {
            try { await api("POST", "/me/alerts/" + a.id + "/resolve", {}); row.remove(); if (!box.children.length) box.remove(); }
            catch (err) { /* leave it */ }
          } }, "Dismiss"));
        box.appendChild(row);
      });
      main.insertBefore(box, main.firstChild);
    } catch (err) { /* alerts are best-effort */ }
  }

  // ---- providers ------------------------------------------------------------------
  async function viewProviders(main) {
    main.appendChild(h("h2", {}, "Providers"));
    main.appendChild(h("p", { class: "sub" }, "Connect AI model providers, choose which model answers, and which one takes over if it fails. Changes apply within seconds, with no restart."));
    var holder = h("div", {});
    main.appendChild(holder);

    async function refresh(notice) {
      try {
        var data = await api("GET", "/providers");
        var kids = [];
        if (notice) kids.push(notice);
        kids.push(approvalPanel(data, refresh), modelSettingsPanel(data, refresh));
        data.providers.forEach(function (p) { kids.push(providerPanel(p, refresh)); });
        kids.push(addProviderPanel(data, refresh));
        holder.replaceChildren.apply(holder, kids);
      } catch (err) { if (me) holder.replaceChildren(msg("bad", err.message)); }
    }
    refresh();
  }

  function statusPill(status) {
    return h("span", { class: "pill " + (status === "Connected" ? "ok" : status === "Failed" ? "bad" : "off") }, status);
  }

  function approvalPanel(data, refresh) {
    var boxes = {};
    var result = h("div", { "aria-live": "polite" });
    var list = h("div", { class: "form" });
    data.types.forEach(function (t) {
      var cb = h("input", { type: "checkbox", id: "ap_" + t.type, checked: t.approved });
      boxes[t.type] = cb;
      list.appendChild(h("div", { class: "field" },
        h("label", { class: "check", for: "ap_" + t.type }, cb, h("b", {}, t.title)),
        t.warning ? h("div", { class: "help warnline" }, t.warning) : null));
    });
    return h("div", { class: "panel" }, h("h3", {}, "Approved providers"),
      h("p", { class: "note" }, "Only approved providers can be connected. Nothing is approved until you choose. Questions and document passages are sent to the provider you pick, so approve only what your data policy allows."),
      list,
      h("div", { class: "actions" }, h("button", { type: "button", class: "btn primary", onclick: async function () {
        result.replaceChildren(msg("wait", "Saving…"));
        try {
          await api("PUT", "/providers/approval", { approved: Object.keys(boxes).filter(function (k) { return boxes[k].checked; }) });
          refresh(msg("ok", "Approved providers saved."));
        } catch (err) { if (me) result.replaceChildren(msg("bad", err.message)); }
      } }, "Save approval")), result);
  }

  function modelSettingsPanel(data, refresh) {
    var result = h("div", { "aria-live": "polite" });
    if (!data.providers.length) {
      return h("div", { class: "panel" }, h("h3", {}, "Model settings"),
        h("p", { class: "note" }, "Add a provider below, then choose the preferred model and an optional fallback here."));
    }
    var cur = data.model_settings || { preferred: null, fallback: null, request_timeout: 60 };

    function picker(id, label, current, optional) {
      var prov = h("select", { id: id + "_p" });
      if (optional) prov.appendChild(h("option", { value: "", text: "None" }));
      data.providers.forEach(function (p) { prov.appendChild(h("option", { value: String(p.id), text: p.name })); });
      var model = h("select", { id: id + "_m" });
      function fill() {
        var p = data.providers.filter(function (x) { return String(x.id) === prov.value; })[0];
        model.replaceChildren.apply(model, (p ? p.available_models : []).map(function (m) { return h("option", { value: m, text: m }); }));
      }
      prov.addEventListener("change", fill);
      prov.value = current && current.provider_id ? String(current.provider_id) : (optional ? "" : String(data.providers[0].id));
      fill();
      if (current && current.model) model.value = current.model;
      return { prov: prov, model: model, el: h("div", { class: "field" }, h("label", { class: "l", for: id + "_p" }, label), prov, h("label", { class: "l", for: id + "_m" }, "Model"), model),
               get: function () { return prov.value ? { provider_id: Number(prov.value), model: model.value } : null; } };
    }
    var pref = picker("pref", "Preferred provider", cur.preferred, false);
    var fb = picker("fb", "Fallback provider (optional)", cur.fallback, true);
    var timeout = h("input", { type: "number", id: "tmo", min: 5, max: 300, step: 1, value: cur.request_timeout });

    return h("div", { class: "panel" }, h("h3", {}, "Model settings"),
      h("p", { class: "note" }, "The preferred model answers every question. If it fails with no credit (402), a rate limit (429), a server error (5xx) or a timeout, the fallback answers instead. A rejected request is never retried. Use a different provider for the fallback."),
      h("div", { class: "form" }, pref.el, fb.el,
        h("div", { class: "field" }, h("label", { class: "l", for: "tmo" }, "Request timeout (seconds)"), timeout,
          h("div", { class: "help" }, "5 to 300. How long to wait for a model before giving up on it."))),
      h("div", { class: "actions" }, h("button", { type: "button", class: "btn primary", onclick: async function () {
        result.replaceChildren(msg("wait", "Saving…"));
        try {
          var r = await api("PUT", "/model-settings", { preferred: pref.get(), fallback: fb.get(), request_timeout: Number(timeout.value) });
          var box = h("div", {}, msg("ok", "Saved. The new models are used from the next question."));
          (r.warnings || []).forEach(function (w) { box.appendChild(h("div", { class: "msg warn" }, w)); });
          result.replaceChildren(box);
        } catch (err) { if (me) result.replaceChildren(msg("bad", err.message)); }
      } }, "Save model settings")), result);
  }

  function providerPanel(p, refresh) {
    var nameInput = h("input", { type: "text", id: "pn_" + p.id, value: p.name, autocomplete: "off" });
    var sf = specForm(p.fields);
    var result = h("div", { "aria-live": "polite" });
    return h("div", { class: "panel" },
      h("div", { class: "row" },
        h("div", {}, h("h3", {}, p.name), h("div", { class: "meta" }, p.title + " · " + p.available_models.length + " model(s)")),
        statusPill(p.status)),
      p.status_detail ? msg("bad", p.status_detail) : null,
      p.warning ? h("div", { class: "help warnline" }, p.warning) : null,
      h("div", { class: "form" }, h("div", { class: "field" }, h("label", { class: "l", for: "pn_" + p.id }, "Name"), nameInput), sf.el),
      h("div", { class: "actions" },
        h("button", { type: "button", class: "btn primary", onclick: async function () {
          result.replaceChildren(msg("wait", "Testing and saving…"));
          try { await api("PUT", "/providers/" + p.id, { name: nameInput.value, values: sf.collect() }); refresh(msg("ok", "Saved " + nameInput.value + ".")); }
          catch (err) { if (me) result.replaceChildren(msg("bad", err.message)); }
        } }, "Save changes"),
        h("button", { type: "button", class: "btn", onclick: async function () {
          result.replaceChildren(msg("wait", "Testing…"));
          try { var r = await api("POST", "/providers/" + p.id + "/test", {}); refresh(msg(r.ok ? "ok" : "bad", p.name + ": " + r.message)); }
          catch (err) { if (me) result.replaceChildren(msg("bad", err.message)); }
        } }, "Test connection"),
        confirmButton("Delete", "Confirm delete", async function () {
          try { await api("DELETE", "/providers/" + p.id); refresh(msg("ok", "Deleted " + p.name + ".")); }
          catch (err) { if (me) result.replaceChildren(msg("bad", err.message)); }
        }, true)),
      result);
  }

  function addProviderPanel(data, refresh) {
    var approved = data.types.filter(function (t) { return t.approved; });
    if (!approved.length) {
      return h("div", { class: "panel" }, h("h3", {}, "Add a provider"),
        h("p", { class: "note" }, "Approve at least one provider type above, then add it here."));
    }
    var typeSel = h("select", { id: "np_type" }, approved.map(function (t) { return h("option", { value: t.type, text: t.title }); }));
    var nameInput = h("input", { type: "text", id: "np_name", autocomplete: "off", placeholder: "For example Gemini (work)" });
    var formBox = h("div", {});
    var warn = h("div", {});
    var result = h("div", { "aria-live": "polite" });
    var sf = null;

    function draw() {
      var t = approved.filter(function (x) { return x.type === typeSel.value; })[0];
      sf = specForm(t.fields);
      formBox.replaceChildren(sf.el);
      warn.replaceChildren(t.warning ? h("div", { class: "help warnline" }, t.warning) : "");
      if (!nameInput.value) nameInput.placeholder = t.title;
    }
    typeSel.addEventListener("change", draw);
    draw();

    function body() { return { type: typeSel.value, name: nameInput.value.trim(), values: sf.collect() }; }
    return h("div", { class: "panel" }, h("h3", {}, "Add a provider"),
      h("div", { class: "form" },
        h("div", { class: "field" }, h("label", { class: "l", for: "np_type" }, "Provider"), typeSel),
        h("div", { class: "field" }, h("label", { class: "l", for: "np_name" }, "Name"), nameInput)),
      warn, formBox,
      h("p", { class: "note" }, "The key is checked against the provider before anything is saved, then stored encrypted and never shown again."),
      h("div", { class: "actions" },
        h("button", { type: "button", class: "btn primary", onclick: async function () {
          result.replaceChildren(msg("wait", "Testing and saving…"));
          try { var created = await api("POST", "/providers", body()); refresh(msg("ok", "Added " + created.name + ". Choose its models under Model settings.")); }
          catch (err) { if (me) result.replaceChildren(msg("bad", err.message)); }
        } }, "Test and add"),
        h("button", { type: "button", class: "btn", onclick: async function () {
          result.replaceChildren(msg("wait", "Testing…"));
          try { var r = await api("POST", "/providers/test", body()); result.replaceChildren(msg("ok", r.message)); }
          catch (err) { if (me) result.replaceChildren(msg("bad", err.message)); }
        } }, "Test only")),
      result);
  }

  // ---- audit ----------------------------------------------------------------------
  async function viewAudit(main) {
    main.appendChild(h("h2", {}, "Audit log"));
    main.appendChild(h("p", { class: "sub" }, "Who changed what and when. Secret values are never recorded. Filter by person, action, target and date."));
    var PAGE = 50, offset = 0, first = true;
    var actor = selectOf([["", "Anyone"]], "", function () { offset = 0; load(); }, "Person");
    actor.id = "au_actor";
    var action = selectOf([["", "Any action"]], "", function () { offset = 0; load(); }, "Action");
    action.id = "au_action";
    var target = h("input", { type: "search", id: "au_target", placeholder: "target contains…", autocomplete: "off", "aria-label": "Target contains",
      onchange: function () { offset = 0; load(); } });
    var dr = dateRange("au", function () { offset = 0; load(); }, true);
    var from = dr.from, to = dr.to;
    var holder = h("div", {}, msg("wait", "Loading…"));
    main.appendChild(h("div", { class: "filters" }, actor, action, target, dr.el));
    main.appendChild(holder);

    function fill(sel, items, label) {
      var keep = sel.value;
      sel.replaceChildren(h("option", { value: "" }, label));
      items.forEach(function (x) { sel.appendChild(h("option", { value: x }, x)); });
      sel.value = items.indexOf(keep) >= 0 ? keep : "";
    }
    async function load() {
      var q = "?actor=" + encodeURIComponent(actor.value) + "&action=" + encodeURIComponent(action.value) + "&target=" + encodeURIComponent(target.value)
        + "&date_from=" + from.value + "&date_to=" + to.value + "&limit=" + PAGE + "&offset=" + offset;
      try {
        var r = await api("GET", "/audit/search" + q);
        if (first) { fill(actor, r.actors, "Anyone"); fill(action, r.actions, "Any action"); first = false; }
        var body = h("tbody", {});
        r.rows.forEach(function (x) {
          body.appendChild(h("tr", {}, h("td", { class: "nw", "data-label": "Time" }, when(x.ts)),
            h("td", { class: "wrap", "data-label": "Actor" }, x.actor),
            h("td", { class: "nw", "data-label": "Action" }, x.action),
            h("td", { class: "wrap", "data-label": "Target" }, x.target),
            h("td", { class: "wrap", "data-label": "Detail" }, x.detail)));
        });
        var last = Math.min(offset + PAGE, r.total);
        var prev = h("button", { type: "button", class: "btn", disabled: offset === 0, onclick: function () { offset = Math.max(0, offset - PAGE); load(); } }, "Previous");
        var next = h("button", { type: "button", class: "btn", disabled: last >= r.total, onclick: function () { offset += PAGE; load(); } }, "Next");
        holder.replaceChildren(
          r.rows.length ? h("div", { class: "tablewrap" }, h("table", {},
            h("thead", {}, h("tr", {}, ["Time", "Actor", "Action", "Target", "Detail"].map(function (c) { return h("th", {}, c); }))), body)) : h("p", { class: "sub" }, "Nothing matches."),
          h("div", { class: "actions" }, prev, next, h("span", { class: "meta", "aria-live": "polite" }, r.total ? (offset + 1) + "–" + last + " of " + r.total : "0 results")));
      } catch (err) { if (me) holder.replaceChildren(msg("bad", err.message)); }
    }
    load();
  }

  // ---- reports ---------------------------------------------------------------------
  async function viewReports(main) {
    main.appendChild(h("h2", {}, "Reports"));
    main.appendChild(h("p", { class: "sub" }, "Summaries of guardrail activity, knowledge bases and channels, taken straight from what is stored."));
    var dr = dateRange("rp", function () { load(); }, true);
    var from = dr.from, to = dr.to;
    var holder = h("div", {}, msg("wait", "Loading…"));
    main.appendChild(h("div", { class: "filters" }, dr.el));
    main.appendChild(holder);
    function n(x) { return Number(x).toLocaleString(); }
    function table(cols, rows) {
      var body = h("tbody", {});
      rows.forEach(function (r) {
        body.appendChild(h("tr", {}, r.map(function (c, i) { return h("td", { class: "wrap", "data-label": cols[i] }, c === null || c === undefined ? "–" : String(c)); })));
      });
      return rows.length ? h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, cols.map(function (c) { return h("th", {}, c); }))), body)) : h("p", { class: "sub" }, "Nothing yet.");
    }
    function bytes(x) { return x < 1024 ? x + " B" : x < 1048576 ? (x / 1024).toFixed(1) + " KB" : (x / 1048576).toFixed(1) + " MB"; }
    async function load() {
      var q = "?date_from=" + from.value + "&date_to=" + to.value;
      try {
        var g = await api("GET", "/reports/guardrails" + q);
        var k = await api("GET", "/reports/knowledge" + q);
        var c = await api("GET", "/reports/channels");
        function counts(list) { return table(["Name", "Times"], list.map(function (x) { return [x.name, n(x.count)]; })); }
        holder.replaceChildren(
          h("div", { class: "panel" }, h("h3", {}, "Guardrails (" + n(g.total) + " events)"),
            h("h4", {}, "By check"), counts(g.by_check), h("h4", {}, "By direction"), counts(g.by_direction),
            h("h4", {}, "By action"), counts(g.by_action), h("h4", {}, "By department"), counts(g.by_department)),
          h("div", { class: "panel" }, h("h3", {}, "Knowledge bases"),
            table(["Knowledge base", "Documents", "Chunks", "Size", "Last indexed", "Answered", "Not found", "Embedding tokens"],
              k.rows.map(function (x) {
                return [x.name, Object.keys(x.by_status).map(function (s) { return x.by_status[s] + " " + s.toLowerCase(); }).join(", ") || "none", n(x.chunks), bytes(x.size_bytes),
                  x.last_indexed ? when(x.last_indexed) : "never", n(x.answered), n(x.not_found), n(x.embedding_tokens)];
              }))),
          h("div", { class: "panel" }, h("h3", {}, "Channels"),
            table(["Channel", "Switched on", "Running", "Conversations", "Last activity"],
              c.rows.map(function (x) { return [x.title, x.enabled ? "yes" : "no", x.running ? "yes" : "no", n(x.conversations), x.last_activity ? when(x.last_activity) : "never"]; }))));
      } catch (err) { if (me) holder.replaceChildren(msg("bad", err.message)); }
    }
    load();
  }

  // ---- set-password page (opened from a one-time link: /admin#setpw=<token>) ---------------
  function renderSetPassword(token) {
    var nw = h("input", { type: "password", id: "spw", autocomplete: "new-password", required: true });
    var rep = h("input", { type: "password", id: "spr", autocomplete: "new-password", required: true });
    var out = h("div", { "aria-live": "polite" });
    var form = h("form", { onsubmit: async function (e) {
      e.preventDefault();
      if (nw.value !== rep.value) { out.replaceChildren(msg("bad", "The two passwords do not match.")); return; }
      out.replaceChildren(msg("wait", "Saving…"));
      try {
        await api("POST", "/auth/set-password", { token: token, password: nw.value });
        renderLogin("Password saved. Sign in with your email and the new password.");
      } catch (err) { out.replaceChildren(msg("bad", err.message)); }
    } },
      h("div", { class: "field" }, h("label", { class: "l", for: "spw" }, "New password (12+ characters)"), nw),
      h("div", { class: "field" }, h("label", { class: "l", for: "spr" }, "Repeat password"), rep),
      h("div", { class: "actions" }, h("button", { class: "btn primary", type: "submit" }, "Save password")), out);
    mount(h("div", { class: "login" }, h("h1", {}, "Channel Agnostic Agent"), h("p", {}, "Choose your password"), form));
    nw.focus();
  }

  // ---- shared bits for users and departments -----------------------------------------
  var ROLE_LABEL = { admin: "Admin", editor: "Editor", viewer: "Viewer" };

  function linkBox(link) {
    var url = link.target === "web" ? link.url : location.origin + "/admin#setpw=" + link.token;
    var field = h("input", { type: "text", readonly: true, value: url, "aria-label": "One-time password link" });
    var copied = h("span", { class: "meta" });
    return h("div", { class: "panel" },
      h("b", {}, "One-time password link"),
      h("p", { class: "note" }, "Send this to the person yourself (chat or in person). It works once and expires " + when(link.expires_at) + ". It is shown only now."),
      field,
      h("div", { class: "actions" }, h("button", { type: "button", class: "btn small", onclick: function () {
        field.select();
        if (navigator.clipboard) navigator.clipboard.writeText(url).then(function () { copied.textContent = "Copied."; }, function () { copied.textContent = "Press Ctrl+C to copy."; });
        else copied.textContent = "Press Ctrl+C to copy.";
      } }, "Copy link"), copied));
  }

  // From / To dates with quick ranges, so the usual question ("the last 7 days") is one click and no date has to be typed.
  // `apply` runs when a quick range is picked, and also when a date is edited by hand if `live` is true.
  function dateRange(prefix, apply, live) {
    var from = h("input", { type: "date", id: prefix + "_from", "aria-label": "From date" });
    var to = h("input", { type: "date", id: prefix + "_to", "aria-label": "To date" });
    var chips = [];
    function iso(d) { var m = d.getMonth() + 1, day = d.getDate(); return d.getFullYear() + "-" + (m < 10 ? "0" : "") + m + "-" + (day < 10 ? "0" : "") + day; }
    function ago(days) { var d = new Date(); d.setDate(d.getDate() - days); return iso(d); }
    var RANGES = [
      ["Today", function () { return [iso(new Date()), iso(new Date())]; }],
      ["Last 7 days", function () { return [ago(6), iso(new Date())]; }],
      ["Last 30 days", function () { return [ago(29), iso(new Date())]; }],
      ["This month", function () { var n = new Date(); return [iso(new Date(n.getFullYear(), n.getMonth(), 1)), iso(n)]; }],
      ["Any time", function () { return ["", ""]; }]
    ];
    function mark() {                                         // highlight the quick range that matches, and stop the two dates crossing
      to.min = from.value || ""; from.max = to.value || "";
      chips.forEach(function (c) { var r = c.range(); c.btn.setAttribute("aria-pressed", String(r[0] === from.value && r[1] === to.value)); });
    }
    RANGES.forEach(function (r) {
      var btn = h("button", { type: "button", class: "chip", "aria-pressed": "false", onclick: function () {
        var v = r[1](); from.value = v[0]; to.value = v[1]; mark(); if (apply) apply();
      } }, r[0]);
      chips.push({ btn: btn, range: r[1] });
    });
    [from, to].forEach(function (el) { el.addEventListener("change", function () { mark(); if (live && apply) apply(); }); });
    mark();
    var el = h("div", { class: "daterange" },
      h("div", { class: "field" }, h("label", { class: "l", for: prefix + "_from" }, "From"), from),
      h("div", { class: "field" }, h("label", { class: "l", for: prefix + "_to" }, "To"), to),
      h("div", { class: "chips", role: "group", "aria-label": "Quick date ranges" }, chips.map(function (c) { return c.btn; })));
    return { el: el, from: from, to: to };
  }

  function selectOf(options, value, onchange, label) {
    var s = h("select", { "aria-label": label, onchange: onchange },
      options.map(function (o) { return h("option", { value: String(o[0]), text: o[1] }); }));
    s.value = String(value);
    return s;
  }

  // ---- users -----------------------------------------------------------------------
  async function viewUsers(main) {
    main.appendChild(h("h2", {}, "Users"));
    main.appendChild(h("p", { class: "sub" }, "Admins and Editors sign in to this page; everyone (Viewers too) can sign in to the Web UI with the password they set from their link. Only company-domain addresses can be added."));
    var list = h("div", {});
    var result = h("div", { "aria-live": "polite" });
    var depts = [];
    try { depts = (await api("GET", "/departments")).departments; }
    catch (err) { if (me) main.appendChild(msg("bad", err.message)); return; }
    var deptOptions = depts.map(function (d) { return [d.id, d.name]; });
    var roleOptions = [["admin", "Admin"], ["editor", "Editor"], ["viewer", "Viewer"]];

    async function save(u, change, okText) {
      try { await api("PUT", "/users/" + u.id, change); result.replaceChildren(msg("ok", okText)); }
      catch (err) { if (me) result.replaceChildren(msg("bad", err.message)); }
      refresh();
    }
    async function newLink(u) {
      try {
        var r = await api("POST", "/users/" + u.id + "/reset-link", {});
        result.replaceChildren(linkBox(r.link));
      } catch (err) { if (me) result.replaceChildren(msg("bad", err.message)); }
    }

    async function refresh() {
      try {
        var rows = await api("GET", "/users");
        var body = h("tbody", {});
        rows.forEach(function (u) {
          var self = u.email === me;
          var panel = true;                       // everyone signs in somewhere: Admins/Editors here, Viewers on the Web UI
          body.appendChild(h("tr", {},
            h("td", { class: "wrap", "data-label": "Person" }, h("b", {}, u.name || u.email), h("div", { class: "meta" }, u.email)),
            h("td", { "data-label": "Role" }, self ? ROLE_LABEL[u.role] : selectOf(roleOptions, u.role, function (e) { save(u, { role: e.target.value }, "Role updated."); }, "Role of " + u.email)),
            h("td", { "data-label": "Department" }, selectOf(deptOptions, u.department_id, function (e) { save(u, { department_id: Number(e.target.value) }, "Department updated."); }, "Department of " + u.email)),
            h("td", { "data-label": "Status" }, h("span", { class: "pill " + (u.status === "Active" ? "ok" : "off") }, u.status),
              panel && !u.credentials_set ? h("div", { class: "meta" }, "no password yet") : null),
            h("td", { class: "nw", "data-label": "Last sign-in" }, when(u.last_sign_in)),
            h("td", { class: "act" }, self ? h("span", { class: "meta" }, "you") : [
              panel && u.status === "Active" ? h("button", { type: "button", class: "btn small", onclick: function () { newLink(u); } }, "New link") : null,
              u.status === "Active"
                ? confirmButton("Deactivate", "Confirm deactivate", function () { save(u, { status: "Deactivated" }, "Deactivated " + u.email + "."); }, true)
                : h("button", { type: "button", class: "btn small", onclick: function () { save(u, { status: "Active" }, "Activated " + u.email + "."); } }, "Activate")])));
        });
        list.replaceChildren(h("div", { class: "tablewrap" }, h("table", {},
          h("thead", {}, h("tr", {}, ["Person", "Role", "Department", "Status", "Last sign-in", ""].map(function (t) { return h("th", {}, t); }))), body)));
      } catch (err) { if (me) list.replaceChildren(msg("bad", err.message)); }
    }

    var email = h("input", { type: "email", id: "nu_email", autocomplete: "off", required: true });
    var name = h("input", { type: "text", id: "nu_name", autocomplete: "off" });
    var role = selectOf(roleOptions, "viewer", null, "Role");
    role.id = "nu_role";
    var dept = selectOf(deptOptions, (depts.filter(function (d) { return d.is_default; })[0] || depts[0] || { id: "" }).id, null, "Department");
    dept.id = "nu_dept";
    main.appendChild(list);
    main.appendChild(h("h3", {}, "Add a person"));
    main.appendChild(h("form", { class: "inline", onsubmit: async function (e) {
      e.preventDefault();
      result.replaceChildren(msg("wait", "Adding…"));
      try {
        var r = await api("POST", "/users", { email: email.value, name: name.value, role: role.value, department_id: Number(dept.value) });
        email.value = ""; name.value = "";
        result.replaceChildren(msg("ok", "Added " + r.user.email + "."), r.link ? linkBox(r.link) : null);
        refresh();
      } catch (err) { if (me) result.replaceChildren(msg("bad", err.message)); }
    } },
      h("div", { class: "field" }, h("label", { class: "l", for: "nu_email" }, "Email"), email),
      h("div", { class: "field" }, h("label", { class: "l", for: "nu_name" }, "Name"), name),
      h("div", { class: "field" }, h("label", { class: "l", for: "nu_role" }, "Role"), role),
      h("div", { class: "field" }, h("label", { class: "l", for: "nu_dept" }, "Department"), dept),
      h("div", {}, h("button", { class: "btn primary", type: "submit" }, "Add person"))));
    main.appendChild(result);
    refresh();
  }

  // ---- who can see a document: shared picker --------------------------------------------
  var VISIBLE = [["everyone", "Everyone"], ["groups", "Selected groups"], ["people", "Selected people"]];

  // Draws "Visible to" with the group / people check lists. value() -> {visibility, group_ids, user_ids}.
  function accessPicker(initial, groupList, people, opts) {
    opts = opts || {};
    var wrap = h("div", { class: "picker" });
    var sel = selectOf(opts.noPeople ? VISIBLE.filter(function (o) { return o[0] !== "people"; }) : VISIBLE, initial.visibility || "everyone", function () { redraw(); }, opts.label || "Visible to");
    var lists = h("div", {});
    var gBoxes = groupList.map(function (g) { return { id: g.id, cb: h("input", { type: "checkbox", checked: (initial.group_ids || []).indexOf(g.id) >= 0 }), label: g.name }; });
    var uBoxes = people.map(function (u) { return { id: u.id, cb: h("input", { type: "checkbox", checked: (initial.user_ids || []).indexOf(u.id) >= 0 }), label: (u.name || u.email) + " · " + u.email }; });
    function box(b) { return h("label", { class: "check" }, b.cb, b.label); }
    function redraw() {
      var v = sel.value;
      if (v === "groups") lists.replaceChildren(gBoxes.length ? h("div", { class: "kbpick" }, gBoxes.map(box)) : h("p", { class: "meta" }, "No groups yet. Create one on the Groups screen."));
      else if (v === "people") lists.replaceChildren(h("div", { class: "kbpick" }, uBoxes.map(box)));
      else lists.replaceChildren(h("p", { class: "meta" }, opts.everyoneText || "Anyone who can use the channel can see this document."));
    }
    redraw();
    wrap.appendChild(sel);
    wrap.appendChild(lists);
    return { el: wrap, value: function () {
      return { visibility: sel.value,
        group_ids: sel.value === "groups" ? gBoxes.filter(function (b) { return b.cb.checked; }).map(function (b) { return b.id; }) : [],
        user_ids: sel.value === "people" ? uBoxes.filter(function (b) { return b.cb.checked; }).map(function (b) { return b.id; }) : [] };
    } };
  }

  async function loadChoices() {
    var g = await api("GET", "/groups");
    var d = await api("GET", "/directory");
    return { groups: g.groups, people: d };
  }

  // ---- groups ---------------------------------------------------------------------------
  async function viewGroups(main) {
    main.appendChild(h("h2", {}, "Groups"));
    main.appendChild(h("p", { class: "sub" }, "A group is a named set of people, used to share documents. Membership is checked each time someone asks a question, so a change applies straight away."));
    var holder = h("div", {}, msg("wait", "Loading…"));
    var result = h("div", { "aria-live": "polite" });
    main.appendChild(holder);
    var people = [];
    try { people = await api("GET", "/directory"); } catch (err) { if (me) holder.replaceChildren(msg("bad", err.message)); return; }

    function card(g, data) {
      var name = h("input", { type: "text", id: "gn" + g.id, value: g.name, maxlength: "80" });
      var desc = h("input", { type: "text", id: "gd" + g.id, value: g.description, maxlength: "500" });
      var deptOptions = (state.perm && state.perm.can_manage_all ? [["", "None (Admins only)"]] : []).concat(data.departments.map(function (d) { return [d.id, d.name]; }));
      var dept = selectOf(deptOptions, g.department_id === null ? "" : g.department_id, null, "Department");
      dept.id = "gp" + g.id;
      var current = {};
      g.members.forEach(function (m) { current[m.id] = m; });
      var pool = people.slice();
      g.members.forEach(function (m) { if (!pool.some(function (p) { return p.id === m.id; })) pool.push({ id: m.id, name: m.name, email: m.email + " (deactivated)" }); });
      var boxes = pool.map(function (p) { return { id: p.id, cb: h("input", { type: "checkbox", checked: !!current[p.id] }), p: p }; });
      var out = h("div", { "aria-live": "polite" });
      var save = h("button", { type: "button", class: "btn primary", onclick: async function () {
        out.replaceChildren(msg("wait", "Saving…"));
        try {
          await api("PUT", "/groups/" + g.id, { name: name.value, description: desc.value, department_id: dept.value === "" ? null : Number(dept.value),
            member_ids: boxes.filter(function (b) { return b.cb.checked; }).map(function (b) { return b.id; }) });
          await load(msg("ok", "Saved " + name.value + "."));
        } catch (err) { if (me) out.replaceChildren(msg("bad", err.message)); }
      } }, "Save changes");
      var del = confirmButton("Delete", "Confirm delete", async function () {
        try { await api("DELETE", "/groups/" + g.id); await load(msg("ok", "Deleted " + g.name + ".")); }
        catch (err) { if (me) out.replaceChildren(msg("bad", err.message)); }
      }, true);
      return h("div", { class: "panel" },
        h("h3", {}, g.name), h("div", { class: "meta" }, g.members.length + (g.members.length === 1 ? " member" : " members") + (g.department ? " · " + g.department : " · Admins manage")),
        h("div", { class: "form" },
          h("div", { class: "field" }, h("label", { class: "l", for: "gn" + g.id }, "Name"), name),
          h("div", { class: "field" }, h("label", { class: "l", for: "gp" + g.id }, "Managed by department"), dept),
          h("div", { class: "field wide" }, h("label", { class: "l", for: "gd" + g.id }, "Description"), desc),
          h("div", { class: "field wide" }, h("div", { class: "l" }, "Members"),
            h("div", { class: "kbpick" }, boxes.map(function (b) { return h("label", { class: "check" }, b.cb, (b.p.name || b.p.email) + " · " + b.p.email); })))),
        h("div", { class: "actions" }, save, del), out);
    }

    async function load(notice) {
      try {
        var data = await api("GET", "/groups");
        var kids = [notice || null];
        if (!data.groups.length) kids.push(h("p", { class: "sub" }, "No groups yet."));
        data.groups.forEach(function (g) { kids.push(card(g, data)); });
        var nn = h("input", { type: "text", id: "ng_name", autocomplete: "off", required: true, maxlength: "80" });
        var opts = (state.perm && state.perm.can_manage_all ? [["", "None (Admins only)"]] : []).concat(data.departments.map(function (d) { return [d.id, d.name]; }));
        var nd = selectOf(opts, opts.length ? opts[0][0] : "", null, "Department");
        nd.id = "ng_dept";
        kids.push(h("h3", {}, "Add a group"));
        kids.push(h("form", { class: "inline", onsubmit: async function (e) {
          e.preventDefault();
          result.replaceChildren(msg("wait", "Adding…"));
          try {
            await api("POST", "/groups", { name: nn.value, department_id: nd.value === "" ? null : Number(nd.value) });
            await load(msg("ok", "Group added. Open it to choose its members."));
          } catch (err) { if (me) result.replaceChildren(msg("bad", err.message)); }
        } },
          h("div", { class: "field" }, h("label", { class: "l", for: "ng_name" }, "Name"), nn),
          h("div", { class: "field" }, h("label", { class: "l", for: "ng_dept" }, "Managed by department"), nd),
          h("div", {}, h("button", { class: "btn", type: "submit" }, "Add group"))));
        kids.push(result);
        holder.replaceChildren.apply(holder, kids.filter(Boolean));
      } catch (err) { if (me) holder.replaceChildren(msg("bad", err.message)); }
    }
    await load();
  }

  // ---- knowledge bases ------------------------------------------------------------
  async function viewKnowledge(main) {
    main.appendChild(h("h2", {}, "Knowledge bases"));
    main.appendChild(h("p", { class: "sub" }, "A knowledge base is a set of documents the assistant answers from. Each belongs to a department and can use its own chunk settings. Choose which ones a channel uses on the Channels screen."));
    // Two parts: 1 is where documents are uploaded and managed, 2 is where a knowledge base is configured.
    var PARTS = [["documents", "Part 1: Documents", "Upload files and manage who can read them"], ["config", "Part 2: Configuration", "Chunking, embedding model, vector store, rebuilds and who can ask"]];
    if (!state.kbPart) state.kbPart = "documents";
    var partNote = h("p", { class: "note" });
    var partButtons = PARTS.map(function (p) {
      return h("button", { type: "button", class: "chip", title: p[2], onclick: function () { state.kbPart = p[0]; drawParts(); load(); } }, p[1]);
    });
    function drawParts() {
      partButtons.forEach(function (b, i) { b.setAttribute("aria-pressed", String(PARTS[i][0] === state.kbPart)); });
      partNote.textContent = PARTS.filter(function (p) { return p[0] === state.kbPart; })[0][2] + ".";
    }
    main.appendChild(h("div", { class: "chips", role: "group", "aria-label": "Knowledge base sections" }, partButtons));
    main.appendChild(partNote);
    drawParts();
    var holder = h("div", {}, msg("wait", "Loading…"));
    var result = h("div", { "aria-live": "polite" });
    main.appendChild(holder);

    function blank(v) { return v === null || v === undefined ? "" : v; }
    function intOrNull(text) {
      var t = text.trim();
      if (t === "") return null;
      var n = Number(t);
      return isNaN(n) ? t : n;                                    // let the server explain
    }

    // ---- documents of one knowledge base ---------------------------------------------
    function size(n) { return n >= 1048576 ? (n / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1024)) + " KB"; }

    function docsPanel(k, data) {
      var box = h("div", { class: "docs" });
      var out = h("div", { "aria-live": "polite" });
      var table = h("div", {});
      var timer = null;
      var fileInput = h("input", { type: "file", id: "up" + k.id, "aria-label": "File to upload" });

      function stop() { if (timer) { clearInterval(timer); timer = null; } }
      function watch(docs) {
        var busy = docs.some(function (d) { return d.status === "Pending" || d.status === "Indexing"; });
        if (busy && !timer) timer = setInterval(function () {
          if (!document.body.contains(box)) { stop(); return; }                // screen changed
          refresh();
        }, 2500);
        if (!busy) stop();
      }
      async function act(promise, okText) {
        try { await promise; if (okText) out.replaceChildren(msg("ok", okText)); }
        catch (err) { if (me) out.replaceChildren(msg("bad", err.message)); }
        refresh();
      }
      var accessBox = h("div", {});
      async function openAccess(d) {
        accessBox.replaceChildren(msg("wait", "Loading…"));
        try {
          var cur = await api("GET", "/documents/" + d.id + "/access");
          var ch = await loadChoices();
          var picker = accessPicker(cur, ch.groups, ch.people);
          accessBox.replaceChildren(h("div", { class: "panel" }, h("b", {}, "Who can see " + d.name),
            h("p", { class: "note" }, "Takes effect on the next question. The document is not re-indexed."), picker.el,
            h("div", { class: "actions" },
              h("button", { type: "button", class: "btn primary", onclick: async function () {
                try { await api("PUT", "/documents/" + d.id + "/access", picker.value()); accessBox.replaceChildren(); out.replaceChildren(msg("ok", "Access updated for " + d.name + ".")); refresh(); }
                catch (err) { if (me) accessBox.insertBefore(msg("bad", err.message), accessBox.firstChild); }
              } }, "Save access"),
              h("button", { type: "button", class: "btn small", onclick: function () { accessBox.replaceChildren(); } }, "Close"))));
        } catch (err) { if (me) accessBox.replaceChildren(msg("bad", err.message)); }
      }
      function pill(d) {
        var kind = d.status === "Indexed" ? "ok" : d.status === "Failed" ? "bad" : "off";
        return h("span", { class: "pill " + kind }, d.status);
      }
      function row(d, types) {
        var replaceInput = h("input", { type: "file", class: "hidden", "aria-label": "New version of " + d.name, accept: types.join(","),
          onchange: function (e) {
            var f = e.target.files[0];
            if (!f) return;
            var fd = new FormData(); fd.append("file", f);
            act(api("POST", "/documents/" + d.id + "/replace", fd), "Replaced " + d.name + "; indexing again.");
          } });
        var others = data.kbs.filter(function (x) { return x.id !== k.id; });
        var mover = others.length ? selectOf([["", "Move to…"]].concat(others.map(function (x) { return [x.id, x.name]; })), "", function (e) {
          var to = Number(e.target.value);
          if (to) act(api("POST", "/documents/" + d.id + "/move", { kb_id: to }), "Moved " + d.name + "; indexing in the new knowledge base.");
        }, "Move " + d.name + " to another knowledge base") : null;
        return h("tr", {},
          h("td", { class: "wrap", "data-label": "Document" }, h("b", {}, d.name),
            h("div", { class: "meta" }, d.visibility === "everyone" ? "Visible to everyone" : d.visibility === "groups" ? "Restricted to selected groups" : "Restricted to selected people"),
            d.error ? h("div", { class: "meta bad" }, d.error) : null),
          h("td", { "data-label": "Status" }, pill(d)),
          h("td", { class: "nw", "data-label": "Chunks" }, d.status === "Indexed" ? String(d.chunks) : "–"),
          h("td", { class: "nw", "data-label": "Size" }, d.size ? size(d.size) : "–"),
          h("td", { class: "nw", "data-label": "Updated" }, when(d.updated_at)),
          h("td", { class: "act" },
            h("button", { type: "button", class: "btn small", onclick: function () { act(api("POST", "/documents/" + d.id + "/reindex", {}), "Re-indexing " + d.name + "."); } }, d.status === "Failed" ? "Retry" : "Re-index"),
            h("button", { type: "button", class: "btn small", onclick: function () { openAccess(d); } }, "Access"),
            h("button", { type: "button", class: "btn small", onclick: function () { replaceInput.click(); } }, "Replace"), replaceInput, mover,
            confirmButton("Delete", "Confirm delete", function () { act(api("DELETE", "/documents/" + d.id), "Deleted " + d.name + "."); }, true)));
      }
      async function refresh() {
        try {
          var r = await api("GET", "/kbs/" + k.id + "/documents");
          var body = h("tbody", {});
          r.documents.forEach(function (d) { body.appendChild(row(d, r.types)); });
          table.replaceChildren(r.documents.length
            ? h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {},
                ["Document", "Status", "Chunks", "Size", "Updated", ""].map(function (t) { return h("th", {}, t); }))), body))
            : h("p", { class: "meta" }, "No documents yet. Upload one below (" + r.types.join(", ") + "; up to " + r.max_upload_mb + " MB)."));
          fileInput.accept = r.types.join(",");
          watch(r.documents);
        } catch (err) { stop(); if (me) table.replaceChildren(msg("bad", err.message)); }
      }
      var uploadAccess = h("div", {});
      var uploadPicker = null;
      var showAccess = h("button", { type: "button", class: "btn small", onclick: async function () {
        if (uploadPicker) { uploadPicker = null; uploadAccess.replaceChildren(); return; }       // toggle back to "Everyone"
        try { var ch = await loadChoices(); uploadPicker = accessPicker({ visibility: "groups" }, ch.groups, ch.people); uploadAccess.replaceChildren(uploadPicker.el); }
        catch (err) { if (me) out.replaceChildren(msg("bad", err.message)); }
      } }, "Restrict who can see it…");
      var uploadBtn = h("button", { type: "button", class: "btn", onclick: function () {
        var f = fileInput.files[0];
        if (!f) { out.replaceChildren(msg("bad", "Choose a file first.")); return; }
        var fd = new FormData(); fd.append("file", f);
        if (uploadPicker) {
          var v = uploadPicker.value();
          fd.append("visibility", v.visibility); fd.append("group_ids", v.group_ids.join(",")); fd.append("user_ids", v.user_ids.join(","));
        }
        out.replaceChildren(msg("wait", "Uploading " + f.name + "…"));
        act(api("POST", "/kbs/" + k.id + "/documents", fd).then(function () { fileInput.value = ""; }), "Uploaded " + f.name + "; indexing has started.");
      } }, "Upload");
      var reindexAll = h("button", { type: "button", class: "btn small", onclick: function () {
        act(api("POST", "/kbs/" + k.id + "/reindex", {}).then(function (r) { out.replaceChildren(msg("ok", r.queued + " documents queued for re-indexing.")); }));
      } }, "Re-index all");
      box.appendChild(h("h4", {}, "Documents"));
      box.appendChild(table);
      box.appendChild(accessBox);
      box.appendChild(h("div", { class: "actions" }, fileInput, showAccess, uploadBtn, reindexAll));
      box.appendChild(uploadAccess);
      box.appendChild(out);
      refresh();
      return box;
    }

    // ---- embedding model of this knowledge base -----------------------------------------------
    function embeddingPanel(k, data) {
      var box = h("div", { class: "docs" });
      var out = h("div", { "aria-live": "polite" });
      var form = h("div", {});
      var info = h("div", {});
      var options = data.embeddings;
      var pick = selectOf(options.map(function (o) { return [o.name, o.title]; }), k.embedding, null, "Embedding model");
      pick.id = "em" + k.id;
      var current = null;                                              // { opt, sf, defaults }

      function draw() {
        var opt = options.filter(function (o) { return o.name === pick.value; })[0];
        var same = pick.value === k.embedding;
        var defaults = {};
        var fields = opt.fields.map(function (f) {
          if (f.default !== null && f.default !== undefined && f.default !== "") defaults[f.name] = f.default;
          var v = same && k.embedding_config[f.name] !== undefined ? k.embedding_config[f.name] : (f.default === null || f.default === undefined ? "" : f.default);
          return Object.assign({}, f, { value: v, is_set: same && k.embedding_secrets_set.indexOf(f.name) >= 0, kind: f.kind === "number" ? "text" : f.kind });
        });
        var sf = specForm(fields);
        current = { opt: opt, sf: sf, defaults: defaults };
        var kids = [];
        if (opt.warning) kids.push(h("div", { class: "meta bad" }, opt.warning));
        if (opt.fields.length) kids.push(sf.el);
        if (opt.needs_download) {
          kids.push(h("p", { class: "note" }, "This model must be downloaded once (about 129 MB, from huggingface.co/intfloat/multilingual-e5-small, MIT licence). It then runs on this machine."));
          if (state.perm && state.perm.can_manage_all) kids.push(h("div", { class: "actions" }, h("button", { type: "button", class: "btn", onclick: async function () {
            out.replaceChildren(msg("wait", "Downloading… this can take a minute."));
            try { var r = await api("POST", "/kbs/" + k.id + "/embedding/download", {}); opt.needs_download = false; out.replaceChildren(msg("ok", r.message)); draw(); }
            catch (err) { if (me) out.replaceChildren(msg("bad", err.message)); }
          } }, "Download the model")));
        }
        form.replaceChildren.apply(form, kids);
        info.replaceChildren(h("div", { class: "meta" }, "The current index was built with: " + (options.filter(function (o) { return o.name === k.active_embedding; })[0] || { title: k.active_embedding || "?" }).title
          + (k.embedding !== k.active_embedding ? ". The chosen model is different, so the index needs a rebuild." : ".")));
      }
      function collect() {
        var vals = Object.assign({}, current.defaults, current.sf.collect());
        var secrets = {}, config = {};
        current.opt.fields.forEach(function (f) {
          if (vals[f.name] === undefined) return;
          if (f.secret) secrets[f.name] = vals[f.name]; else config[f.name] = vals[f.name];
        });
        return { embedding: pick.value, embedding_config: config, embedding_secrets: secrets };
      }
      pick.addEventListener("change", draw);
      var test = h("button", { type: "button", class: "btn", onclick: async function () {
        out.replaceChildren(msg("wait", "Testing…"));
        try { var r = await api("POST", "/kbs/" + k.id + "/embedding/test", collect()); out.replaceChildren(msg(r.ok ? "ok" : "bad", r.message)); }
        catch (err) { if (me) out.replaceChildren(msg("bad", err.message)); }
      } }, "Test");
      var save = h("button", { type: "button", class: "btn primary", onclick: async function () {
        out.replaceChildren(msg("wait", "Testing and saving…"));
        try { var r = await api("PUT", "/kbs/" + k.id, collect()); out.replaceChildren(msg("ok", "Saved. Rebuild the index below to start using it; until then the current index keeps answering.")); }
        catch (err) { if (me) out.replaceChildren(msg("bad", err.message)); }
      } }, "Save");
      box.appendChild(h("h4", {}, "Embedding model"));
      box.appendChild(h("p", { class: "note" }, "Turns document text into vectors for searching. Changing it needs a rebuild of the index (see below); questions keep using the model the current index was built with until the switch."));
      box.appendChild(h("div", { class: "field wide" }, h("label", { class: "l", for: "em" + k.id }, "Model"), pick));
      box.appendChild(info);
      box.appendChild(form);
      box.appendChild(h("div", { class: "actions" }, test, save));
      box.appendChild(out);
      draw();
      return box;
    }

    // ---- vector store of this knowledge base --------------------------------------------------
    function storePanel(k, data) {
      var box = h("div", { class: "docs" });
      var out = h("div", { "aria-live": "polite" });
      var form = h("div", {});
      var options = data.stores;
      var title = function (n) { return (options.filter(function (o) { return o.name === n; })[0] || { title: n || "?" }).title; };
      var pick = selectOf(options.map(function (o) { return [o.name, o.title]; }), k.store, null, "Vector store");
      pick.id = "vs" + k.id;
      var current = null;

      function draw() {
        var opt = options.filter(function (o) { return o.name === pick.value; })[0];
        var same = pick.value === k.store;
        var fields = opt.fields.map(function (f) {
          var v = same && k.store_config[f.name] !== undefined ? k.store_config[f.name] : (f.default === null || f.default === undefined ? "" : f.default);
          return Object.assign({}, f, { value: v, is_set: same && k.store_secrets_set.indexOf(f.name) >= 0 });
        });
        var defaults = {};
        opt.fields.forEach(function (f) { if (f.default !== null && f.default !== undefined && f.default !== "") defaults[f.name] = f.default; });
        var sf = specForm(fields);
        current = { opt: opt, sf: sf, defaults: defaults };
        var kids = [];
        if (opt.warning) kids.push(h("div", { class: "meta bad" }, opt.warning));
        if (opt.fields.length) kids.push(sf.el);
        form.replaceChildren.apply(form, kids);
      }
      function collect() {
        var vals = Object.assign({}, current.defaults, current.sf.collect());
        var secrets = {}, config = {};
        current.opt.fields.forEach(function (f) {
          if (vals[f.name] === undefined) return;
          if (f.secret) secrets[f.name] = vals[f.name]; else config[f.name] = vals[f.name];
        });
        return { store: pick.value, store_config: config, store_secrets: secrets };
      }
      pick.addEventListener("change", draw);
      var size = k.size_bytes ? " · " + (k.size_bytes >= 1048576 ? (k.size_bytes / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(k.size_bytes / 1024)) + " KB") : "";
      var status = h("div", { class: "meta" }, h("span", { class: "pill " + (k.health && k.health.ok ? "ok" : "bad") }, k.health && k.health.ok ? "Reachable" : "Not reachable"),
        " Active index in " + title(k.active_store) + " · " + (k.chunks === null ? "?" : k.chunks) + " chunks" + size
        + (k.last_indexed ? " · last indexed " + when(k.last_indexed) : "") + (k.health && !k.health.ok ? " · " + k.health.message : "")
        + (k.store !== k.active_store ? ". The chosen store is different, so the index needs a rebuild." : "."));
      var test = h("button", { type: "button", class: "btn", onclick: async function () {
        out.replaceChildren(msg("wait", "Testing…"));
        try { var r = await api("POST", "/kbs/" + k.id + "/store/test", collect()); out.replaceChildren(msg(r.ok ? "ok" : "bad", r.message)); }
        catch (err) { if (me) out.replaceChildren(msg("bad", err.message)); }
      } }, "Test");
      var save = h("button", { type: "button", class: "btn primary", onclick: async function () {
        out.replaceChildren(msg("wait", "Testing and saving…"));
        try { await api("PUT", "/kbs/" + k.id, collect()); out.replaceChildren(msg("ok", "Saved. Rebuild the index below to move it; until then the current index keeps answering.")); }
        catch (err) { if (me) out.replaceChildren(msg("bad", err.message)); }
      } }, "Save");
      box.appendChild(h("h4", {}, "Vector store"));
      box.appendChild(h("p", { class: "note" }, "Where this knowledge base keeps its vectors. Changing it needs a rebuild of the index (see below): the new index is built beside the old one and used only when it is complete."));
      box.appendChild(status);
      box.appendChild(h("div", { class: "field wide" }, h("label", { class: "l", for: "vs" + k.id }, "Store"), pick));
      box.appendChild(form);
      box.appendChild(h("div", { class: "actions" }, test, save));
      box.appendChild(out);
      draw();
      return box;
    }

    // ---- chunk preview: run a strategy on a sample file without saving anything ---------------
    function previewPanel(k, data, strat, size, overlap) {
      var box = h("div", { class: "docs" });
      var file = h("input", { type: "file", id: "pf" + k.id, "aria-label": "Sample file" });
      var out = h("div", { "aria-live": "polite" });
      var run = h("button", { type: "button", class: "btn", onclick: async function () {
        var f = file.files[0];
        if (!f) { out.replaceChildren(msg("bad", "Choose a sample file first.")); return; }
        var fd = new FormData();
        fd.append("file", f); fd.append("strategy", strat.value); fd.append("chunk_size", size.value); fd.append("chunk_overlap", overlap.value);
        out.replaceChildren(msg("wait", "Working out the chunks…"));
        try {
          var r = await api("POST", "/kbs/" + k.id + "/chunk-preview", fd);
          var body = h("tbody", {});
          r.chunks.forEach(function (c) {
            body.appendChild(h("tr", {},
              h("td", { class: "nw", "data-label": "#" }, String(c.n + 1)),
              h("td", { class: "nw", "data-label": "Page" }, c.page ? String(c.page) : "–"),
              h("td", { class: "wrap", "data-label": "Heading" }, c.heading_path || "–"),
              h("td", { class: "nw", "data-label": "Size" }, c.chars + " chars"),
              h("td", { class: "wrap", "data-label": "Text" }, c.text)));
          });
          out.replaceChildren(
            msg("ok", r.count + " chunks, average " + r.average_chars + " characters, longest " + r.longest + ". Nothing was saved."),
            r.notes.map(function (n) { return h("div", { class: "meta" }, n); }),
            h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["#", "Page", "Heading", "Size", "Text"].map(function (t) { return h("th", {}, t); }))), body)),
            r.count > r.shown ? h("div", { class: "meta" }, "Showing the first " + r.shown + " chunks.") : null);
        } catch (err) { if (me) out.replaceChildren(msg("bad", err.message)); }
      } }, "Preview chunks");
      box.appendChild(h("h4", {}, "Preview chunking"));
      box.appendChild(h("p", { class: "note" }, "Try the strategy and sizes above on a sample file before you save them. Nothing is stored or indexed."));
      box.appendChild(h("div", { class: "actions" }, file, run));
      box.appendChild(out);
      return box;
    }

    // ---- index versions of one knowledge base ------------------------------------------
    function indexPanel(k) {
      var box = h("div", { class: "docs" });
      var top = h("div", {});
      var action = h("div", {});
      var table = h("div", {});
      var out = h("div", { "aria-live": "polite" });
      var timer = null;
      function stop() { if (timer) { clearInterval(timer); timer = null; } }
      function money(e) { return e.cost_usd === null ? "cost unknown" : e.cost_usd === 0 ? "no cost" : "about $" + e.cost_usd; }
      function secs(n) { return n === null ? "unknown" : n < 90 ? n + " seconds" : Math.round(n / 60) + " minutes"; }

      async function review() {
        action.replaceChildren(msg("wait", "Working out the estimate…"));
        try {
          var e = await api("GET", "/kbs/" + k.id + "/index/estimate");
          action.replaceChildren(h("div", { class: "panel" },
            h("b", {}, "Rebuild estimate"),
            h("p", { class: "note" }, e.documents + " documents → about " + e.chunks + " chunks (" + e.tokens + " tokens), chunk size " + e.target.chunk_size
              + " / overlap " + e.target.chunk_overlap + ", " + e.model + ". Time: about " + secs(e.seconds) + ". " + money(e) + ". "
              + e.note + (e.warning ? " " + e.warning : "") + (e.skipped_documents ? " " + e.skipped_documents + " document(s) cannot be read and will be left out." : "")),
            h("p", { class: "note" }, "The current index keeps answering until the new one is complete. Documents cannot be changed while it builds."),
            h("div", { class: "actions" },
              h("button", { type: "button", class: "btn primary", onclick: start }, "Start rebuild"),
              h("button", { type: "button", class: "btn small", onclick: function () { action.replaceChildren(); } }, "Not now"))));
        } catch (err) { if (me) action.replaceChildren(msg("bad", err.message)); }
      }
      async function start() {
        try { await api("POST", "/kbs/" + k.id + "/index/rebuild", {}); action.replaceChildren(); out.replaceChildren(msg("ok", "Rebuild started.")); }
        catch (err) { if (me) out.replaceChildren(msg("bad", err.message)); }
        refresh();
      }
      async function rollback(v) {
        try {
          var r = await api("POST", "/kbs/" + k.id + "/index/rollback", { version_id: v.id });
          out.replaceChildren(msg("ok", "Back on version " + r.version + (r.reindexed ? "; " + r.reindexed + " document(s) changed since then are being re-indexed." : ".")));
        } catch (err) { if (me) out.replaceChildren(msg("bad", err.message)); }
        refresh();
      }
      async function cancelBuild() {
        try { await api("POST", "/kbs/" + k.id + "/index/cancel", {}); out.replaceChildren(msg("ok", "Cancelling; the current index stays in use.")); }
        catch (err) { if (me) out.replaceChildren(msg("bad", err.message)); }
        refresh();
      }
      async function refresh() {
        try {
          var s = await api("GET", "/kbs/" + k.id + "/index");
          var act = s.versions.filter(function (v) { return v.is_active; })[0];
          var kids = [];
          if (act) kids.push(h("div", { class: "meta" }, "Active: version " + act.version + " · chunk size " + act.chunk_size + " / overlap " + act.chunk_overlap
            + " · " + act.chunks + " chunks · " + act.embedding));
          if (s.building) {
            var b = s.building;
            kids.push(msg("wait", "Building version " + b.version + ": " + b.progress_done + " of " + b.progress_total + " documents. The current index is still in use."));
            kids.push(h("div", { class: "actions" }, h("button", { type: "button", class: "btn small", onclick: cancelBuild }, "Cancel rebuild")));
          } else if (s.out_of_date) {
            kids.push(msg("bad", "The chunk settings changed (now " + s.desired.chunk_size + " / " + s.desired.chunk_overlap + "), but the index was built with different ones. Rebuild to apply them."));
          }
          if (!s.building) kids.push(h("div", { class: "actions" }, h("button", { type: "button", class: "btn" + (s.out_of_date ? " primary" : ""), onclick: review }, "Rebuild index…")));
          top.replaceChildren.apply(top, kids);
          var body = h("tbody", {});
          s.versions.forEach(function (v) {
            var pillKind = v.status === "Ready" ? "ok" : v.status === "Building" ? "off" : "bad";
            body.appendChild(h("tr", {},
              h("td", { "data-label": "Version" }, "v" + v.version, v.is_active ? h("span", { class: "pill ok" }, "Active") : null),
              h("td", { "data-label": "Settings" }, v.chunk_size + " / " + v.chunk_overlap),
              h("td", { class: "nw", "data-label": "Chunks" }, String(v.chunks)),
              h("td", { "data-label": "Status" }, h("span", { class: "pill " + pillKind }, v.status), v.error ? h("div", { class: "meta bad" }, v.error) : null),
              h("td", { class: "nw", "data-label": "Built" }, when(v.finished_at || v.created_at)),
              h("td", { class: "act" }, v.status === "Ready" && !v.is_active && !s.building
                ? confirmButton("Roll back", "Confirm roll back", function () { rollback(v); }, true) : null)));
          });
          table.replaceChildren(s.versions.length ? h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {},
            ["Version", "Chunk size / overlap", "Chunks", "Status", "Built", ""].map(function (t) { return h("th", {}, t); }))), body)) : null);
          if (s.building && !timer) timer = setInterval(function () { if (!document.body.contains(box)) { stop(); return; } refresh(); }, 2500);
          if (!s.building) stop();
        } catch (err) { stop(); if (me) top.replaceChildren(msg("bad", err.message)); }
      }
      box.appendChild(h("h4", {}, "Index"));
      box.appendChild(top);
      box.appendChild(action);
      box.appendChild(table);
      box.appendChild(out);
      refresh();
      return box;
    }

    // ---- who may ask this knowledge base ----------------------------------------------
    function askPanel(k) {
      var box = h("div", { class: "docs" });
      var out = h("div", { "aria-live": "polite" });
      var body = h("div", {});
      async function show() {
        try {
          var cur = await api("GET", "/kbs/" + k.id + "/access");
          var ch = await api("GET", "/groups");
          var picker = accessPicker({ visibility: cur.restricted ? "groups" : "everyone", group_ids: cur.group_ids }, ch.groups, [],
            { noPeople: true, label: "Who can ask", everyoneText: "Anyone on a channel that uses this knowledge base can ask it. They still only see documents they are allowed to see." });
          body.replaceChildren(picker.el, h("div", { class: "actions" }, h("button", { type: "button", class: "btn", onclick: async function () {
            var v = picker.value();
            try { await api("PUT", "/kbs/" + k.id + "/access", { group_ids: v.visibility === "groups" ? v.group_ids : [] }); out.replaceChildren(msg("ok", "Saved. Applies to the next question.")); }
            catch (err) { if (me) out.replaceChildren(msg("bad", err.message)); }
          } }, "Save who can ask")));
        } catch (err) { if (me) body.replaceChildren(msg("bad", err.message)); }
      }
      box.appendChild(h("h4", {}, "Who can ask"));
      box.appendChild(body);
      box.appendChild(out);
      show();
      return box;
    }

    function card(k, data) {
      var name = h("input", { type: "text", id: "kn" + k.id, value: k.name, maxlength: "80" });
      var desc = h("textarea", { id: "kd" + k.id, rows: "2" });
      desc.value = k.description;
      var dept = selectOf(data.departments.map(function (d) { return [d.id, d.name]; }), k.department_id, null, "Department");
      dept.id = "kp" + k.id;
      var size = h("input", { type: "number", id: "ks" + k.id, value: blank(k.chunk_size), min: 100, max: 8000, step: 50, placeholder: "inherit (" + data.global.chunk_size + ")" });
      var strat = selectOf(data.strategies.map(function (s) { return [s.name, s.title]; }), k.chunk_strategy, null, "Chunking strategy");
      strat.id = "kst" + k.id;
      var stratHelp = h("div", { class: "help" });
      function showStratHelp() { var s = data.strategies.filter(function (x) { return x.name === strat.value; })[0]; stratHelp.textContent = s ? s.help : ""; }
      strat.addEventListener("change", showStratHelp);
      showStratHelp();
      var overlap = h("input", { type: "number", id: "ko" + k.id, value: blank(k.chunk_overlap), min: 0, max: 7999, step: 10, placeholder: "inherit (" + data.global.chunk_overlap + ")" });
      var out = h("div", { "aria-live": "polite" });
      var save = h("button", { type: "button", class: "btn primary", onclick: async function () {
        out.replaceChildren(msg("wait", "Saving…"));
        try {
          var r = await api("PUT", "/kbs/" + k.id, { name: name.value, description: desc.value, department_id: Number(dept.value),
            chunk_size: intOrNull(size.value), chunk_overlap: intOrNull(overlap.value), chunk_strategy: strat.value });
          await load(msg("ok", "Saved " + r.name + "." + (r.note ? " " + r.note : "")));
        } catch (err) { if (me) out.replaceChildren(msg("bad", err.message)); }
      } }, "Save changes");
      var del = confirmButton("Delete", "Confirm delete", async function () {
        try {
          var r = await api("DELETE", "/kbs/" + k.id);
          await load(msg("ok", "Deleted " + r.name + ": " + r.documents_removed + " documents and " + r.chunks_removed + " chunks removed" + (r.channels_unbound.length ? ", unbound from " + r.channels_unbound.join(", ") : "") + "."));
        } catch (err) { if (me) out.replaceChildren(msg("bad", err.message)); }
      }, !k.is_default);
      return h("div", { class: "panel" },
        h("h3", {}, k.name, k.is_default ? h("span", { class: "pill ok" }, "Default") : null),
        h("div", { class: "meta" }, (k.documents === null ? "?" : k.documents) + " documents · " + (k.chunks === null ? "?" : k.chunks) + " chunks · " + k.department
          + (k.bound_channels.length ? " · used by " + k.bound_channels.join(", ") : " · not bound to a channel")),
        h("div", { class: "form" },
          h("div", { class: "field" }, h("label", { class: "l", for: "kn" + k.id }, "Name"), name),
          h("div", { class: "field" }, h("label", { class: "l", for: "kp" + k.id }, "Owning department"), dept),
          h("div", { class: "field wide" }, h("label", { class: "l", for: "kd" + k.id }, "Description"), desc),
          h("div", { class: "field wide" }, h("label", { class: "l", for: "kst" + k.id }, "Chunking strategy"), strat, stratHelp),
          h("div", { class: "field" }, h("label", { class: "l", for: "ks" + k.id }, "Chunk size (characters)"), size,
            h("div", { class: "help" }, "Empty = use the global value. For per-page, heading and semantic strategies this is the largest a chunk can be. The index keeps its current settings until you rebuild it.")),
          h("div", { class: "field" }, h("label", { class: "l", for: "ko" + k.id }, "Chunk overlap (characters)"), overlap),
          h("div", { class: "field" }, h("div", { class: "l" }, "Embedding model"), h("div", {}, "Local MiniLM (built in)")),
          h("div", { class: "field" }, h("div", { class: "l" }, "Vector store"), h("div", {}, "Chroma"))),
        h("div", { class: "actions" }, save, del), out, askPanel(k), embeddingPanel(k, data), storePanel(k, data), previewPanel(k, data, strat, size, overlap), indexPanel(k));
    }

    function docsCard(k, data) {
      return h("div", { class: "panel" },
        h("h3", {}, k.name, k.is_default ? h("span", { class: "pill ok" }, "Default") : null),
        h("div", { class: "meta" }, (k.documents === null ? "?" : k.documents) + " documents · " + (k.chunks === null ? "?" : k.chunks) + " chunks · " + k.department
          + (k.bound_channels.length ? " · used by " + k.bound_channels.join(", ") : " · not bound to a channel")),
        docsPanel(k, data));
    }

    async function load(notice) {
      try {
        var data = await api("GET", "/kbs");
        var kids = [notice || null];
        var documentsPart = state.kbPart === "documents";
        if (!data.kbs.length) kids.push(h("p", { class: "sub" }, documentsPart ? "No knowledge bases for your departments yet. Add one under Part 2: Configuration, then come back to upload documents." : "No knowledge bases for your departments yet."));
        data.kbs.forEach(function (k) { kids.push(documentsPart ? docsCard(k, data) : card(k, data)); });
        if (documentsPart) { holder.replaceChildren.apply(holder, kids.filter(Boolean)); return; }
        kids.push(h("h3", {}, "Add a knowledge base"));
        var nn = h("input", { type: "text", id: "nk_name", autocomplete: "off", required: true, maxlength: "80" });
        var nd = selectOf(data.departments.map(function (d) { return [d.id, d.name]; }), data.departments.length ? data.departments[0].id : "", null, "Department");
        nd.id = "nk_dept";
        kids.push(h("form", { class: "inline", onsubmit: async function (e) {
          e.preventDefault();
          result.replaceChildren(msg("wait", "Adding…"));
          try {
            await api("POST", "/kbs", { name: nn.value, department_id: Number(nd.value) });
            await load(msg("ok", "Knowledge base added."));
          } catch (err) { if (me) result.replaceChildren(msg("bad", err.message)); }
        } },
          h("div", { class: "field" }, h("label", { class: "l", for: "nk_name" }, "Name"), nn),
          h("div", { class: "field" }, h("label", { class: "l", for: "nk_dept" }, "Owning department"), nd),
          h("div", {}, h("button", { class: "btn", type: "submit" }, "Add knowledge base"))));
        kids.push(result);
        holder.replaceChildren.apply(holder, kids.filter(Boolean));
      } catch (err) { if (me) holder.replaceChildren(msg("bad", err.message)); }
    }
    await load();
  }

  // ---- prompts -------------------------------------------------------------------------
  // One editor for one prompt (a base prompt or a department's): text box, save draft, publish, preview, versions.
  function promptEditor(opts) {
    var box = h("div", { class: "panel" });
    var area = h("textarea", { id: "pt_" + opts.scope + "_" + opts.department_id, rows: "12", spellcheck: "false", "aria-label": opts.title });
    var counter = h("span", { class: "meta" });
    var live = h("div", { class: "meta" });
    var out = h("div", { "aria-live": "polite" });
    var table = h("div", {});
    var previewOut = h("div", { "aria-live": "polite" });
    var versions = [];
    var q = "?scope=" + encodeURIComponent(opts.scope) + "&department_id=" + opts.department_id;

    function count() {
      var n = area.value.trim().length;
      counter.textContent = n + " / " + opts.limit + " characters";
      counter.className = n > opts.limit ? "meta bad" : "meta";
    }
    area.addEventListener("input", count);

    async function saveDraft() {
      return api("POST", "/prompts/drafts", { scope: opts.scope, department_id: opts.department_id, text: area.value });
    }
    async function refresh(notice) {
      try {
        var r = await api("GET", "/prompts/versions" + q);
        versions = r.versions;
        var pub = versions.filter(function (v) { return v.status === "Published"; })[0];
        var draft = versions.filter(function (v) { return v.status === "Draft"; })[0];
        live.textContent = pub ? "Live: version " + pub.version + ", published " + when(pub.published_at) + " by " + (pub.published_by || "?")
          : (opts.scope === "dept" ? "No department prompt is live. The base prompt is used on its own." : "Using the built-in text.");
        if (notice !== "keep") { area.value = draft ? draft.text : pub ? pub.text : ""; count(); }
        var body = h("tbody", {});
        versions.forEach(function (v) {
          var kind = v.status === "Published" ? "ok" : v.status === "Draft" ? "off" : "bad";
          body.appendChild(h("tr", {},
            h("td", { "data-label": "Version" }, "v" + v.version),
            h("td", { "data-label": "Status" }, h("span", { class: "pill " + kind }, v.status)),
            h("td", { class: "wrap", "data-label": "Saved by" }, v.created_by),
            h("td", { class: "nw", "data-label": "Saved" }, when(v.created_at)),
            h("td", { class: "nw", "data-label": "Size" }, v.chars + " chars"),
            h("td", { class: "act" },
              h("button", { type: "button", class: "btn small", onclick: function () { area.value = v.text; count(); out.replaceChildren(msg("ok", "Version " + v.version + " is in the box. Save it as a draft or publish it.")); } }, "Load"),
              v.status === "Draft" ? confirmButton("Publish", "Confirm publish", async function () { await doPublish(v.id); }, true) : null,
              v.status === "Archived" ? confirmButton("Roll back", "Confirm roll back", async function () { await doRollback(v.id); }, true) : null)));
        });
        table.replaceChildren(versions.length ? h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {},
          ["Version", "Status", "Saved by", "Saved", "Size", ""].map(function (t) { return h("th", {}, t); }))), body)) : null);
      } catch (err) { if (me) out.replaceChildren(msg("bad", err.message)); }
    }
    async function doPublish(id) {
      try { var r = await api("POST", "/prompts/versions/" + id + "/publish", {}); await refresh(); out.replaceChildren(msg("ok", "Published version " + r.version + ". It is used from the next question.")); if (opts.onChange) opts.onChange(); }
      catch (err) { if (me) out.replaceChildren(msg("bad", err.message)); }
    }
    async function doRollback(id) {
      try { var r = await api("POST", "/prompts/versions/" + id + "/rollback", {}); await refresh(); out.replaceChildren(msg("ok", "Rolled back to version " + r.version + ". It is used from the next question.")); if (opts.onChange) opts.onChange(); }
      catch (err) { if (me) out.replaceChildren(msg("bad", err.message)); }
    }

    var saveBtn = h("button", { type: "button", class: "btn", onclick: async function () {
      out.replaceChildren(msg("wait", "Saving…"));
      try { var v = await saveDraft(); await refresh("keep"); out.replaceChildren(msg("ok", "Saved as draft version " + v.version + ". Users are not affected until you publish.")); }
      catch (err) { if (me) out.replaceChildren(msg("bad", err.message)); }
    } }, "Save as draft");
    var publishBtn = confirmButton("Save and publish", "Confirm: publish now", async function () {
      out.replaceChildren(msg("wait", "Publishing…"));
      try { var v = await saveDraft(); await doPublish(v.id); }
      catch (err) { if (me) out.replaceChildren(msg("bad", err.message)); }
    }, true);

    var question = h("input", { type: "text", id: "pq_" + opts.scope + "_" + opts.department_id, placeholder: "Type a test question", autocomplete: "off", "aria-label": "Test question" });
    var tryBtn = h("button", { type: "button", class: "btn", onclick: async function () {
      previewOut.replaceChildren(msg("wait", "Asking with the text in the box…"));
      try {
        var r = await api("POST", "/prompts/preview", { scope: opts.scope, department_id: opts.department_id, text: area.value, question: question.value });
        previewOut.replaceChildren(h("div", { class: "panel" }, h("b", {}, "Preview answer"), h("p", {}, r.answer),
          r.sources && r.sources.length ? h("div", { class: "meta" }, "Sources: " + r.sources.map(function (s) { return s.source + (s.page ? " p." + s.page : ""); }).join(", ")) : null,
          h("div", { class: "meta" }, "Not saved to any conversation. Users are not affected.")));
      } catch (err) { if (me) previewOut.replaceChildren(msg("bad", err.message)); }
    } }, "Try it");

    box.appendChild(h("h3", {}, opts.title));
    box.appendChild(live);
    box.appendChild(h("div", { class: "field wide" }, area, counter));
    box.appendChild(h("div", { class: "actions" }, saveBtn, publishBtn));
    box.appendChild(out);
    box.appendChild(h("h4", {}, "Try it before publishing"));
    box.appendChild(h("p", { class: "note" }, "Runs the normal answering steps with the text in the box. Nothing is published or saved to a conversation."));
    box.appendChild(h("div", { class: "actions" }, question, tryBtn));
    box.appendChild(previewOut);
    box.appendChild(h("h4", {}, "Versions"));
    box.appendChild(table);
    refresh();
    return box;
  }

  async function viewPrompts(main) {
    var isAdmin = state.perm && state.perm.can_manage_all;
    main.appendChild(h("h2", {}, "Prompts"));
    main.appendChild(h("p", { class: "sub" }, isAdmin
      ? "The base prompts set who the assistant is and how it answers; only Admins change them and a department cannot override them. A department prompt adds tone and focus for that department. Every save is a new draft, nothing goes live until you publish, and you can roll back in one step."
      : "Your department's prompt adds tone and focus on top of the base prompt, which always takes priority. Every save is a new draft; nothing goes live until you publish, and you can roll back in one step."));
    var holder = h("div", {}, msg("wait", "Loading…"));
    main.appendChild(holder);
    try {
      var o = await api("GET", "/prompts");
      var kids = [];
      if (isAdmin) {
        kids.push(h("h3", {}, "Base prompts"));
        o.base.forEach(function (b) {
          kids.push(promptEditor({ scope: b.scope, department_id: 0, title: b.title, limit: o.base_limit }));
        });
      }
      kids.push(h("h3", {}, "Department prompts"));
      if (!o.departments.length) kids.push(h("p", { class: "sub" }, "You are not an editor of any department yet. Ask an Admin."));
      else {
        var slot = h("div", {});
        var choices = o.departments.map(function (d) { return [d.department_id, d.department + (d.published ? " (v" + d.published.version + " live)" : " (none)")]; });
        var pick = selectOf(choices, choices[0][0], function () { show(); }, "Department");
        pick.id = "pt_dept";
        function show() {
          var d = o.departments.filter(function (x) { return String(x.department_id) === pick.value; })[0];
          slot.replaceChildren(promptEditor({ scope: "dept", department_id: d.department_id, title: "Prompt for " + d.department, limit: o.limit }));
        }
        if (o.departments.length > 1) kids.push(h("div", { class: "field" }, h("label", { class: "l", for: "pt_dept" }, "Department"), pick));
        kids.push(slot);
        show();
      }
      holder.replaceChildren.apply(holder, kids);
    } catch (err) { if (me) holder.replaceChildren(msg("bad", err.message)); }
  }

  // ---- guardrails -----------------------------------------------------------------------
  async function viewGuardrails(main) {
    main.appendChild(h("h2", {}, "Guardrails"));
    main.appendChild(h("p", { class: "sub" }, "Checks around every question and answer. A blocked question gets the blocked reply and no model is called. Personal data is masked before it reaches the model, the conversation history or any log. Banned topics are set per department on the Departments screen. Changes apply within a few seconds."));
    var holder = h("div", {}, msg("wait", "Loading…"));
    main.appendChild(holder);

    function show(data, notice) {
      var sf = specForm(data.fields);
      var result = h("div", { "aria-live": "polite" }, notice || null);
      var kind = data.status === "ready" ? "ok" : data.status === "off" ? "off" : data.status === "loading" ? "off" : "bad";
      var label = { ready: "Detection model ready", off: "Guardrails off", loading: "Detection model loading…", error: "Detection model failed to load" }[data.status] || data.status;
      var save = h("button", { type: "button", class: "btn primary", onclick: async function () {
        result.replaceChildren(msg("wait", "Saving…"));
        try { show(await api("PUT", "/guardrails", { values: sf.collect() }), msg("ok", "Saved. The new values are in use within a few seconds.")); }
        catch (err) { if (me) result.replaceChildren(msg("bad", err.message)); }
      } }, "Save changes");
      holder.replaceChildren(h("div", { class: "panel" },
        h("div", { class: "meta" }, h("span", { class: "pill " + kind }, label), " Last changed " + when(data.updated_at) + " by " + data.updated_by),
        sf.el, h("div", { class: "actions" }, save), result,
        h("p", { class: "note" }, "Injection, toxicity and banned-topic checks are rule-based: they catch known phrasing and listed words, not paraphrases or other languages. Names are not masked unless you turn that on; detection of names is approximate.")));
    }
    try { show(await api("GET", "/guardrails")); }
    catch (err) { if (me) holder.replaceChildren(msg("bad", err.message)); }
  }

  // ---- conversations (Admin) -------------------------------------------------------------
  async function viewConversations(main) {
    main.appendChild(h("h2", {}, "Conversations"));
    main.appendChild(h("p", { class: "sub" }, "Stored conversations from every channel. Personal data was masked before it was stored. Opening or deleting one is recorded in the audit log."));
    var filters = h("div", { class: "form" });
    var dept = h("select", { id: "cv_dept", "aria-label": "Department" }, h("option", { value: "", text: "All departments" }));
    var chan = selectOf([["", "All channels"], ["web", "Web UI"], ["slack", "Slack"], ["teams", "Teams"], ["gchat", "Google Chat"], ["other", "Other"]], "", null, "Channel");
    chan.id = "cv_chan";
    var person = h("input", { type: "text", id: "cv_person", placeholder: "Part of an email", autocomplete: "off", "aria-label": "Person" });
    var dr = dateRange("cv", function () { page = 0; load(); }, false);
    var from = dr.from, to = dr.to;
    var list = h("div", {});
    var detail = h("div", {});
    var page = 0, size = 25;

    async function load() {
      var q = "?limit=" + size + "&offset=" + (page * size) + "&department=" + encodeURIComponent(dept.value) + "&channel=" + encodeURIComponent(chan.value)
        + "&person=" + encodeURIComponent(person.value) + "&date_from=" + from.value + "&date_to=" + to.value;
      try {
        var r = await api("GET", "/conversations" + q);
        if (dept.options.length === 1) r.departments.forEach(function (d) { dept.appendChild(h("option", { value: d, text: d })); });
        var body = h("tbody", {});
        r.conversations.forEach(function (c) {
          body.appendChild(h("tr", {},
            h("td", { class: "wrap", "data-label": "Conversation" }, h("b", {}, c.title), h("div", { class: "meta" }, c.person)),
            h("td", { "data-label": "Channel" }, c.channel),
            h("td", { "data-label": "Department" }, c.department || "–"),
            h("td", { class: "nw", "data-label": "Messages" }, String(c.messages)),
            h("td", { class: "nw", "data-label": "Last activity" }, when(c.updated_at)),
            h("td", { class: "act" },
              h("button", { type: "button", class: "btn small", onclick: function () { open(c.id); } }, "Open"),
              confirmButton("Delete", "Confirm delete", async function () {
                try { await api("DELETE", "/conversations/" + encodeURIComponent(c.id)); detail.replaceChildren(msg("ok", "Deleted.")); load(); }
                catch (err) { if (me) detail.replaceChildren(msg("bad", err.message)); }
              }, true))));
        });
        var pages = Math.max(1, Math.ceil(r.total / size));
        list.replaceChildren(
          r.conversations.length ? h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {},
            ["Conversation", "Channel", "Department", "Messages", "Last activity", ""].map(function (t) { return h("th", {}, t); }))), body))
            : h("p", { class: "sub" }, "No conversations match."),
          h("div", { class: "actions" },
            h("button", { type: "button", class: "btn small", disabled: page === 0, onclick: function () { page--; load(); } }, "Previous"),
            h("span", { class: "meta" }, "Page " + (page + 1) + " of " + pages + " · " + r.total + " conversations"),
            h("button", { type: "button", class: "btn small", disabled: page + 1 >= pages, onclick: function () { page++; load(); } }, "Next")));
      } catch (err) { if (me) list.replaceChildren(msg("bad", err.message)); }
    }
    async function open(id) {
      detail.replaceChildren(msg("wait", "Loading…"));
      try {
        var r = await api("GET", "/conversations/" + encodeURIComponent(id));
        detail.replaceChildren(h("div", { class: "panel" },
          h("h3", {}, r.title || "Conversation"), h("div", { class: "meta" }, r.person + " · " + r.channel + (r.department ? " · " + r.department : "")),
          r.messages.map(function (m) {
            return h("div", { class: "field wide" }, h("div", { class: "l" }, m.role === "user" ? "Question" : "Answer" + (m.incomplete ? " (interrupted)" : "")),
              h("div", {}, m.content));
          }),
          h("div", { class: "actions" }, h("button", { type: "button", class: "btn small", onclick: function () { detail.replaceChildren(); } }, "Close"))));
      } catch (err) { if (me) detail.replaceChildren(msg("bad", err.message)); }
    }
    // retention and export panels
    var retBox = h("div", { class: "panel" });
    var exBox = h("div", { class: "panel" });
    async function loadRetention(notice) {
      try {
        var r = await api("GET", "/conversations/retention/status");
        var total = r.preview.reduce(function (n, d) { return n + d.expired; }, 0);
        var body = h("tbody", {});
        r.preview.forEach(function (d) {
          body.appendChild(h("tr", {}, h("td", { "data-label": "Department" }, d.department), h("td", { "data-label": "Keeps" }, d.days + " days"),
            h("td", { "data-label": "Expired now" }, String(d.expired))));
        });
        retBox.replaceChildren(h("h3", {}, "Retention"),
          h("p", { class: "note" }, "Conversations with no activity for longer than their department's retention are deleted for good, once a day" + (r.enabled ? "." : " (the daily job is turned off in Settings).")),
          h("div", { class: "meta" }, r.last_run ? "Last run " + when(r.last_run.ts) + " by " + r.last_run.actor + ": " + r.last_run.detail : "No run yet."),
          h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Department", "Keeps", "Expired now"].map(function (t) { return h("th", {}, t); }))), body)),
          h("div", { class: "actions" },
            confirmButton("Delete " + total + " expired now", "Confirm: delete for good", async function () {
              try { var d = await api("POST", "/conversations/retention/run", {}); await loadRetention(msg("ok", "Deleted " + d.conversations + " conversations (" + d.messages + " messages).")); load(); }
              catch (err) { if (me) retBox.insertBefore(msg("bad", err.message), retBox.firstChild); }
            }, total > 0),
            h("button", { type: "button", class: "btn small", onclick: function () { loadRetention(); } }, "Refresh")),
          notice || null);
      } catch (err) { if (me) retBox.replaceChildren(msg("bad", err.message)); }
    }
    async function loadExport() {
      try {
        var s = await api("GET", "/settings");
        var on = s.fields.filter(function (f) { return f.name === "export_enabled"; })[0].value;
        if (!on) {
          exBox.replaceChildren(h("h3", {}, "Export"), h("p", { class: "note" }, "Conversation export is turned off. Turn on \"Allow conversation export\" under Settings once the privacy sign-off is recorded."));
          return;
        }
        var reason = h("textarea", { id: "ex_reason", rows: "2", placeholder: "Why is this export needed? (recorded in the audit log)", "aria-label": "Reason" });
        var fmt = selectOf([["csv", "CSV"], ["json", "JSON"]], "csv", null, "Format");
        var out = h("div", { "aria-live": "polite" });
        exBox.replaceChildren(h("h3", {}, "Export"),
          h("p", { class: "note" }, "Exports the stored (masked) messages for the department and dates chosen in the filters above. People appear as anonymous references, not email addresses. Every export is recorded with your reason."),
          h("div", { class: "field wide" }, h("label", { class: "l", for: "ex_reason" }, "Reason"), reason),
          h("div", { class: "actions" }, fmt, h("button", { type: "button", class: "btn", onclick: async function () {
            out.replaceChildren(msg("wait", "Preparing the file…"));
            try {
              var res = await fetch(API + "/conversations/export", { method: "POST", credentials: "same-origin",
                headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
                body: JSON.stringify({ reason: reason.value, format: fmt.value, department: dept.value, date_from: from.value, date_to: to.value }) });
              if (!res.ok) { var d = {}; try { d = await res.json(); } catch (e) { /* none */ } throw new Error(d.detail || "Export failed (" + res.status + ")"); }
              var blob = await res.blob();
              var a = document.createElement("a");
              a.href = URL.createObjectURL(blob); a.download = "conversations." + fmt.value; document.body.appendChild(a); a.click(); a.remove();
              out.replaceChildren(msg("ok", "Downloaded. The export was recorded in the audit log."));
            } catch (err) { out.replaceChildren(msg("bad", err.message)); }
          } }, "Export")), out);
      } catch (err) { if (me) exBox.replaceChildren(msg("bad", err.message)); }
    }
    var go = h("button", { type: "button", class: "btn", onclick: function () { page = 0; load(); } }, "Apply filters");
    filters.appendChild(h("div", { class: "field" }, h("label", { class: "l", for: "cv_dept" }, "Department"), dept));
    filters.appendChild(h("div", { class: "field" }, h("label", { class: "l", for: "cv_chan" }, "Channel"), chan));
    filters.appendChild(h("div", { class: "field" }, h("label", { class: "l", for: "cv_person" }, "Person"), person));
    filters.appendChild(h("div", { class: "field wide" }, dr.el));
    main.appendChild(filters);
    main.appendChild(h("div", { class: "actions" }, go));
    main.appendChild(detail);
    main.appendChild(list);
    main.appendChild(retBox);
    main.appendChild(exBox);
    load();
    loadRetention();
    loadExport();
  }

  // ---- usage and cost -------------------------------------------------------------------
  async function viewUsage(main) {
    var isAdmin = state.perm && state.perm.can_manage_all;
    main.appendChild(h("h2", {}, "Usage and cost"));
    main.appendChild(h("p", { class: "sub" }, (isAdmin ? "Every model call: answers, routing, summaries, previews and embeddings." : "Model usage of your departments.")
      + " Cost is counted only for models that have a price; calls without one are shown as unpriced, so a total is never the whole truth."));
    var GROUPS = [["provider", "Provider"], ["model", "Model"], ["department", "Department"], ["kb", "Knowledge base"], ["purpose", "Purpose"], ["kind", "Answers or embeddings"], ["day", "Day"], ["month", "Month"]];
    var group = selectOf(GROUPS, "provider", null, "Group by");
    group.id = "us_group";
    var dr = dateRange("us", function () { load(); }, false);
    var from = dr.from, to = dr.to;
    var list = h("div", {});
    var priceBox = h("div", { class: "panel" });
    function n(x) { return Number(x).toLocaleString(); }
    function money(x) { return x === 0 ? "$0" : "$" + (x < 0.01 ? x.toFixed(6) : x.toFixed(4)); }

    async function load() {
      try {
        var r = await api("GET", "/usage?group=" + group.value + "&date_from=" + from.value + "&date_to=" + to.value);
        var maxCost = Math.max.apply(null, r.rows.map(function (x) { return x.cost; }).concat([0.000001]));
        var body = h("tbody", {});
        r.rows.forEach(function (x) {
          body.appendChild(h("tr", {},
            h("td", { class: "wrap", "data-label": "Group" }, x.label),
            h("td", { class: "nw", "data-label": "Calls" }, n(x.calls) + (x.failed_calls ? " (" + x.failed_calls + " failed)" : "")),
            h("td", { class: "nw", "data-label": "Tokens in" }, n(x.input_tokens)),
            h("td", { class: "nw", "data-label": "Tokens out" }, n(x.output_tokens)),
            h("td", { class: "nw", "data-label": "Cost" }, money(x.cost), " ", h("meter", { min: 0, max: maxCost, value: x.cost, "aria-label": "Share of cost" })),
            h("td", { class: "nw", "data-label": "Unpriced" }, x.unpriced_calls ? h("span", { class: "pill off" }, x.unpriced_calls + " calls") : "–")));
        });
        var t = r.totals;
        body.appendChild(h("tr", {}, h("td", {}, h("b", {}, "Total (" + r.scope + ")")), h("td", { class: "nw" }, n(t.calls)), h("td", { class: "nw" }, n(t.input_tokens)),
          h("td", { class: "nw" }, n(t.output_tokens)), h("td", { class: "nw" }, h("b", {}, money(t.cost))),
          h("td", { class: "nw" }, t.unpriced_calls ? h("span", { class: "pill bad" }, t.unpriced_calls + " unpriced") : "–")));
        list.replaceChildren(r.rows.length ? h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {},
          ["Group", "Calls", "Tokens in", "Tokens out", "Cost", "Unpriced"].map(function (c) { return h("th", {}, c); }))), body)) : h("p", { class: "sub" }, "No usage in this period."),
          t.unpriced_calls ? msg("bad", t.unpriced_calls + " successful calls have no price, so the cost above is a minimum. " + (isAdmin ? "Set prices below." : "Ask an Admin to set prices.")) : null);
      } catch (err) { if (me) list.replaceChildren(msg("bad", err.message)); }
    }

    async function loadPrices(notice) {
      try {
        var r = await api("GET", "/prices");
        var body = h("tbody", {});
        r.prices.forEach(function (p) {
          body.appendChild(h("tr", {}, h("td", { "data-label": "Provider type" }, p.provider_type), h("td", { class: "wrap", "data-label": "Model" }, p.model),
            h("td", { class: "nw", "data-label": "Input / 1k tokens" }, "$" + p.input_per_1k), h("td", { class: "nw", "data-label": "Output / 1k tokens" }, "$" + p.output_per_1k),
            h("td", { class: "act" }, confirmButton("Delete", "Confirm delete", async function () {
              try { await api("DELETE", "/prices/" + p.id); await loadPrices(msg("ok", "Price removed.")); load(); } catch (err) { if (me) priceBox.insertBefore(msg("bad", err.message), priceBox.firstChild); }
            }, true))));
        });
        var ptype = h("input", { type: "text", id: "pr_type", placeholder: "openai, google, anthropic…", autocomplete: "off", "aria-label": "Provider type" });
        var model = h("input", { type: "text", id: "pr_model", placeholder: "model name", autocomplete: "off", "aria-label": "Model" });
        var pin = h("input", { type: "number", id: "pr_in", step: "any", min: 0, placeholder: "USD per 1,000 input tokens", "aria-label": "Input price" });
        var pout = h("input", { type: "number", id: "pr_out", step: "any", min: 0, placeholder: "USD per 1,000 output tokens", "aria-label": "Output price" });
        var out = h("div", { "aria-live": "polite" });
        priceBox.replaceChildren(h("h3", {}, "Prices"),
          h("p", { class: "note" }, "Typed in by hand, per 1,000 tokens (check your provider's price page). Nothing is guessed: a model without a price shows its calls as unpriced."),
          r.unpriced.length ? h("div", { class: "meta" }, "Used without a price: " + r.unpriced.map(function (u) { return u.provider + " / " + u.model + " (" + u.unpriced_calls + ")"; }).join(", ")) : null,
          r.prices.length ? h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Provider type", "Model", "Input / 1k", "Output / 1k", ""].map(function (c) { return h("th", {}, c); }))), body)) : null,
          h("form", { class: "inline", onsubmit: async function (e) {
            e.preventDefault();
            try { await api("PUT", "/prices", { provider_type: ptype.value, model: model.value, input_per_1k: Number(pin.value), output_per_1k: Number(pout.value || 0) }); await loadPrices(msg("ok", "Price saved. It applies to calls from now on.")); }
            catch (err) { if (me) out.replaceChildren(msg("bad", err.message)); }
          } },
            h("div", { class: "field" }, h("label", { class: "l", for: "pr_type" }, "Provider type"), ptype),
            h("div", { class: "field" }, h("label", { class: "l", for: "pr_model" }, "Model"), model),
            h("div", { class: "field" }, h("label", { class: "l", for: "pr_in" }, "Input price"), pin),
            h("div", { class: "field" }, h("label", { class: "l", for: "pr_out" }, "Output price"), pout),
            h("div", {}, h("button", { class: "btn", type: "submit" }, "Save price"))),
          out, notice || null);
      } catch (err) { if (me) priceBox.replaceChildren(msg("bad", err.message)); }
    }

    var filters = h("div", { class: "form" },
      h("div", { class: "field" }, h("label", { class: "l", for: "us_group" }, "Group by"), group),
      h("div", { class: "field wide" }, dr.el));
    main.appendChild(filters);
    main.appendChild(h("div", { class: "actions" }, h("button", { type: "button", class: "btn", onclick: load }, "Show")));
    main.appendChild(list);
    if (isAdmin) { main.appendChild(priceBox); loadPrices(); }
    load();
  }

  // ---- departments -----------------------------------------------------------------
  async function viewDepartments(main) {
    var isAdmin = state.perm && state.perm.can_manage_all;
    main.appendChild(h("h2", {}, "Departments"));
    main.appendChild(h("p", { class: "sub" }, isAdmin
      ? "Each department has its own conversation retention and topics the assistant must not discuss. Editors can change these for the departments they look after."
      : "The departments you look after. You can change the banned topics and how long conversations are kept."));
    var holder = h("div", {}, msg("wait", "Loading…"));
    main.appendChild(holder);
    var result = h("div", { "aria-live": "polite" });
    var editors = [];
    if (isAdmin) {
      try { editors = (await api("GET", "/users")).filter(function (u) { return u.role === "editor" && u.status === "Active"; }); }
      catch (err) { /* the list simply stays empty */ }
    }

    function card(d, max) {
      var name = h("input", { type: "text", id: "dn" + d.id, value: d.name, maxlength: "80" });
      var days = h("input", { type: "number", id: "dr" + d.id, value: d.retention_days, min: 1, max: max, step: 1 });
      var topics = h("textarea", { id: "dt" + d.id, rows: "5", placeholder: "One topic per line" });
      topics.value = d.banned_topics;
      var picks = editors.map(function (u) {
        var cb = h("input", { type: "checkbox", id: "de" + d.id + "_" + u.id, checked: d.editors.some(function (x) { return x.id === u.id; }) });
        return [u, cb];
      });
      var out = h("div", { "aria-live": "polite" });
      var save = h("button", { type: "button", class: "btn primary", onclick: async function () {
        out.replaceChildren(msg("wait", "Saving…"));
        var change = { retention_days: days.value === "" ? null : Number(days.value), banned_topics: topics.value };
        if (isAdmin) {
          change.name = name.value;
          change.editor_ids = picks.filter(function (p) { return p[1].checked; }).map(function (p) { return p[0].id; });
        }
        try { await api("PUT", "/departments/" + d.id, change); await load(msg("ok", "Saved " + (isAdmin ? name.value : d.name) + ".")); }
        catch (err) { if (me) out.replaceChildren(msg("bad", err.message)); }
      } }, "Save changes");
      var extra = [];
      if (isAdmin) {
        if (!d.is_default) extra.push(h("button", { type: "button", class: "btn small", onclick: async function () {
          try { await api("PUT", "/departments/" + d.id, { is_default: true }); await load(msg("ok", d.name + " is now the default.")); }
          catch (err) { if (me) out.replaceChildren(msg("bad", err.message)); }
        } }, "Make default"));
        extra.push(confirmButton("Delete", "Confirm delete", async function () {
          try { await api("DELETE", "/departments/" + d.id); await load(msg("ok", "Deleted " + d.name + ".")); }
          catch (err) { if (me) out.replaceChildren(msg("bad", err.message)); }
        }, !d.is_default));
      }
      return h("div", { class: "panel" },
        h("h3", {}, d.name, d.is_default ? h("span", { class: "pill ok" }, "Default") : null),
        h("div", { class: "meta" }, d.members + (d.members === 1 ? " person" : " people")),
        h("div", { class: "form" },
          isAdmin ? h("div", { class: "field" }, h("label", { class: "l", for: "dn" + d.id }, "Name"), name) : null,
          h("div", { class: "field" }, h("label", { class: "l", for: "dr" + d.id }, "Keep conversations (days)"), days,
            h("div", { class: "help" }, "Between 1 and " + max + " (the global maximum).")),
          h("div", { class: "field wide" }, h("label", { class: "l", for: "dt" + d.id }, "Banned topics"), topics,
            h("div", { class: "help" }, "The assistant politely declines these. One per line."))),
        isAdmin ? h("div", { class: "field" }, h("div", { class: "l" }, "Editors"),
          picks.length ? picks.map(function (p) { return h("label", { class: "check", for: p[1].id }, p[1], p[0].name || p[0].email); })
                       : h("div", { class: "help" }, "No active Editors yet. Add a user with the Editor role first.")) : null,
        h("div", { class: "actions" }, save, extra), out);
    }

    async function load(notice) {
      try {
        var data = await api("GET", "/departments");
        var kids = [notice || null];
        if (!data.departments.length) kids.push(h("p", { class: "sub" }, "You are not an editor of any department yet. Ask an Admin."));
        data.departments.forEach(function (d) { kids.push(card(d, data.max_retention_days)); });
        holder.replaceChildren.apply(holder, kids.filter(Boolean));
      } catch (err) { if (me) holder.replaceChildren(msg("bad", err.message)); }
    }

    if (isAdmin) {
      var nn = h("input", { type: "text", id: "nd_name", autocomplete: "off", required: true, maxlength: "80" });
      var nr = h("input", { type: "number", id: "nd_days", value: 90, min: 1, step: 1 });
      main.appendChild(h("h3", {}, "Add a department"));
      main.appendChild(h("form", { class: "inline", onsubmit: async function (e) {
        e.preventDefault();
        result.replaceChildren(msg("wait", "Adding…"));
        try {
          await api("POST", "/departments", { name: nn.value, retention_days: Number(nr.value) });
          nn.value = "";
          result.replaceChildren(msg("ok", "Department added."));
          await load();
        } catch (err) { if (me) result.replaceChildren(msg("bad", err.message)); }
      } },
        h("div", { class: "field" }, h("label", { class: "l", for: "nd_name" }, "Name"), nn),
        h("div", { class: "field" }, h("label", { class: "l", for: "nd_days" }, "Keep conversations (days)"), nr),
        h("div", {}, h("button", { class: "btn", type: "submit" }, "Add department"))));
      main.appendChild(result);
    }
    await load();
  }

  // ---- account --------------------------------------------------------------------
  function viewAccount(main) {
    main.appendChild(h("h2", {}, "Account"));
    main.appendChild(h("p", { class: "sub" }, "Signed in as " + me + ". Changing your password signs out every other session."));
    var cur = h("input", { type: "password", id: "cur", autocomplete: "current-password", required: true });
    var nw = h("input", { type: "password", id: "nw", autocomplete: "new-password", required: true });
    var rep = h("input", { type: "password", id: "rep", autocomplete: "new-password", required: true });
    var result = h("div", { "aria-live": "polite" });
    main.appendChild(h("form", { class: "inline", onsubmit: async function (e) {
      e.preventDefault();
      if (nw.value !== rep.value) { result.replaceChildren(msg("bad", "The new passwords do not match.")); return; }
      result.replaceChildren(msg("wait", "Updating…"));
      try {
        var r = await api("POST", "/auth/password", { current: cur.value, new: nw.value });
        csrf = r.csrf;
        cur.value = nw.value = rep.value = "";
        result.replaceChildren(msg("ok", "Password updated."));
      } catch (err) { if (me) result.replaceChildren(msg("bad", err.message)); }
    } },
      h("div", { class: "field" }, h("label", { class: "l", for: "cur" }, "Current password"), cur),
      h("div", { class: "field" }, h("label", { class: "l", for: "nw" }, "New password (12+ characters)"), nw),
      h("div", { class: "field" }, h("label", { class: "l", for: "rep" }, "Repeat new password"), rep),
      h("div", {}, h("button", { class: "btn primary", type: "submit" }, "Change password"))));
    main.appendChild(result);
  }

  boot();
})();
