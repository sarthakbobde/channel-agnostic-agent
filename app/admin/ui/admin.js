/* RAG Assistant admin page. Talks only to /admin/api. Builds the page with DOM calls and
   textContent (never HTML strings), so text from the API or the audit log cannot inject markup. */
(function () {
  "use strict";

  var API = "/admin/api";
  var csrf = null;
  var me = null;
  var state = { tab: "channels", channels: [], selected: null, flash: null };

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
    if (body !== undefined) { opts.body = JSON.stringify(body); opts.headers["Content-Type"] = "application/json"; }
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
    mount(h("div", { class: "login" }, h("h1", {}, "RAG Assistant"), h("p", {}, "Admin sign-in"), form));
    email.focus();
  }

  async function boot() {
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
    state.tab = "channels";
    await loadChannels();
    renderShell();
  }

  async function loadChannels() {
    state.channels = await api("GET", "/channels");
    if (!state.channels.some(function (c) { return c.type === state.selected; })) {
      state.selected = state.channels.length ? state.channels[0].type : null;
    }
  }

  // ---- shell ----------------------------------------------------------------------
  var TABS = [["channels", "Channels"], ["audit", "Audit log"], ["admins", "Admins"], ["account", "Account"]];

  function renderShell() {
    var nav = h("nav", { class: "side", "aria-label": "Admin sections" }, h("h1", {}, "RAG Assistant"));
    TABS.forEach(function (t) {
      nav.appendChild(h("button", {
        type: "button", class: state.tab === t[0] ? "on" : "", "aria-current": state.tab === t[0] ? "page" : null,
        onclick: function () { state.tab = t[0]; renderShell(); }
      }, t[1]));
    });
    nav.appendChild(h("button", { type: "button", disabled: true, title: "Planned for a later phase" }, "Documents (later)"));
    nav.appendChild(h("div", { class: "who" }, h("b", {}, me), "Admin · local sign-in",
      h("br"), h("button", { type: "button", onclick: signOut }, "Sign out")));
    var main = h("main", {});
    mount(h("div", { class: "app" }, nav, main));
    var view = { channels: viewChannels, audit: viewAudit, admins: viewAdmins, account: viewAccount }[state.tab];
    view(main);
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

  function sameValue(a, b) { return JSON.stringify(a) === JSON.stringify(b); }
  function normalize(v, f) {
    if (f.kind === "bool") return !!v;
    if (f.kind === "list") return Array.isArray(v) ? v : [];
    return v === null || v === undefined ? "" : String(v);
  }

  function channelPanel(ch) {
    var inputs = {};
    var result = h("div", { "aria-live": "polite" });
    if (state.flash) { result.appendChild(msg(state.flash.kind, state.flash.text)); state.flash = null; }

    var enabled = h("input", { type: "checkbox", id: "enabled", checked: ch.enabled });
    var form = h("div", { class: "form" });

    ch.fields.forEach(function (f) {
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
        wrap.appendChild(h("label", { class: "l", for: id }, f.label + (f.required ? " *" : "")));
        wrap.appendChild(control);
      } else {
        var inp;
        if (f.kind === "bool") {
          inp = h("input", { type: "checkbox", id: id, checked: !!f.value });
          control = h("label", { class: "check", for: id }, inp, "On");
        } else if (f.kind === "choice") {
          inp = h("select", { id: id }, (f.choices || []).map(function (c) { return h("option", { value: c, text: c }); }));
          inp.value = f.value || "";
          control = inp;
        } else {
          inp = h("input", { type: "text", id: id, value: f.kind === "list" ? (f.value || []).join(", ") : (f.value || ""),
                             placeholder: f.kind === "list" ? "comma-separated" : null, autocomplete: "off" });
          control = inp;
        }
        inputs[f.name] = inp;
        wrap.appendChild(h("label", { class: "l", for: id }, f.label + (f.required ? " *" : "")));
        wrap.appendChild(control);
      }
      if (f.help) wrap.appendChild(h("div", { class: "help" }, f.help));
      form.appendChild(wrap);
    });

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

    function collect() {
      var out = {};
      ch.fields.forEach(function (f) {
        var inp = inputs[f.name];
        if (f.secret) { if (inp && inp.value) out[f.name] = inp.value; return; }
        var v;
        if (f.kind === "bool") v = inp.checked;
        else if (f.kind === "list") v = inp.value.split(",").map(function (s) { return s.trim(); }).filter(Boolean);
        else v = inp.value.trim();
        if (!sameValue(v, normalize(f.value, f))) out[f.name] = (v === "" ? null : v);
      });
      return out;
    }

    var saveBtn = h("button", { type: "button", class: "btn primary", onclick: async function () {
      result.replaceChildren(msg("wait", "Saving…"));
      try {
        var updated = await api("PUT", "/channels/" + ch.type, { enabled: enabled.checked, values: collect() });
        await loadChannels();
        state.flash = { kind: "ok", text: updated.enabled ? "Saved. The channel is running with the new settings." : "Saved. The channel is off." };
        renderShell();
      } catch (err) { if (me) result.replaceChildren(msg("bad", err.message)); }
    } }, "Save changes");

    var testBtn = h("button", { type: "button", class: "btn", onclick: async function () {
      result.replaceChildren(msg("wait", "Testing…"));
      try {
        var r = await api("POST", "/channels/" + ch.type + "/test", { values: collect() });
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
        body.appendChild(h("tr", {}, h("td", { class: "nw" }, when(r.ts)), h("td", { class: "wrap" }, r.actor),
          h("td", { class: "nw" }, r.action), h("td", { class: "nw" }, r.target), h("td", { class: "wrap" }, r.detail)));
      });
      holder.replaceChildren(h("div", { class: "tablewrap" }, h("table", {},
        h("thead", {}, h("tr", {}, ["Time", "Actor", "Action", "Target", "Detail"].map(function (t) { return h("th", {}, t); }))), body)));
    } catch (err) { if (me) holder.replaceChildren(msg("bad", err.message)); }
  }

  // ---- admins ---------------------------------------------------------------------
  async function viewAdmins(main) {
    main.appendChild(h("h2", {}, "Admins"));
    main.appendChild(h("p", { class: "sub" }, "People who can sign in to this page. Only addresses on the configured company domain can be added."));
    var list = h("div", {});
    var result = h("div", { "aria-live": "polite" });
    main.appendChild(list);

    async function refresh() {
      try {
        var admins = await api("GET", "/admins");
        var body = h("tbody", {});
        admins.forEach(function (a) {
          body.appendChild(h("tr", {}, h("td", { class: "wrap" }, a.email), h("td", {}, a.role), h("td", {}, a.provider),
            h("td", {}, a.email === me ? h("span", { class: "meta" }, "you") : confirmButton("Remove", "Confirm remove", async function () {
              try { await api("DELETE", "/admins/" + encodeURIComponent(a.email)); result.replaceChildren(msg("ok", "Removed " + a.email + ".")); refresh(); }
              catch (err) { if (me) result.replaceChildren(msg("bad", err.message)); }
            }, true))));
        });
        list.replaceChildren(h("div", { class: "tablewrap" }, h("table", {},
          h("thead", {}, h("tr", {}, ["Email", "Role", "Sign-in", ""].map(function (t) { return h("th", {}, t); }))), body)));
      } catch (err) { if (me) list.replaceChildren(msg("bad", err.message)); }
    }

    var email = h("input", { type: "email", id: "new_email", autocomplete: "off", required: true });
    var pw = h("input", { type: "password", id: "new_pw", autocomplete: "new-password", required: true });
    main.appendChild(h("h3", {}, "Add an admin"));
    main.appendChild(h("form", { class: "inline", onsubmit: async function (e) {
      e.preventDefault();
      result.replaceChildren(msg("wait", "Adding…"));
      try {
        var a = await api("POST", "/admins", { email: email.value, password: pw.value });
        email.value = ""; pw.value = "";
        result.replaceChildren(msg("ok", "Added " + a.email + ". Share the password with them securely; they can change it under Account."));
        refresh();
      } catch (err) { if (me) result.replaceChildren(msg("bad", err.message)); }
    } },
      h("div", { class: "field" }, h("label", { class: "l", for: "new_email" }, "Email"), email),
      h("div", { class: "field" }, h("label", { class: "l", for: "new_pw" }, "Temporary password (12+ characters)"), pw),
      h("div", {}, h("button", { class: "btn", type: "submit" }, "Add admin"))));
    main.appendChild(result);
    refresh();
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
