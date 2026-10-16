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
  var TABS = [["channels", "Channels"], ["settings", "Settings"], ["providers", "Providers"], ["knowledge", "Knowledge bases"], ["departments", "Departments"], ["users", "Users"], ["audit", "Audit log"], ["account", "Account"]];
  // Planned, not built yet: shown greyed out so admins can see what is coming. [label, tooltip]
  var UPCOMING = [
    ["Documents", "Upcoming: upload documents and control who can add and see which files."],
    ["Chunking", "Upcoming: choose how documents are split. Today: fixed 900 characters with 150 overlap. Planned: fixed size, per page, by heading, semantic."],
    ["Vector store", "Upcoming: choose where document vectors are kept. Today: ChromaDB. Planned: pgvector, Qdrant, Azure AI Search."],
    ["Retrieval", "Upcoming: tune how passages are found. Today: vector similarity with a score threshold. Planned: hybrid keyword + vector, re-ranker, query rewriting."]
  ];

  function renderShell() {
    var tabs = h("div", { class: "tabs" });
    var allowed = state.perm ? state.perm.screens : [];
    TABS.filter(function (t) { return allowed.indexOf(t[0]) >= 0; }).forEach(function (t) {
      tabs.appendChild(h("button", {
        type: "button", class: state.tab === t[0] ? "on" : "", "aria-current": state.tab === t[0] ? "page" : null,
        onclick: function () { state.tab = t[0]; renderShell(); }
      }, t[1]));
    });
    (state.perm && state.perm.can_manage_all ? UPCOMING : []).forEach(function (u) {
      tabs.appendChild(h("button", { type: "button", disabled: true, title: u[1] }, u[0] + " (upcoming)"));
    });
    var nav = h("nav", { class: "side", "aria-label": "Admin sections" },
      h("h1", {}, "Channel Agnostic Agent"),
      h("div", { class: "who" }, h("b", {}, me), h("span", {}, (state.perm ? ROLE_LABEL[state.perm.role] : "") + " · local sign-in"),
        h("button", { type: "button", onclick: signOut }, "Sign out")),
      tabs);
    var main = h("main", {});
    mount(h("div", { class: "app" }, nav, main));
    var view = { channels: viewChannels, settings: viewSettings, providers: viewProviders, knowledge: viewKnowledge, departments: viewDepartments, users: viewUsers, audit: viewAudit, account: viewAccount }[state.tab];
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
      var list = await api("GET", "/alerts");
      if (!list.length) return;
      var box = h("div", { class: "banners", role: "region", "aria-label": "Alerts" });
      list.forEach(function (a) {
        var row = h("div", { class: "banner " + a.severity },
          h("span", {}, a.message + (a.count > 1 ? " (" + a.count + " times)" : "")),
          h("button", { type: "button", class: "btn small", onclick: async function () {
            try { await api("POST", "/alerts/" + a.id + "/resolve", {}); row.remove(); if (!box.children.length) box.remove(); }
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
    main.appendChild(h("p", { class: "sub" }, "Who changed what and when. Secret values are never recorded."));
    var holder = h("div", {}, msg("wait", "Loading…"));
    main.appendChild(holder);
    try {
      var rows = await api("GET", "/audit?limit=200");
      var body = h("tbody", {});
      rows.forEach(function (r) {
        body.appendChild(h("tr", {}, h("td", { class: "nw", "data-label": "Time" }, when(r.ts)),
          h("td", { class: "wrap", "data-label": "Actor" }, r.actor),
          h("td", { class: "nw", "data-label": "Action" }, r.action),
          h("td", { class: "nw", "data-label": "Target" }, r.target),
          h("td", { class: "wrap", "data-label": "Detail" }, r.detail)));
      });
      holder.replaceChildren(h("div", { class: "tablewrap" }, h("table", {},
        h("thead", {}, h("tr", {}, ["Time", "Actor", "Action", "Target", "Detail"].map(function (t) { return h("th", {}, t); }))), body)));
    } catch (err) { if (me) holder.replaceChildren(msg("bad", err.message)); }
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

  // ---- knowledge bases ------------------------------------------------------------
  async function viewKnowledge(main) {
    main.appendChild(h("h2", {}, "Knowledge bases"));
    main.appendChild(h("p", { class: "sub" }, "A knowledge base is a set of documents the assistant answers from. Each belongs to a department and can use its own chunk settings. Choose which ones a channel uses on the Channels screen."));
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
          h("td", { class: "wrap", "data-label": "Document" }, h("b", {}, d.name), d.error ? h("div", { class: "meta bad" }, d.error) : null),
          h("td", { "data-label": "Status" }, pill(d)),
          h("td", { class: "nw", "data-label": "Chunks" }, d.status === "Indexed" ? String(d.chunks) : "–"),
          h("td", { class: "nw", "data-label": "Size" }, d.size ? size(d.size) : "–"),
          h("td", { class: "nw", "data-label": "Updated" }, when(d.updated_at)),
          h("td", { class: "act" },
            h("button", { type: "button", class: "btn small", onclick: function () { act(api("POST", "/documents/" + d.id + "/reindex", {}), "Re-indexing " + d.name + "."); } }, d.status === "Failed" ? "Retry" : "Re-index"),
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
      var uploadBtn = h("button", { type: "button", class: "btn", onclick: function () {
        var f = fileInput.files[0];
        if (!f) { out.replaceChildren(msg("bad", "Choose a file first.")); return; }
        var fd = new FormData(); fd.append("file", f);
        out.replaceChildren(msg("wait", "Uploading " + f.name + "…"));
        act(api("POST", "/kbs/" + k.id + "/documents", fd).then(function () { fileInput.value = ""; }), "Uploaded " + f.name + "; indexing has started.");
      } }, "Upload");
      var reindexAll = h("button", { type: "button", class: "btn small", onclick: function () {
        act(api("POST", "/kbs/" + k.id + "/reindex", {}).then(function (r) { out.replaceChildren(msg("ok", r.queued + " documents queued for re-indexing.")); }));
      } }, "Re-index all");
      box.appendChild(h("h4", {}, "Documents"));
      box.appendChild(table);
      box.appendChild(h("div", { class: "actions" }, fileInput, uploadBtn, reindexAll));
      box.appendChild(out);
      refresh();
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
              + e.note + (e.skipped_documents ? " " + e.skipped_documents + " document(s) cannot be read and will be left out." : "")),
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

    function card(k, data) {
      var name = h("input", { type: "text", id: "kn" + k.id, value: k.name, maxlength: "80" });
      var desc = h("textarea", { id: "kd" + k.id, rows: "2" });
      desc.value = k.description;
      var dept = selectOf(data.departments.map(function (d) { return [d.id, d.name]; }), k.department_id, null, "Department");
      dept.id = "kp" + k.id;
      var size = h("input", { type: "number", id: "ks" + k.id, value: blank(k.chunk_size), min: 100, max: 8000, step: 50, placeholder: "inherit (" + data.global.chunk_size + ")" });
      var overlap = h("input", { type: "number", id: "ko" + k.id, value: blank(k.chunk_overlap), min: 0, max: 7999, step: 10, placeholder: "inherit (" + data.global.chunk_overlap + ")" });
      var out = h("div", { "aria-live": "polite" });
      var save = h("button", { type: "button", class: "btn primary", onclick: async function () {
        out.replaceChildren(msg("wait", "Saving…"));
        try {
          var r = await api("PUT", "/kbs/" + k.id, { name: name.value, description: desc.value, department_id: Number(dept.value),
            chunk_size: intOrNull(size.value), chunk_overlap: intOrNull(overlap.value) });
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
          h("div", { class: "field" }, h("label", { class: "l", for: "ks" + k.id }, "Chunk size (characters)"), size,
            h("div", { class: "help" }, "Empty = use the global value. The index keeps its current settings until you rebuild it.")),
          h("div", { class: "field" }, h("label", { class: "l", for: "ko" + k.id }, "Chunk overlap (characters)"), overlap),
          h("div", { class: "field" }, h("div", { class: "l" }, "Embedding model"), h("div", {}, "Local MiniLM (built in)")),
          h("div", { class: "field" }, h("div", { class: "l" }, "Vector store"), h("div", {}, "Chroma"))),
        h("div", { class: "actions" }, save, del), out, indexPanel(k), docsPanel(k, data));
    }

    async function load(notice) {
      try {
        var data = await api("GET", "/kbs");
        var kids = [notice || null];
        if (!data.kbs.length) kids.push(h("p", { class: "sub" }, "No knowledge bases for your departments yet."));
        data.kbs.forEach(function (k) { kids.push(card(k, data)); });
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
