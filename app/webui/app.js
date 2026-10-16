/* ============================================================
 * Grimoria webinterface — app.js
 * Vanilla JS, geen build-chain, geen externe afhankelijkheden.
 *
 * Werkt tegen de API op dezelfde origin:
 *   GET  /api/state          live status (1 Hz poll)
 *   GET/PUT /api/config      instellingen
 *   GET  /api/library        bibliotheeklijst
 *   POST /api/library        upload (multipart, veld "file")
 *   DELETE /api/library/<id> item verwijderen
 *   POST /api/goto/<id>      direct naar een fase
 *   POST /api/kind           kind-modus toggle {"kind": bool}
 * ============================================================ */

"use strict";

/* ---------- kleine helpers ---------- */
const $ = (id) => document.getElementById(id);

/** getal → tekst met komma als decimaalteken (nl). */
function fmt(n, dec = 2) {
  return Number(n).toFixed(dec).replace(".", ",");
}

function num(v, fallback = 0) {
  const n = Number(v);
  return Number.isFinite(n) ? n : fallback;
}

/** HTML-escapes voor dynamisch ingevoegde teksten. */
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

/* ---------- toast-meldingen ---------- */
function toast(msg, soort = "ok") {
  const el = document.createElement("div");
  el.className = `toast toast-${soort}`;
  el.textContent = msg;
  $("toasts").appendChild(el);
  setTimeout(() => el.remove(), 3500);
}

/* ============================================================
 * API-laag
 * ============================================================ */

async function api(path, opt = {}) {
  let resp;
  try {
    resp = await fetch(path, opt);
  } catch (e) {
    throw new Error("netwerkfout: " + e.message);
  }
  if (resp.status === 204) return null;
  if (!resp.ok) {
    let detail = "";
    try {
      const j = await resp.json();
      detail = j && (j.error || j.detail) ? " — " + (j.error || j.detail) : "";
    } catch { /* geen json-body */ }
    throw new Error(`HTTP ${resp.status}${detail}`);
  }
  const ct = resp.headers.get("content-type") || "";
  return ct.includes("application/json") ? resp.json() : resp.text();
}

const getConfig = () => api("/api/config");
const getLibrary = () => api("/api/library");
const getState = () => api("/api/state");
const putConfig = (cfg) =>
  api("/api/config", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(cfg),
  });
const postKind = (kind) =>
  api("/api/kind", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ kind: !!kind }),
  });
const postGoto = (id) => api(`/api/goto/${encodeURIComponent(id)}`, { method: "POST" });
const deleteLib = (id) => api(`/api/library/${encodeURIComponent(id)}`, { method: "DELETE" });

/* ============================================================
 * App-state
 * ============================================================ */

const state = {
  config: null,      // laatst geladen/opgeslagen config (servervorm)
  library: [],       // bibliotheek-items
  online: false,     // laatste poll-resultaat
};

/* ---------- statusbalk + poll ---------- */

const poll = {
  timer: null,       // setTimeout-id
  running: false,    // er is een verzoek onderweg
  stopped: false,    // permanent gestopt (tab sluit / harde fout)
};

function schedulePoll() {
  if (poll.stopped || poll.timer) return;
  poll.timer = setTimeout(pollOnce, 1000);
}

async function pollOnce() {
  if (poll.stopped) return;
  poll.timer = null;
  if (poll.running) { schedulePoll(); return; } // vorige nog niet klaar
  poll.running = true;
  let st;
  try {
    st = await getState();
    state.online = true;
  } catch (e) {
    state.online = false;
    toonOffline(`(${e.message})`);
  } finally {
    poll.running = false;
  }
  if (state.online && st) {
    toonOnline();
    $("st-phase").textContent = st.name || st.state || "—";
    $("st-phase-id").textContent = st.state ? `(${st.state})` : "";
    $("st-phase-time").textContent = `${fmt(num(st.since_secs), 1)} s`;
    const p = st.person || {};
    const el = $("st-person");
    el.textContent = p.present
      ? `aanwezig (${fmt(num(p.conf), 2)})`
      : "geen";
    el.classList.toggle("persoon-aan", !!p.present);
    el.classList.toggle("persoon-uit", !p.present);
    $("st-detector").textContent = st.detector || "—";
    // kind-mode alleen overschrijven als de gebruiker er net niet op drukt
    if (!kindBusy) $("sb-kind").checked = !!st.kind_mode;
  }
  schedulePoll();
}

/** Poll netjes afbreken: bij tab-sluit en bij permanente fout. */
function stopPoll() {
  poll.stopped = true;
  if (poll.timer) { clearTimeout(poll.timer); poll.timer = null; }
}

window.addEventListener("pagehide", stopPoll);
window.addEventListener("beforeunload", stopPoll);
if (document.visibilityState === "hidden") {
  // voor de volledigheid: als de tab al verborgen is bij start niet pollen
}
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "hidden") stopPoll();
});

function toonOnline() {
  if (!state.online) return; // pas na succesvolle poll
  $("st-conn").textContent = "live";
  $("st-conn").className = "conn conn-ok";
  $("offline-banner").hidden = true;
}

function toonOffline(detail) {
  $("st-conn").textContent = "offline";
  $("st-conn").className = "conn conn-err";
  $("offline-banner").hidden = false;
  $("offline-detail").textContent = detail || "";
}

/* ============================================================
 * Dirty-tracking / opslaan
 * ============================================================ */

function zetDirty(aan) {
  $("savebar").hidden = !aan;
  $("btn-save").disabled = !aan;
  $("btn-revert").disabled = !aan;
}

/** deep-clone via JSON (config is puur JSON). */
const kloon = (o) => JSON.parse(JSON.stringify(o));

/** vergelijk twee JSON-waarden. */
function gelijk(a, b) {
  return JSON.stringify(a) === JSON.stringify(b);
}

