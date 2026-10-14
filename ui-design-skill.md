---
name: ui-design
description: Design rules for the Channel Agnostic Agent admin page and any other page in this project. Use before adding or changing UI (admin sections, forms, tables, one-pagers, mockups) so new screens look and behave like the existing ones.
---

# UI design skill: Channel Agnostic Agent

The admin page (`app/admin/ui/`) is plain HTML, CSS and JavaScript served by the same FastAPI
process. No framework, no build step, no external requests. These rules describe what already
exists, so new screens match it. Where a rule is enforced by a test, the test is named.

## 1. Principles
1. **Operate first, decorate second.** An admin scans and acts. Put status and the primary action
   where the eye lands; keep copy short and specific.
2. **Show state in form, not just words.** Running, Off, Not running, Not set up are pills with
   colour; errors and successes are tinted message bars.
3. **Never show a secret.** Secret fields display "(set)" and offer Replace. Nothing in the UI,
   an error message or the audit log may contain a secret value.
4. **Say what will happen.** Buttons name the action ("Save changes", "Test connection"), and a
   note explains side effects ("Saving checks the settings first; if they fail, the working
   connection stays").
5. **Planned features are visible but inert.** Greyed-out sidebar items labelled "(upcoming)",
   with a tooltip giving today versus planned. Do not build fake screens.
6. **Everything works on a phone** (section 6) and from the keyboard (section 7).

## 2. Visual system
Define colours once as tokens on `:root` in `admin.css` and use only tokens in components.
This page is deliberately single-theme (light), matching the one-pagers; it sets
`color-scheme: light` and explicit colours so it looks right on any host background.

| Token | Value | Use |
|---|---|---|
| `--bg` | `#edf1f6` | page background |
| `--surface` | `#fff` | cards, panels, the app shell |
| `--ink` / `--muted` | `#1b2a41` / `#4a5a70` | text / secondary text |
| `--line` / `--edge` | `#dbe2ec` / `#b9c6dd` | hairlines / input borders |
| `--blue` / `--mint` | `#d6e0f7` / `#cff3e6` | sidebar and banners / soft highlights |
| `--tile` | `#f1f4f9` | resting cards, table cards |
| `--accent` | `#2563eb` | primary buttons, focus ring, links |
| `--ok` `--bad` `--off` (+ `-soft`) | green, red, grey | status pills and message bars |

Semantic colours (ok, bad, off) are separate from the accent; do not reuse the accent for state.

**Type.** Body is the system UI stack (Segoe UI first). Headings use a serif stack (Source Serif 4
if installed, then Georgia); titles are weight 400. Labels are bold 12.5 px; helper text is 12 px
muted. Small-caps style labels (table headers, card field names) use 11.5 px, bold, letter-spaced
uppercase. No web fonts are loaded, so the page makes no external requests.

**Shape.** 18 px radius on the shell, 16 px on cards and panels, 10 px on inputs and buttons,
fully round pills and switches. One border weight (1 px), no heavy shadows; a selected card gets
an accent border plus a soft ring.

## 3. Layout
- **Shell:** two columns, sidebar 210 px (180 px on tablets) and a flexible main area with
  `min-width: 0` so long content cannot push the page wider. Max width 1180 px, centred.
- **Main:** page title (serif, 24 px), one-sentence subtitle (max 70 characters wide), then
  content. Group with `gap`, not margins.
- **Cards grid:** `repeat(auto-fit, minmax(240px, 1fr))`. **Forms:** `repeat(auto-fit,
  minmax(270px, 1fr))`, with long fields (lists, URLs) spanning the full row.
- **Selected item + detail panel:** pick a card, edit in the panel below (Channels is the pattern
  for any future "list of things with settings").

## 4. Components (reuse, do not reinvent)
| Component | Notes |
|---|---|
| Card button `.card` | A real `<button>`; `.sel` marks selection; holds title, status pill, one meta line |
| Pill `.pill.ok/.off/.bad` | Status only |
| Panel `.panel` | Titled block with `.row`, `.form`, `.actions`, result area |
| Buttons `.btn`, `.primary`, `.danger`, `.small` | One primary action per panel |
| Switch `.switch` | Real checkbox styled as a toggle; label on the right |
| Secret field `.secret` | "(set)" box + Replace; becomes a password input; Cancel restores it |
| Message bar `.msg.ok/.bad/.wait` | `role="status"`, inside an `aria-live="polite"` region |
| Two-step confirm | First click changes the label ("Confirm delete") for 5 seconds; second click acts. Never use `alert()`/`confirm()` |
| Table `.tablewrap` | Horizontal scroll on wide screens; stacked cards under 600 px (needs `data-label` on cells) |

## 5. Copy
Plain and specific: "Could not start the channel: ..." not "Error". State what to do next. Use the
product name exactly as **Channel Agnostic Agent** (no hyphen). Errors from the server are shown
as returned (they are already scrubbed of secrets); do not invent extra detail.

## 6. Responsive rules
Breakpoints used: **980 px** (narrower sidebar), **760 px** (phone layout), **600 px** (tables
become cards), plus `(pointer: coarse)` for touch.
- Phone: header row (name left, account and Sign out right), then a horizontally scrolling tab
  strip, then a single column. Tabs do not wrap on phones; they may wrap in the wider sidebar.
- Inputs are 16 px on phones so iOS does not zoom; touch targets are at least 40 px high.
- Honour safe areas (`env(safe-area-inset-*)`) in the body padding.
- Rule: **no sideways page scroll at 375 px.** Check with
  `document.documentElement.scrollWidth <= clientWidth`. Only tables, code and diagrams may
  scroll, each inside its own `overflow-x: auto` wrapper.
- Hide empty table cells on phones (`td:empty`) instead of showing a label over nothing.

## 7. Accessibility
- Every control has a `<label for>`; helper text is visible, not placeholder-only.
- Use real elements (`button`, `input`, `select`, `nav`, `main`); keep a visible focus ring
  (`:focus-visible`, 2 px accent outline).
- `aria-current="page"` on the active tab; `aria-live="polite"` for results; disabled items use
  the `disabled` attribute, not just a grey style.
- Respect `prefers-reduced-motion` (transitions are switched off).
- Contrast: body text on white is at least 4.5:1; the lightest text (`--muted`) must stay at or
  above that on its background.

## 8. Security rules for UI code (enforced by tests)
These are not style preferences. `tests/test_admin_ui.py` fails if they are broken.
1. **No HTML strings.** Build content with the `h(tag, attrs, ...children)` helper and
   `textContent`. `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `document.write` and `eval` are
   forbidden, because audit-log text (for example a failed-login email) is attacker-influenced.
2. **No inline script or style.** The Content-Security-Policy is `default-src 'none'` plus
   `'self'` for script, style, connect and images. Use classes in `admin.css`; no `style=`
   attributes and no `element.style` in JS.
3. **Everything from this origin.** No CDN scripts, fonts or images. The page must work offline
   from the server alone.
4. **State changes carry the CSRF token** (`X-CSRF-Token`, kept in memory, never in storage).
   No secrets or tokens in `localStorage`, URLs or the DOM after saving.
5. **The page is local-only**; new routes must go through `check_local` and the admin session
   guard.

## 9. Adding a section
1. Add the tab to `TABS` and a `view...(main)` function in `admin.js`; call the JSON API with the
   `api()` helper (it handles CSRF and signs the user out on 401).
2. Build the view with `h()`; reuse the components above. Draw forms from server-provided field
   lists (as Channels does from each adapter's `SPEC`) so new options need no UI code.
3. Add backend routes under `/admin/api`, local-only, audited, secrets write-only (see
   `ADMIN.md`).
4. For tables, add `data-label` to every cell.
5. Run `node --check app/admin/ui/admin.js` and `python -m pytest tests`.

## 10. Verification checklist
- [ ] Looks right at 1280, 768 and 375 px; no sideways scroll at 375.
- [ ] Reachable and operable with the keyboard only; focus ring visible.
- [ ] Empty, loading, success and error states all designed.
- [ ] No secret visible anywhere, including error text and the audit log.
- [ ] `tests/test_admin_ui.py` passes (no `innerHTML`, no inline style, headers present).
- [ ] Checked in a real browser against a throwaway server and database, never your live data.
- [ ] Docs updated (`ADMIN.md`, `PROGRESS.md`) if behaviour changed.

## 11. Other pages (one-pagers, architecture, mockups)
The PDF one-pagers and the architecture page share this palette (soft blue and mint on navy ink,
serif headings, rounded tiles) so everything reads as one product. They are printed from HTML with
headless Edge (`--print-to-pdf`, A4, `@page margin`); keep each to a single page and check the page
count after changes. Do not invent metrics: tiles show only facts the project can back up.
