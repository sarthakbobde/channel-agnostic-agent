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
    if (isNaN(d)) return String(s);
    try { return d.toLocaleString(undefined, { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" }); }
    catch (e) { return d.toLocaleString(); }
  }
  function msg(kind, text) { return h("div", { class: "msg " + kind, role: "status" }, text); }

  // ---- layout helpers: short titles, with explanations behind an (i) instead of paragraphs ------------
  var tipSeq = 0;
  function tip(text) {                                     // shown on hover or keyboard focus, and toggled by a tap
    var id = "tip" + (++tipSeq);
    var wrap = h("span", { class: "tip" });
    wrap.append(h("button", { type: "button", class: "tip-btn", "aria-label": "More information", "aria-describedby": id,
      onclick: function (e) { e.preventDefault(); e.stopPropagation(); wrap.classList.toggle("open"); },
      onkeydown: function (e) { if (e.key === "Escape") wrap.classList.remove("open"); },
      onblur: function () { wrap.classList.remove("open"); } }, icon("info", 16)),
      h("span", { class: "tip-text", role: "tooltip", id: id }, text));
    return wrap;
  }
  function heading(tag, title, hint, right) {
    return h("div", { class: "head" }, h(tag, {}, title, hint ? tip(hint) : null), right ? h("div", { class: "head-right" }, right) : null);
  }
  // A card with an optional title row; any further arguments become its body.
  function panel(title, hint, right) {
    var p = h("section", { class: "panel" });
    if (title) p.appendChild(heading("h3", title, hint, right));
    for (var i = 3; i < arguments.length; i++) add(p, arguments[i]);
    return p;
  }
  // Every screen starts the same way: its menu group, its title with (i), and its main action on the right.
  function pageHead(main, title, hint, actions) {
    var group = groupOf(state.tab);
    main.appendChild(h("div", { class: "page-head" },
      h("div", { class: "titles" }, group ? h("div", { class: "eyebrow" }, group) : null, h("h2", {}, title, hint ? tip(hint) : null)),
      actions ? h("div", { class: "actions" }, actions) : null));
  }
  // "+ Add …" for the page header: shows or hides a form panel instead of keeping it open under the list.
  function addToggle(label, target) {
    target.hidden = true;
    var b = h("button", { type: "button", class: "btn primary", "aria-expanded": "false", onclick: function () {
      target.hidden = !target.hidden;
      b.setAttribute("aria-expanded", String(!target.hidden));
      if (!target.hidden) { var f = target.querySelector("input, select, textarea"); if (f) f.focus(); }
    } }, icon("plus", 16), label);
    return b;
  }
  // A collapsible sub-section inside a card (closed by default).
  function fold(title, hint) {
    return h("details", { class: "fold" }, h("summary", {}, h("span", { class: "fold-title" }, title), hint ? tip(hint) : null));
  }
  // Label for a field, with its explanation behind an (i) when it is too long to sit under the box.
  function fieldLabel(forId, text, hint) {
    var l = h("label", { class: "l", for: forId }, text);
    return hint ? h("div", { class: "lrow" }, l, tip(hint)) : l;
  }
  function hint(text) { return h("div", { class: "help" }, text); }

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
    mount(h("div", { class: "login-page" },
      h("div", { class: "login" },
        h("div", { class: "login-brand" }, h("span", { class: "brandmark" }, icon("brand", 20)), h("span", {}, "Channel Agnostic Agent")),
        h("h1", {}, "Sign in"), h("p", {}, "Admin center for your knowledge assistant"), form)));
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
    var first = [];
    NAV_GROUPS.forEach(function (g) { first = first.concat(g[1].filter(canSee)); });
    state.tab = first[0] || "account";                            // the first screen of the menu this person can use
    if (state.perm.screens.indexOf("channels") >= 0) await loadChannels();
    await loadProfile();
    renderShell();
  }

  async function loadChannels() {
    state.channels = await api("GET", "/channels");
    if (!state.channels.some(function (c) { return c.type === state.selected; })) {
      state.selected = state.channels.length ? state.channels[0].type : null;
    }
  }

  // ---- icons (stroke paths on a 24px grid; built with createElementNS, never markup strings) ----------
  var ICONS = {
    brand: "M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z",
    channels: "M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z",
    knowledge: "M4 19.5A2.5 2.5 0 0 1 6.5 17H20V3H6.5A2.5 2.5 0 0 0 4 5.5zM4 19.5A2.5 2.5 0 0 0 6.5 22H20v-5",
    providers: "M12 3l9 5-9 5-9-5 9-5zM3 13l9 5 9-5",
    prompts: "M4 20h4L19 9l-4-4L4 16zM13.5 6.5l4 4",
    guardrails: "M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z",
    settings: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z",
    users: "M16 21v-2a4 4 0 0 0-8 0v2M12 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8z",
    groups: "M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM23 21v-2a4 4 0 0 0-3-3.9M16 3.1a4 4 0 0 1 0 7.8",
    departments: "M3 21h18M5 21V7l7-4 7 4v14M9 21v-6h6v6",
    conversations: "M8 10h8M8 14h5M21 12a9 9 0 0 1-13.3 7.9L3 21l1.1-4.7A9 9 0 1 1 21 12z",
    usage: "M4 20V10M10 20V4M16 20v-8M22 20H2",
    reports: "M14 3H6v18h12V7zM14 3v4h4M9 13h6M9 17h6",
    audit: "M12 7v5l3 2M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z",
    account: "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM4 21a8 8 0 0 1 16 0",
    signout: "M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9",
    info: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 16v-4.5M12 8h.01",
    menu: "M4 6h16M4 12h16M4 18h16",
    plus: "M12 5v14M5 12h14",
    copy: "M9 9h11v11H9zM5 15H4V4h11v1",
    kbconfig: "M12 8c4.4 0 8-1.3 8-3s-3.6-3-8-3-8 1.3-8 3 3.6 3 8 3zM4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3",
    kbdocs: "M14 3H6v18h12V7zM14 3v4h4M12 17v-6M9.5 13.5L12 11l2.5 2.5",
    moon: "M20 14.5A8 8 0 0 1 9.5 4 8 8 0 1 0 20 14.5z",
    sun: "M12 16a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4",
    arrow: "M5 12h14M13 6l6 6-6 6",
    plug: "M9 2v6M15 2v6M6 8h12v4a6 6 0 0 1-12 0zM12 18v4",
    edit: "M4 20h4L19 9l-4-4L4 16z",
    chevron: "M6 9l6 6 6-6",
    camera: "M4 7h3l2-3h6l2 3h3v13H4zM12 17a4 4 0 1 0 0-8 4 4 0 0 0 0 8z",
    trash: "M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3",
    star: "M12 3l2.7 5.6 6.1.9-4.4 4.3 1 6.1L12 17l-5.4 2.9 1-6.1-4.4-4.3 6.1-.9z",
    backup: "M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6zM9 12.5l2 2 4-4.5",
    search: "M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14zM21 21l-5-5",
    home: "M3 11l9-8 9 8M5 10v10h14V10M10 20v-6h4v6",
    check: "M5 12.5l4.5 4.5L19 7.5"
  };

  // Official platform marks (Slack, Microsoft Teams, Google Chat; from each brand's published SVG) drawn with DOM calls,
  // so they need no extra file, route or request. The Web UI has no brand: a plain globe on the app's blue.
  var logoSeq = 0;
  var LOGOS = {
    slack: function () { return { box: "0 0 127 127", parts: [
      ["path", { fill: "#E01E5A", d: "M27.2 80c0 7.3-5.9 13.2-13.2 13.2C6.7 93.2.8 87.3.8 80c0-7.3 5.9-13.2 13.2-13.2h13.2V80zm6.6 0c0-7.3 5.9-13.2 13.2-13.2 7.3 0 13.2 5.9 13.2 13.2v33c0 7.3-5.9 13.2-13.2 13.2-7.3 0-13.2-5.9-13.2-13.2V80z" }],
      ["path", { fill: "#36C5F0", d: "M47 27c-7.3 0-13.2-5.9-13.2-13.2C33.8 6.5 39.7.6 47 .6c7.3 0 13.2 5.9 13.2 13.2V27H47zm0 6.7c7.3 0 13.2 5.9 13.2 13.2 0 7.3-5.9 13.2-13.2 13.2H13.9C6.6 60.1.7 54.2.7 46.9c0-7.3 5.9-13.2 13.2-13.2H47z" }],
      ["path", { fill: "#2EB67D", d: "M99.9 46.9c0-7.3 5.9-13.2 13.2-13.2 7.3 0 13.2 5.9 13.2 13.2 0 7.3-5.9 13.2-13.2 13.2H99.9V46.9zm-6.6 0c0 7.3-5.9 13.2-13.2 13.2-7.3 0-13.2-5.9-13.2-13.2V13.8C66.9 6.5 72.8.6 80.1.6c7.3 0 13.2 5.9 13.2 13.2v33.1z" }],
      ["path", { fill: "#ECB22E", d: "M80.1 99.8c7.3 0 13.2 5.9 13.2 13.2 0 7.3-5.9 13.2-13.2 13.2-7.3 0-13.2-5.9-13.2-13.2V99.8h13.2zm0-6.6c-7.3 0-13.2-5.9-13.2-13.2 0-7.3 5.9-13.2 13.2-13.2h33.1c7.3 0 13.2 5.9 13.2 13.2 0 7.3-5.9 13.2-13.2 13.2H80.1z" }]] }; },
    teams: function () { var g = "caa-teams-" + (++logoSeq); return { box: "0 0 2228.833 2073.333", parts: [
      ["defs", {}, [["linearGradient", { id: g, gradientUnits: "userSpaceOnUse", x1: "198.099", y1: "1683.0726", x2: "942.2344", y2: "394.2607", gradientTransform: "matrix(1 0 0 -1 0 2075.3333)" },
        [["stop", { offset: "0", "stop-color": "#5a62c3" }], ["stop", { offset: ".5", "stop-color": "#4d55bd" }], ["stop", { offset: "1", "stop-color": "#3940ab" }]]]]],
      ["path", { fill: "#5059C9", d: "M1554.637,777.5h575.713c54.391,0,98.483,44.092,98.483,98.483c0,0,0,0,0,0v524.398 c0,199.901-162.051,361.952-361.952,361.952h0h-1.711c-199.901,0.028-361.975-162-362.004-361.901c0-0.017,0-0.034,0-0.052V828.971 C1503.167,800.544,1526.211,777.5,1554.637,777.5L1554.637,777.5z" }],
      ["circle", { fill: "#5059C9", cx: "1943.75", cy: "440.583", r: "233.25" }],
      ["circle", { fill: "#7B83EB", cx: "1218.083", cy: "336.917", r: "336.917" }],
      ["path", { fill: "#7B83EB", d: "M1667.323,777.5H717.01c-53.743,1.33-96.257,45.931-95.01,99.676v598.105 c-7.505,322.519,247.657,590.16,570.167,598.053c322.51-7.893,577.671-275.534,570.167-598.053V877.176 C1763.579,823.431,1721.066,778.83,1667.323,777.5z" }],
      ["path", { opacity: ".1", d: "M1244,777.5v838.145c-0.258,38.435-23.549,72.964-59.09,87.598 c-11.316,4.787-23.478,7.254-35.765,7.257H667.613c-6.738-17.105-12.958-34.21-18.142-51.833 c-18.144-59.477-27.402-121.307-27.472-183.49V877.02c-1.246-53.659,41.198-98.19,94.855-99.52H1244z" }],
      ["path", { opacity: ".2", d: "M1192.167,777.5v889.978c-0.002,12.287-2.47,24.449-7.257,35.765 c-14.634,35.541-49.163,58.833-87.598,59.09H691.975c-8.812-17.105-17.105-34.21-24.362-51.833 c-7.257-17.623-12.958-34.21-18.142-51.833c-18.144-59.476-27.402-121.307-27.472-183.49V877.02 c-1.246-53.659,41.198-98.19,94.855-99.52H1192.167z" }],
      ["path", { opacity: ".2", d: "M1192.167,777.5v786.312c-0.395,52.223-42.632,94.46-94.855,94.855h-447.84 c-18.144-59.476-27.402-121.307-27.472-183.49V877.02c-1.246-53.659,41.198-98.19,94.855-99.52H1192.167z" }],
      ["path", { opacity: ".2", d: "M1140.333,777.5v786.312c-0.395,52.223-42.632,94.46-94.855,94.855H649.472 c-18.144-59.476-27.402-121.307-27.472-183.49V877.02c-1.246-53.659,41.198-98.19,94.855-99.52H1140.333z" }],
      ["path", { opacity: ".1", d: "M1244,509.522v163.275c-8.812,0.518-17.105,1.037-25.917,1.037 c-8.812,0-17.105-0.518-25.917-1.037c-17.496-1.161-34.848-3.937-51.833-8.293c-104.963-24.857-191.679-98.469-233.25-198.003 c-7.153-16.715-12.706-34.071-16.587-51.833h258.648C1201.449,414.866,1243.801,457.217,1244,509.522z" }],
      ["path", { opacity: ".2", d: "M1192.167,561.355v111.442c-17.496-1.161-34.848-3.937-51.833-8.293 c-104.963-24.857-191.679-98.469-233.25-198.003h190.228C1149.616,466.699,1191.968,509.051,1192.167,561.355z" }],
      ["path", { opacity: ".2", d: "M1192.167,561.355v111.442c-17.496-1.161-34.848-3.937-51.833-8.293 c-104.963-24.857-191.679-98.469-233.25-198.003h190.228C1149.616,466.699,1191.968,509.051,1192.167,561.355z" }],
      ["path", { opacity: ".2", d: "M1140.333,561.355v103.148c-104.963-24.857-191.679-98.469-233.25-198.003 h138.395C1097.783,466.699,1140.134,509.051,1140.333,561.355z" }],
      ["path", { fill: "url(#" + g + ")", d: "M95.01,466.5h950.312c52.473,0,95.01,42.538,95.01,95.01v950.312c0,52.473-42.538,95.01-95.01,95.01 H95.01c-52.473,0-95.01-42.538-95.01-95.01V561.51C0,509.038,42.538,466.5,95.01,466.5z" }],
      ["path", { fill: "#FFF", d: "M820.211,828.193H630.241v517.297H509.211V828.193H320.123V727.844h500.088V828.193z" }]] }; },
    gchat: function () { return { box: "0 0 479.4 500", parts: [
      ["path", { fill: "#00AC47", d: "M108.8,269.7V107.2h-71C16.9,107.2,0,124.2,0,145.1V481c0,16.9,20.4,25.3,32.3,13.4l78.1-78.1h222.4 c20.9,0,37.8-16.9,37.8-37.8v-71H146.7C125.8,307.5,108.8,290.6,108.8,269.7z" }],
      ["path", { fill: "#5BB974", d: "M441.6,0H146.7c-20.9,0-37.8,16.9-37.8,37.8v69.4h223.9c20.9,0,37.8,16.9,37.8,37.8v162.4h71 c20.9,0,37.8-16.9,37.8-37.8V37.8C479.4,16.9,462.5,0,441.6,0z" }],
      ["path", { fill: "#00832D", d: "M332.8,107.2H108.8v162.4c0,20.9,16.9,37.8,37.8,37.8h223.9V145.1C370.6,124.2,353.7,107.2,332.8,107.2z" }]] }; },
    // LLM provider marks (Lobe Icons @lobehub/icons-static-svg 1.97.0, MIT; the marks belong to their owners).
    // Single-colour marks use currentColor so they follow the light or dark theme.
    openai: function () { return { box: "0 0 24 24", parts: [["path", { fill: "currentColor", "fill-rule": "evenodd",
      d: "M9.205 8.658v-2.26c0-.19.072-.333.238-.428l4.543-2.616c.619-.357 1.356-.523 2.117-.523 2.854 0 4.662 2.212 4.662 4.566 0 .167 0 .357-.024.547l-4.71-2.759a.797.797 0 00-.856 0l-5.97 3.473zm10.609 8.8V12.06c0-.333-.143-.57-.429-.737l-5.97-3.473 1.95-1.118a.433.433 0 01.476 0l4.543 2.617c1.309.76 2.189 2.378 2.189 3.948 0 1.808-1.07 3.473-2.76 4.163zM7.802 12.703l-1.95-1.142c-.167-.095-.239-.238-.239-.428V5.899c0-2.545 1.95-4.472 4.591-4.472 1 0 1.927.333 2.712.928L8.23 5.067c-.285.166-.428.404-.428.737v6.898zM12 15.128l-2.795-1.57v-3.33L12 8.658l2.795 1.57v3.33L12 15.128zm1.796 7.23c-1 0-1.927-.332-2.712-.927l4.686-2.712c.285-.166.428-.404.428-.737v-6.898l1.974 1.142c.167.095.238.238.238.428v5.233c0 2.545-1.974 4.472-4.614 4.472zm-5.637-5.303l-4.544-2.617c-1.308-.761-2.188-2.378-2.188-3.948A4.482 4.482 0 014.21 6.327v5.423c0 .333.143.571.428.738l5.947 3.449-1.95 1.118a.432.432 0 01-.476 0zm-.262 3.9c-2.688 0-4.662-2.021-4.662-4.519 0-.19.024-.38.047-.57l4.686 2.71c.286.167.571.167.856 0l5.97-3.448v2.26c0 .19-.07.333-.237.428l-4.543 2.616c-.619.357-1.356.523-2.117.523zm5.899 2.83a5.947 5.947 0 005.827-4.756C22.287 18.339 24 15.84 24 13.296c0-1.665-.713-3.282-1.998-4.448.119-.5.19-.999.19-1.498 0-3.401-2.759-5.947-5.946-5.947-.642 0-1.26.095-1.88.31A5.962 5.962 0 0010.205 0a5.947 5.947 0 00-5.827 4.757C1.713 5.447 0 7.945 0 10.49c0 1.666.713 3.283 1.998 4.448-.119.5-.19 1-.19 1.499 0 3.401 2.759 5.946 5.946 5.946.642 0 1.26-.095 1.88-.309a5.96 5.96 0 004.162 1.713z" }]] }; },
    anthropic: function () { return { box: "0 0 24 24", parts: [["path", { fill: "currentColor", "fill-rule": "evenodd",
      d: "M13.827 3.52h3.603L24 20h-3.603l-6.57-16.48zm-7.258 0h3.767L16.906 20h-3.674l-1.343-3.461H5.017l-1.344 3.46H0L6.57 3.522zm4.132 9.959L8.453 7.687 6.205 13.48H10.7z" }]] }; },
    xai: function () { return { box: "0 0 24 24", parts: [["path", { fill: "currentColor", "fill-rule": "evenodd",
      d: "M6.469 8.776L16.512 23h-4.464L2.005 8.776H6.47zm-.004 7.9l2.233 3.164L6.467 23H2l4.465-6.324zM22 2.582V23h-3.659V7.764L22 2.582zM22 1l-9.952 14.095-2.233-3.163L17.533 1H22z" }]] }; },
    google: function () {
      var g = "caa-gem-" + (++logoSeq);
      var star = "M20.616 10.835a14.147 14.147 0 01-4.45-3.001 14.111 14.111 0 01-3.678-6.452.503.503 0 00-.975 0 14.134 14.134 0 01-3.679 6.452 14.155 14.155 0 01-4.45 3.001c-.65.28-1.318.505-2.002.678a.502.502 0 000 .975c.684.172 1.35.397 2.002.677a14.147 14.147 0 014.45 3.001 14.112 14.112 0 013.679 6.453.502.502 0 00.975 0c.172-.685.397-1.351.677-2.003a14.145 14.145 0 013.001-4.45 14.113 14.113 0 016.453-3.678.503.503 0 000-.975 13.245 13.245 0 01-2.003-.678z";
      function grad(i, x1, y1, x2, y2, color, stopAt) {
        return ["linearGradient", { id: g + i, gradientUnits: "userSpaceOnUse", x1: x1, y1: y1, x2: x2, y2: y2 },
          [["stop", { "stop-color": color }], ["stop", { offset: stopAt, "stop-color": color, "stop-opacity": "0" }]]];
      }
      return { box: "0 0 24 24", parts: [
        ["defs", {}, [grad(0, "7", "15.5", "11", "12", "#08B962", "1"), grad(1, "8", "5.5", "11.5", "11", "#F94543", "1"), grad(2, "3.5", "13.5", "17.5", "12", "#FABC12", ".46")]],
        ["path", { d: star, fill: "#3186FF" }], ["path", { d: star, fill: "url(#" + g + "0)" }],
        ["path", { d: star, fill: "url(#" + g + "1)" }], ["path", { d: star, fill: "url(#" + g + "2)" }]] };
    },
    deepseek: function () { return { box: "0 0 24 24", parts: [["path", { fill: "#4D6BFE",
      d: "M23.748 4.482c-.254-.124-.364.113-.512.234-.051.039-.094.09-.137.136-.372.397-.806.657-1.373.626-.829-.046-1.537.214-2.163.848-.133-.782-.575-1.248-1.247-1.548-.352-.156-.708-.311-.955-.65-.172-.241-.219-.51-.305-.774-.055-.16-.11-.323-.293-.35-.2-.031-.278.136-.356.276-.313.572-.434 1.202-.422 1.84.027 1.436.633 2.58 1.838 3.393.137.093.172.187.129.323-.082.28-.18.552-.266.833-.055.179-.137.217-.329.14a5.526 5.526 0 01-1.736-1.18c-.857-.828-1.631-1.742-2.597-2.458a11.365 11.365 0 00-.689-.471c-.985-.957.13-1.743.388-1.836.27-.098.093-.432-.779-.428-.872.004-1.67.295-2.687.684a3.055 3.055 0 01-.465.137 9.597 9.597 0 00-2.883-.102c-1.885.21-3.39 1.102-4.497 2.623C.082 8.606-.231 10.684.152 12.85c.403 2.284 1.569 4.175 3.36 5.653 1.858 1.533 3.997 2.284 6.438 2.14 1.482-.085 3.133-.284 4.994-1.86.47.234.962.327 1.78.397.63.059 1.236-.03 1.705-.128.735-.156.684-.837.419-.961-2.155-1.004-1.682-.595-2.113-.926 1.096-1.296 2.746-2.642 3.392-7.003.05-.347.007-.565 0-.845-.004-.17.035-.237.23-.256a4.173 4.173 0 001.545-.475c1.396-.763 1.96-2.015 2.093-3.517.02-.23-.004-.467-.247-.588zM11.581 18c-2.089-1.642-3.102-2.183-3.52-2.16-.392.024-.321.471-.235.763.09.288.207.486.371.739.114.167.192.416-.113.603-.673.416-1.842-.14-1.897-.167-1.361-.802-2.5-1.86-3.301-3.307-.774-1.393-1.224-2.887-1.298-4.482-.02-.386.093-.522.477-.592a4.696 4.696 0 011.529-.039c2.132.312 3.946 1.265 5.468 2.774.868.86 1.525 1.887 2.202 2.891.72 1.066 1.494 2.082 2.48 2.914.348.292.625.514.891.677-.802.09-2.14.11-3.054-.614zm1-6.44a.306.306 0 01.415-.287.302.302 0 01.2.288.306.306 0 01-.31.307.303.303 0 01-.304-.308zm3.11 1.596c-.2.081-.399.151-.59.16a1.245 1.245 0 01-.798-.254c-.274-.23-.47-.358-.552-.758a1.73 1.73 0 01.016-.588c.07-.327-.008-.537-.239-.727-.187-.156-.426-.199-.688-.199a.559.559 0 01-.254-.078c-.11-.054-.2-.19-.114-.358.028-.054.16-.186.192-.21.356-.202.767-.136 1.146.016.352.144.618.408 1.001.782.391.451.462.576.685.914.176.265.336.537.445.848.067.195-.019.354-.25.452z" }]] }; },
    azure_openai: function () {
      var g = "caa-az-" + (++logoSeq);
      return { box: "0 0 24 24", parts: [
        ["defs", {}, [
          ["linearGradient", { id: g + "0", gradientUnits: "userSpaceOnUse", x1: "8.247", x2: "1.002", y1: "1.626", y2: "23.03" }, [["stop", { "stop-color": "#114A8B" }], ["stop", { offset: "1", "stop-color": "#0669BC" }]]],
          ["linearGradient", { id: g + "1", gradientUnits: "userSpaceOnUse", x1: "14.042", x2: "12.324", y1: "15.302", y2: "15.888" },
            [["stop", { "stop-opacity": ".3" }], ["stop", { offset: ".071", "stop-opacity": ".2" }], ["stop", { offset: ".321", "stop-opacity": ".1" }], ["stop", { offset: ".623", "stop-opacity": ".05" }], ["stop", { offset: "1", "stop-opacity": "0" }]]],
          ["linearGradient", { id: g + "2", gradientUnits: "userSpaceOnUse", x1: "12.841", x2: "20.793", y1: "1.626", y2: "22.814" }, [["stop", { "stop-color": "#3CCBF4" }], ["stop", { offset: "1", "stop-color": "#2892DF" }]]]]],
        ["path", { fill: "url(#" + g + "0)", d: "M7.242 1.613A1.11 1.11 0 018.295.857h6.977L8.03 22.316a1.11 1.11 0 01-1.052.755h-5.43a1.11 1.11 0 01-1.053-1.466L7.242 1.613z" }],
        ["path", { fill: "#0078D4", d: "M18.397 15.296H7.4a.51.51 0 00-.347.882l7.066 6.595c.206.192.477.298.758.298h6.226l-2.706-7.775z" }],
        ["path", { fill: "url(#" + g + "1)", d: "M15.272.857H7.497L0 23.071h7.775l1.596-4.73 5.068 4.73h6.665l-2.707-7.775h-7.998L15.272.857z" }],
        ["path", { fill: "url(#" + g + "2)", d: "M17.193 1.613a1.11 1.11 0 00-1.052-.756h-7.81.035c.477 0 .9.304 1.052.756l6.748 19.992a1.11 1.11 0 01-1.052 1.466h-.12 7.895a1.11 1.11 0 001.052-1.466L17.193 1.613z" }]] };
    },
    web: function () { return { box: "0 0 48 48", parts: [
      ["rect", { width: "48", height: "48", rx: "10", fill: "#0F6CBD" }],
      ["g", { fill: "none", stroke: "#fff", "stroke-width": "2.4", "stroke-linecap": "round", "stroke-linejoin": "round" }, [
        ["circle", { cx: "24", cy: "24", r: "13" }],
        ["path", { d: "M11 24h26M24 11c3.6 3.6 5.4 8 5.4 13S27.6 33.4 24 37c-3.6-3.6-5.4-8-5.4-13S20.4 14.6 24 11z" }]]]] }; }
  };
  function svgNode(spec) {                                  // [tag, attributes, children or text]
    var el = document.createElementNS("http://www.w3.org/2000/svg", spec[0]);
    Object.keys(spec[1]).forEach(function (k) { el.setAttribute(k, spec[1][k]); });
    if (typeof spec[2] === "string") el.textContent = spec[2];
    else (spec[2] || []).forEach(function (c) { el.appendChild(svgNode(c)); });
    return el;
  }
  function logo(type, size) {
    if (!LOGOS[type]) return null;
    var d = LOGOS[type]();
    return svgNode(["svg", { class: "logo", viewBox: d.box, width: String(size), height: String(size), "aria-hidden": "true", focusable: "false" }, d.parts]);
  }
  var CHANNEL_TITLES = { slack: "Slack", teams: "Microsoft Teams", gchat: "Google Chat", web: "Web UI" };
  function channelName(code, title) {
    var key = String(code || "").toLowerCase();
    if (!LOGOS[key]) Object.keys(CHANNEL_TITLES).forEach(function (k) { if (CHANNEL_TITLES[k].toLowerCase() === key) key = k; });
    return h("span", { class: "chan-name" }, LOGOS[key] ? logo(key, 20) : null, title || CHANNEL_TITLES[key] || code || "–");
  }
  // A provider's mark in a fixed-size tile; a generic plug for "any OpenAI-compatible endpoint".
  function provLogo(type, size) {
    return h("span", { class: "plogo" + (size > 30 ? " big" : "") }, logo(type, Math.round(size * 0.62)) || icon("plug", Math.round(size * 0.6)));
  }
  function icon(name, size) {
    var NS = "http://www.w3.org/2000/svg";
    var s = document.createElementNS(NS, "svg");
    [["width", size || 20], ["height", size || 20], ["viewBox", "0 0 24 24"], ["fill", "none"], ["stroke", "currentColor"],
     ["stroke-width", "1.7"], ["stroke-linecap", "round"], ["stroke-linejoin", "round"], ["aria-hidden", "true"], ["focusable", "false"]]
      .forEach(function (a) { s.setAttribute(a[0], String(a[1])); });
    var p = document.createElementNS(NS, "path");
    p.setAttribute("d", ICONS[name] || ICONS.settings);
    s.appendChild(p);
    return s;
  }

  // ---- shell ----------------------------------------------------------------------
  // [screen, menu label, permission the server grants for it (defaults to the screen name)]
  var TABS = [["home", "Home"], ["channels", "Channels"], ["providers", "LLM providers"], ["kbconfig", "KB configuration", "knowledge"], ["settings", "RAG settings"], ["guardrails", "Guardrails"],
              ["users", "Users"], ["groups", "Groups"], ["departments", "Departments"],
              ["prompts", "Prompts"], ["kbdocs", "KB documents", "knowledge"],
              ["reports", "Reports"], ["audit", "Audit log"], ["conversations", "Conversations"], ["usage", "Usage and cost"],
              ["account", "Account"]];
  // The side menu, in the order an admin works: configure the assistant, add people, give it content, then watch it run.
  var NAV_GROUPS = [["", ["home"]],
                    ["Settings", ["channels", "providers", "kbconfig", "settings", "guardrails"]],
                    ["User management", ["users", "groups", "departments"]],
                    ["Input", ["prompts", "kbdocs"]],
                    ["Monitoring", ["reports", "audit", "conversations", "usage"]]];
  function tabLabel(k) { var t = TABS.filter(function (x) { return x[0] === k; })[0]; return t ? t[1] : k; }
  function canSee(k) {
    var t = TABS.filter(function (x) { return x[0] === k; })[0];
    if (k === "home") return !!state.perm;                       // everyone with a panel gets the home screen
    return !!t && !!state.perm && state.perm.screens.indexOf(t[2] || t[0]) >= 0;
  }
  function groupOf(k) { var g = NAV_GROUPS.filter(function (x) { return x[1].indexOf(k) >= 0; })[0]; return g ? g[0] : ""; }
  function go(k) { state.tab = k; state.flash = null; renderShell(); window.scrollTo(0, 0); }
  // A link to another screen, used where one screen's next step lives on another (empty states, hints).
  function goLink(k, text) {
    return canSee(k) ? h("button", { type: "button", class: "linkbtn", onclick: function () { go(k); } }, text || tabLabel(k), icon("arrow", 14)) : null;
  }

  // Light or dark theme: the viewer's choice, else the system setting. Stored in this browser only.
  function themePref(value) {
    try {
      if (value === undefined) return localStorage.getItem("caa.theme");
      localStorage.setItem("caa.theme", value);
    } catch (e) { /* storage unavailable: the choice lasts until the page is reloaded */ }
    return null;
  }
  function currentTheme() {
    return document.documentElement.getAttribute("data-theme") ||
      (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  }
  function applyTheme(t) { document.documentElement.setAttribute("data-theme", t); }
  var savedTheme = themePref();
  if (savedTheme === "dark" || savedTheme === "light") applyTheme(savedTheme);
  else applyTheme(currentTheme());

  // Accent colour: one of a fixed set (each defined for light and dark in admin.css). Stored in this browser only.
  var ACCENTS = [["blue", "Blue"], ["teal", "Teal"], ["purple", "Purple"], ["green", "Green"], ["orange", "Orange"], ["pink", "Pink"], ["graphite", "Graphite"]];
  function accentPref(value) {
    try {
      if (value === undefined) return localStorage.getItem("caa.accent");
      localStorage.setItem("caa.accent", value);
    } catch (e) { /* storage unavailable */ }
    return null;
  }
  function applyAccent(a) { document.documentElement.setAttribute("data-accent", a); }
  function currentAccent() { return document.documentElement.getAttribute("data-accent") || "blue"; }
  var savedAccent = accentPref();
  applyAccent(ACCENTS.some(function (a) { return a[0] === savedAccent; }) ? savedAccent : "blue");
  function initials(email) {
    var name = String(email || "").split("@")[0].split(/[._-]+/).filter(Boolean);
    return ((name[0] || "?")[0] + (name[1] ? name[1][0] : "")).toUpperCase();
  }
  // Collapsed or expanded menu, remembered in this browser only (a convenience; storage may be unavailable).
  function navCollapsedPref(value) {
    try {
      if (value === undefined) return localStorage.getItem("caa.nav") === "collapsed";
      localStorage.setItem("caa.nav", value ? "collapsed" : "open");
    } catch (e) { /* private window or blocked storage: keep the default */ }
    return false;
  }
  var PHONE = "(max-width: 760px)";
  function renderShell() {
    if (state.navCollapsed === undefined) state.navCollapsed = navCollapsedPref();
    var nav = h("nav", { class: "nav", id: "nav", "aria-label": "Admin sections" });
    NAV_GROUPS.forEach(function (g) {
      var items = g[1].filter(canSee);
      if (!items.length) return;
      var list = h("div", { class: "nav-group", role: "group", "aria-label": g[0] || "Home" }, g[0] ? h("div", { class: "nav-title", "aria-hidden": "true" }, g[0]) : null);
      items.forEach(function (k) {
        var on = state.tab === k;
        list.appendChild(h("button", {
          type: "button", class: on ? "on" : "", "aria-current": on ? "page" : null, title: tabLabel(k),
          onclick: function () { go(k); }
        }, icon(k, 20), h("span", { class: "nav-label" }, tabLabel(k))));
      });
      nav.appendChild(list);
    });
    var app = h("div", { class: "app" + (state.navCollapsed ? " nav-collapsed" : "") });
    var toggle = h("button", { type: "button", class: "navtoggle", "aria-controls": "nav", onclick: function () {
      if (window.matchMedia(PHONE).matches) app.classList.toggle("nav-open");
      else { state.navCollapsed = !state.navCollapsed; navCollapsedPref(state.navCollapsed); app.classList.toggle("nav-collapsed", state.navCollapsed); }
      sync();
    } }, icon("menu", 20));
    function sync() {
      var open = window.matchMedia(PHONE).matches ? app.classList.contains("nav-open") : !state.navCollapsed;
      toggle.setAttribute("aria-expanded", String(open));
      toggle.setAttribute("aria-label", open ? "Collapse menu" : "Expand menu");
    }
    sync();
    var role = state.perm ? ROLE_LABEL[state.perm.role] : "";
    var themeBtn = h("button", { type: "button", class: "iconbtn", onclick: function () {
      var next = currentTheme() === "dark" ? "light" : "dark";
      applyTheme(next); themePref(next); paintTheme();
    } });
    function paintTheme() {
      var dark = currentTheme() === "dark";
      themeBtn.replaceChildren(icon(dark ? "sun" : "moon", 20));
      themeBtn.setAttribute("aria-label", dark ? "Switch to light theme" : "Switch to dark theme");
      themeBtn.setAttribute("title", dark ? "Light theme" : "Dark theme");
    }
    paintTheme();
    var top = h("header", { class: "topbar" }, toggle,
      h("span", { class: "brandmark" }, icon("brand", 18)),
      h("span", { class: "product" }, "Channel Agnostic Agent"),
      h("span", { class: "area" }, "Admin center"),
      h("div", { class: "who" }, themeBtn,
        h("button", { type: "button", class: "account" + (state.tab === "account" ? " on" : ""), title: "Account", onclick: function () { go("account"); } },
          avatarEl(),
          h("span", { class: "id" }, h("b", {}, (state.profile && state.profile.name) || me), h("span", {}, role + (state.profile && state.profile.department ? " · " + state.profile.department : "")))),
        h("button", { type: "button", class: "iconbtn", onclick: signOut, "aria-label": "Sign out", title: "Sign out" }, icon("signout", 20))));
    var main = h("main", { id: "main" });
    var scrim = h("div", { class: "scrim", "aria-hidden": "true", onclick: function () { app.classList.remove("nav-open"); sync(); } });
    app.append(top, nav, scrim, main);
    mount(app);
    var view = { home: viewHome, channels: viewChannels, settings: viewSettings, providers: viewProviders,
      kbconfig: viewKnowledge, kbdocs: viewKbDocs,
      groups: viewGroups, prompts: viewPrompts, guardrails: viewGuardrails, conversations: viewConversations, usage: viewUsage, reports: viewReports,
      departments: viewDepartments, users: viewUsers, audit: viewAudit, account: viewAccount }[state.tab] || viewAccount;
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
    pageHead(main, "Channels", "Where people talk to the assistant. Every channel uses the same assistant; choose its knowledge bases in each channel's settings.");
    var grid = h("div", { class: "grid" });
    state.channels.forEach(function (ch) {
      var s = status(ch);
      grid.appendChild(h("button", {
        type: "button", class: "card" + (ch.type === state.selected ? " sel" : ""), "aria-pressed": String(ch.type === state.selected),
        onclick: function () { state.selected = ch.type; state.flash = null; renderShell(); }
      }, h("div", { class: "top" }, h("span", { class: "chan" }, logo(ch.type, 28), h("b", {}, ch.title)), h("span", { class: "pill " + s[0] }, s[1])),
         h("div", { class: "meta" }, ch.configured ? "Updated " + when(ch.updated_at) : "Not set up")));
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
        if (f.value != null && (f.choices || []).indexOf(f.value) >= 0) inp.value = f.value;      // no saved value: keep the first choice selected, never a blank box
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
    var long = f.help && f.help.length > 60;                  // short help stays under the box, longer help goes behind (i)
    wrap.appendChild(fieldLabel(id, f.label + (f.required ? " *" : ""), long ? f.help : null));
    wrap.appendChild(control);
    wrap.appendChild(hint(f.help && !long ? f.help : ""));      // always three rows (label, box, hint) so a row of fields lines up
    return wrap;
  }

  function specForm(fields) {
    var inputs = {};
    var form = h("div", { class: "form aligned" });
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
        h("div", { class: "lrow" }, h("span", { class: "l" }, "Webhook URL"),
          tip("Enter this on the platform. It needs a public HTTPS address (a tunnel works for testing); replace <your-public-host> with yours.")),
        h("div", { class: "copyrow" }, h("div", { class: "url" }, full),
          h("button", { type: "button", class: "btn", onclick: function () {
            try { navigator.clipboard.writeText(ch.webhook_path).then(function () { flash(result, "ok", "Path copied."); },
                  function () { flash(result, "bad", "Copying was blocked. Select the text and copy it."); }); }
            catch (e) { flash(result, "bad", "Copying was blocked. Select the text and copy it."); }
          } }, icon("copy", 16), "Copy path"))));
    }

    var saveBtn = h("button", { type: "button", class: "btn primary", onclick: async function () {
      result.replaceChildren(msg("wait", "Saving…"));
      try {
        var updated = await api("PUT", "/channels/" + ch.type, { enabled: enabled.checked, values: sf.collect() });
        await loadChannels();
        state.flash = { kind: "ok", text: updated.enabled ? "Saved. The channel is running." : "Saved. The channel is off." };
        renderShell();
      } catch (err) { if (me) result.replaceChildren(msg("bad", err.message)); }
    } }, "Save");

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

    var s = status(ch);
    return h("section", { class: "panel" },
      h("div", { class: "head" },
        h("div", { class: "chan" }, logo(ch.type, 36), h("div", {},
          h("h3", {}, ch.title, tip("Keys and tokens are encrypted and never shown again; leave a box empty to keep the stored value. New settings are tested before they replace working ones.")),
          h("span", { class: "pill " + s[0] }, s[1]))),
        h("div", { class: "head-right" }, h("label", { class: "switch", for: "enabled" }, enabled, "Enabled"))),
      form, actions, result);
  }

  // ---- settings -------------------------------------------------------------------
  async function viewSettings(main) {
    pageHead(main, "RAG settings", "How answers are found and how conversations are kept, across all knowledge bases. Changes apply within seconds, with no restart.");
    var holder = h("div", { class: "stack" }, msg("wait", "Loading…"));
    main.appendChild(holder);

    var SECTIONS = [
      ["Finding answers", "How documents are searched, and how much of the conversation is remembered.", ["top_k", "score_threshold", "recent_turns"]],
      ["Splitting documents", "How documents are cut into searchable passages. Applies the next time documents are indexed.", ["chunk_size", "chunk_overlap"]],
      ["Conversations and privacy", "How long conversations are kept, when long chats are summarised, and whether they can be exported.", ["max_retention_days", "retention_job_enabled", "summarise_after_turns", "export_enabled"]],
      ["Limits", "Caps on question length and on how fast one person can ask.", ["prompt_max_chars", "chat_rate_per_minute"]]
    ];

    function show(data, notice) {
      var byName = {}; data.fields.forEach(function (f) { byName[f.name] = f; });
      var used = {};
      var forms = [];
      var panels = SECTIONS.map(function (s) {
        var fields = s[2].filter(function (n) { return byName[n]; }).map(function (n) { used[n] = true; return byName[n]; });
        var sf = specForm(fields); forms.push(sf);
        return panel(s[0], s[1], null, sf.el);
      });
      var rest = data.fields.filter(function (f) { return !used[f.name]; });
      if (rest.length) { var other = specForm(rest); forms.push(other); panels.push(panel("Other", null, null, other.el)); }
      var result = h("div", { "aria-live": "polite" }, notice || null);
      var save = h("button", { type: "button", class: "btn primary", onclick: async function () {
        result.replaceChildren(msg("wait", "Saving…"));
        var values = {}; forms.forEach(function (f) { Object.assign(values, f.collect()); });
        try {
          var updated = await api("PUT", "/settings", { values: values });
          show(updated, msg("ok", "Saved. In use within a few seconds."));
        } catch (err) { if (me) result.replaceChildren(msg("bad", err.message)); }
      } }, "Save");
      holder.replaceChildren.apply(holder, [h("div", { class: "cols" }, panels), result,
        h("div", { class: "savebar" }, h("span", { class: "meta" }, "Last changed " + when(data.updated_at) + " by " + data.updated_by), h("div", { class: "actions" }, save))]);
    }
    try { show(await api("GET", "/settings")); }
    catch (err) { if (me) holder.replaceChildren(msg("bad", err.message)); }

    // optional notifications: where new alerts are also sent (off by default)
    var sinkBox = h("section", { class: "panel" });
    async function loadSinks(notice) {
      try {
        var c = await api("GET", "/alert-sinks");
        var wOn = h("input", { type: "checkbox", id: "as_w", checked: c.webhook_enabled });
        var wUrl = h("input", { type: "password", id: "as_wurl", autocomplete: "off", placeholder: c.secrets_set.indexOf("webhook_url") >= 0 ? "Saved · type to replace" : "https://hooks.slack.com/…" });
        var mOn = h("input", { type: "checkbox", id: "as_m", checked: c.smtp_enabled });
        var host = h("input", { type: "text", id: "as_host", value: c.smtp_host, autocomplete: "off" });
        var port = h("input", { type: "number", id: "as_port", value: c.smtp_port, min: 1, max: 65535 });
        var user = h("input", { type: "text", id: "as_user", value: c.smtp_user, autocomplete: "off" });
        var pw = h("input", { type: "password", id: "as_pw", autocomplete: "new-password", placeholder: c.secrets_set.indexOf("smtp_password") >= 0 ? "Saved · type to replace" : "" });
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
        sinkBox.replaceChildren(heading("h3", "Alert notifications", "Alerts always appear as banners in this admin center. You can also send each new alert to a Slack-compatible webhook, by email, or both. Addresses and passwords are stored encrypted."),
          h("div", { class: "form" },
            h("div", { class: "field" }, fieldLabel("as_sev", "Send"), sev),
            h("div", { class: "field wide subhead" }, h("label", { class: "switch", for: "as_w" }, wOn, "Webhook")),
            h("div", { class: "field wide" }, fieldLabel("as_wurl", "Webhook URL"), wUrl),
            h("div", { class: "field wide subhead" }, h("label", { class: "switch", for: "as_m" }, mOn, "Email")),
            h("div", { class: "field" }, fieldLabel("as_host", "Mail server"), host),
            h("div", { class: "field" }, fieldLabel("as_port", "Port"), port),
            h("div", { class: "field" }, fieldLabel("as_user", "User name"), user),
            h("div", { class: "field" }, fieldLabel("as_pw", "Password"), pw),
            h("div", { class: "field" }, fieldLabel("as_from", "From"), from),
            h("div", { class: "field" }, fieldLabel("as_to", "To"), to)),
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
  // One screen, three jobs: which model answers (top), the connections it can use (a compact list, one open at a time),
  // and which provider companies may be used at all (side column). "+ Add connection" opens the add form on demand.
  async function viewProviders(main) {
    var addBox = h("section", { class: "panel" });
    pageHead(main, "LLM providers", "Choose the model that answers and a fallback, manage the connections they use, and decide which providers are allowed. Changes apply within seconds.",
      addToggle("Add connection", addBox));
    var notice = h("div", { "aria-live": "polite" });
    var holder = h("div", {}, msg("wait", "Loading…"));
    main.append(addBox, notice, holder);
    var openId = null;                                           // the one connection whose settings are open

    async function refresh(note) {
      try {
        var data = await api("GET", "/providers");
        notice.replaceChildren(note || "");
        addBox.replaceChildren.apply(addBox, addProviderBody(data, refresh, function () { addBox.hidden = true; }));
        holder.replaceChildren(h("div", { class: "split" },
          h("div", { class: "stack" }, modelSettingsPanel(data, refresh), connectionsPanel(data, refresh)),
          h("aside", { class: "stack" }, approvalPanel(data, refresh))));
      } catch (err) { if (me) holder.replaceChildren(msg("bad", err.message)); }
    }

    function connectionsPanel(data, refresh) {
      var ms = data.model_settings || {};
      var roleOf = function (p) {
        return [ms.preferred && ms.preferred.provider_id === p.id ? h("span", { class: "pill ok" }, "Preferred") : null,
                ms.fallback && ms.fallback.provider_id === p.id ? h("span", { class: "pill off" }, "Fallback") : null];
      };
      var list = h("div", { class: "rows" });
      data.providers.forEach(function (p) {
        var result = h("div", { "aria-live": "polite" });
        var editBox = h("div", { class: "row-detail" });
        var open = openId === p.id;
        var editBtn = h("button", { type: "button", class: "btn small", "aria-expanded": String(open), onclick: function () {
          openId = openId === p.id ? null : p.id; refresh();
        } }, icon("edit", 14), open ? "Close" : "Edit");
        var testBtn = h("button", { type: "button", class: "btn small", onclick: async function () {
          result.replaceChildren(msg("wait", "Testing…"));
          try { var r = await api("POST", "/providers/" + p.id + "/test", {}); refresh(msg(r.ok ? "ok" : "bad", p.name + ": " + r.message)); }
          catch (err) { if (me) result.replaceChildren(msg("bad", err.message)); }
        } }, "Test");
        if (open) {
          var nameInput = h("input", { type: "text", id: "pn_" + p.id, value: p.name, autocomplete: "off" });
          var sf = specForm(p.fields);
          editBox.append(
            p.warning ? h("div", { class: "msg warn" }, p.warning) : null,
            h("div", { class: "form" }, h("div", { class: "field" }, fieldLabel("pn_" + p.id, "Name"), nameInput)), sf.el,
            h("div", { class: "actions" },
              h("button", { type: "button", class: "btn primary", onclick: async function () {
                result.replaceChildren(msg("wait", "Testing and saving…"));
                try { await api("PUT", "/providers/" + p.id, { name: nameInput.value, values: sf.collect() }); openId = null; refresh(msg("ok", "Saved " + nameInput.value + ".")); }
                catch (err) { if (me) result.replaceChildren(msg("bad", err.message)); }
              } }, "Save"),
              h("button", { type: "button", class: "btn", onclick: function () { openId = null; refresh(); } }, "Cancel"),
              confirmButton("Delete", "Confirm delete", async function () {
                try { await api("DELETE", "/providers/" + p.id); openId = null; refresh(msg("ok", "Deleted " + p.name + ".")); }
                catch (err) { if (me) result.replaceChildren(msg("bad", err.message)); }
              }, true)));
        }
        list.appendChild(h("div", { class: "row" + (open ? " open" : "") },
          h("div", { class: "row-main" },
            provLogo(p.type, 36),
            h("div", { class: "row-text" }, h("b", {}, p.name, roleOf(p)),
              h("div", { class: "meta" }, p.title + " · " + p.available_models.length + (p.available_models.length === 1 ? " model" : " models"))),
            h("div", { class: "row-side" }, statusPill(p.status), testBtn, editBtn)),
          p.status_detail ? msg("bad", p.status_detail) : null,
          result, open ? editBox : null));
      });
      return panel("Connections", "Each connection is one provider account and key. Connect two different providers so the fallback still works when one is down.",
        h("span", { class: "pill off" }, data.providers.length + (data.providers.length === 1 ? " connection" : " connections")),
        data.providers.length ? list : h("div", { class: "empty" }, "No connections yet. Use “Add connection” at the top of the page."));
    }

    refresh();
  }

  function statusPill(status) {
    return h("span", { class: "pill " + (status === "Connected" ? "ok" : status === "Failed" ? "bad" : "off") }, status);
  }

  // Which provider companies may be connected at all: a short list of logo check boxes.
  function approvalPanel(data, refresh) {
    var boxes = {};
    var result = h("div", { "aria-live": "polite" });
    var list = h("div", { class: "choice-list" });
    data.types.forEach(function (t) {
      var cb = h("input", { type: "checkbox", id: "ap_" + t.type, checked: t.approved });
      boxes[t.type] = cb;
      list.appendChild(h("div", { class: "choice" },
        h("label", { class: "choice-label", for: "ap_" + t.type }, cb, provLogo(t.type, 28), h("span", {}, t.title)),
        t.warning ? tip(t.warning) : null));
    });
    return panel("Allowed providers", "Only allowed providers can be connected. Questions and the document passages used to answer them are sent to the provider, so allow only what your data policy permits.", null,
      list,
      h("div", { class: "actions" }, h("button", { type: "button", class: "btn primary", onclick: async function () {
        result.replaceChildren(msg("wait", "Saving…"));
        try {
          await api("PUT", "/providers/approval", { approved: Object.keys(boxes).filter(function (k) { return boxes[k].checked; }) });
          refresh(msg("ok", "Allowed providers saved."));
        } catch (err) { if (me) result.replaceChildren(msg("bad", err.message)); }
      } }, "Save")), result);
  }

  // Preferred and fallback model, side by side.
  function modelSettingsPanel(data, refresh) {
    var result = h("div", { "aria-live": "polite" });
    var hintText = "The preferred model answers every question. On no credit, rate limits, server errors or a timeout, the fallback answers and a banner appears. Invalid requests are not retried.";
    if (!data.providers.length) {
      return panel("Which model answers", hintText, null, h("div", { class: "empty" }, "Add a connection first, then choose its model here."));
    }
    var cur = data.model_settings || { preferred: null, fallback: null, request_timeout: 60 };

    function slot(id, title, current, optional, roleIcon, roleText) {
      var mark = h("span", { class: "slot-prov" });
      var prov = h("select", { id: id + "_p" });
      if (optional) prov.appendChild(h("option", { value: "", text: "None" }));
      data.providers.forEach(function (p) { prov.appendChild(h("option", { value: String(p.id), text: p.name })); });
      var model = h("select", { id: id + "_m" });
      function fill() {
        var p = data.providers.filter(function (x) { return String(x.id) === prov.value; })[0];
        model.replaceChildren.apply(model, (p ? p.available_models : []).map(function (m) { return h("option", { value: m, text: m }); }));
        model.disabled = !p;
        mark.replaceChildren(p ? provLogo(p.type, 28) : h("span", { class: "meta" }, "Not set"));
      }
      prov.addEventListener("change", fill);
      prov.value = current && current.provider_id ? String(current.provider_id) : (optional ? "" : String(data.providers[0].id));
      fill();
      if (current && current.model) model.value = current.model;
      return { el: h("div", { class: "slot" },
          h("div", { class: "slot-head" }, h("span", { class: "role-icon " + id }, icon(roleIcon, 20)),
            h("div", { class: "row-text" }, h("b", {}, title), h("span", { class: "meta" }, roleText)), mark),
          h("div", { class: "field" }, fieldLabel(id + "_p", "Connection"), prov),
          h("div", { class: "field" }, fieldLabel(id + "_m", "Model"), model)),
        get: function () { return prov.value ? { provider_id: Number(prov.value), model: model.value } : null; } };
    }
    var pref = slot("pref", "Preferred", cur.preferred, false, "star", "Answers every question");
    var fb = slot("fb", "Fallback", cur.fallback, true, "backup", "Takes over if the preferred one fails");
    var timeout = h("input", { type: "number", id: "tmo", min: 5, max: 300, step: 1, value: cur.request_timeout });

    return panel("Which model answers", hintText, null,
      h("div", { class: "slots" }, pref.el, fb.el),
      h("div", { class: "actions spread" },
        h("div", { class: "field inline-field" }, fieldLabel("tmo", "Timeout (seconds)", "5–300. How long to wait before the fallback answers."), timeout),
        h("button", { type: "button", class: "btn primary", onclick: async function () {
          result.replaceChildren(msg("wait", "Saving…"));
          try {
            var r = await api("PUT", "/model-settings", { preferred: pref.get(), fallback: fb.get(), request_timeout: Number(timeout.value) });
            var box = h("div", {}, msg("ok", "Saved. Used from the next question."));
            (r.warnings || []).forEach(function (w) { box.appendChild(h("div", { class: "msg warn" }, w)); });
            refresh(box);
          } catch (err) { if (me) result.replaceChildren(msg("bad", err.message)); }
        } }, "Save")), result);
  }

  // The add form: pick a provider by its logo, name the connection, enter its key. Tested before anything is saved.
  function addProviderBody(data, refresh, close) {
    var approved = data.types.filter(function (t) { return t.approved; });
    var head = heading("h3", "Add a connection", "The connection is tested first; nothing is saved if the test fails. Keys are stored encrypted and never shown again.");
    if (!approved.length) return [head, h("div", { class: "empty" }, "Allow a provider first, under Allowed providers.")];
    var chosen = approved[0].type;
    var tiles = h("div", { class: "tiles", role: "radiogroup", "aria-label": "Provider" });
    var nameInput = h("input", { type: "text", id: "np_name", autocomplete: "off" });
    var formBox = h("div", {});
    var warn = h("div", {});
    var result = h("div", { "aria-live": "polite" });
    var sf = null;
    function draw() {
      var t = approved.filter(function (x) { return x.type === chosen; })[0];
      tiles.replaceChildren.apply(tiles, approved.map(function (x) {
        var on = x.type === chosen;
        return h("button", { type: "button", class: "tile" + (on ? " on" : ""), role: "radio", "aria-checked": String(on),
          onclick: function () { chosen = x.type; draw(); } }, provLogo(x.type, 32), h("span", {}, x.title));
      }));
      sf = specForm(t.fields);
      formBox.replaceChildren(sf.el);
      warn.replaceChildren(t.warning ? h("div", { class: "msg warn" }, t.warning) : "");
      nameInput.placeholder = "e.g. " + t.title + " (work)";
    }
    draw();
    function body() { return { type: chosen, name: nameInput.value.trim(), values: sf.collect() }; }
    return [head, h("span", { class: "l" }, "Provider"), tiles, warn,
      h("div", { class: "form" }, h("div", { class: "field" }, fieldLabel("np_name", "Name"), nameInput)), formBox,
      h("div", { class: "actions" },
        h("button", { type: "button", class: "btn primary", onclick: async function () {
          result.replaceChildren(msg("wait", "Testing and saving…"));
          try { var created = await api("POST", "/providers", body()); close(); refresh(msg("ok", "Added " + created.name + ". Choose it under “Which model answers”.")); }
          catch (err) { if (me) result.replaceChildren(msg("bad", err.message)); }
        } }, "Test and add"),
        h("button", { type: "button", class: "btn", onclick: async function () {
          result.replaceChildren(msg("wait", "Testing…"));
          try { var r = await api("POST", "/providers/test", body()); result.replaceChildren(msg("ok", r.message)); }
          catch (err) { if (me) result.replaceChildren(msg("bad", err.message)); }
        } }, "Test only")),
      result];
  }

  // Plain-language names for the codes the audit log and the reports store. The code stays visible on hover.
  var ACTION_LABELS = {
    "login": "Signed in", "login.failed": "Failed sign-in", "password.change": "Changed their own password", "password.set": "Chose a password with a link", "profile.photo": "Changed their profile photo", "logout": "Signed out",
    "admin.add": "Added an admin", "admin.remove": "Removed an admin", "user.add": "Added a person", "user.update": "Changed a person", "user.link": "Issued a password link",
    "department.add": "Added a department", "department.update": "Changed a department", "department.delete": "Deleted a department",
    "group.create": "Created a group", "group.update": "Changed a group", "group.delete": "Deleted a group",
    "channel.create": "Set up a channel", "channel.update": "Changed a channel", "channel.delete": "Deleted a channel", "channel.enable": "Switched a channel on",
    "channel.disable": "Switched a channel off", "channel.test": "Tested a channel", "secret.replace": "Replaced a stored key or token",
    "provider.create": "Connected an AI provider", "provider.update": "Changed an AI provider", "provider.delete": "Removed an AI provider", "provider.test": "Tested an AI provider",
    "providers.approval": "Changed the allowed AI providers", "model_settings.update": "Changed which model answers", "settings.update": "Changed settings",
    "guardrails.update": "Changed guardrails", "prompt.save": "Saved a prompt draft", "prompt.publish": "Published a prompt", "prompt.rollback": "Rolled a prompt back", "prompt.preview": "Tried a prompt",
    "kb.create": "Created a knowledge base", "kb.update": "Changed a knowledge base", "kb.delete": "Deleted a knowledge base", "kb.access": "Changed who can ask a knowledge base",
    "kb.reindex": "Reprocessed a knowledge base", "kb.rollback": "Rolled back a knowledge base", "kb.rebuild.start": "Started a rebuild", "kb.rebuild.finish": "Finished a rebuild",
    "kb.rebuild.fail": "A rebuild failed", "kb.rebuild.cancel": "Cancelled a rebuild", "document.upload": "Uploaded a document", "document.replace": "Replaced a document",
    "document.delete": "Deleted a document", "document.move": "Moved a document", "document.reindex": "Reprocessed a document", "document.access": "Changed who can read a document",
    "embedding.download": "Downloaded a search model", "conversation.view": "Opened a conversation", "conversation.delete": "Deleted a conversation",
    "conversation.export": "Started an export", "conversation.export.done": "Finished an export", "retention.run": "Ran automatic deletion", "alert.resolve": "Dismissed an alert",
    "alert_sinks.update": "Changed alert notifications", "alert_sinks.test": "Tested alert notifications", "price.set": "Set a price", "price.delete": "Removed a price",
    "import": "Imported settings", "audit.cleanup": "Cleaned up the audit log"
  };
  var REPORT_LABELS = {
    injection: "Prompt injection", toxicity: "Toxic language", banned_topic: "Banned topic", max_length: "Question too long", personal_data: "Personal data", leak: "Instructions leaked",
    input: "Question", output: "Answer", blocked: "Blocked", masked: "Masked", redacted: "Redacted"
  };
  function tidy(code) { var s = String(code).replace(/[._]/g, " "); return s.charAt(0).toUpperCase() + s.slice(1); }
  function actionLabel(code) { return ACTION_LABELS[code] || tidy(code); }
  function reportLabel(code) { return REPORT_LABELS[code] || (code === "(none)" ? "(none)" : tidy(code)); }

  // ---- audit ----------------------------------------------------------------------
  async function viewAudit(main) {
    pageHead(main, "Audit log", "Who changed what, and when. Keys and passwords are never recorded.");
    var PAGE = 50, offset = 0, first = true;
    var actor = selectOf([["", "Anyone"]], "", function () { offset = 0; load(); }, "Person");
    actor.id = "au_actor";
    var action = selectOf([["", "Any action"]], "", function () { offset = 0; load(); }, "Action");
    action.id = "au_action";
    var target = h("input", { type: "search", id: "au_target", placeholder: "Search by name", autocomplete: "off", "aria-label": "About (part of a name)",
      onchange: function () { offset = 0; load(); } });
    var dr = dateRange("au", function () { offset = 0; load(); }, true);
    var from = dr.from, to = dr.to;
    var holder = h("div", {}, msg("wait", "Loading…"));
    main.appendChild(h("section", { class: "panel filterbar" }, h("div", { class: "filters" }, actor, action, target), dr.el));
    main.appendChild(h("section", { class: "panel" }, holder));

    function fill(sel, items, label, labeler) {
      var keep = sel.value;
      sel.replaceChildren(h("option", { value: "" }, label));
      items.forEach(function (x) { sel.appendChild(h("option", { value: x }, labeler ? labeler(x) : x)); });
      sel.value = items.indexOf(keep) >= 0 ? keep : "";
    }
    async function load() {
      var q = "?actor=" + encodeURIComponent(actor.value) + "&action=" + encodeURIComponent(action.value) + "&target=" + encodeURIComponent(target.value)
        + "&date_from=" + from.value + "&date_to=" + to.value + "&limit=" + PAGE + "&offset=" + offset;
      try {
        var r = await api("GET", "/audit/search" + q);
        if (first) { fill(actor, r.actors, "Anyone"); fill(action, r.actions.slice().sort(function (a, b) { return actionLabel(a).localeCompare(actionLabel(b)); }), "Any action", actionLabel); first = false; }
        var body = h("tbody", {});
        r.rows.forEach(function (x) {
          body.appendChild(h("tr", {}, h("td", { class: "nw", "data-label": "When" }, when(x.ts)),
            h("td", { class: "wrap", "data-label": "Who" }, x.actor),
            h("td", { class: "wrap", "data-label": "What happened" }, h("span", { title: x.action }, actionLabel(x.action))),
            h("td", { class: "wrap", "data-label": "About" }, x.target),
            h("td", { class: "wrap", "data-label": "Details" }, x.detail)));
        });
        var last = Math.min(offset + PAGE, r.total);
        var prev = h("button", { type: "button", class: "btn", disabled: offset === 0, onclick: function () { offset = Math.max(0, offset - PAGE); load(); } }, "Previous");
        var next = h("button", { type: "button", class: "btn", disabled: last >= r.total, onclick: function () { offset += PAGE; load(); } }, "Next");
        holder.replaceChildren(
          r.rows.length ? h("div", { class: "tablewrap" }, h("table", {},
            h("thead", {}, h("tr", {}, ["When", "Who", "What happened", "About", "Details"].map(function (c) { return h("th", {}, c); }))), body)) : h("p", { class: "empty" }, "Nothing matches."),
          h("div", { class: "pager" }, h("span", { class: "meta", "aria-live": "polite" }, r.total ? (offset + 1) + "–" + last + " of " + r.total : "0 results"), h("div", { class: "actions" }, prev, next)));
      } catch (err) { if (me) holder.replaceChildren(msg("bad", err.message)); }
    }
    load();
  }

  // ---- reports ---------------------------------------------------------------------
  async function viewReports(main) {
    pageHead(main, "Reports", "What the guardrails caught, how each knowledge base is doing, and how each channel is used.");
    var dr = dateRange("rp", function () { load(); }, true);
    var from = dr.from, to = dr.to;
    var holder = h("div", { class: "stack" }, msg("wait", "Loading…"));
    main.appendChild(h("section", { class: "panel filterbar" }, dr.el));
    main.appendChild(holder);
    function n(x) { return Number(x).toLocaleString(); }
    function table(cols, rows) {
      var body = h("tbody", {});
      rows.forEach(function (r) {
        body.appendChild(h("tr", {}, r.map(function (c, i) { return h("td", { class: "wrap", "data-label": cols[i] }, c === null || c === undefined ? "–" : typeof c === "object" ? c : String(c)); })));
      });
      return rows.length ? h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, cols.map(function (c) { return h("th", {}, c); }))), body)) : h("p", { class: "empty" }, "Nothing yet.");
    }
    function bytes(x) { return x < 1024 ? x + " B" : x < 1048576 ? (x / 1024).toFixed(1) + " KB" : (x / 1048576).toFixed(1) + " MB"; }
    async function load() {
      var q = "?date_from=" + from.value + "&date_to=" + to.value;
      try {
        var g = await api("GET", "/reports/guardrails" + q);
        var k = await api("GET", "/reports/knowledge" + q);
        var c = await api("GET", "/reports/channels");
        function counts(list, plain) { return table(["What", "Times"], list.map(function (x) { return [plain ? reportLabel(x.name) : x.name, n(x.count)]; })); }
        holder.replaceChildren(
          panel("Guardrails", null, h("span", { class: "pill " + (g.total ? "warn" : "off") }, n(g.total) + " caught"),
            h("div", { class: "quad" },
              h("div", {}, h("h4", {}, "By check"), counts(g.by_check, true)), h("div", {}, h("h4", {}, "Question or answer"), counts(g.by_direction, true)),
              h("div", {}, h("h4", {}, "Action taken"), counts(g.by_action, true)), h("div", {}, h("h4", {}, "By department"), counts(g.by_department)))),
          h("section", { class: "panel" }, heading("h3", "Knowledge bases"),
            table(["Knowledge base", "Documents", "Passages", "Size", "Last updated", "Answered", "No answer found", "Tokens used for search"],
              k.rows.map(function (x) {
                return [x.name, Object.keys(x.by_status).map(function (s) { return x.by_status[s] + " " + s.toLowerCase(); }).join(", ") || "none", n(x.chunks), bytes(x.size_bytes),
                  x.last_indexed ? when(x.last_indexed) : "never", n(x.answered), n(x.not_found), n(x.embedding_tokens)];
              }))),
          h("section", { class: "panel" }, heading("h3", "Channels"),
            table(["Channel", "Enabled", "Running", "Conversations", "Last activity"],
              c.rows.map(function (x) {
                return [channelName(x.type, x.title), h("span", { class: "pill " + (x.enabled ? "ok" : "off") }, x.enabled ? "Yes" : "No"),
                  h("span", { class: "pill " + (x.running ? "ok" : x.enabled ? "bad" : "off") }, x.running ? "Running" : "Not running"),
                  n(x.conversations), x.last_activity ? when(x.last_activity) : "Never"];
              }))));
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
    mount(h("div", { class: "login-page" },
      h("div", { class: "login" },
        h("div", { class: "login-brand" }, h("span", { class: "brandmark" }, icon("brand", 20)), h("span", {}, "Channel Agnostic Agent")),
        h("h1", {}, "Choose your password"), h("p", {}, "You will use it to sign in to the admin center"), form)));
    nw.focus();
  }

  // ---- shared bits for users and departments -----------------------------------------
  var ROLE_LABEL = { admin: "Admin", editor: "Editor", viewer: "Viewer" };

  function linkBox(link) {
    var url = link.target === "web" ? link.url : location.origin + "/admin#setpw=" + link.token;
    var field = h("input", { type: "text", readonly: true, value: url, "aria-label": "One-time password link" });
    var copied = h("span", { class: "meta" });
    return panel("One-time password link", "Send it to the person yourself. It works once and is shown only now.", h("span", { class: "meta" }, "Expires " + when(link.expires_at)),
      h("div", { class: "copyrow" }, field, h("button", { type: "button", class: "btn", onclick: function () {
        field.select();
        if (navigator.clipboard) navigator.clipboard.writeText(url).then(function () { copied.textContent = "Copied."; }, function () { copied.textContent = "Press Ctrl+C to copy."; });
        else copied.textContent = "Press Ctrl+C to copy.";
      } }, icon("copy", 16), "Copy")), copied);
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
    var addBox = h("section", { class: "panel" });
    pageHead(main, "Users", "Admins and Editors sign in here. Everyone, Viewers included, can use the Web UI with the password set from their link. Company email addresses only.",
      addToggle("Add person", addBox));
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

    // search and filters work on the loaded list, so typing is instant
    var all = [];
    var search = h("input", { type: "search", placeholder: "Search name or email", autocomplete: "off", "aria-label": "Search people", oninput: draw });
    var roleF = selectOf([["", "Any role"]].concat(roleOptions), "", draw, "Role");
    var deptF = selectOf([["", "Any department"]].concat(deptOptions), "", draw, "Department");
    var statusF = selectOf([["", "Any status"], ["Active", "Active"], ["Deactivated", "Deactivated"], ["nopw", "No password yet"]], "", draw, "Status");
    var count = h("span", { class: "pill off" });

    function personCell(u, self) {
      return h("div", { class: "file" }, h("span", { class: "avatar sm", "aria-hidden": "true" }, initials(u.email)),
        h("div", { class: "row-text" }, h("b", {}, u.name || u.email, self ? h("span", { class: "pill ok" }, "You") : null), h("span", { class: "meta" }, u.email)));
    }
    function draw() {
      var q = search.value.trim().toLowerCase();
      var rows = all.filter(function (u) {
        if (q && (u.name + " " + u.email).toLowerCase().indexOf(q) < 0) return false;
        if (roleF.value && u.role !== roleF.value) return false;
        if (deptF.value && String(u.department_id) !== deptF.value) return false;
        if (statusF.value === "nopw") return !u.credentials_set;
        if (statusF.value && u.status !== statusF.value) return false;
        return true;
      });
      count.textContent = (rows.length === all.length ? all.length : rows.length + " of " + all.length) + (all.length === 1 ? " person" : " people");
      if (!rows.length) { list.replaceChildren(h("div", { class: "empty" }, all.length ? "Nobody matches the search or filters." : "No people yet.")); return; }
      var body = h("tbody", {});
      rows.forEach(function (u) {
        var self = u.email === me;                    // you cannot change your own role or deactivate yourself
        body.appendChild(h("tr", {},
          h("td", { class: "wrap", "data-label": "Person" }, personCell(u, self)),
          h("td", { "data-label": "Role" }, self ? h("span", { class: "fixed-value", title: "You cannot change your own role" }, ROLE_LABEL[u.role])
            : selectOf(roleOptions, u.role, function (e) { save(u, { role: e.target.value }, "Role updated."); }, "Role of " + u.email)),
          h("td", { "data-label": "Department" }, selectOf(deptOptions, u.department_id, function (e) { save(u, { department_id: Number(e.target.value) }, "Department updated."); }, "Department of " + u.email)),
          h("td", { "data-label": "Status" }, h("span", { class: "pill " + (u.status === "Active" ? "ok" : "off") }, u.status),
            !u.credentials_set ? h("div", { class: "meta" }, "No password yet") : null),
          h("td", { class: "nw", "data-label": "Last sign-in" }, u.last_sign_in ? when(u.last_sign_in) : h("span", { class: "meta" }, "Never")),
          h("td", { class: "act" }, self ? null : [
            u.status === "Active" ? h("button", { type: "button", class: "btn small", onclick: function () { newLink(u); } }, "Password link") : null,
            u.status === "Active"
              ? confirmButton("Deactivate", "Confirm deactivate", function () { save(u, { status: "Deactivated" }, "Deactivated " + u.email + "."); }, true)
              : h("button", { type: "button", class: "btn small", onclick: function () { save(u, { status: "Active" }, "Activated " + u.email + "."); } }, "Activate")])));
      });
      list.replaceChildren(h("div", { class: "tablewrap" }, h("table", { class: "people-table" },
        h("thead", {}, h("tr", {}, ["Person", "Role", "Department", "Status", "Last sign-in", ""].map(function (t) { return h("th", {}, t); }))), body)));
    }
    async function refresh() {
      try { all = await api("GET", "/users"); draw(); }
      catch (err) { if (me) list.replaceChildren(msg("bad", err.message)); }
    }

    var email = h("input", { type: "email", id: "nu_email", autocomplete: "off", required: true });
    var name = h("input", { type: "text", id: "nu_name", autocomplete: "off" });
    var role = selectOf(roleOptions, "viewer", null, "Role");
    role.id = "nu_role";
    var dept = selectOf(deptOptions, (depts.filter(function (d) { return d.is_default; })[0] || depts[0] || { id: "" }).id, null, "Department");
    dept.id = "nu_dept";
    main.appendChild(addBox);
    main.appendChild(result);
    main.appendChild(panel("People", null, count, h("div", { class: "toolbar" }, h("div", { class: "toolbar-left" }, search, roleF, deptF, statusF)), list));
    addBox.append(heading("h3", "Add a person", "They get a one-time link to choose a password."), h("form", { class: "inline", onsubmit: async function (e) {
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
    refresh();
  }

  // ---- who can see a document: shared picker --------------------------------------------
  var VISIBLE = [["everyone", "Everyone"], ["groups", "Selected groups"], ["people", "Selected people"]];

  // Draws "Visible to" with the group / people check lists. value() -> {visibility, group_ids, user_ids}.
  function accessPicker(initial, groupList, people, opts) {
    opts = opts || {};
    var wrap = h("div", { class: "picker" });
    var options = opts.noPeople ? VISIBLE.filter(function (o) { return o[0] !== "people"; }) : VISIBLE;
    var sel;
    if (opts.cards) {                    // three option cards with an icon and a line each, instead of a drop-down
      var DESC = { everyone: [opts.everyoneText || "Anyone who can use a channel bound to this knowledge base.", "channels"],
                   groups: ["Only members of the groups you pick.", "groups"], people: ["Only the people you pick.", "account"] };
      var name = "vis" + (++tipSeq);
      sel = { value: initial.visibility || "everyone" };
      sel.el = h("div", { class: "vis-cards", role: "radiogroup", "aria-label": opts.label || "Visible to" }, options.map(function (o) {
        var r = h("input", { type: "radio", name: name, value: o[0], checked: sel.value === o[0], class: "visually-hidden",
          onchange: function () { sel.value = o[0]; redraw(); } });
        return h("label", { class: "vis-card" }, r, h("span", { class: "vis-icon", "aria-hidden": "true" }, icon(DESC[o[0]][1], 18)),
          h("span", { class: "row-text" }, h("b", {}, o[1]), h("span", { class: "meta" }, DESC[o[0]][0])));
      }));
    } else {
      sel = selectOf(options, initial.visibility || "everyone", function () { redraw(); }, opts.label || "Visible to");
    }
    var lists = h("div", {});
    var gBoxes = groupList.map(function (g) { return { id: g.id, cb: h("input", { type: "checkbox", checked: (initial.group_ids || []).indexOf(g.id) >= 0 }), label: g.name }; });
    var uBoxes = people.map(function (u) { return { id: u.id, cb: h("input", { type: "checkbox", checked: (initial.user_ids || []).indexOf(u.id) >= 0 }), label: (u.name || u.email) + " · " + u.email }; });
    function box(b) { return h("label", { class: "check" }, b.cb, b.label); }
    function redraw() {
      var v = sel.value;
      if (opts.cards) Array.prototype.forEach.call(sel.el.children, function (c) { c.classList.toggle("on", c.firstChild.checked); });
      if (v === "groups") lists.replaceChildren(gBoxes.length ? h("div", { class: opts.cards ? "pick-list" : "kbpick" }, gBoxes.map(box)) : h("div", { class: "meta" }, "No groups yet. ", goLink("groups", "Add one under Groups")));
      else if (v === "people") lists.replaceChildren(h("div", { class: opts.cards ? "pick-list" : "kbpick" }, uBoxes.map(box)));
      else lists.replaceChildren(opts.cards ? "" : h("p", { class: "meta" }, opts.everyoneText || "Anyone who can use the channel."));
    }
    wrap.appendChild(opts.cards ? sel.el : sel);
    redraw();
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
    var addBox = h("section", { class: "panel" });
    pageHead(main, "Groups", "Named lists of people, used to decide who can read a document or ask a knowledge base. Membership changes apply from the next question.",
      addToggle("Add group", addBox));
    var holder = h("div", { class: "stack" }, msg("wait", "Loading…"));
    var result = h("div", { "aria-live": "polite" });
    main.appendChild(addBox);
    main.appendChild(result);
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
      } }, "Save");
      var del = confirmButton("Delete", "Confirm delete", async function () {
        try { await api("DELETE", "/groups/" + g.id); openId = null; await load(msg("ok", "Deleted " + g.name + ".")); }
        catch (err) { if (me) out.replaceChildren(msg("bad", err.message)); }
      }, true);
      var labels = boxes.map(function (b) { return h("label", { class: "check" }, b.cb, h("span", {}, b.p.name || b.p.email, h("span", { class: "meta" }, " · " + b.p.email))); });
      var picked = h("span", { class: "meta" });
      function countPicked() { var n = boxes.filter(function (b) { return b.cb.checked; }).length; picked.textContent = n + (n === 1 ? " member selected" : " members selected"); }
      boxes.forEach(function (b) { b.cb.addEventListener("change", countPicked); });
      countPicked();
      var filter = h("input", { type: "search", placeholder: "Filter people", autocomplete: "off", "aria-label": "Filter people", oninput: function () {
        var q = filter.value.trim().toLowerCase();
        labels.forEach(function (l, i) { l.hidden = !!q && ((boxes[i].p.name || "") + " " + boxes[i].p.email).toLowerCase().indexOf(q) < 0; });
      } });
      return h("div", {},
        h("div", { class: "form" },
          h("div", { class: "field" }, fieldLabel("gn" + g.id, "Name"), name),
          h("div", { class: "field" }, fieldLabel("gp" + g.id, "Managed by", "The department whose Editors may change this group."), dept),
          h("div", { class: "field wide" }, fieldLabel("gd" + g.id, "Description"), desc),
          h("div", { class: "field wide" }, h("div", { class: "lrow spread" }, h("span", { class: "l" }, "Members"), picked),
            pool.length > 8 ? filter : null, h("div", { class: "checks pick-list" }, labels))),
        h("div", { class: "actions" }, save, h("button", { type: "button", class: "btn", onclick: function () { openId = null; load(); } }, "Cancel"), del), out);
    }
    var openId = null;                                           // the one group open for editing

    async function load(notice) {
      try {
        var data = await api("GET", "/groups");
        var kids = [notice || null];
        var list = h("div", { class: "rows" }, data.groups.map(function (g) {
          var open = openId === g.id;
          return h("div", { class: "row" + (open ? " open" : "") },
            h("div", { class: "row-main" }, h("span", { class: "vis-icon", "aria-hidden": "true" }, icon("groups", 18)),
              h("div", { class: "row-text" }, h("b", {}, g.name), h("span", { class: "meta" }, g.description || (g.department ? "Managed by " + g.department : "Managed by Admins"))),
              h("div", { class: "row-side" }, h("span", { class: "pill off" }, g.members.length + (g.members.length === 1 ? " member" : " members")),
                g.description ? h("span", { class: "meta" }, g.department ? g.department : "Admins") : null,
                h("button", { type: "button", class: "btn small", "aria-expanded": String(open), onclick: function () { openId = open ? null : g.id; load(); } }, icon("edit", 14), open ? "Close" : "Edit"))),
            open ? h("div", { class: "row-detail" }, card(g, data)) : null);
        }));
        kids.push(panel("All groups", null, h("span", { class: "pill off" }, data.groups.length + (data.groups.length === 1 ? " group" : " groups")),
          data.groups.length ? list : h("div", { class: "empty" }, "No groups yet. Use “Add group” above.")));
        var nn = h("input", { type: "text", id: "ng_name", autocomplete: "off", required: true, maxlength: "80" });
        var opts = (state.perm && state.perm.can_manage_all ? [["", "None (Admins only)"]] : []).concat(data.departments.map(function (d) { return [d.id, d.name]; }));
        var nd = selectOf(opts, opts.length ? opts[0][0] : "", null, "Department");
        nd.id = "ng_dept";
        addBox.replaceChildren(heading("h3", "Add a group"), h("form", { class: "inline", onsubmit: async function (e) {
          e.preventDefault();
          result.replaceChildren(msg("wait", "Adding…"));
          try {
            var created = await api("POST", "/groups", { name: nn.value, department_id: nd.value === "" ? null : Number(nd.value) });
            result.replaceChildren(); addBox.hidden = true;
            if (created && created.id) openId = created.id;      // open the new group so its members can be chosen
            await load(msg("ok", "Group added. Choose its members below."));
          } catch (err) { if (me) result.replaceChildren(msg("bad", err.message)); }
        } },
          h("div", { class: "field" }, fieldLabel("ng_name", "Name"), nn),
          h("div", { class: "field" }, fieldLabel("ng_dept", "Managed by"), nd),
          h("div", {}, h("button", { class: "btn primary", type: "submit" }, "Add group"))));
        holder.replaceChildren.apply(holder, kids.filter(Boolean));
      } catch (err) { if (me) holder.replaceChildren(msg("bad", err.message)); }
    }
    await load();
  }

  // ---- knowledge bases ------------------------------------------------------------
  // ---- KB documents -----------------------------------------------------------------
  // Pick a knowledge base, upload into it (top), then the list of its files with who can see each one (below).
  var FILE_KINDS = { pdf: ["PDF", "pdf"], docx: ["DOC", "doc"], doc: ["DOC", "doc"], pptx: ["PPT", "ppt"], ppt: ["PPT", "ppt"],
                     md: ["MD", "md"], markdown: ["MD", "md"], txt: ["TXT", "txt"], html: ["HTML", "txt"], csv: ["CSV", "xls"], xlsx: ["XLS", "xls"] };
  function fileIcon(name) {
    var ext = (String(name).split(".").pop() || "").toLowerCase();
    var k = FILE_KINDS[ext] || [(ext || "file").slice(0, 4).toUpperCase(), "other"];
    return svgNode(["svg", { class: "ficon ft-" + k[1], viewBox: "0 0 32 40", width: "28", height: "35", "aria-hidden": "true", focusable: "false" }, [
      ["path", { class: "ficon-page", d: "M5 1h16l10 10v26a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V3a2 2 0 0 1 2-2z" }],
      ["path", { class: "ficon-fold", d: "M21 1v8a2 2 0 0 0 2 2h8" }],
      ["rect", { class: "ficon-band", x: "0", y: "21", width: "26", height: "12", rx: "2" }],
      ["text", { class: "ficon-text", x: "13", y: "30", "text-anchor": "middle" }, k[0]]]]);
  }
  function fileSize(n) { return !n ? "–" : n >= 1048576 ? (n / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1024)) + " KB"; }
  function docStatus(d) {
    var kind = d.status === "Indexed" ? "ok" : d.status === "Failed" ? "bad" : "warn";
    return h("span", { class: "pill " + kind }, d.status === "Pending" || d.status === "Indexing" ? d.status + "…" : d.status);
  }
  function visibleTo(d) {
    if (d.visibility === "everyone") return h("span", { class: "pill off" }, "Everyone");
    var names = d.access_names || [];
    var shown = names.slice(0, 2).map(function (n) { return h("span", { class: "pill off" }, n); });
    if (names.length > 2) shown.push(h("span", { class: "pill off", title: names.slice(2).join(", ") }, "+" + (names.length - 2)));
    return h("div", { class: "vis" }, h("span", { class: "vis-kind" }, d.visibility === "groups" ? "Groups" : "People"),
      names.length ? h("div", { class: "chips-static" }, shown) : h("span", { class: "meta bad" }, "Nobody selected"));
  }
  // A small "more actions" menu (details/summary: keyboard friendly, no script positioning).
  function moreMenu(label, items) {
    var menu = h("details", { class: "menu" }, h("summary", { class: "btn small", "aria-label": label, title: label }, "⋯"));
    menu.appendChild(h("div", { class: "menu-list", role: "menu" }, items.filter(Boolean)));
    menu.addEventListener("click", function (e) { if (e.target.closest(".menu-list button")) setTimeout(function () { menu.open = false; }, 0); });
    return menu;
  }

  async function viewKbDocs(main) {
    var picker = h("select", { id: "kd_pick", "aria-label": "Knowledge base" });
    pageHead(main, "KB documents", "Upload the files each knowledge base answers from, and choose who may read each one. New files are indexed automatically.",
      h("div", { class: "field kb-pick" }, fieldLabel("kd_pick", "Knowledge base"), picker));
    var body = h("div", { class: "stack" }, msg("wait", "Loading…"));
    main.appendChild(body);
    var data;
    try { data = await api("GET", "/kbs"); } catch (err) { if (me) body.replaceChildren(msg("bad", err.message)); return; }
    if (!data.kbs.length) {
      picker.disabled = true;
      body.replaceChildren(panel(null, null, null, h("div", { class: "empty" }, "No knowledge bases for your departments yet. ", goLink("kbconfig", "Create one in KB configuration"))));
      return;
    }
    data.kbs.forEach(function (k) { picker.appendChild(h("option", { value: String(k.id), text: k.name + (k.is_default ? " (default)" : "") })); });
    if (!data.kbs.some(function (k) { return k.id === state.kbDocSel; })) state.kbDocSel = data.kbs[0].id;
    picker.value = String(state.kbDocSel);
    picker.addEventListener("change", function () { state.kbDocSel = Number(picker.value); go("kbdocs"); });
    var k = data.kbs.filter(function (x) { return x.id === state.kbDocSel; })[0];
    var choices = null;
    try { choices = await loadChoices(); } catch (e) { choices = { groups: [], people: [] }; }

    var out = h("div", { "aria-live": "polite" });
    var docs = [], types = [], maxMb = 0, timer = null;

    // ---- upload -------------------------------------------------------------------
    var queue = [];
    var fileInput = h("input", { type: "file", id: "kd_file", multiple: true, class: "visually-hidden", onchange: function () { take(fileInput.files); } });
    var chosen = h("div", { class: "chosen" });
    var accepted = h("span", { class: "help" });
    var drop = h("label", { class: "dropzone", for: "kd_file",
      ondragover: function (e) { e.preventDefault(); drop.classList.add("over"); },
      ondragleave: function () { drop.classList.remove("over"); },
      ondrop: function (e) { e.preventDefault(); drop.classList.remove("over"); take(e.dataTransfer.files); } },
      icon("kbdocs", 28), h("span", { class: "dz-title" }, "Drop files here or ", h("span", { class: "dz-link" }, "browse")), accepted);
    function take(list) {
      queue = Array.prototype.slice.call(list || []);
      chosen.replaceChildren.apply(chosen, queue.map(function (f, i) {
        return h("div", { class: "chosen-file" }, fileIcon(f.name), h("div", { class: "row-text" }, h("b", {}, f.name), h("span", { class: "meta" }, fileSize(f.size))),
          h("button", { type: "button", class: "btn small", "aria-label": "Remove " + f.name, onclick: function () { queue.splice(i, 1); take(queue); } }, "Remove"));
      }));
      uploadBtn.disabled = !queue.length;
      uploadBtn.textContent = queue.length > 1 ? "Upload " + queue.length + " files" : "Upload";
      var bytes = queue.reduce(function (n, f) { return n + f.size; }, 0);
      ready.textContent = queue.length ? queue.length + (queue.length === 1 ? " file" : " files") + " ready · " + fileSize(bytes) : "No files chosen";
    }
    var access = accessPicker({ visibility: "everyone" }, choices.groups, choices.people, { label: "Who can see the uploaded files", cards: true });
    var ready = h("span", { class: "meta" }, "No files chosen");
    var uploadBtn = h("button", { type: "button", class: "btn primary", disabled: true, onclick: async function () {
      var v = access.value(), done = 0, failed = [];
      uploadBtn.disabled = true;
      for (var i = 0; i < queue.length; i++) {
        var f = queue[i];
        out.replaceChildren(msg("wait", "Uploading " + f.name + " (" + (i + 1) + " of " + queue.length + ")…"));
        var fd = new FormData(); fd.append("file", f);
        if (v.visibility !== "everyone") { fd.append("visibility", v.visibility); fd.append("group_ids", v.group_ids.join(",")); fd.append("user_ids", v.user_ids.join(",")); }
        try { await api("POST", "/kbs/" + k.id + "/documents", fd); done++; } catch (err) { failed.push(f.name + ": " + err.message); }
      }
      out.replaceChildren(done ? msg("ok", "Uploaded " + done + (done === 1 ? " file" : " files") + ". Indexing has started.") : "",
        failed.length ? msg("bad", failed.join(" · ")) : "");
      fileInput.value = ""; take([]); refresh();
    } }, "Upload");
    body.replaceChildren(
      panel("Upload to " + k.name, "Uploaded files are split and indexed automatically. To upload a new version of a file, use Replace in its row's ⋯ menu.",
        h("span", { class: "meta" }, k.department + " · " + (k.bound_channels.length ? "used by " + k.bound_channels.join(", ") : "not used by any channel")),
        h("div", { class: "upload-grid" },
          h("div", { class: "stack-sm" }, drop, fileInput, chosen),
          h("div", { class: "upload-side" }, h("span", { class: "l" }, "Who can see them"), access.el,
            h("div", { class: "upload-foot" }, ready, uploadBtn)))),
      out);

    // ---- list -----------------------------------------------------------------------
    var search = h("input", { type: "search", id: "kd_q", placeholder: "Search files", autocomplete: "off", "aria-label": "Search files", oninput: draw });
    var statusF = selectOf([["", "Any status"], ["Indexed", "Indexed"], ["busy", "Indexing or pending"], ["Failed", "Failed"]], "", draw, "Status");
    var visF = selectOf([["", "Anyone can see"], ["everyone", "Everyone"], ["restricted", "Restricted"]], "", draw, "Visibility");
    var count = h("span", { class: "pill off" });
    var tableBox = h("div", {});
    var openAccessId = null;
    var reindexAll = h("button", { type: "button", class: "btn", onclick: async function () {
      try { var r = await api("POST", "/kbs/" + k.id + "/reindex", {}); out.replaceChildren(msg("ok", r.queued + " documents queued for re-indexing.")); }
      catch (err) { if (me) out.replaceChildren(msg("bad", err.message)); }
      refresh();
    } }, "Re-index all");
    body.appendChild(panel("Documents", null, count,
      h("div", { class: "toolbar" }, h("div", { class: "toolbar-left" }, search, statusF, visF), reindexAll),
      tableBox));

    async function act(promise, okText) {
      try { await promise; if (okText) out.replaceChildren(msg("ok", okText)); }
      catch (err) { if (me) out.replaceChildren(msg("bad", err.message)); }
      refresh();
    }
    function accessRow(d) {
      var p = accessPicker({ visibility: d.visibility, group_ids: [], user_ids: [] }, choices.groups, choices.people);
      var cell = h("td", { colspan: "7" }, h("div", { class: "subpanel" }, heading("h4", "Who can see " + d.name, "Applies from the next question; the file is not re-indexed."), msg("wait", "Loading…")));
      api("GET", "/documents/" + d.id + "/access").then(function (cur) {
        p = accessPicker(cur, choices.groups, choices.people);
        cell.firstChild.replaceChildren(heading("h4", "Who can see " + d.name, "Applies from the next question; the file is not re-indexed."), p.el,
          h("div", { class: "actions" },
            h("button", { type: "button", class: "btn primary", onclick: function () { openAccessId = null; act(api("PUT", "/documents/" + d.id + "/access", p.value()), "Access updated for " + d.name + "."); } }, "Save"),
            h("button", { type: "button", class: "btn", onclick: function () { openAccessId = null; draw(); } }, "Cancel")));
      }, function (err) { cell.firstChild.replaceChildren(msg("bad", err.message)); });
      return h("tr", { class: "detail" }, cell);
    }
    function row(d) {
      var replaceInput = h("input", { type: "file", class: "hidden", accept: types.join(","), "aria-label": "New version of " + d.name, onchange: function (e) {
        var f = e.target.files[0]; if (!f) return;
        var fd = new FormData(); fd.append("file", f);
        act(api("POST", "/documents/" + d.id + "/replace", fd), "Replaced " + d.name + "; indexing again.");
      } });
      var others = data.kbs.filter(function (x) { return x.id !== k.id; });
      var menu = moreMenu("More actions for " + d.name, [
        h("button", { type: "button", role: "menuitem", onclick: function () { act(api("POST", "/documents/" + d.id + "/reindex", {}), "Re-indexing " + d.name + "."); } }, d.status === "Failed" ? "Retry indexing" : "Re-index"),
        h("button", { type: "button", role: "menuitem", onclick: function () { replaceInput.click(); } }, "Replace with a new version…"),
        others.length ? h("div", { class: "menu-sub" }, h("span", { class: "meta" }, "Move to"), others.map(function (x) {
          return h("button", { type: "button", role: "menuitem", onclick: function () { act(api("POST", "/documents/" + d.id + "/move", { kb_id: x.id }), "Moved " + d.name + " to " + x.name + "."); } }, x.name);
        })) : null,
        confirmButton("Delete", "Confirm delete", function () { act(api("DELETE", "/documents/" + d.id), "Deleted " + d.name + "."); }, true)]);
      return h("tr", { class: openAccessId === d.id ? "is-open" : "" },
        h("td", { class: "wrap", "data-label": "File" }, h("div", { class: "file" }, fileIcon(d.name),
          h("div", { class: "row-text" }, h("b", { class: "fname", title: d.name }, d.name),
            h("span", { class: "meta" }, d.uploaded_by ? "Uploaded by " + d.uploaded_by : ""),
            d.error ? h("span", { class: "meta bad" }, d.error) : null))),
        h("td", { "data-label": "Visible to" }, visibleTo(d)),
        h("td", { "data-label": "Status" }, docStatus(d)),
        h("td", { class: "nw num", "data-label": "Passages" }, d.status === "Indexed" ? String(d.chunks) : "–"),
        h("td", { class: "nw num", "data-label": "Size" }, fileSize(d.size)),
        h("td", { class: "nw", "data-label": "Updated" }, when(d.updated_at)),
        h("td", { class: "act" },
          h("button", { type: "button", class: "btn small", "aria-expanded": String(openAccessId === d.id), onclick: function () { openAccessId = openAccessId === d.id ? null : d.id; draw(); } }, "Access"),
          menu, replaceInput));
    }
    function draw() {
      var q = search.value.trim().toLowerCase(), st = statusF.value, vf = visF.value;
      var list = docs.filter(function (d) {
        if (q && d.name.toLowerCase().indexOf(q) < 0 && (d.access_names || []).join(" ").toLowerCase().indexOf(q) < 0) return false;
        if (st === "busy" && !(d.status === "Pending" || d.status === "Indexing")) return false;
        if (st && st !== "busy" && d.status !== st) return false;
        if (vf === "everyone" && d.visibility !== "everyone") return false;
        if (vf === "restricted" && d.visibility === "everyone") return false;
        return true;
      });
      count.textContent = (list.length === docs.length ? docs.length : list.length + " of " + docs.length) + (docs.length === 1 ? " file" : " files");
      if (!docs.length) { tableBox.replaceChildren(h("div", { class: "empty" }, "No documents yet. Upload the first one above.")); return; }
      if (!list.length) { tableBox.replaceChildren(h("div", { class: "empty" }, "No files match the search or filters.")); return; }
      var tb = h("tbody", {});
      list.forEach(function (d) { tb.appendChild(row(d)); if (openAccessId === d.id) tb.appendChild(accessRow(d)); });
      tableBox.replaceChildren(h("div", { class: "tablewrap" }, h("table", { class: "doc-table" },
        h("thead", {}, h("tr", {}, ["File", "Visible to", "Status", "Passages", "Size", "Updated", ""].map(function (t, i) { return h("th", { class: i === 3 || i === 4 ? "num" : null }, t); }))), tb)));
    }
    async function refresh() {
      try {
        var r = await api("GET", "/kbs/" + k.id + "/documents");
        docs = r.documents; types = r.types; maxMb = r.max_upload_mb;
        fileInput.accept = types.join(",");
        accepted.textContent = types.join(", ") + " · up to " + maxMb + " MB each";
        draw();
        var busy = docs.some(function (d) { return d.status === "Pending" || d.status === "Indexing"; });
        if (busy && !timer) timer = setInterval(function () { if (!document.body.contains(tableBox)) { clearInterval(timer); timer = null; return; } refresh(); }, 2500);
        if (!busy && timer) { clearInterval(timer); timer = null; }
      } catch (err) { if (me) tableBox.replaceChildren(msg("bad", err.message)); }
    }
    refresh();
  }

  async function viewKnowledge(main) {
    // KB configuration: create knowledge bases and set how their documents are split, searched and rebuilt, and who can ask.
    // One knowledge base at a time, picked in the header (the same choice as on KB documents).
    var addBox = h("section", { class: "panel" });
    var picker = h("select", { id: "kc_pick", "aria-label": "Knowledge base", onchange: function () { state.kbDocSel = Number(picker.value); load(); } });
    pageHead(main, "KB configuration", "A knowledge base is a set of documents owned by a department. Set how its documents are split and searched here, upload them under KB documents, and choose which channel uses it under Channels.",
      [h("div", { class: "field kb-pick" }, fieldLabel("kc_pick", "Knowledge base"), picker), addToggle("Add knowledge base", addBox)]);
    main.appendChild(addBox);
    var holder = h("div", { class: "stack" }, msg("wait", "Loading…"));
    var result = h("div", { "aria-live": "polite" });
    main.appendChild(holder);

    function blank(v) { return v === null || v === undefined ? "" : v; }
    function intOrNull(text) {
      var t = text.trim();
      if (t === "") return null;
      var n = Number(t);
      return isNaN(n) ? t : n;                                    // let the server explain
    }

    // ---- embedding model of this knowledge base -----------------------------------------------
    function embeddingPanel(k, data) {
      var box = fold("Search model", "The embedding model that makes documents searchable. Changing it needs a rebuild; until then the current model keeps answering.");
      var out = h("div", { "aria-live": "polite" });
      var form = h("div", {});
      var info = h("div", {});
      var options = data.embeddings;
      var pick = selectOf(options.map(function (o) { return [o.name, o.title]; }), k.embedding, null, "Search model");
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
          kids.push(h("div", { class: "help" }, "One-time download, about 129 MB (multilingual-e5-small, MIT licence). Runs on this server."));
          if (state.perm && state.perm.can_manage_all) kids.push(h("div", { class: "actions" }, h("button", { type: "button", class: "btn", onclick: async function () {
            out.replaceChildren(msg("wait", "Downloading… this can take a minute."));
            try { var r = await api("POST", "/kbs/" + k.id + "/embedding/download", {}); opt.needs_download = false; out.replaceChildren(msg("ok", r.message)); draw(); }
            catch (err) { if (me) out.replaceChildren(msg("bad", err.message)); }
          } }, "Download the model")));
        }
        form.replaceChildren.apply(form, kids);
        info.replaceChildren(h("div", { class: "meta" }, "In use: " + (options.filter(function (o) { return o.name === k.active_embedding; })[0] || { title: k.active_embedding || "?" }).title),
          k.embedding !== k.active_embedding ? h("div", { class: "msg warn" }, "A different model is selected. Rebuild the index to start using it.") : null);
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
        try { var r = await api("PUT", "/kbs/" + k.id, collect()); out.replaceChildren(msg("ok", "Saved. Rebuild the index to start using it.")); }
        catch (err) { if (me) out.replaceChildren(msg("bad", err.message)); }
      } }, "Save");
      box.appendChild(h("div", { class: "field" }, fieldLabel("em" + k.id, "Model"), pick));
      box.appendChild(info);
      box.appendChild(form);
      box.appendChild(h("div", { class: "actions" }, test, save));
      box.appendChild(out);
      draw();
      return box;
    }

    // ---- vector store of this knowledge base --------------------------------------------------
    function storePanel(k, data) {
      var box = fold("Search storage", "Where the search data is kept. Changing it needs a rebuild; the new copy is built beside the old one and used once complete.");
      var out = h("div", { "aria-live": "polite" });
      var form = h("div", {});
      var options = data.stores;
      var title = function (n) { return (options.filter(function (o) { return o.name === n; })[0] || { title: n || "?" }).title; };
      var pick = selectOf(options.map(function (o) { return [o.name, o.title]; }), k.store, null, "Search storage");
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
      var status = h("div", {}, h("div", { class: "statusline" }, h("span", { class: "pill " + (k.health && k.health.ok ? "ok" : "bad") }, k.health && k.health.ok ? "Reachable" : "Not reachable"),
        h("span", { class: "meta" }, "In use: " + title(k.active_store) + " · " + (k.chunks === null ? "?" : k.chunks) + " passages" + size
          + (k.last_indexed ? " · indexed " + when(k.last_indexed) : "") + (k.health && !k.health.ok ? " · " + k.health.message : ""))),
        k.store !== k.active_store ? h("div", { class: "msg warn" }, "A different store is selected. Rebuild the index to move the search data.") : null);
      var test = h("button", { type: "button", class: "btn", onclick: async function () {
        out.replaceChildren(msg("wait", "Testing…"));
        try { var r = await api("POST", "/kbs/" + k.id + "/store/test", collect()); out.replaceChildren(msg(r.ok ? "ok" : "bad", r.message)); }
        catch (err) { if (me) out.replaceChildren(msg("bad", err.message)); }
      } }, "Test");
      var save = h("button", { type: "button", class: "btn primary", onclick: async function () {
        out.replaceChildren(msg("wait", "Testing and saving…"));
        try { await api("PUT", "/kbs/" + k.id, collect()); out.replaceChildren(msg("ok", "Saved. Rebuild the index to move the search data.")); }
        catch (err) { if (me) out.replaceChildren(msg("bad", err.message)); }
      } }, "Save");
      box.appendChild(status);
      box.appendChild(h("div", { class: "field" }, fieldLabel("vs" + k.id, "Store"), pick));
      box.appendChild(form);
      box.appendChild(h("div", { class: "actions" }, test, save));
      box.appendChild(out);
      draw();
      return box;
    }

    // ---- chunk preview: run a strategy on a sample file without saving anything ---------------
    function previewPanel(k, data, strat, size, overlap) {
      var box = fold("Preview splitting", "Try the splitting settings above on a sample file before saving them. Nothing is stored.");
      var file = h("input", { type: "file", id: "pf" + k.id, "aria-label": "Sample file" });
      var out = h("div", { "aria-live": "polite" });
      var run = h("button", { type: "button", class: "btn", onclick: async function () {
        var f = file.files[0];
        if (!f) { out.replaceChildren(msg("bad", "Choose a sample file first.")); return; }
        var fd = new FormData();
        fd.append("file", f); fd.append("strategy", strat.value); fd.append("chunk_size", size.value); fd.append("chunk_overlap", overlap.value);
        out.replaceChildren(msg("wait", "Splitting the sample…"));
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
            msg("ok", r.count + " passages · average " + r.average_chars + " characters · longest " + r.longest + ". Nothing was saved."),
            r.notes.map(function (n) { return h("div", { class: "meta" }, n); }),
            h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["#", "Page", "Heading", "Size", "Text"].map(function (t) { return h("th", {}, t); }))), body)),
            r.count > r.shown ? h("div", { class: "meta" }, "Showing the first " + r.shown + " passages.") : null);
        } catch (err) { if (me) out.replaceChildren(msg("bad", err.message)); }
      } }, "Preview");
      box.appendChild(h("div", { class: "uploadbar" }, h("div", { class: "upload-pick" }, file), h("div", { class: "actions" }, run)));
      box.appendChild(out);
      return box;
    }

    // ---- index versions of one knowledge base ------------------------------------------
    function indexPanel(k) {
      var box = fold("Rebuild index", "Builds a new version beside the current one, which keeps answering until the new one is ready. Earlier versions can be rolled back to.");
      var flag = h("span", { class: "fold-flag" });
      box.firstChild.appendChild(flag);
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
          action.replaceChildren(h("div", { class: "subpanel" },
            heading("h4", "Rebuild estimate", "The current version keeps answering until the new one is complete. Documents are locked while it builds."),
            h("dl", { class: "facts" },
              h("dt", {}, "Documents"), h("dd", {}, String(e.documents)),
              h("dt", {}, "Passages"), h("dd", {}, "≈ " + e.chunks + " (" + e.tokens + " tokens)"),
              h("dt", {}, "Size / overlap"), h("dd", {}, e.target.chunk_size + " / " + e.target.chunk_overlap),
              h("dt", {}, "Model"), h("dd", {}, e.model),
              h("dt", {}, "Time"), h("dd", {}, "≈ " + secs(e.seconds)),
              h("dt", {}, "Cost"), h("dd", {}, money(e))),
            e.note ? h("div", { class: "help" }, e.note) : null,
            e.warning ? h("div", { class: "msg warn" }, e.warning) : null,
            e.skipped_documents ? h("div", { class: "msg warn" }, e.skipped_documents + " document(s) cannot be read and will be left out.") : null,
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
          if (act) kids.push(h("div", { class: "meta" }, "Active: v" + act.version + " · size " + act.chunk_size + " / overlap " + act.chunk_overlap
            + " · " + act.chunks + " passages · " + act.embedding));
          if (s.building) {
            var b = s.building;
            kids.push(msg("wait", "Building v" + b.version + ": " + b.progress_done + " of " + b.progress_total + " documents. The current index is still answering."));
            kids.push(h("div", { class: "actions" }, h("button", { type: "button", class: "btn small", onclick: cancelBuild }, "Cancel rebuild")));
          } else if (s.out_of_date) {
            kids.push(msg("warn", "Splitting settings changed (now " + s.desired.chunk_size + " / " + s.desired.chunk_overlap + "). Rebuild to apply them."));
          }
          if (s.building || s.out_of_date) box.open = true;               // never hide a build in progress or a pending change
          flag.replaceChildren(s.building ? h("span", { class: "pill off" }, "Building") : s.out_of_date ? h("span", { class: "pill warn" }, "Rebuild needed") : "");
          if (!s.building) kids.push(h("div", { class: "actions" }, h("button", { type: "button", class: "btn" + (s.out_of_date ? " primary" : ""), onclick: review }, "Rebuild…")));
          top.replaceChildren.apply(top, kids);
          var body = h("tbody", {});
          s.versions.forEach(function (v) {
            var pillKind = v.status === "Ready" ? "ok" : v.status === "Building" ? "off" : "bad";
            body.appendChild(h("tr", {},
              h("td", { "data-label": "Version" }, "v" + v.version, v.is_active ? h("span", { class: "pill ok" }, "Active") : null),
              h("td", { "data-label": "Settings" }, v.chunk_size + " / " + v.chunk_overlap),
              h("td", { class: "nw", "data-label": "Passages" }, String(v.chunks)),
              h("td", { "data-label": "Status" }, h("span", { class: "pill " + pillKind }, v.status), v.error ? h("div", { class: "meta bad" }, v.error) : null),
              h("td", { class: "nw", "data-label": "Built" }, when(v.finished_at || v.created_at)),
              h("td", { class: "act" }, v.status === "Ready" && !v.is_active && !s.building
                ? confirmButton("Roll back", "Confirm roll back", function () { rollback(v); }, true) : null)));
          });
          table.replaceChildren(s.versions.length ? h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {},
            ["Version", "Passage size / overlap", "Passages", "Status", "Built", ""].map(function (t) { return h("th", {}, t); }))), body)) : null);
          if (s.building && !timer) timer = setInterval(function () { if (!document.body.contains(box)) { stop(); return; } refresh(); }, 2500);
          if (!s.building) stop();
        } catch (err) { stop(); if (me) top.replaceChildren(msg("bad", err.message)); }
      }
      box.appendChild(top);
      box.appendChild(action);
      box.appendChild(table);
      box.appendChild(out);
      refresh();
      return box;
    }

    // ---- who may ask this knowledge base ----------------------------------------------
    function askPanel(k) {
      var box = fold("Who can ask", "People on a channel bound to this knowledge base can ask it. They only get answers from documents they may read.");
      var out = h("div", { "aria-live": "polite" });
      var body = h("div", {});
      async function show() {
        try {
          var cur = await api("GET", "/kbs/" + k.id + "/access");
          var ch = await api("GET", "/groups");
          var picker = accessPicker({ visibility: cur.restricted ? "groups" : "everyone", group_ids: cur.group_ids }, ch.groups, [],
            { noPeople: true, label: "Who can ask", everyoneText: "Anyone on a channel that uses this knowledge base." });
          body.replaceChildren(picker.el, h("div", { class: "actions" }, h("button", { type: "button", class: "btn primary", onclick: async function () {
            var v = picker.value();
            try { await api("PUT", "/kbs/" + k.id + "/access", { group_ids: v.visibility === "groups" ? v.group_ids : [] }); out.replaceChildren(msg("ok", "Saved. Applies from the next question.")); }
            catch (err) { if (me) out.replaceChildren(msg("bad", err.message)); }
          } }, "Save")));
        } catch (err) { if (me) body.replaceChildren(msg("bad", err.message)); }
      }
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
      var strat = selectOf(data.strategies.map(function (s) { return [s.name, s.title]; }), k.chunk_strategy, null, "How to split documents");
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
      } }, "Save");
      var del = confirmButton("Delete", "Confirm delete", async function () {
        try {
          var r = await api("DELETE", "/kbs/" + k.id);
          await load(msg("ok", "Deleted " + r.name + ": " + r.documents_removed + " documents and " + r.chunks_removed + " passages removed" + (r.channels_unbound.length ? ", unbound from " + r.channels_unbound.join(", ") : "") + "."));
        } catch (err) { if (me) out.replaceChildren(msg("bad", err.message)); }
      }, !k.is_default);
      return h("section", { class: "panel" },
        kbHead(k),
        h("h4", { class: "formhead" }, "General"),
        h("div", { class: "form" },
          h("div", { class: "field" }, fieldLabel("kn" + k.id, "Name"), name),
          h("div", { class: "field" }, fieldLabel("kp" + k.id, "Owning department"), dept),
          h("div", { class: "field wide" }, fieldLabel("kd" + k.id, "Description"), desc)),
        h("h4", { class: "formhead" }, "Splitting", tip("How documents are cut into passages. Leave size and overlap empty to use the defaults from Settings. Changes apply after a rebuild.")),
        h("div", { class: "form" },
          h("div", { class: "field wide" }, fieldLabel("kst" + k.id, "Strategy"), strat, stratHelp),
          h("div", { class: "field" }, fieldLabel("ks" + k.id, "Passage size (characters)", "For per-page, by-heading and by-meaning splitting, this is the largest a passage can be."), size),
          h("div", { class: "field" }, fieldLabel("ko" + k.id, "Overlap (characters)"), overlap)),
        h("div", { class: "actions" }, save, del), out,
        h("div", { class: "folds" }, previewPanel(k, data, strat, size, overlap), embeddingPanel(k, data), storePanel(k, data), indexPanel(k), askPanel(k)));
    }

    function kbHead(k) {
      return h("div", { class: "head" },
        h("div", {}, h("h3", {}, k.name, k.is_default ? h("span", { class: "pill ok" }, "Default") : null),
          h("div", { class: "meta" }, k.department + " · " + (k.documents === null ? "?" : k.documents) + " documents · " + (k.chunks === null ? "?" : k.chunks) + " passages")),
        h("div", { class: "head-right" }, k.bound_channels.length
          ? h("div", { class: "chips-static" }, k.bound_channels.map(function (c) { return h("span", { class: "pill off" }, c); }))
          : h("span", { class: "pill warn", title: "Choose it in a channel's settings under Channels" }, "Not used by any channel")));
    }

    async function load(notice) {
      try {
        var data = await api("GET", "/kbs");
        var kids = [notice || null, result];
        if (!data.kbs.some(function (k) { return k.id === state.kbDocSel; })) state.kbDocSel = data.kbs.length ? data.kbs[0].id : null;
        picker.replaceChildren.apply(picker, data.kbs.map(function (k) { return h("option", { value: String(k.id), text: k.name + (k.is_default ? " (default)" : "") }); }));
        picker.disabled = !data.kbs.length;
        if (data.kbs.length) picker.value = String(state.kbDocSel);
        if (!data.kbs.length) kids.push(panel(null, null, null, h("div", { class: "empty" }, "No knowledge bases yet. Use “Add knowledge base” above.")));
        data.kbs.forEach(function (k) { if (k.id === state.kbDocSel) kids.push(card(k, data)); });
        var nn = h("input", { type: "text", id: "nk_name", autocomplete: "off", required: true, maxlength: "80" });
        var nd = selectOf(data.departments.map(function (d) { return [d.id, d.name]; }), data.departments.length ? data.departments[0].id : "", null, "Department");
        nd.id = "nk_dept";
        addBox.replaceChildren(heading("h3", "Add a knowledge base", "Then set how it splits and searches documents below, and upload its files under KB documents."),
          h("form", { class: "inline", onsubmit: async function (e) {
            e.preventDefault();
            result.replaceChildren(msg("wait", "Adding…"));
            try {
              var created = await api("POST", "/kbs", { name: nn.value, department_id: Number(nd.value) });
              result.replaceChildren(); addBox.hidden = true;
              if (created && created.id) state.kbDocSel = created.id;          // open the new one
              await load(msg("ok", "Knowledge base added. Set it up below, then upload its files under KB documents."));
            } catch (err) { if (me) result.replaceChildren(msg("bad", err.message)); }
          } },
            h("div", { class: "field" }, fieldLabel("nk_name", "Name"), nn),
            h("div", { class: "field" }, fieldLabel("nk_dept", "Owning department"), nd),
            h("div", {}, h("button", { class: "btn primary", type: "submit" }, "Add knowledge base"))));
        holder.replaceChildren.apply(holder, kids.filter(Boolean));
      } catch (err) { if (me) holder.replaceChildren(msg("bad", err.message)); }
    }
    await load();
  }

  // ---- prompts -------------------------------------------------------------------------
  // A list of every prompt on the left, one prompt open on the right with three tabs: Edit, Test, History.
  function promptStatus(item, scope) {
    if (item.published) return h("span", { class: "pill ok" }, "Live v" + item.published.version);
    return h("span", { class: "pill off" }, scope === "dept" ? "Not set" : "Built-in");
  }

  // One editor for one prompt (a base prompt or a department's).
  function promptEditor(opts) {
    var box = h("section", { class: "panel prompt-editor" });
    var area = h("textarea", { id: "pt_" + opts.scope + "_" + opts.department_id, rows: "16", spellcheck: "false", "aria-label": opts.title });
    var counter = h("span", { class: "meta" });
    var live = h("div", { class: "statusline" });
    var dirty = h("span", { class: "pill warn", hidden: true }, "Unsaved changes");
    var out = h("div", { "aria-live": "polite" });
    var table = h("div", {});
    var previewOut = h("div", { "aria-live": "polite" });
    var versions = [], loadedText = "";
    var q = "?scope=" + encodeURIComponent(opts.scope) + "&department_id=" + opts.department_id;

    function count() {
      var n = area.value.trim().length;
      counter.textContent = n.toLocaleString() + " / " + opts.limit.toLocaleString() + " characters";
      counter.className = n > opts.limit ? "meta bad" : "meta";
      dirty.hidden = area.value === loadedText;
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
        live.replaceChildren(pub ? h("span", { class: "pill ok" }, "Live v" + pub.version) : h("span", { class: "pill off" }, opts.scope === "dept" ? "Not set" : "Built-in"),
          draft ? h("span", { class: "pill warn" }, "Draft v" + draft.version) : null,
          h("span", { class: "meta" }, pub ? "Published " + when(pub.published_at) + " by " + (pub.published_by || "?")
            : (opts.scope === "dept" ? "The base prompt is used on its own." : "Using the built-in text.")), dirty);
        if (notice !== "keep") { area.value = draft ? draft.text : pub ? pub.text : (r.builtin || ""); loadedText = area.value; }
        else loadedText = area.value;
        count();
        tabs.setCount(versions.length);
        var body = h("tbody", {});
        versions.forEach(function (v) {
          var kind = v.status === "Published" ? "ok" : v.status === "Draft" ? "warn" : "off";
          body.appendChild(h("tr", {},
            h("td", { "data-label": "Version" }, h("b", {}, "v" + v.version)),
            h("td", { "data-label": "Status" }, h("span", { class: "pill " + kind }, v.status)),
            h("td", { class: "wrap", "data-label": "Saved by" }, v.created_by),
            h("td", { class: "nw", "data-label": "Saved" }, when(v.created_at)),
            h("td", { class: "nw num", "data-label": "Size" }, v.chars.toLocaleString() + " chars"),
            h("td", { class: "act" },
              h("button", { type: "button", class: "btn small", onclick: function () { area.value = v.text; count(); tabs.show("edit"); out.replaceChildren(msg("ok", "Loaded v" + v.version + " into the editor. Save it as a draft or publish it.")); } }, "Open in editor"),
              v.status === "Draft" ? confirmButton("Publish", "Confirm publish", async function () { await doPublish(v.id); }, true) : null,
              v.status === "Archived" ? confirmButton("Roll back", "Confirm roll back", async function () { await doRollback(v.id); }, true) : null)));
        });
        table.replaceChildren(versions.length ? h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {},
          ["Version", "Status", "Saved by", "Saved", "Size", ""].map(function (t, i) { return h("th", { class: i === 4 ? "num" : null }, t); }))), body))
          : h("div", { class: "empty" }, "No versions yet. The first save creates version 1."));
      } catch (err) { if (me) out.replaceChildren(msg("bad", err.message)); }
    }
    async function doPublish(id) {
      try { var r = await api("POST", "/prompts/versions/" + id + "/publish", {}); await refresh(); out.replaceChildren(msg("ok", "Published v" + r.version + ". Used from the next question.")); if (opts.onChange) opts.onChange(); }
      catch (err) { if (me) out.replaceChildren(msg("bad", err.message)); }
    }
    async function doRollback(id) {
      try { var r = await api("POST", "/prompts/versions/" + id + "/rollback", {}); await refresh(); out.replaceChildren(msg("ok", "Rolled back to v" + r.version + ". Used from the next question.")); if (opts.onChange) opts.onChange(); }
      catch (err) { if (me) out.replaceChildren(msg("bad", err.message)); }
    }

    var saveBtn = h("button", { type: "button", class: "btn", onclick: async function () {
      out.replaceChildren(msg("wait", "Saving…"));
      try { var v = await saveDraft(); await refresh("keep"); out.replaceChildren(msg("ok", "Saved draft v" + v.version + ". Not live until published.")); if (opts.onChange) opts.onChange(); }
      catch (err) { if (me) out.replaceChildren(msg("bad", err.message)); }
    } }, "Save draft");
    var publishBtn = confirmButton("Save and publish", "Confirm: publish now", async function () {
      out.replaceChildren(msg("wait", "Publishing…"));
      try { var v = await saveDraft(); await doPublish(v.id); }
      catch (err) { if (me) out.replaceChildren(msg("bad", err.message)); }
    }, true);
    publishBtn.className = "btn primary";
    var revert = h("button", { type: "button", class: "btn", onclick: function () { area.value = loadedText; count(); out.replaceChildren(); } }, "Discard edits");

    var question = h("input", { type: "text", id: "pq_" + opts.scope + "_" + opts.department_id, placeholder: "Type a question people might ask", autocomplete: "off", "aria-label": "Test question",
      onkeydown: function (e) { if (e.key === "Enter") { e.preventDefault(); tryBtn.click(); } } });
    var tryBtn = h("button", { type: "button", class: "btn primary", onclick: async function () {
      if (!question.value.trim()) { previewOut.replaceChildren(msg("bad", "Type a question first.")); return; }
      previewOut.replaceChildren(msg("wait", "Asking with the text in the editor…"));
      try {
        var r = await api("POST", "/prompts/preview", { scope: opts.scope, department_id: opts.department_id, text: area.value, question: question.value });
        previewOut.replaceChildren(h("div", { class: "thread" },
          h("div", { class: "turn q" }, h("span", { class: "l" }, "Question"), h("div", { class: "bubble" }, question.value)),
          h("div", { class: "turn a" }, h("span", { class: "l" }, "Answer"), h("div", { class: "bubble" }, r.answer),
            r.sources && r.sources.length ? h("div", { class: "meta" }, "Sources: " + r.sources.map(function (s) { return s.source + (s.page ? " p." + s.page : ""); }).join(", ")) : null)));
      } catch (err) { if (me) previewOut.replaceChildren(msg("bad", err.message)); }
    } }, "Ask");

    var panes = {
      edit: h("div", { class: "pane" }, h("div", { class: "field editor" }, area, h("div", { class: "editor-foot" }, counter)),
        h("div", { class: "actions" }, publishBtn, saveBtn, revert), out),
      test: h("div", { class: "pane" }, h("p", { class: "help" }, "Uses the text in the editor, including unsaved edits. Nothing is published or stored in any conversation."),
        h("div", { class: "copyrow" }, question, tryBtn), previewOut),
      history: h("div", { class: "pane" }, table)
    };
    var tabs = tabBar([["edit", "Edit"], ["test", "Test"], ["history", "History"]], panes, "edit");
    box.append(heading("h3", opts.title, "Every save is a draft. Nothing changes for people until you publish, and you can roll back in one step."), live, tabs.el, panes.edit, panes.test, panes.history);
    refresh();
    return box;
  }

  // Tabs that switch between panes in place. setCount() puts a number on the last tab (e.g. History 4).
  function tabBar(items, panes, first) {
    var badge = h("span", { class: "tab-count" });
    var btns = items.map(function (t, i) {
      return h("button", { type: "button", role: "tab", onclick: function () { show(t[0]); } }, t[1], i === items.length - 1 ? badge : null);
    });
    function show(key) {
      items.forEach(function (t, i) { var on = t[0] === key; btns[i].className = on ? "on" : ""; btns[i].setAttribute("aria-selected", String(on)); panes[t[0]].hidden = !on; });
    }
    show(first);
    return { el: h("div", { class: "tabs", role: "tablist" }, btns), show: show, setCount: function (n) { badge.textContent = n ? String(n) : ""; } };
  }

  async function viewPrompts(main) {
    var isAdmin = state.perm && state.perm.can_manage_all;
    pageHead(main, "Prompts", isAdmin
      ? "Base prompts set who the assistant is and how it answers; departments cannot override them. Department prompts add tone and focus for one department."
      : "Your department's prompt adds tone and focus on top of the base prompt, which always comes first.");
    var holder = h("div", {}, msg("wait", "Loading…"));
    main.appendChild(holder);
    var o;
    async function load() {
      try { o = await api("GET", "/prompts"); } catch (err) { if (me) holder.replaceChildren(msg("bad", err.message)); return false; }
      return true;
    }
    if (!(await load())) return;
    // in the list, base prompts drop their "Base prompt: " prefix (they already sit under that heading)
    function short(t) { var x = t.replace(/^base prompt:\s*/i, ""); return x.charAt(0).toUpperCase() + x.slice(1); }
    var items = o.base.map(function (b) { return { key: b.scope + "|0", scope: b.scope, dept: 0, title: b.title, label: short(b.title), group: "Base prompts", data: b, limit: o.base_limit }; })
      .concat(o.departments.map(function (d) { return { key: "dept|" + d.department_id, scope: "dept", dept: d.department_id, title: d.department, label: d.department, group: "Department prompts", data: d, limit: o.limit }; }));
    if (!items.length) { holder.replaceChildren(panel(null, null, null, h("div", { class: "empty" }, "You are not an editor of any department yet. Ask an Admin."))); return; }
    if (!items.some(function (i) { return i.key === state.promptSel; })) state.promptSel = items[0].key;

    var listBox = h("nav", { class: "plist", "aria-label": "Prompts" });
    var search = items.length > 8 ? h("input", { type: "search", placeholder: "Find a department", "aria-label": "Find a prompt", oninput: drawList }) : null;
    var editorSlot = h("div", {});
    function drawList() {
      var qtext = search ? search.value.trim().toLowerCase() : "";
      var kids = [], lastGroup = null;
      items.forEach(function (it) {
        if (qtext && it.title.toLowerCase().indexOf(qtext) < 0) return;
        if (it.group !== lastGroup) { kids.push(h("div", { class: "nav-title" }, it.group)); lastGroup = it.group; }
        var on = it.key === state.promptSel;
        kids.push(h("button", { type: "button", class: "plist-item" + (on ? " on" : ""), "aria-current": on ? "true" : null, onclick: function () { state.promptSel = it.key; drawList(); openEditor(); } },
          h("span", { class: "plist-name", title: it.title }, it.label),
          h("span", { class: "plist-badges" }, it.data.drafts ? h("span", { class: "pill warn" }, "Draft") : null, promptStatus(it.data, it.scope))));
      });
      if (!kids.length) kids.push(h("div", { class: "meta" }, "No match."));
      listBox.replaceChildren.apply(listBox, kids);
    }
    function openEditor() {
      var it = items.filter(function (i) { return i.key === state.promptSel; })[0];
      editorSlot.replaceChildren(promptEditor({ scope: it.scope, department_id: it.dept, title: it.scope === "dept" ? "Prompt for " + it.title : it.title, limit: it.limit,
        onChange: async function () {                           // keep the list's badges in step with what was saved or published
          if (!(await load())) return;
          items.forEach(function (i) {
            var src = i.scope === "dept" ? o.departments.filter(function (d) { return d.department_id === i.dept; })[0] : o.base.filter(function (b) { return b.scope === i.scope; })[0];
            if (src) i.data = src;
          });
          drawList();
        } }));
    }
    holder.replaceChildren(h("div", { class: "prompt-layout" },
      h("section", { class: "panel plist-panel" }, search, listBox), editorSlot));
    drawList();
    openEditor();
  }

  // ---- guardrails -----------------------------------------------------------------------
  async function viewGuardrails(main) {
    pageHead(main, "Guardrails", "Safety checks around every question and answer; changes apply within seconds. Injection, toxicity and banned-topic checks are rules: they catch known phrasing and listed words, not paraphrases or other languages. Personal data is detected on this server before anything reaches the AI model.");
    var holder = h("div", {}, msg("wait", "Loading…"));
    main.appendChild(holder);

    var TYPE_LABELS = { EMAIL_ADDRESS: "Email addresses", PHONE_NUMBER: "Phone numbers", CREDIT_CARD: "Card numbers", US_SSN: "US social security numbers", IN_AADHAAR: "Aadhaar numbers (India)",
      IN_PAN: "PAN numbers (India)", IBAN_CODE: "Bank account numbers (IBAN)", IP_ADDRESS: "IP addresses", URL: "Web addresses", US_PASSPORT: "US passport numbers", US_BANK_NUMBER: "US bank account numbers",
      US_DRIVER_LICENSE: "US driving licences", US_ITIN: "US tax numbers (ITIN)", UK_NHS: "UK NHS numbers", CRYPTO: "Crypto wallet addresses", MEDICAL_LICENSE: "Medical licence numbers" };
    var LEVELS = {
      injection_sensitivity: { low: "Blocks only clear attacks. Fewest false alarms.", medium: "Balanced. Recommended.", high: "Also blocks mild wording, and may block some normal questions." },
      toxicity_sensitivity: { low: "Threats only.", medium: "Threats and profanity. Recommended.", high: "Also plain insults." }
    };

    function show(data, notice) {
      var fields = {}; data.fields.forEach(function (f) { fields[f.name] = f; });
      var orig = {}, cur = {};
      data.fields.forEach(function (f) { orig[f.name] = JSON.parse(JSON.stringify(f.value)); cur[f.name] = JSON.parse(JSON.stringify(f.value)); });
      var redraw = [];                                   // functions that refresh parts of the screen when something changes
      var uid = 0;

      function changed() { return Object.keys(cur).filter(function (k) { return JSON.stringify(cur[k]) !== JSON.stringify(orig[k]); }); }
      function set(name, value) { cur[name] = value; redraw.forEach(function (fn) { fn(); }); }
      function id() { return "gr" + (++uid); }

      // ---- small controls ------------------------------------------------------------
      function toggle(name, label) {
        var cb = h("input", { type: "checkbox", id: id(), checked: cur[name], "aria-label": label, onchange: function () { set(name, cb.checked); } });
        redraw.push(function () { cb.checked = cur[name]; });
        return h("label", { class: "switch" }, cb);
      }
      function segmented(name, label) {
        var wrap = h("div", { class: "field" });
        var note = h("div", { class: "help", "aria-live": "polite" });
        var seg = h("div", { class: "chips", role: "group", "aria-label": label });
        var btns = fields[name].choices.map(function (c) {
          return h("button", { type: "button", class: "chip", onclick: function () { set(name, c); } }, c.charAt(0).toUpperCase() + c.slice(1));
        });
        btns.forEach(function (b) { seg.appendChild(b); });
        function paint() { btns.forEach(function (b, i) { b.setAttribute("aria-pressed", String(fields[name].choices[i] === cur[name])); }); note.textContent = LEVELS[name][cur[name]] || ""; }
        redraw.push(paint); paint();
        wrap.append(h("span", { class: "l" }, label), seg, note);
        return wrap;
      }
      function tags(name, label, help, placeholder) {
        var wrap = h("div", { class: "field" });
        var box = h("div", { class: "tags" });
        var inp = h("input", { type: "text", id: id(), autocomplete: "off", placeholder: placeholder, "aria-label": label });
        function add(text) {
          var v = text.trim(); if (!v) return;
          if (cur[name].some(function (x) { return x.toLowerCase() === v.toLowerCase(); })) return;
          set(name, cur[name].concat([v]));
        }
        function commit() { inp.value.split(",").forEach(add); inp.value = ""; }
        inp.addEventListener("keydown", function (e) {
          if (e.key === "Enter" || e.key === ",") { e.preventDefault(); commit(); }
          else if (e.key === "Backspace" && !inp.value && cur[name].length) set(name, cur[name].slice(0, -1));
        });
        inp.addEventListener("blur", commit);
        function paint() {
          box.replaceChildren.apply(box, cur[name].map(function (v) {
            return h("span", { class: "tag" }, v, h("button", { type: "button", class: "x", "aria-label": "Remove " + v, onclick: function () { set(name, cur[name].filter(function (x) { return x !== v; })); } }, "×"));
          }).concat([inp]));
        }
        redraw.push(paint); paint();
        box.addEventListener("click", function () { inp.focus(); });
        wrap.append(fieldLabel(inp.id, label, help), box);
        return wrap;
      }
      function typeChecks() {
        var wrap = h("div", { class: "field" });
        var grid = h("div", { class: "checks" });
        var boxes = fields.masked_types.choices.map(function (t) {
          var cb = h("input", { type: "checkbox", id: id(), checked: cur.masked_types.indexOf(t) >= 0, onchange: function () {
            var s = cur.masked_types.filter(function (x) { return x !== t; }); if (cb.checked) s.push(t);
            set("masked_types", fields.masked_types.choices.filter(function (x) { return s.indexOf(x) >= 0; }));
          } });
          grid.appendChild(h("label", { class: "check", for: cb.id }, cb, TYPE_LABELS[t] || t));
          return [t, cb];
        });
        redraw.push(function () { boxes.forEach(function (p) { p[1].checked = cur.masked_types.indexOf(p[0]) >= 0; }); });
        wrap.append(h("span", { class: "l" }, "What counts as personal data"), grid);
        return wrap;
      }
      function sub2(name, label, help) {                    // a plain on/off option inside a card
        var t = toggle(name, label);
        return h("div", { class: "opt" }, t, h("b", {}, label), help ? tip(help) : null);
      }
      function lengthControl() {
        var n = h("input", { type: "number", id: id(), min: fields.max_input_tokens.min, max: fields.max_input_tokens.max, step: fields.max_input_tokens.step, value: cur.max_input_tokens,
          "aria-label": "Longest question in tokens", oninput: function () { var v = Number(n.value); set("max_input_tokens", n.value === "" || isNaN(v) ? n.value : v); } });
        var approx = h("span", { class: "help" });
        redraw.push(function () { var v = Number(cur.max_input_tokens); approx.textContent = isNaN(v) ? "" : "≈ " + (v * 4).toLocaleString() + " characters (" + Math.max(1, Math.round(v * 4 / 6)).toLocaleString() + " words)"; });
        redraw[redraw.length - 1]();
        return h("div", { class: "field" }, fieldLabel(n.id, "Longest question (tokens)", "Longer questions get the blocked reply."), n, approx);
      }

      // ---- one row per check: name and switch on one line, its settings underneath while it is on ---------
      function card(title, scope, what, enabledName, body, needs) {         // needs: another switch this check only works under
        var name = h("div", { class: "gr-name" }, h("h4", {}, title, tip(what)),
          scope === "Questions and answers" ? h("span", { class: "pill off" }, "Also checks answers") : null);
        if (!enabledName) {                                   // a check that is always on: no switch, only its setting
          return h("div", { class: "gr-row" }, h("div", { class: "gr-head" }, name, h("span", { class: "pill ok" }, "Always on")),
            h("div", { class: "gr-body" }, body));
        }
        var tog = toggle(enabledName, title);
        var detail = h("div", { class: "gr-body" });
        detail.appendChild(body);
        var el = h("div", { class: "gr-row" }, h("div", { class: "gr-head" }, name, tog), detail);
        var needNote = needs ? h("div", { class: "meta need-note", hidden: true }, "Not applied while “Check answers” is off. Switch that on for this to work.") : null;
        if (needNote) el.appendChild(needNote);
        redraw.push(function () {
          var on = !!cur[enabledName]; detail.hidden = !on; el.classList.toggle("is-off", !on);
          var blocked = !!needs && !cur[needs];
          el.classList.toggle("needs-off", blocked);
          if (needNote) needNote.hidden = !blocked;
        });
        redraw[redraw.length - 1]();
        return el;
      }
      function stack() { var d = h("div", { class: "gr-stack" }); for (var i = 0; i < arguments.length; i++) d.appendChild(arguments[i]); return d; }

      var kind = data.status === "ready" ? "ok" : data.status === "off" ? "off" : data.status === "loading" ? "off" : "bad";
      var label = { ready: "Detection model ready", off: "Guardrails off", loading: "Detection model loading…", error: "Detection model failed to load" }[data.status] || data.status;

      // master switch
      var masterTitle = h("h3", {});
      var masterNote = h("div", { class: "meta" });
      var master = h("section", { class: "gr-master" },
        h("div", {}, masterTitle, masterNote), h("div", { class: "gr-switch" }, h("span", { class: "pill " + kind }, label), toggle("enabled", "Guardrails")));
      var warn = h("div", { class: "msg warn" }, "Guardrails are off. Questions and answers are not checked.");
      redraw.push(function () { var on = !!cur.enabled; masterTitle.textContent = on ? "Guardrails are on" : "Guardrails are off"; masterNote.textContent = "Last changed " + when(data.updated_at) + " by " + data.updated_by; warn.hidden = on; page.classList.toggle("all-off", !on); });

      var questionCards = h("div", { class: "gr-list" },
        card("Question length", "Questions", "Very long messages are refused before any AI model is called.", null, lengthControl()),
        card("Prompt injection", "Questions", "Blocks attempts to make the assistant ignore its rules or reveal its instructions.", "injection_enabled",
          stack(segmented("injection_sensitivity", "Strictness"), tags("injection_phrases", "Always block these phrases", "Press Enter after each phrase. Any question containing one is blocked.", "e.g. act as root"))),
        card("Toxic language", "Questions and answers", "Blocks threats, profanity and insults in questions and answers.", "toxicity_enabled",
          stack(segmented("toxicity_sensitivity", "Strictness"), tags("toxicity_words", "Also block", "Add words your workplace needs. The built-in list leaves slurs out on purpose.", "Type a word, press Enter"),
            tags("toxicity_allow", "Always allow", "For example a product or place name that looks rude.", "Type a word, press Enter"))),
        card("Banned topics", "Questions and answers", "Blocks anything about a topic banned for the asker's department.", "banned_topics_enabled",
          h("div", { class: "help" }, "Topics are set per department. ", goLink("departments", "Edit in Departments"))),
        card("Mask personal data", "Questions", "Replaces personal data with a label such as <EMAIL_ADDRESS> before the question reaches the AI model, the history or any log.", "mask_enabled",
          stack(typeChecks(), sub2("mask_names", "Also mask names", "Off by default, because names are often needed to answer. Detection is approximate."))));
      var answerCards = h("div", { class: "gr-list" },
        card("Check answers", "Answers", "The finished answer is checked before anyone sees it. This is the master switch for all three answer checks: if it is off, redaction and instruction protection below are not applied either.", "output_checks_enabled", hint("Failed answers are replaced by the blocked reply.")),
        card("Redact personal data", "Answers", "Personal data found in an answer is replaced, except values you allow.", "output_redaction",
          tags("allow_list", "Never redact", "Exact values that may appear in answers, for example hr@company.com.", "name@company.com"), "output_checks_enabled"),
        card("Protect instructions", "Answers", "A hidden marker is added to the instructions; an answer that repeats it is replaced by the blocked reply.", "leak_check_enabled", hint("Stops answers that leak the hidden instructions."), "output_checks_enabled"));

      // blocked reply with a preview
      var reply = h("textarea", { id: id(), rows: "3", "aria-label": "Blocked reply text", oninput: function () { set("blocked_reply", reply.value); } });
      reply.value = cur.blocked_reply;
      var bubble = h("div", { class: "bubble" });
      redraw.push(function () { bubble.textContent = cur.blocked_reply || "(empty: people would see nothing)"; });
      var blockedBox = panel("Blocked reply", "What people see instead of an answer. No AI model is called for a blocked question.", null,
        h("div", { class: "stack-sm" }, h("div", { class: "field" }, h("label", { class: "l", for: reply.id }, "Message"), reply),
          h("div", { class: "field" }, h("span", { class: "l" }, "Preview"), bubble)));

      // save bar
      var result = h("div", { "aria-live": "polite" }, notice || null);
      var count = h("span", { class: "meta" });
      var saveBtn = h("button", { type: "button", class: "btn primary", onclick: async function () {
        var values = {}; changed().forEach(function (k) { values[k] = cur[k]; });
        result.replaceChildren(msg("wait", "Saving…"));
        try { show(await api("PUT", "/guardrails", { values: values }), msg("ok", "Saved. The new values are in use within a few seconds.")); }
        catch (err) { if (me) result.replaceChildren(msg("bad", err.message)); }
      } }, "Save");
      var discard = h("button", { type: "button", class: "btn", onclick: function () { show(data); } }, "Discard");
      var bar = h("div", { class: "savebar" }, count, h("div", { class: "actions" }, discard, saveBtn));
      redraw.push(function () { var n = changed().length; count.textContent = n ? n + " unsaved change" + (n === 1 ? "" : "s") : "No unsaved changes"; saveBtn.disabled = !n; discard.disabled = !n; bar.classList.toggle("dirty", n > 0); });

      var page = h("div", { class: "gr-page" }, master, warn,
        h("div", { class: "gr-cols" },
          panel("Question checks", "Run first. A blocked question never reaches the AI model.", null, questionCards),
          h("div", { class: "stack" },
            panel("Answer checks", "Run on the finished answer, before the person sees it.", null, answerCards),
            blockedBox)),
        result, bar);
      holder.replaceChildren(page);
      redraw.forEach(function (fn) { fn(); });
    }
    try { show(await api("GET", "/guardrails")); }
    catch (err) { if (me) holder.replaceChildren(msg("bad", err.message)); }
  }

  // ---- conversations (Admin) -------------------------------------------------------------
  async function viewConversations(main) {
    pageHead(main, "Conversations", "Every stored conversation from every channel. Personal data is masked before storage. Opening or deleting a conversation is recorded in the audit log.");
    var filters = h("div", { class: "filters" });
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
            h("td", { "data-label": "Channel" }, channelName(c.channel)),
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
            : h("p", { class: "empty" }, "No conversations match."),
          h("div", { class: "pager" },
            h("span", { class: "meta" }, "Page " + (page + 1) + " of " + pages + " · " + r.total + " conversations"),
            h("div", { class: "actions" },
              h("button", { type: "button", class: "btn", disabled: page === 0, onclick: function () { page--; load(); } }, "Previous"),
              h("button", { type: "button", class: "btn", disabled: page + 1 >= pages, onclick: function () { page++; load(); } }, "Next"))));
      } catch (err) { if (me) list.replaceChildren(msg("bad", err.message)); }
    }
    async function open(id) {
      detail.replaceChildren(msg("wait", "Loading…"));
      try {
        var r = await api("GET", "/conversations/" + encodeURIComponent(id));
        detail.replaceChildren(panel(r.title || "Conversation", null,
          h("button", { type: "button", class: "btn small", onclick: function () { detail.replaceChildren(); } }, "Close"),
          h("div", { class: "meta" }, r.person + " · " + r.channel + (r.department ? " · " + r.department : "")),
          h("div", { class: "thread" }, r.messages.map(function (m) {
            return h("div", { class: "turn " + (m.role === "user" ? "q" : "a") },
              h("span", { class: "l" }, m.role === "user" ? "Question" : "Answer" + (m.incomplete ? " (interrupted)" : "")),
              h("div", { class: "bubble" }, m.content));
          }))));
      } catch (err) { if (me) detail.replaceChildren(msg("bad", err.message)); }
    }
    // retention and export panels
    var retBox = h("section", { class: "panel" });
    var exBox = h("section", { class: "panel" });
    async function loadRetention(notice) {
      try {
        var r = await api("GET", "/conversations/retention/status");
        var total = r.preview.reduce(function (n, d) { return n + d.expired; }, 0);
        var body = h("tbody", {});
        r.preview.forEach(function (d) {
          body.appendChild(h("tr", {}, h("td", { "data-label": "Department" }, d.department), h("td", { "data-label": "Kept for" }, d.days + " days"),
            h("td", { "data-label": "Due for deletion" }, String(d.expired))));
        });
        retBox.replaceChildren(heading("h3", "Automatic deletion", "Once a day, conversations idle longer than their department's retention period are deleted for good.",
            h("span", { class: "pill " + (r.enabled ? "ok" : "off") }, r.enabled ? "Daily job on" : "Daily job off")),
          h("div", { class: "meta" }, r.last_run ? "Last run " + when(r.last_run.ts) + " by " + r.last_run.actor + ": " + r.last_run.detail : "No run yet"),
          h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Department", "Kept for", "Due for deletion"].map(function (t) { return h("th", {}, t); }))), body)),
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
          exBox.replaceChildren(heading("h3", "Export", null, h("span", { class: "pill off" }, "Off")),
            h("div", { class: "empty" }, "Turn on “Allow exporting conversations” once privacy sign-off is recorded. ", goLink("settings", "Open RAG settings")));
          return;
        }
        var reason = h("textarea", { id: "ex_reason", rows: "2", placeholder: "Why is this export needed?", "aria-label": "Reason" });
        var fmt = selectOf([["csv", "CSV"], ["json", "JSON"]], "csv", null, "Format");
        var out = h("div", { "aria-live": "polite" });
        exBox.replaceChildren(heading("h3", "Export", "Exports masked messages for the department and dates in the filters above. People appear as anonymous references. Every export is audited with your reason."),
          h("div", { class: "field wide" }, fieldLabel("ex_reason", "Reason", "Recorded in the audit log."), reason),
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
    var go = h("button", { type: "button", class: "btn primary", onclick: function () { page = 0; load(); } }, "Apply");
    filters.appendChild(h("div", { class: "field" }, fieldLabel("cv_dept", "Department"), dept));
    filters.appendChild(h("div", { class: "field" }, fieldLabel("cv_chan", "Channel"), chan));
    filters.appendChild(h("div", { class: "field" }, fieldLabel("cv_person", "Person"), person));
    main.appendChild(h("section", { class: "panel filterbar" }, filters, h("div", { class: "filterrow" }, dr.el, go)));
    main.appendChild(detail);
    main.appendChild(h("section", { class: "panel" }, list));
    main.appendChild(h("div", { class: "cols" }, retBox, exBox));
    load();
    loadRetention();
    loadExport();
  }

  // ---- usage and cost -------------------------------------------------------------------
  async function viewUsage(main) {
    var isAdmin = state.perm && state.perm.can_manage_all;
    pageHead(main, "Usage and cost", (isAdmin ? "Every model call: answers, routing, summaries, previews and embeddings." : "Model usage of your departments.")
      + " Cost counts only models with a price; other calls are listed as “no price”, so a total is never mistaken for the whole bill.");
    var GROUPS = [["provider", "Provider"], ["model", "Model"], ["department", "Department"], ["kb", "Knowledge base"], ["purpose", "Purpose"], ["kind", "Type of use"], ["day", "Day"], ["month", "Month"]];
    var group = selectOf(GROUPS, "provider", null, "Group by");
    group.id = "us_group";
    var dr = dateRange("us", function () { load(); }, false);
    var from = dr.from, to = dr.to;
    var list = h("div", {});
    var priceBox = h("section", { class: "panel" });
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
            h("td", { class: "nw", "data-label": "No price" }, x.unpriced_calls ? h("span", { class: "pill off" }, x.unpriced_calls + " calls") : "–")));
        });
        var t = r.totals;
        body.appendChild(h("tr", {}, h("td", {}, h("b", {}, "Total (" + r.scope + ")")), h("td", { class: "nw" }, n(t.calls)), h("td", { class: "nw" }, n(t.input_tokens)),
          h("td", { class: "nw" }, n(t.output_tokens)), h("td", { class: "nw" }, h("b", {}, money(t.cost))),
          h("td", { class: "nw" }, t.unpriced_calls ? h("span", { class: "pill bad" }, t.unpriced_calls + " unpriced") : "–")));
        list.replaceChildren(r.rows.length ? h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {},
          ["Group", "Calls", "Tokens in", "Tokens out", "Cost", "No price"].map(function (c) { return h("th", {}, c); }))), body)) : h("p", { class: "empty" }, "No usage in this period."),
          t.unpriced_calls ? msg("warn", t.unpriced_calls + " calls have no price, so the cost is a minimum. " + (isAdmin ? "Set prices below." : "Ask an Admin to set prices.")) : null);
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
        var pin = h("input", { type: "number", id: "pr_in", step: "any", min: 0, placeholder: "USD", "aria-label": "Input price" });
        var pout = h("input", { type: "number", id: "pr_out", step: "any", min: 0, placeholder: "USD", "aria-label": "Output price" });
        var out = h("div", { "aria-live": "polite" });
        priceBox.replaceChildren(heading("h3", "Prices", "USD per 1,000 tokens, from your provider's price page. Nothing is guessed: calls to a model without a price are left out of the cost."),
          r.unpriced.length ? h("div", { class: "msg warn" }, "Used without a price: " + r.unpriced.map(function (u) { return u.provider + " / " + u.model + " (" + u.unpriced_calls + ")"; }).join(", ")) : null,
          r.prices.length ? h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Provider type", "Model", "Input / 1k", "Output / 1k", ""].map(function (c) { return h("th", {}, c); }))), body)) : null,
          h("form", { class: "inline", onsubmit: async function (e) {
            e.preventDefault();
            try { await api("PUT", "/prices", { provider_type: ptype.value, model: model.value, input_per_1k: Number(pin.value), output_per_1k: Number(pout.value || 0) }); await loadPrices(msg("ok", "Price saved. It applies to calls from now on.")); }
            catch (err) { if (me) out.replaceChildren(msg("bad", err.message)); }
          } },
            h("div", { class: "field" }, h("label", { class: "l", for: "pr_type" }, "Provider type"), ptype),
            h("div", { class: "field" }, h("label", { class: "l", for: "pr_model" }, "Model"), model),
            h("div", { class: "field" }, h("label", { class: "l", for: "pr_in" }, "Input / 1k tokens"), pin),
            h("div", { class: "field" }, h("label", { class: "l", for: "pr_out" }, "Output / 1k tokens"), pout),
            h("div", {}, h("button", { class: "btn primary", type: "submit" }, "Save price"))),
          out, notice || null);
      } catch (err) { if (me) priceBox.replaceChildren(msg("bad", err.message)); }
    }

    main.appendChild(h("section", { class: "panel filterbar" },
      h("div", { class: "filters" }, h("div", { class: "field" }, fieldLabel("us_group", "Group by"), group)),
      h("div", { class: "filterrow" }, dr.el, h("button", { type: "button", class: "btn primary", onclick: load }, "Apply"))));
    main.appendChild(h("section", { class: "panel" }, list));
    if (isAdmin) { main.appendChild(priceBox); loadPrices(); }
    load();
  }

  // ---- departments -----------------------------------------------------------------
  async function viewDepartments(main) {
    var isAdmin = state.perm && state.perm.can_manage_all;
    var addBox = h("section", { class: "panel" });
    pageHead(main, "Departments", isAdmin
      ? "Each department has its own retention period and banned topics. Its Editors can change both."
      : "The departments you look after: banned topics and how long conversations are kept.",
      isAdmin ? addToggle("Add department", addBox) : null);
    var holder = h("div", { class: "stack" }, msg("wait", "Loading…"));
    var result = h("div", { "aria-live": "polite" });
    if (isAdmin) main.appendChild(addBox);
    main.appendChild(result);
    main.appendChild(holder);
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
      } }, "Save");
      var extra = [];
      if (isAdmin) {
        if (!d.is_default) extra.push(h("button", { type: "button", class: "btn small", onclick: async function () {
          try { await api("PUT", "/departments/" + d.id, { is_default: true }); await load(msg("ok", d.name + " is now the default.")); }
          catch (err) { if (me) out.replaceChildren(msg("bad", err.message)); }
        } }, "Make default"));
        extra.push(confirmButton("Delete", "Confirm delete", async function () {
          try { await api("DELETE", "/departments/" + d.id); openId = null; await load(msg("ok", "Deleted " + d.name + ".")); }
          catch (err) { if (me) out.replaceChildren(msg("bad", err.message)); }
        }, !d.is_default));
      }
      return h("div", {},
        h("div", { class: "form" },
          isAdmin ? h("div", { class: "field" }, fieldLabel("dn" + d.id, "Name"), name) : null,
          h("div", { class: "field" }, fieldLabel("dr" + d.id, "Keep conversations for (days)"), days, hint("1–" + max + " days")),
          h("div", { class: "field wide" }, fieldLabel("dt" + d.id, "Banned topics", "Topics the assistant politely refuses to discuss, one per line."), topics),
          isAdmin ? h("div", { class: "field wide" }, h("span", { class: "l" }, "Editors"),
            picks.length ? h("div", { class: "checks" }, picks.map(function (p) { return h("label", { class: "check", for: p[1].id }, p[1], p[0].name || p[0].email); }))
                         : hint("No active Editors yet. Give a user the Editor role first.")) : null),
        h("div", { class: "actions" }, save, h("button", { type: "button", class: "btn", onclick: function () { openId = null; load(); } }, "Cancel"), extra), out);
    }
    var openId = null;                                           // the one department open for editing

    async function load(notice) {
      try {
        var data = await api("GET", "/departments");
        var kids = [notice || null];
        if (data.departments.length === 1 && openId === null) openId = data.departments[0].id;   // only one: open it
        var list = h("div", { class: "rows" }, data.departments.map(function (d) {
          var open = openId === d.id;
          var topicCount = String(d.banned_topics || "").split(/\r?\n/).filter(function (x) { return x.trim(); }).length;
          var eds = (d.editors || []).map(function (x) { return x.name || x.email; });
          return h("div", { class: "row" + (open ? " open" : "") },
            h("div", { class: "row-main" }, h("span", { class: "vis-icon", "aria-hidden": "true" }, icon("departments", 18)),
              h("div", { class: "row-text" }, h("b", {}, d.name, d.is_default ? h("span", { class: "pill ok" }, "Default") : null),
                h("span", { class: "meta" }, "Keeps conversations " + d.retention_days + " days · " + topicCount + (topicCount === 1 ? " banned topic" : " banned topics")
                  + " · " + (eds.length ? "Editors: " + eds.join(", ") : "No editors"))),
              h("div", { class: "row-side" }, h("span", { class: "pill off" }, d.members + (d.members === 1 ? " person" : " people")),
                h("button", { type: "button", class: "btn small", "aria-expanded": String(open), onclick: function () { openId = open ? null : d.id; load(); } }, icon("edit", 14), open ? "Close" : "Edit"))),
            open ? h("div", { class: "row-detail" }, card(d, data.max_retention_days)) : null);
        }));
        kids.push(panel(isAdmin ? "All departments" : "Your departments", null, h("span", { class: "pill off" }, data.departments.length + (data.departments.length === 1 ? " department" : " departments")),
          data.departments.length ? list : h("div", { class: "empty" }, "You are not an editor of any department yet. Ask an Admin.")));
        holder.replaceChildren.apply(holder, kids.filter(Boolean));
      } catch (err) { if (me) holder.replaceChildren(msg("bad", err.message)); }
    }

    if (isAdmin) {
      var nn = h("input", { type: "text", id: "nd_name", autocomplete: "off", required: true, maxlength: "80" });
      var nr = h("input", { type: "number", id: "nd_days", value: 90, min: 1, step: 1 });
      addBox.append(heading("h3", "Add a department"), h("form", { class: "inline", onsubmit: async function (e) {
        e.preventDefault();
        result.replaceChildren(msg("wait", "Adding…"));
        try {
          var created = await api("POST", "/departments", { name: nn.value, retention_days: Number(nr.value) });
          nn.value = ""; addBox.hidden = true;
          if (created && created.id) openId = created.id;
          result.replaceChildren(msg("ok", "Department added. Set its banned topics and Editors below."));
          await load();
        } catch (err) { if (me) result.replaceChildren(msg("bad", err.message)); }
      } },
        h("div", { class: "field" }, h("label", { class: "l", for: "nd_name" }, "Name"), nn),
        h("div", { class: "field" }, h("label", { class: "l", for: "nd_days" }, "Keep conversations for (days)"), nr),
        h("div", {}, h("button", { class: "btn primary", type: "submit" }, "Add department"))));
    }
    await load();
  }

  // ---- home -----------------------------------------------------------------------
  // The landing screen: a greeting, what is left to set up (checked against the real configuration), this week's numbers
  // against last week's, channel status and anything that needs attention. Each part loads on its own and is left out
  // when this person may not see its data.
  function isoDay(d) { var m = d.getMonth() + 1, x = d.getDate(); return d.getFullYear() + "-" + (m < 10 ? "0" : "") + m + "-" + (x < 10 ? "0" : "") + x; }
  function daysAgo(n) { var d = new Date(); d.setDate(d.getDate() - n); return isoDay(d); }
  function firstName() {
    var n = (state.profile && state.profile.name) || String(me || "").split("@")[0];
    n = n.split(/[\s._-]+/)[0] || n;
    return n.charAt(0).toUpperCase() + n.slice(1);
  }
  async function quiet(path) { try { return await api("GET", path); } catch (e) { return null; } }

  async function viewHome(main) {
    var hour = new Date().getHours();
    var hello = (hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening") + ", " + firstName();
    main.appendChild(h("div", { class: "page-head" },
      h("div", { class: "titles" }, h("h2", {}, hello), h("p", { class: "lede" }, "Here is what is happening with the assistant this week.")),
      h("div", { class: "actions" },
        canSee("reports") ? h("button", { type: "button", class: "btn", onclick: function () { go("reports"); } }, "View reports") : null,
        canSee("kbdocs") ? h("button", { type: "button", class: "btn primary", onclick: function () { go("kbdocs"); } }, icon("plus", 16), "Upload documents") : null)));
    var setupBox = h("div", {}), kpiBox = h("div", {}), lower = h("div", { class: "cols" });
    main.append(setupBox, kpiBox, lower);

    var thisWeek = "?date_from=" + daysAgo(6) + "&date_to=" + isoDay(new Date());
    var lastWeek = "?date_from=" + daysAgo(13) + "&date_to=" + daysAgo(7);
    var got = await Promise.all([
      canSee("providers") ? quiet("/providers") : null, canSee("kbconfig") ? quiet("/kbs") : null,
      canSee("users") ? quiet("/users") : null, canSee("guardrails") ? quiet("/guardrails") : null,
      canSee("reports") ? quiet("/reports/knowledge" + thisWeek) : null, canSee("reports") ? quiet("/reports/knowledge" + lastWeek) : null,
      canSee("reports") ? quiet("/reports/guardrails" + thisWeek) : null, canSee("reports") ? quiet("/reports/guardrails" + lastWeek) : null,
      canSee("usage") ? quiet("/usage?group=provider&date_from=" + daysAgo(6) + "&date_to=" + isoDay(new Date())) : null,
      canSee("usage") ? quiet("/usage?group=provider&date_from=" + daysAgo(13) + "&date_to=" + daysAgo(7)) : null,
      quiet("/me/alerts")]);
    if (!document.body.contains(main)) return;                        // moved to another screen while loading
    var prov = got[0], kbsData = got[1], people = got[2], guard = got[3], kNow = got[4], kThen = got[5],
        gNow = got[6], gThen = got[7], uNow = got[8], uThen = got[9], alerts = got[10] || [];

    // ---- finish setting up ----------------------------------------------------------
    var kbs = kbsData ? kbsData.kbs : [];
    var guardOn = guard && (guard.fields || []).some(function (f) { return f.name === "enabled" && f.value; });
    var steps = [
      ["providers", "Connect an LLM provider", prov && prov.providers.length && prov.model_settings && prov.model_settings.preferred],
      ["kbdocs", "Upload documents to a knowledge base", kbs.some(function (k) { return k.documents > 0; })],
      ["channels", "Connect a channel", state.channels.some(function (c) { return c.running; })],
      ["channels", "Use a knowledge base on a channel", kbs.some(function (k) { return k.bound_channels && k.bound_channels.length; })],
      ["users", "Add the people who will use it", people && people.length > 1],
      ["guardrails", "Switch on guardrails", guardOn]
    ].filter(function (s) { return canSee(s[0]) && (s[0] !== "kbdocs" || kbsData) && (s[0] !== "providers" || prov) && (s[0] !== "users" || people) && (s[0] !== "guardrails" || guard); });
    var done = steps.filter(function (s) { return s[2]; }).length;
    if (steps.length && done < steps.length) {
      setupBox.appendChild(panel("Finish setting up", "Each step is checked against the current configuration. Select a step to open its screen.",
        h("span", { class: "meta" }, done + " of " + steps.length + " steps done"),
        h("div", { class: "progress", role: "progressbar", "aria-valuemin": "0", "aria-valuemax": String(steps.length), "aria-valuenow": String(done),
          "aria-label": "Setup progress" }, h("div", { class: "progress-bar p" + Math.round(done / steps.length * 100 / 5) * 5 })),
        h("div", { class: "steps" }, steps.map(function (s) {
          return h("button", { type: "button", class: "step-card" + (s[2] ? " done" : ""), onclick: function () { go(s[0]); } },
            h("span", { class: "step-check", "aria-hidden": "true" }, s[2] ? icon("check", 14) : null),
            h("span", { class: "step-text" }, s[1]), s[2] ? h("span", { class: "visually-hidden" }, " (done)") : icon("arrow", 14));
        }))));
    } else if (steps.length) {
      setupBox.appendChild(h("div", { class: "msg ok" }, "Setup complete: every step is done."));
    }

    // ---- this week ----------------------------------------------------------------------
    function sum(rep, key) { return rep ? rep.rows.reduce(function (n, r) { return n + (r[key] || 0); }, 0) : null; }
    function delta(now, then, fmt, lowerIsBetter) {
      if (now === null || then === null) return h("span", { class: "meta" }, "");
      if (then === 0 && now === 0) return h("span", { class: "meta" }, "Same as last week");
      var diff = now - then, up = diff > 0;
      var good = diff === 0 ? null : (up !== !!lowerIsBetter);
      return h("span", { class: "delta" + (good === null ? "" : good ? " good" : " bad") },
        diff === 0 ? "Same as last week" : (up ? "▲ " : "▼ ") + fmt(Math.abs(diff)) + " vs last week");
    }
    var asked = kNow ? sum(kNow, "answered") + sum(kNow, "not_found") : null;
    var askedThen = kThen ? sum(kThen, "answered") + sum(kThen, "not_found") : null;
    var rate = asked ? Math.round(sum(kNow, "answered") / asked * 100) : null;
    var rateThen = askedThen ? Math.round(sum(kThen, "answered") / askedThen * 100) : null;
    var tiles = [];
    if (kNow) tiles.push(["Questions searched", asked.toLocaleString(), delta(asked, askedThen, function (x) { return x.toLocaleString(); })]);
    if (kNow) tiles.push(["Answered from documents", rate === null ? "–" : rate + "%", delta(rate, rateThen, function (x) { return x + " pts"; })]);
    if (gNow) tiles.push(["Blocked by guardrails", gNow.total.toLocaleString(), delta(gNow.total, gThen ? gThen.total : null, function (x) { return x.toLocaleString(); }, true)]);
    if (uNow) {
      var c = uNow.totals.cost, ct = uThen ? uThen.totals.cost : null;
      tiles.push(["AI cost", "$" + c.toFixed(2), uNow.totals.unpriced_calls ? h("span", { class: "meta" }, uNow.totals.unpriced_calls + " calls have no price")
        : delta(c, ct, function (x) { return "$" + x.toFixed(2); }, true)]);
    }
    if (tiles.length) kpiBox.appendChild(h("div", { class: "kpis" }, tiles.map(function (t) {
      return h("div", { class: "kpi" }, h("span", { class: "kpi-label" }, t[0]), h("span", { class: "kpi-value" }, t[1]), t[2]);
    })));

    // ---- channels and attention ----------------------------------------------------------
    if (canSee("channels")) {
      lower.appendChild(panel("Channels", null, goLink("channels", "Manage"),
        h("div", { class: "rows" }, state.channels.map(function (ch) {
          var s = status(ch);
          return h("div", { class: "row" }, h("div", { class: "row-main" }, logo(ch.type, 28),
            h("div", { class: "row-text" }, h("b", {}, ch.title), h("span", { class: "meta" }, ch.configured ? "Updated " + when(ch.updated_at) : "Not set up")),
            h("span", { class: "pill " + s[0] }, s[1])));
        }))));
    }
    var items = alerts.map(function (a) { return [a.severity === "error" ? "bad" : "warn", a.message + (a.count > 1 ? " (" + a.count + " times)" : ""), null]; });
    state.channels.forEach(function (ch) { if (ch.configured && ch.enabled && !ch.running) items.push(["bad", ch.title + " is switched on but not running.", "channels"]); });
    (prov ? prov.providers : []).forEach(function (p) { if (p.status === "Failed") items.push(["bad", "The " + p.name + " connection is failing" + (p.status_detail ? ": " + p.status_detail : "."), "providers"]); });
    lower.appendChild(panel("Needs your attention", null, items.length ? h("span", { class: "pill warn" }, String(items.length)) : null,
      items.length ? h("div", { class: "rows" }, items.map(function (it) {
        return h("div", { class: "row attention " + it[0] }, h("div", { class: "row-main" },
          h("span", { class: "row-text" }, it[1]), it[2] ? goLink(it[2], "Open") : null));
      })) : h("div", { class: "empty" }, "Nothing needs attention.")));
  }

  // ---- account --------------------------------------------------------------------
  // Your profile (photo, department, groups), how the admin center looks for you, and your password.
  function avatarEl(cls) {
    var p = state.profile;
    return p && p.has_avatar
      ? h("img", { class: "avatar " + (cls || ""), src: API + "/me/avatar?v=" + (state.avatarVer || 0), alt: "" })
      : h("span", { class: "avatar " + (cls || ""), "aria-hidden": "true" }, initials(me));
  }
  async function loadProfile() {
    try { state.profile = await api("GET", "/me/profile"); } catch (e) { state.profile = null; }
  }
  // Crop the picked image to a centred square of 256 px and re-encode it, so any photo becomes a small JPEG.
  async function squarePhoto(file) {
    var bmp = await createImageBitmap(file);
    var side = Math.min(bmp.width, bmp.height);
    var c = document.createElement("canvas");
    c.width = c.height = 256;
    c.getContext("2d").drawImage(bmp, (bmp.width - side) / 2, (bmp.height - side) / 2, side, side, 0, 0, 256, 256);
    return new Promise(function (ok, fail) { c.toBlob(function (b) { b ? ok(b) : fail(new Error("Could not read that image.")); }, "image/jpeg", 0.88); });
  }

  function viewAccount(main) {
    pageHead(main, "Account", "Your profile and how the admin center looks for you.");
    var p = state.profile || { email: me, name: "", role: state.perm ? state.perm.role : "", department: null, groups: [], editor_of: [] };

    // profile
    var photoOut = h("div", { "aria-live": "polite" });
    var file = h("input", { type: "file", accept: "image/png,image/jpeg,image/webp", class: "hidden", "aria-label": "Choose a profile photo", onchange: async function () {
      var f = file.files[0]; if (!f) return;
      photoOut.replaceChildren(msg("wait", "Uploading…"));
      try {
        var blob = await squarePhoto(f);
        var fd = new FormData(); fd.append("file", blob, "photo.jpg");
        await api("PUT", "/me/avatar", fd);
        state.avatarVer = Date.now(); await loadProfile(); go("account");
      } catch (err) { photoOut.replaceChildren(msg("bad", err.message)); }
      file.value = "";
    } });
    var removeBtn = p.has_avatar ? confirmButton("Remove", "Confirm remove", async function () {
      try { await api("DELETE", "/me/avatar"); state.avatarVer = Date.now(); await loadProfile(); go("account"); }
      catch (err) { photoOut.replaceChildren(msg("bad", err.message)); }
    }, true) : null;
    function pills(list, kind) {
      return list && list.length ? h("div", { class: "chips-static" }, list.map(function (x) { return h("span", { class: "pill " + kind }, x); })) : h("span", { class: "meta" }, "None");
    }
    main.appendChild(panel("Profile", "Your department and groups decide which documents and knowledge bases you can see. Admins change them under User management.", null,
      h("div", { class: "profile" },
        h("div", { class: "profile-photo" }, avatarEl("xl"),
          h("div", { class: "actions" }, h("button", { type: "button", class: "btn small", onclick: function () { file.click(); } }, icon("camera", 14), p.has_avatar ? "Change photo" : "Add photo"), removeBtn),
          file, photoOut),
        h("dl", { class: "facts" },
          h("dt", {}, "Name"), h("dd", {}, p.name || "–"),
          h("dt", {}, "Email"), h("dd", {}, p.email),
          h("dt", {}, "Role"), h("dd", {}, h("span", { class: "pill ok" }, ROLE_LABEL[p.role] || p.role)),
          h("dt", {}, "Department"), h("dd", {}, p.department || "–"),
          h("dt", {}, "Groups"), h("dd", {}, pills(p.groups, "off")),
          p.editor_of && p.editor_of.length ? [h("dt", {}, "Editor of"), h("dd", {}, pills(p.editor_of, "off"))] : null,
          h("dt", {}, "Last sign-in"), h("dd", {}, when(p.last_sign_in))))));

    // appearance
    var theme = themePref() === "dark" || themePref() === "light" ? themePref() : "system";
    var themeSeg = h("div", { class: "seg", role: "radiogroup", "aria-label": "Theme" }, [["light", "Light", "sun"], ["dark", "Dark", "moon"], ["system", "System", "settings"]].map(function (t) {
      return h("button", { type: "button", role: "radio", "aria-checked": String(theme === t[0]), class: theme === t[0] ? "on" : "", onclick: function () {
        if (t[0] === "system") { themePref(""); applyTheme(window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"); }
        else { themePref(t[0]); applyTheme(t[0]); }
        go("account");
      } }, icon(t[2], 16), t[1]);
    }));
    var accent = currentAccent();
    var swatches = h("div", { class: "swatches", role: "radiogroup", "aria-label": "Accent colour" }, ACCENTS.map(function (a) {
      return h("button", { type: "button", role: "radio", "aria-checked": String(accent === a[0]), "aria-label": a[1], title: a[1],
        class: "swatch sw-" + a[0] + (accent === a[0] ? " on" : ""), onclick: function () { accentPref(a[0]); applyAccent(a[0]); go("account"); } });
    }));
    main.appendChild(panel("Appearance", "Saved in this browser only.", null,
      h("div", { class: "form" },
        h("div", { class: "field" }, h("span", { class: "l" }, "Theme"), themeSeg),
        h("div", { class: "field" }, h("span", { class: "l" }, "Accent colour"), swatches,
          h("div", { class: "help" }, (ACCENTS.filter(function (a) { return a[0] === accent; })[0] || ACCENTS[0])[1])))));

    // password
    var cur = h("input", { type: "password", id: "cur", autocomplete: "current-password", required: true });
    var nw = h("input", { type: "password", id: "nw", autocomplete: "new-password", required: true, placeholder: "12+ characters" });
    var rep = h("input", { type: "password", id: "rep", autocomplete: "new-password", required: true });
    var result = h("div", { "aria-live": "polite" });
    main.appendChild(panel("Change password", "Changing your password signs out your other sessions.", null, h("form", { class: "inline", onsubmit: async function (e) {
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
      h("div", { class: "field" }, h("label", { class: "l", for: "nw" }, "New password"), nw),
      h("div", { class: "field" }, h("label", { class: "l", for: "rep" }, "Repeat new password"), rep),
      h("div", { class: "form-submit" }, h("button", { class: "btn primary", type: "submit" }, "Change password"))), result));
  }

  boot();
})();