/** UI → config-object (servervorm). */
function uiNaarConfig() {
  // guard: vóór de eerste config-load is state.config nog null; kaarten
  // bestaan dan al wel — klikken mogen dan niet crashen (race-fix)
  const c = kloon(state.config ?? {});
  c.screens = c.screens ?? { layout: { mode: "horizontal", swap: false },
                             swap: false, offset: { dx: 0, dy: 0 },
                             items: [{}, {}] };

  // schermen algemeen
  c.screens.layout = wandLeesLayout();
  c.screens.swap = c.screens.layout.swap; // afgeleid veld, synchroon houden
  c.screens.offset.dx = num($("scr-dx").value);
  c.screens.offset.dy = num($("scr-dy").value);

  // per scherm
  for (let i = 0; i < 2; i++) {
    const item = c.screens.items[i] || (c.screens.items[i] = {});
    item.rotation = num($(`rot-${i}`).dataset.val ?? $(`rot-${i}`).value);
    item.overlay = item.overlay || {};
    item.overlay.enabled = $(`ovl-aan-${i}`).checked;
    item.overlay.text = $(`ovl-tekst-${i}`).value;
    item.overlay.x = num($(`ovl-x-${i}`).value);
    item.overlay.y = num($(`ovl-y-${i}`).value);
    item.overlay.size = num($(`ovl-size-${i}`).value, 48);
    item.overlay.color = $(`ovl-kleur-${i}`).value;
  }

  // algemeen
  c.entry_state = $("ph-entry").value;
  c.kind_mode = $("ph-kind").checked;

  // beweging/timing
  c.motion = c.motion || {};
  c.motion.person_hold_secs = num($("tim-hold").value);
  c.motion.still_secs = num($("tim-still").value);
  c.motion.detect_fps = Math.round(num($("tim-fps").value, 3));
  c.motion.person_conf = num($("tim-conf").value, 0.5);
  c.timing = c.timing || {};
  c.timing.scare_cooldown = num($("tim-cooldown").value);

  // per fase
  for (const st of c.states || []) {
    st.canvas_mode = leesCanvasMode(st.id);
    st.media = st.media || {};
    // alleen de velden van de ACTIEVE canvas-modus meesturen; de andere
    // keys opruimen zodat de round-trip schoon blijft
    if (st.canvas_mode === "wide") {
      const sel = $(`media-${st.id}-wide`);
      st.media = sel ? { wide: sel.value || "" } : {};
    } else {
      st.media = {
        "0": ($(`media-${st.id}-0`)?.value) || "",
        "1": ($(`media-${st.id}-1`)?.value) || "",
      };
    }
    const adv = st.advance = st.advance || {};
    // motion-edge alleen als er een geldig doel is (kaarten zonder motion-
    // velden mogen geen lege edge achterlaten)
    const toEl = $(`adv-to-${st.id}`);
    const to = toEl ? (toEl.value || "") : "";
    if (to) {
      const m = { to };
      const rqEl = $(`adv-rq-${st.id}`);
      if (rqEl && rqEl.value !== "") m.require_motion_secs = num(rqEl.value);
      const cdEl = $(`adv-cd-${st.id}`);
      if (cdEl && cdEl.value !== "") m.cooldown = num(cdEl.value);
      adv.motion = m;
    } else {
      delete adv.motion;
    }
    const tsecs = $(`adv-tsecs-${st.id}`);
    const tto = $(`adv-tto-${st.id}`);
    if (tsecs && tto) {
      adv.timeout = tsecs.value === "" || tto.value === ""
        ? null
        : { secs: num(tsecs.value), to: tto.value };
    } else if (adv.timeout === undefined) {
      adv.timeout = null;
    }
  }
  return c;
}

function checkOpslaan() {
  zetDirty(!gelijk(uiNaarConfig(), state.config));
  planAutoOpslaan();
}

let _autoOpslaanTimer = null;
let _autoOpslaanBezig = false;
let _referentieUi = null; // uiNaarConfig()-momentopname bij load/save (auto-save-referentie)

/** Auto-opslaan: directe instellingen (rotatie, wand-indeling, overlay)
   zijn zichtbaar-op-scherm-instellingen — die moeten zonder tussenkomst
   van de Opslaan-knop doorgaan (gedebounce 600 ms). Formuliervelden die
   grotere gevolgen hebben (fases, timing) blijven via de savebar lopen. */
function planAutoOpslaan() {
  if (_autoOpslaanTimer) clearTimeout(_autoOpslaanTimer);
  _autoOpslaanTimer = setTimeout(async () => {
    _autoOpslaanTimer = null;
    if (_autoOpslaanBezig || !state.config) return;
    const nu = uiNaarConfig();
    if (gelijk(nu, state.config)) return;
    if (!state.config || !_referentieUi) return;
    // auto-save alléén als het verschil met de laatste bekende UI-toestand
    // beperkt blijft tot het screens-blok (rotatie/wand/overlay/offsets);
    // zit de wijziging ook elders (fases, timing) -> savebar-regime
    const zs = (o) => { const c = kloon(o); delete c.screens; return c; };
    if (!gelijk(zs(nu), zs(_referentieUi))) return;
    _autoOpslaanBezig = true;
    try {
      const cfg = uiNaarConfig();
      await putConfig(cfg);
      state.config = cfg;
      _referentieUi = kloon(cfg);
      zetDirty(false);
      toast("Automatisch opgeslagen");
    } catch (e) {
      toast("Auto-opslaan mislukt: " + e.message, "err");
    } finally {
      _autoOpslaanBezig = false;
    }
  }, 600);
}

/** Alle formuliervelden terugzetten vanuit state.config. */
function configNaarUi() {
  const c = state.config;

  wandZetLayout(wandNormaliseerLayout(c.screens?.layout, c.screens?.swap));
  $("scr-dx").value = num(c.screens?.offset?.dx);
  $("scr-dy").value = num(c.screens?.offset?.dy);
  $("val-scr-dx").textContent = fmt(num(c.screens?.offset?.dx), 3);
  $("val-scr-dy").textContent = fmt(num(c.screens?.offset?.dy), 3);

  for (let i = 0; i < 2; i++) {
    const item = c.screens?.items?.[i] || {};
    const rot = num(item.rotation);
    zetRotatieUi(i, rot, true);
    const ov = item.overlay || {};
    $(`ovl-aan-${i}`).checked = !!ov.enabled;
    $(`ovl-tekst-${i}`).value = ov.text || "";
    $(`ovl-x-${i}`).value = num(ov.x, 0.5).toFixed(2);
    $(`ovl-y-${i}`).value = num(ov.y, 0.85).toFixed(2);
    $(`ovl-size-${i}`).value = num(ov.size, 48);
    $(`ovl-kleur-${i}`).value = ov.color || "#ffaa00";
    updateOverlayEl(i);
    updateDndTekst(i);
  }

  // fase-dropdowns opnieuw vullen (opties kunnen gewijzigd zijn)
  vulStateDropdowns();

  $("ph-entry").value = c.entry_state || "";
  $("ph-kind").checked = !!c.kind_mode;
  $("sb-kind").checked = !!c.kind_mode;

  $("tim-hold").value = num(c.motion?.person_hold_secs, 3);
  $("tim-still").value = num(c.motion?.still_secs, 5);
  $("tim-fps").value = num(c.motion?.detect_fps, 3);
  $("tim-conf").value = num(c.motion?.person_conf, 0.5);
  $("tim-cooldown").value = num(c.timing?.scare_cooldown, 90);

  for (const st of c.states || []) {
    zetCanvasMode(st.id, st.canvas_mode === "wide" ? "wide" : "split");
    const med = st.media || {};
    for (const k of ["0", "1", "wide"]) {
      const sel = $(`media-${st.id}-${k}`);
      if (sel) sel.value = med[k] || "";
    }
    const m = st.advance?.motion || {};
    const toEl = $(`adv-to-${st.id}`);
    if (toEl) toEl.value = m.to || "";
    const rqEl = $(`adv-rq-${st.id}`);
    if (rqEl) rqEl.value = m.require_motion_secs ?? "";
    const cdEl = $(`adv-cd-${st.id}`);
    if (cdEl) cdEl.value = m.cooldown ?? "";
    const t = st.advance?.timeout;
    $(`adv-tsecs-${st.id}`).value = t ? (t.secs ?? "") : "";
    $(`adv-tto-${st.id}`).value = t ? (t.to || "") : "";
  }
  zetDirty(false);
}

/* ============================================================
 * TAB 1 — Schermen
 * ============================================================ */

const PREVIEWS = {
  "0": { file: "../media/loops/frames/rust/f0021.png", tekst: "placeholder rust (scherm 0 — gezicht)" },
  "1": { file: "../media/loops/frames/jump/f0031.png", tekst: "placeholder jump (scherm 1 — orb)" },
};

/** Bouw de twee scherm-kaarten (één keer bij start). */
function bouwSchermKaarten() {
  const grid = $("screens-grid");
  grid.innerHTML = "";
  for (let i = 0; i < 2; i++) {
    const p = PREVIEWS[String(i)] || {};
    const kaart = document.createElement("fieldset");
    kaart.className = "card";
    kaart.innerHTML = `
      <legend>Scherm ${i}</legend>
      <div class="row">
        <span class="field inline">Rotatie
          <span class="rot-groep" id="rot-${i}" data-val="0">
            <button type="button" data-rot="0">0°</button>
            <button type="button" data-rot="90">90°</button>
            <button type="button" data-rot="180">180°</button>
            <button type="button" data-rot="270">270°</button>
          </span>
        </span>
        <label class="switch" style="margin-left:auto">
          <input type="checkbox" id="ovl-aan-${i}"><span class="track"></span>Overlay aan
        </label>
      </div>
      <div class="preview-wrap">
        <div class="preview" id="prev-${i}">
          <img class="placeholder" id="prev-img-${i}" alt="" src="${p.file || ""}">
          <span class="overlay-label" id="ovl-el-${i}" hidden></span>
        </div>
        <div class="dnd-status" id="dnd-${i}">overlay: sleep de tekst om hem te verplaatsen</div>
      </div>
      <div class="row">
        <label class="field inline grow" style="flex:2 1 200px">Tekst
          <input type="text" id="ovl-tekst-${i}" maxlength="80" placeholder=" overlay-tekst…">
        </label>
        <label class="field inline">Kleur
          <input type="color" id="ovl-kleur-${i}" value="#ffaa00">
        </label>
        <label class="field inline">Grootte
          <input type="number" id="ovl-size-${i}" min="8" max="400" step="1" value="48"> px
        </label>
        <label class="field inline">x
          <input type="number" id="ovl-x-${i}" min="0" max="1" step="0.01" value="0.50">
        </label>
        <label class="field inline">y
          <input type="number" id="ovl-y-${i}" min="0" max="1" step="0.01" value="0.85">
        </label>
      </div>`;
    grid.appendChild(kaart);

    // rotatie-knoppen
    $(`rot-${i}`).addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-rot]");
      if (!btn) return;
      zetRotatieUi(i, num(btn.dataset.rot));
      checkOpslaan();
    });

    // overlay-formulier → preview
    for (const id of [`ovl-aan-${i}`, `ovl-tekst-${i}`, `ovl-kleur-${i}`, `ovl-size-${i}`, `ovl-x-${i}`, `ovl-y-${i}`]) {
      $(id).addEventListener("input", () => { updateOverlayEl(i); updateDndTekst(i); checkOpslaan(); });
    }

    koppelOverlayDrag(i);
    updateOverlayEl(i);
  }

  bouwWandEditor();
}

/** Rotatie-knoppen + placeholder-transform bijwerken (en de wand mee laten draaien). */
function zetRotatieUi(i, graden, ookWand = true) {
  const groep = $(`rot-${i}`);
  groep.dataset.val = String(graden);
  for (const btn of groep.querySelectorAll("button")) {
    btn.classList.toggle("active", num(btn.dataset.rot) === graden);
  }
  $(`prev-img-${i}`).style.transform = `rotate(${graden}deg)`;
  if (ookWand) wandZetRotatie(i, graden);
}

/* ============================================================
 * Wand-editor — visuele plaatsing + rotatie van beide schermen
 * ============================================================ */

const wand = {
  zone: "rechts",        // huidige snap-positie van scherm 1
  drag: null,            // actieve pointer-sleep ({pointerId, i, dx, dy})
  hendelSleepte: false,  // hendel werd gesleept (onderdrukt de klik-draai)
  laatsteZone: null,     // zone waar de ghost tijdens de laatste sleep stond
};

/** zone → {mode, swap} volgens het vaste contract uit het ontwerpdoc. */
function zoneNaarLayout(zone) {
  switch (zone) {
    case "links":  return { mode: "horizontal", swap: true };
    case "boven":  return { mode: "vertical",   swap: false };
    case "onder": return { mode: "vertical",   swap: true };
    case "rechts":
    default:       return { mode: "horizontal", swap: false };
  }
}

/** {mode, swap} → zone (omgekeerde van zoneNaarLayout). */
function layoutNaarZone(mode, swap) {
  if (mode === "vertical") return swap ? "onder" : "boven";
  return swap ? "links" : "rechts";
}

/**
 * Normaliseer screens.layout met backwards compat:
 * geldig layout-object → die; ontbreekt → afleiden uit screens.swap
 * (horizontal: swap→links, anders rechts).
 */
function wandNormaliseerLayout(layout, oudeSwap) {
  if (layout && typeof layout === "object" &&
      (layout.mode === "horizontal" || layout.mode === "vertical")) {
    return { mode: layout.mode, swap: !!layout.swap };
  }
  return { mode: "horizontal", swap: !!oudeSwap };
}

/**
 * Layout → fractieposities (0..1) van de twee blok-centra in de canvas.
 * Blokken zijn 34% van de canvasbreedte (16:9), dus centra op 1/3 en 2/3.
 */
function layoutNaarPosities(mode, swap) {
  if (mode === "vertical") {
    // boven (swap:false): scherm 1 boven scherm 0 · onder (swap:true): andersom
    return swap
      ? { s0: { x: 0.5, y: 0.33 }, s1: { x: 0.5, y: 0.67 } }
      : { s0: { x: 0.5, y: 0.67 }, s1: { x: 0.5, y: 0.33 } };
  }
  return swap
    ? { s0: { x: 0.67, y: 0.5 }, s1: { x: 0.33, y: 0.5 } }
    : { s0: { x: 0.33, y: 0.5 }, s1: { x: 0.67, y: 0.5 } };
}

/** Nadenkstap: bij welke zone zou scherm 1 landen als het nu op p punt? */
function wandZoneOpPunt(p) {
  const hoek = Math.abs(p.dy) / Math.max(Math.abs(p.dx), 1e-6);
  if (Math.abs(p.dx) < 0.08 && Math.abs(p.dy) < 0.08) return null; // op eigen plek
  if (hoek > 1) return p.dy < 0 ? "boven" : "onder";
  return p.dx < 0 ? "links" : "rechts";
}

/**
 * Zone voor een sleep op blok i. De zone beschrijft scherm 1 t.o.v. scherm 0,
 * dus voor blok 0 spiegelen: blok 0 naar links slepen = scherm 1 komt rechts.
 */
function wandZoneVoorBlok(i, p) {
  const z = wandZoneOpPunt(p);
  if (!z || i !== 0) return z;
  const spiegel = { links: "rechts", rechts: "links", boven: "onder", onder: "boven" };
  return spiegel[z];
}

/** UI van de wand lezen (voor uiNaarConfig). */
function wandLeesLayout() {
  return zoneNaarLayout(wand.zone);
}

/** Wand volledig herbouwen — rotaties en layout door één functie laten lopen. */
function wandVervers() {
  const canvas = $("wand-canvas");
  const r0 = num($("rot-0").dataset.val),
        r1 = num($("rot-1").dataset.val);
  const pos = layoutNaarPosities(zoneNaarLayout(wand.zone).mode, zoneNaarLayout(wand.zone).swap);

  canvas.innerHTML = "";
  for (let i = 0; i < 2; i++) {
    const p = PREVIEWS[String(i)] || {};
    const blok = document.createElement("div");
    blok.className = "wand-blok";
    blok.dataset.scherm = String(i);
    blok.tabIndex = 0;
    blok.setAttribute("role", "application");
    blok.setAttribute("aria-roledescription", "scherm-blok");
    blok.setAttribute("aria-label", `Scherm ${i + 1}: pijltjes plaatsen, r draait`);
    blok.style.left = `${pos["s" + i].x * 100}%`;
    blok.style.top = `${pos["s" + i].y * 100}%`;
    blok.innerHTML = `
      <img class="placeholder" src="${p.file || ""}" alt="">
      <span class="wand-label">Scherm ${i + 1}</span>
      <span class="wand-rot" aria-hidden="true">${num([r0, r1][i])}°</span>
      <button type="button" class="wand-hendel" data-i="${i}" aria-label="Draai scherm ${i + 1} (houd ingedrukt en sleep)" title="Draaien (slepen)">⟳</button>
      <span class="wand-snap-knoppen" role="group" aria-label="Rotatie-presets scherm ${i + 1}">
        <button type="button" data-wrot="0">0°</button>
        <button type="button" data-wrot="90">90°</button>
        <button type="button" data-wrot="180">180°</button>
        <button type="button" data-wrot="270">270°</button>
      </span>`;
    canvas.appendChild(blok);

    // mini-preview in het blok getrouw roteren
    const img = blok.querySelector(".placeholder");
    img.style.transform = `rotate(${[r0, r1][i]}deg)`;

    // rotatie-presets (actieve stand markeren)
    blok.querySelectorAll("button[data-wrot]").forEach((btn) => {
      btn.classList.toggle("active", num(btn.dataset.wrot) === [r0, r1][i]);
      btn.addEventListener("click", () => {
        zetRotatieUi(i, num(btn.dataset.wrot));
        checkOpslaan();
      });
    });

    koppelWandDrag(blok, i);
  }

  wandZetStatus();
  wandTekenMini();
}

/** Volledige wand opnieuw opbouwen met een nieuwe zone. */
function wandZetZone(zone) {
  if (zone === wand.zone) { wandVervers(); return; }
  wand.zone = zone;
  wandVervers();
  checkOpslaan();
}

/** Layout {mode, swap} → wand (via configNaarUi). */
function wandZetLayout(layout) {
  wand.zone = layoutNaarZone(layout.mode, layout.swap);
  wandVervers();
}

/** Rotatie van één scherm zetten (vanuit de kaart-rot-groep óf de wand). */
function wandZetRotatie(i, graden) {
  const blok = document.querySelector(`.wand-blok[data-scherm="${i}"]`);
  if (!blok) return;
  blok.querySelector(".placeholder").style.transform = `rotate(${graden}deg)`;
  blok.querySelector(".wand-rot").textContent = `${graden}°`;
  for (const btn of blok.querySelectorAll("button[data-wrot]")) {
    btn.classList.toggle("active", num(btn.dataset.wrot) === graden);
  }
  wandTekenMini();
}

/** Statusregel onder de canvas (WYSIWYG-tekst). */
function wandZetStatus() {
  const l = zoneNaarLayout(wand.zone);
  const naam = { rechts: "Scherm 2 rechts van scherm 1", links: "Scherm 2 links van scherm 1",
                 boven: "Scherm 2 boven scherm 1", onder: "Scherm 2 onder scherm 1" }[wand.zone];
  $("wand-status").textContent =
    `${naam} — layout: { mode: "${l.mode}", swap: ${l.swap ? "true" : "false"} }`;
}

/** WYSIWYG: mini-weergave van het totaalbeeld (grote canvas → wand). */
function wandTekenMini() {
  const mini = $("wand-mini");
  if (!mini) return;
  const r0 = num($("rot-0").dataset.val), r1 = num($("rot-1").dataset.val);
  const l = zoneNaarLayout(wand.zone);
  const pos = layoutNaarPosities(l.mode, l.swap);
  mini.innerHTML = "";
  for (let i = 0; i < 2; i++) {
    const p = PREVIEWS[String(i)] || {};
    const d = document.createElement("div");
    d.className = "wand-mini-blok";
    d.title = `Scherm ${i + 1}: ${[r0, r1][i]}°`;
    d.style.left = `${pos["s" + i].x * 100}%`;
    d.style.top = `${pos["s" + i].y * 100}%`;
    d.style.backgroundImage = `url("${p.file || ""}")`;
    const img = document.createElement("img");
    img.src = p.file || "";
    img.alt = "";
    img.style.transform = `rotate(${[r0, r1][i]}deg)`;
    d.appendChild(img);
    mini.appendChild(d);
  }
}

/** Drag & drop van een scherm-blok (pointer events, snap op 4 zones). */
function koppelWandDrag(blok, i) {
  const canvas = $("wand-canvas");

  const start = (e) => {
    if (e.target.closest("button")) return; // knoppen doen hun eigen ding
    e.preventDefault();
    try { blok.setPointerCapture(e.pointerId); } catch { /* capture kan falen (synthetisch/oude browser) */ }
    blok.classList.add("drag");
    const r = blok.getBoundingClientRect();
    wand.drag = {
      pointerId: e.pointerId, i,
      dx: e.clientX - (r.left + r.width / 2),
      dy: e.clientY - (r.top + r.height / 2),
    };
    wandTekenGhost(wandZoneVoorBlok(i, deltaTovCentrum(e)));
  };

  /** positie van de pointer (met grijp-offset) als fractie t.o.v. canvas-centrum */
  function deltaTovCentrum(e) {
    const rect = canvas.getBoundingClientRect();
    return {
      dx: (e.clientX - wand.drag.dx - (rect.left + rect.width / 2)) / rect.width,
      dy: (e.clientY - wand.drag.dy - (rect.top + rect.height / 2)) / rect.height,
    };
  }

  const verplaats = (e) => {
    if (!wand.drag || e.pointerId !== wand.drag.pointerId) return;
    const rect = canvas.getBoundingClientRect();
    const cx = rect.left + rect.width / 2,
          cy = rect.top + rect.height / 2;
    const p = {
      dx: (e.clientX - wand.drag.dx - cx) / rect.width,
      dy: (e.clientY - wand.drag.dy - cy) / rect.height,
    };
    blok.style.left = `${(e.clientX - wand.drag.dx - rect.left) / rect.width * 100}%`;
    blok.style.top = `${(e.clientY - wand.drag.dy - rect.top) / rect.height * 100}%`;
    wandTekenGhost(wandZoneVoorBlok(wand.drag.i, p));
  };

  const klaar = (e) => {
    if (!wand.drag || e.pointerId !== wand.drag.pointerId) return;
    wand.drag = null;
    blok.classList.remove("drag");
    try { blok.releasePointerCapture(e.pointerId); } catch { /* al los */ }
    const zone = wand.laatsteZone;
    wand.laatsteZone = null;
    wandVerwijderGhost();
    if (zone && zone !== wand.zone) {
      wandZetZone(zone);      // herbouwt de wand + checkOpslaan
    } else {
      wandVervers();          // blok terug op zijn snap-positie
    }
  };

  blok.addEventListener("pointerdown", start);
  blok.addEventListener("pointermove", verplaats);
  blok.addEventListener("pointerup", klaar);
  blok.addEventListener("pointercancel", klaar);
}

/** Snap-ghost tonen op de beoogde positie (visual feedback tijdens het slepen). */
function wandTekenGhost(zone) {
  wand.laatsteZone = zone;
  wandVerwijderGhost();
  if (!zone) return;
  const l = zoneNaarLayout(zone);
  const pos = layoutNaarPosities(l.mode, l.swap);
  // ghost = positie waar het gesleepte blok zou landen
  const i = wand.drag ? wand.drag.i : 1;
  const ghost = document.createElement("div");
  ghost.className = "wand-ghost";
  ghost.dataset.zone = zone;
  ghost.style.left = `${pos["s" + i].x * 100}%`;
  ghost.style.top = `${pos["s" + i].y * 100}%`;
  $("wand-canvas").appendChild(ghost);
  wandZetStatusOpZone(zone);
}

function wandVerwijderGhost() {
  document.querySelectorAll(".wand-ghost").forEach((g) => g.remove());
}

function wandZetStatusOpZone(zone) {
  if (!zone) { wandZetStatus(); return; }
  const l = zoneNaarLayout(zone);
  const naam = { rechts: "Scherm 2 komt rechts van scherm 1", links: "Scherm 2 komt links van scherm 1",
                 boven: "Scherm 2 komt boven scherm 1", onder: "Scherm 2 komt onder scherm 1" }[zone];
  $("wand-status").textContent =
    `${naam} — layout: { mode: "${l.mode}", swap: ${l.swap ? "true" : "false"} }`;
}

/** Bouw de wand-editor (één keer; blokken worden bij zone-wissel herbouwd). */
function bouwWandEditor() {
  if (!$("wand-canvas")) return; // guard voor oude markup/tests
  wandVervers();
  wandKoppelHendels();
  wandKoppelToetsen();
}

/** Rotatie-hendel: hoek slepen rond het blok-middelpunt (pointer events). */
function wandKoppelHendels() {
  // Hendelen zijn per blok; koppel aan de canvas (delegatie) omdat blokken
  // bij elke ververs opnieuw gebouwd worden.
  const canvas = $("wand-canvas");
  let sleep = null; // {pointerId, i, blok, cx, cy, startHoek, startRot}

  canvas.addEventListener("pointerdown", (e) => {
    const hendel = e.target.closest(".wand-hendel");
    if (!hendel) return;
    e.preventDefault();
    e.stopPropagation();
    const i = num(hendel.dataset.i);
    const blok = hendel.closest(".wand-blok");
    const r = blok.getBoundingClientRect();
    const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    sleep = { pointerId: e.pointerId, i, blok, hendel, cx, cy,
              startRot: num($(`rot-${i}`).dataset.val),
              startHoek: Math.atan2(e.clientY - cy, e.clientX - cx) * 180 / Math.PI };
    wand.hendelSleepte = false;
    try { hendel.setPointerCapture(e.pointerId); } catch { /* idem */ }
    blok.classList.add("draai");
  });

  canvas.addEventListener("pointermove", (e) => {
    if (!sleep || e.pointerId !== sleep.pointerId) return;
    const hoek = Math.atan2(e.clientY - sleep.cy, e.clientX - sleep.cx) * 180 / Math.PI;
    let delta = hoek - sleep.startHoek;
    if (delta > 180) delta -= 360;
    if (delta < -180) delta += 360;
    wand.hendelSleepte = true;
    let rot = sleep.startRot + delta;
    // vastklikken op veelvouden van 90° als we dicht in de buurt komen
    const snapNabij = Math.round(rot / 90) * 90;
    if (Math.abs(rot - snapNabij) < 8) rot = snapNabij;
    rot = ((rot % 360) + 360) % 360;
    zetRotatieUi(sleep.i, Math.round(rot));
  });

  const klaar = (e) => {
    if (!sleep || e.pointerId !== sleep.pointerId) return;
    const { blok, hendel } = sleep;
    sleep = null;
    blok.classList.remove("draai");
    try { hendel.releasePointerCapture(e.pointerId); } catch { /* al los */ }
    checkOpslaan();
  };
  canvas.addEventListener("pointerup", klaar);
  canvas.addEventListener("pointercancel", klaar);

  // hendel als echte knop: activeren (klik/Enter/spatie) = +90° draaien,
  // tenzij er net gehendeld (gesleept) is
  canvas.addEventListener("click", (e) => {
    const hendel = e.target.closest(".wand-hendel");
    if (!hendel || wand.hendelSleepte) return;
    const i = num(hendel.dataset.i);
    zetRotatieUi(i, (num($(`rot-${i}`).dataset.val) + 90) % 360);
    checkOpslaan();
  });
}

/** Toetsenbord: pijltjes = zone kiezen, r = 90° draaien van het gefocuste blok. */
function wandKoppelToetsen() {
  $("wand-canvas").addEventListener("keydown", (e) => {
    const blok = e.target.closest(".wand-blok");
    if (!blok) return;
    const i = num(blok.dataset.scherm);
    const zoneMap = { ArrowRight: "rechts", ArrowLeft: "links", ArrowUp: "boven", ArrowDown: "onder" };
    if (zoneMap[e.key]) {
      e.preventDefault();
      // pijl beweegt het gebediende blok in die richting; voor blok 0
      // spiegelen (zone beschrijft scherm 1 t.o.v. scherm 0)
      const zone = i === 0
        ? { links: "rechts", rechts: "links", boven: "onder", onder: "boven" }[zoneMap[e.key]]
        : zoneMap[e.key];
      wandZetZone(zone);
      // focus behouden op hetzelfde blok na de rebuild
      document.querySelector(`.wand-blok[data-scherm="${i}"]`)?.focus();
      return;
    }
    if (e.key === "r" || e.key === "R") {
      e.preventDefault();
      const rot = (num($(`rot-${i}`).dataset.val) + 90) % 360;
      zetRotatieUi(i, rot);
      checkOpslaan();
    }
  });
}

/** Preview-overlay-element afleiden uit de formulierwaarden. */
function updateOverlayEl(i) {
  const el = $(`ovl-el-${i}`);
  const aan = $(`ovl-aan-${i}`).checked;
  const txt = $(`ovl-tekst-${i}`).value.trim();
  el.hidden = !aan || txt === "";
  el.textContent = txt;
  el.style.color = $(`ovl-kleur-${i}`).value;
  el.style.fontSize = `${num($(`ovl-size-${i}`).value, 48)}px`;
  el.style.left = `${num($(`ovl-x-${i}`).value, 0.5) * 100}%`;
  el.style.top = `${num($(`ovl-y-${i}`).value, 0.85) * 100}%`;
}

/** x/y-statusregel onder de preview. */
function updateDndTekst(i) {
  $("dnd-" + i).textContent =
    `overlay: x=${fmt($(`ovl-x-${i}`).value)} · y=${fmt($(`ovl-y-${i}`).value)} (fracties 0–1) — sleep de tekst om te verplaatsen`;
}

/** Drag & drop van de overlay binnen de preview (pointer events). */
function koppelOverlayDrag(i) {
  const preview = $(`prev-${i}`);
  const el = $(`ovl-el-${i}`);
  let sleep = null; // {dx, dy} offset binnen het element

  el.addEventListener("pointerdown", (e) => {
    if (el.hidden) return; // onzichtbare overlay is niet versleepbaar
    e.preventDefault();
    el.setPointerCapture(e.pointerId);
    el.classList.add("drag");
    const rect = el.getBoundingClientRect();
    // grijp-posities binnen het element (in px)
    sleep = { dx: e.clientX - (rect.left + rect.width / 2), dy: e.clientY - (rect.top + rect.height / 2) };
    verplaats(e);
  });
  el.addEventListener("pointermove", (e) => { if (sleep) verplaats(e); });
  const klaar = (e) => {
    if (!sleep) return;
    sleep = null;
    el.classList.remove("drag");
    try { el.releasePointerCapture(e.pointerId); } catch { /* al los */ }
    checkOpslaan();
  };
  el.addEventListener("pointerup", klaar);
  el.addEventListener("pointercancel", klaar);

  function verplaats(e) {
    const rect = preview.getBoundingClientRect();
    let x = (e.clientX - sleep.dx - rect.left) / rect.width;
    let y = (e.clientY - sleep.dy - rect.top) / rect.height;
    x = Math.min(1, Math.max(0, x));
    y = Math.min(1, Math.max(0, y));
    $(`ovl-x-${i}`).value = x.toFixed(2);
    $(`ovl-y-${i}`).value = y.toFixed(2);
    updateOverlayEl(i);
    updateDndTekst(i);
  }
}

/* ============================================================
 * TAB 2 — Fases
 * ============================================================ */

/** Dropdown-opties voor media (gefilterd op canvas_mode). */
function mediaOpties(canvasMode, geselecteerd) {
  const wiltWide = canvasMode === "wide";
  let items = state.library.filter((it) => !!it.wide === wiltWide);
  // huidige selectie behouden, ook als die door het filter "hoort" te verdwijnen
  if (geselecteerd && !items.some((it) => it.id === geselecteerd)) {
    const cur = state.library.find((it) => it.id === geselecteerd);
    if (cur) items = items.concat([cur]);
  }
  items = items.slice().sort((a, b) => a.name.localeCompare(b.name, "nl"));
  let html = `<option value="">— geen —</option>`;
  for (const it of items) {
    const sel = it.id === geselecteerd ? " selected" : "";
    const res = it.width && it.height ? ` (${it.width}×${it.height})` : "";
    html += `<option value="${esc(it.id)}"${sel}>${esc(it.name)}${res}</option>`;
  }
  return html;
}

/** canvas_mode-radio's lezen/schrijven (radio's hebben een name, geen id). */
function leesCanvasMode(st_id) {
  for (const r of $(`cm-groep-${st_id}`).querySelectorAll("input")) {
    if (r.checked) return r.value;
  }
  return "split";
}

function zetCanvasMode(st_id, mode) {
  for (const r of $(`cm-groep-${st_id}`).querySelectorAll("input")) {
    r.checked = r.value === mode;
  }
}

/** Bouw de fase-kaarten (opnieuw bij Bibliotheek-wijziging of canvas_mode-wissel). */
function bouwFaseKaarten() {
  const grid = $("states-grid");
  grid.innerHTML = "";
  const states = state.config?.states || [];

  for (const st of states) {
    const kaart = document.createElement("fieldset");
    kaart.className = "card";
    const med = st.media || {};
    const m = st.advance?.motion || {};
    const t = st.advance?.timeout;

    // motion-kolom alleen tonen als deze fase een motion-overgang heeft
    const motionVelden = m.to !== undefined || st.id === "rust" || st.id === "langzaam" ? `
        <label class="field inline">Bij beweging →
          <select id="adv-to-${esc(st.id)}"></select>
        </label>
        <label class="field inline">Vereist beweging (s)
          <input type="number" id="adv-rq-${esc(st.id)}" min="0" step="0.5" value="${m.require_motion_secs ?? ""}">
        </label>
        <label class="field inline">Cooldown (s)
          <input type="number" id="adv-cd-${esc(st.id)}" min="0" step="1" value="${m.cooldown ?? ""}">
        </label>` : "";

    kaart.innerHTML = `
      <legend>Fase</legend>
      <div class="fase-header">
        <h3>${esc(st.name || st.id)}</h3>
        <span class="fase-id">id: ${esc(st.id)}</span>
        <label class="field inline">Canvas-modus
          <span class="row" id="cm-groep-${esc(st.id)}">
            <label class="radio"><input type="radio" name="cm-${esc(st.id)}" value="split"> split</label>
            <label class="radio"><input type="radio" name="cm-${esc(st.id)}" value="wide"> wide</label>
          </span>
        </label>
        <button type="button" class="btn btn-klein" data-goto="${esc(st.id)}" title="Zet de machine direct in deze fase">▶ naar deze fase</button>
      </div>
      <div class="fase-media-rij">
        <label class="field inline">Film scherm 0
          <select id="media-${esc(st.id)}-0">${mediaOpties("split", med["0"])}</select>
        </label>
        <label class="field inline">Film scherm 1
          <select id="media-${esc(st.id)}-1">${mediaOpties("split", med["1"])}</select>
        </label>
        <label class="field inline" id="media-wide-rij-${esc(st.id)}" hidden>Breed-canvas film
          <select id="media-${esc(st.id)}-wide">${mediaOpties("wide", med.wide)}</select>
        </label>
      </div>
      <div class="row timing-rij">
        ${motionVelden}
        <label class="field inline">Timeout (s)
          <input type="number" id="adv-tsecs-${esc(st.id)}" min="0" step="0.5" value="${t ? (t.secs ?? "") : ""}">
        </label>
        <label class="field inline">Timeout →
          <select id="adv-tto-${esc(st.id)}"></select>
        </label>
      </div>`;
    grid.appendChild(kaart);

    // canvas_mode-radio's zetten vanuit config
    const mode = st.canvas_mode === "wide" ? "wide" : "split";
    zetCanvasMode(st.id, mode);

    // wissel van canvas_mode → scherm-dropdowns en wide-rij bijwerken
    for (const radio of kaart.querySelectorAll(`input[name="cm-${CSS.escape(st.id)}"]`)) {
      radio.addEventListener("change", () => {
        const wide = radio.value === "wide" && radio.checked;
        $(`media-wide-rij-${st.id}`).hidden = !wide;
        $("media-" + st.id + "-0").closest(".field.inline").hidden = wide;
        $("media-" + st.id + "-1").closest(".field.inline").hidden = wide;
        if (wide) {
          $(`media-${st.id}-wide`).innerHTML = mediaOpties("wide", $(`media-${st.id}-wide`).value);
        } else {
          const cur0 = $(`media-${st.id}-0`).value, cur1 = $(`media-${st.id}-1`).value;
          $(`media-${st.id}-0`).innerHTML = mediaOpties("split", cur0);
          $(`media-${st.id}-1`).innerHTML = mediaOpties("split", cur1);
        }
        checkOpslaan();
      });
    }
    // beginstatus toepassen (zonder event)
    if (mode === "wide") {
      $(`media-wide-rij-${st.id}`).hidden = false;
      $("media-" + st.id + "-0").closest(".field.inline").hidden = true;
      $("media-" + st.id + "-1").closest(".field.inline").hidden = true;
    } else {
      $(`media-wide-rij-${st.id}`).hidden = true;
    }

    // goto-knop
    kaart.querySelector(`[data-goto="${CSS.escape(st.id)}"]`).addEventListener("click", async () => {
      try {
        await postGoto(st.id);
        toast(`Gaat naar fase "${st.name || st.id}"`);
      } catch (e) {
        toast("Naar fase gaan mislukt: " + e.message, "err");
      }
    });
  }

  vulStateDropdowns();
  koppelFaseEvents();
}

/** motion.to- en timeout.to-dropdowns vullen met staten. */
function vulStateDropdowns() {
  const states = state.config?.states || [];
  for (const st of states) {
    const to = $(`adv-to-${st.id}`);
    const tto = $(`adv-tto-${st.id}`);
    for (const sel of [to, tto]) {
      if (!sel) continue;
      const cur = sel.value;
      sel.innerHTML = `<option value="">— geen —</option>` +
        states.map((s) => `<option value="${esc(s.id)}"${s.id === cur ? " selected" : ""}>${esc(s.name || s.id)}</option>`).join("");
    }
  }
}

/** change-events op de dynamische fase-velden → dirty-check. */
function koppelFaseEvents() {
  const states = state.config?.states || [];
  for (const st of states) {
    for (const id of [
      `media-${st.id}-0`, `media-${st.id}-1`, `media-${st.id}-wide`,
      `adv-to-${st.id}`, `adv-rq-${st.id}`, `adv-cd-${st.id}`,
      `adv-tsecs-${st.id}`, `adv-tto-${st.id}`,
    ]) {
      const el = $(id);
      if (el) el.addEventListener("change", checkOpslaan);
    }
  }
}

/* ============================================================
 * TAB 3 — Bibliotheek
 * ============================================================ */

function bouwBibliotheekTabel() {
  const tb = $("lib-tbody");
  if (!state.library.length) {
    tb.innerHTML = `<tr><td colspan="7" class="hint">Nog geen films in de bibliotheek.</td></tr>`;
    return;
  }
  tb.innerHTML = state.library.map((it) => {
    const duur = it.duration != null ? `${fmt(it.duration, 1)} s` : "—";
    const res = it.width && it.height ? `${it.width}×${it.height}` : "—";
    const datum = it.created ? new Date(it.created).toLocaleString("nl-NL") : "—";
    return `<tr>
      <td>${esc(it.name || it.id)}</td>
      <td><span class="badge ${it.wide ? "badge-wide" : ""}">${it.wide ? "wide" : "split"}</span></td>
      <td>${res}</td>
      <td>${it.frames ?? "—"}</td>
      <td>${duur}</td>
      <td class="hint">${datum}</td>
      <td><button type="button" class="btn btn-klein btn-danger" data-del="${esc(it.id)}">Verwijderen</button></td>
    </tr>`;
  }).join("");
}

async function laadBibliotheek() {
  const j = await getLibrary();
  state.library = j.items || [];
  bouwBibliotheekTabel();
  // media-dropdowns in de fase-kaarten opnieuw vullen met de nieuwe lijst
  const states = state.config?.states || [];
  for (const st of states) {
    for (const k of ["0", "1", "wide"]) {
      const sel = $(`media-${st.id}-${k}`);
      if (sel) sel.innerHTML = mediaOpties(k === "wide" ? "wide" : "split", sel.value);
    }
  }
  checkOpslaan();
}

function koppelUpload() {
  const dz = $("dropzone");
  const input = $("file-input");

  dz.addEventListener("click", () => input.click());
  dz.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); }
  });
  input.addEventListener("change", () => {
    if (input.files && input.files[0]) uploadBestand(input.files[0]);
    input.value = ""; // zelfde bestand opnieuw kunnen kiezen
  });

  for (const evt of ["dragenter", "dragover"]) {
    dz.addEventListener(evt, (e) => { e.preventDefault(); dz.classList.add("drag"); });
  }
  for (const evt of ["dragleave", "drop"]) {
    dz.addEventListener(evt, (e) => { e.preventDefault(); dz.classList.remove("drag"); });
  }
  dz.addEventListener("drop", (e) => {
    const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
    if (f) uploadBestand(f);
  });
}

async function uploadBestand(bestand) {
  const wide = $("up-wide").checked;
  const fd = new FormData();
  fd.append("file", bestand, bestand.name);
  fd.append("wide", wide ? "true" : "false");

  $("uprog").hidden = false;
  $("uprog-bar").style.width = "5%";
  $("uprog-txt").textContent = `Uploaden: ${bestand.name} (${Math.round(bestand.size / 1048576)} MB)…`;

  // XHR (ipv fetch) voor upload-voortgang
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/library");
  xhr.upload.addEventListener("progress", (e) => {
    if (e.lengthComputable) $("uprog-bar").style.width = `${Math.round((e.loaded / e.total) * 100)}%`;
  });
  xhr.addEventListener("load", async () => {
    $("uprog").hidden = true;
    if (xhr.status >= 200 && xhr.status < 300) {
      toast(`"${bestand.name}" geüpload`);
      try { await laadBibliotheek(); } catch { /* lijst-updates mag mislukken */ }
    } else {
      toast(`Upload mislukt: HTTP ${xhr.status}`, "err");
    }
  });
  xhr.addEventListener("error", () => {
    $("uprog").hidden = true;
    toast("Upload mislukt: netwerkfout", "err");
  });
  xhr.send(fd);
}

function koppelVerwijder() {
  $("lib-tbody").addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-del]");
    if (!btn) return;
    const id = btn.dataset.del;
    const item = state.library.find((x) => x.id === id);
    if (!confirm(`Film "${item?.name || id}" echt verwijderen?`)) return;
    try {
      await deleteLib(id);
      toast("Verwijderd");
      await laadBibliotheek();
    } catch (err) {
      toast("Verwijderen mislukt: " + err.message, "err");
    }
  });
}

/* ============================================================
 * Tabs
 * ============================================================ */

function koppelTabs() {
  for (const btn of document.querySelectorAll("#tabs button[data-tab]")) {
    btn.addEventListener("click", () => {
      for (const b of document.querySelectorAll("#tabs button[data-tab]")) {
        const actief = b === btn;
        b.classList.toggle("active", actief);
        b.setAttribute("aria-selected", String(actief));
      }
      for (const pane of document.querySelectorAll(".tabpanel")) {
        pane.hidden = pane.id !== "tab-" + btn.dataset.tab;
      }
    });
  }
}

/* ============================================================
 * Overige events + start
 * ============================================================ */

let kindBusy = false; // voorkomt dat de poll de toggle meteen terugzet

async function wisselKindMode(aan) {
  kindBusy = true;
  $("ph-kind").checked = aan;
  try {
    await postKind(aan);
    if (state.config) state.config.kind_mode = aan; // lokaal gelijk houden
    toast(`Kind-modus ${aan ? "aan" : "uit"}`);
    checkOpslaan();
  } catch (e) {
    toast("Kind-modus wijzigen mislukt: " + e.message, "err");
    $("sb-kind").checked = !aan; // terugdraaien
    $("ph-kind").checked = !aan;
  } finally {
    kindBusy = false;
  }
}

function koppelEvents() {
  // schermen algemeen (swap/positie zit in de wand-editor; offsets hier)
  for (const id of ["scr-dx", "scr-dy"]) {
    $(id).addEventListener("change", checkOpslaan);
    $(id).addEventListener("input", () => {
      $("val-scr-dx").textContent = fmt($("scr-dx").value, 3);
      $("val-scr-dy").textContent = fmt($("scr-dy").value, 3);
    });
  }

  // fases algemeen
  for (const id of ["ph-entry", "ph-kind", "tim-hold", "tim-still", "tim-cooldown", "tim-fps", "tim-conf"]) {
    $(id).addEventListener("change", checkOpslaan);
  }

  // kind-mode via de statusbalk (direct API-call) en in het formulier
  $("sb-kind").addEventListener("change", () => wisselKindMode($("sb-kind").checked));
  $("ph-kind").addEventListener("change", () => {
    if (!kindBusy) checkOpslaan();
  });

  // savebalk
  $("btn-save").addEventListener("click", async () => {
    try {
      const cfg = uiNaarConfig();
      await putConfig(cfg);
      state.config = cfg;
      _referentieUi = kloon(cfg);
      zetDirty(false);
      toast("Configuratie opgeslagen");
    } catch (e) {
      toast("Opslaan mislukt: " + e.message, "err");
    }
  });
  $("btn-revert").addEventListener("click", () => {
    configNaarUi();
    toast("Wijzigingen teruggedraaid");
  });

  // offline-banner
  $("btn-retry").addEventListener("click", () => {
    poll.stopped = false;
    pollOnce();
  });

  // opslaan met Ctrl/Cmd+S
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
      e.preventDefault();
      if (!$("savebar").hidden) $("btn-save").click();
    }
  });
}

async function start() {
  koppelTabs();
  koppelEvents();
  koppelUpload();
  koppelVerwijder();

  try {
    // EERST de config ophalen, DAARNA de kaarten bouwen: anders kan de
    // gebruiker klikken op een UI waarvan de serverwaarden nog niet
    // geladen zijn, en veegt configNaarUi() zijn wijzigingen weg (race)
    const [cfg, lib] = await Promise.all([getConfig(), getLibrary()]);
    state.config = cfg;
    state.library = lib.items || [];
    bouwSchermKaarten();
    bouwFaseKaarten();
    configNaarUi();
    _referentieUi = uiNaarConfig();   // auto-save-referentie na eerste load
    bouwBibliotheekTabel();
  } catch (e) {
    toonOffline(`config/library laden mislukt (${e.message})`);
  }
  toonOnline();

  pollOnce(); // start de 1 Hz-statusbalk-poll
}

start();
