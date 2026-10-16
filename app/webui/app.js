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
  libraryBuiltin: [],// ingebouwde lussen (kiesbaar als pseudo-media)
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
    state.laatsteState = st;
    state.laatsteStateOp = performance.now();
    zetStatusWeergave(st, 0);
  }
  schedulePoll();
}

/** Statusbalk-weergave zetten; verstreken ms wordt lokaal bijgeteld. */
function zetStatusWeergave(st, extraMs) {
  $("st-phase").textContent = st.name || st.state || "—";
  $("st-phase-id").textContent = st.state ? `(${st.state})` : "";
  $("st-phase-time").textContent = `${fmt(num(st.since_secs) + extraMs / 1000, 1)} s`;
  const p = st.person || {};
  const el = $("st-person");
  el.textContent = p.present
    ? `aanwezig (${fmt(num(p.conf), 2)})`
    : "geen";
  el.classList.toggle("persoon-aan", !!p.present);
  el.classList.toggle("persoon-uit", !p.present);
  $("st-detector").textContent = st.detector || "—";
  if (!kindBusy) $("sb-kind").checked = !!st.kind_mode;
}

// vloeiende klok: tussen polls door loopt de tijd 4x/sec verder op
setInterval(() => {
  if (poll.stopped || !state.laatsteState || !state.laatsteStateOp) return;
  const extra = performance.now() - state.laatsteStateOp;
  if (extra > 10000) return;  // te lang geen poll: niets raden
  zetStatusWeergave(state.laatsteState, extra);
}, 250);

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
  if (document.visibilityState === "hidden") {
    stopPoll();                     // achtergrondtab: niet onnodig pollen
  } else {
    poll.stopped = false;           // terug in beeld: direct herstarten
    pollOnce();
  }
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

/**
 * Fases volledig uit de UI lezen (voor uiNaarConfig). De kaartvolgorde in
 * de UI is leidend — herordenen/nieuw/verwijderen muteren de DOM;
 * state.uiStates bewaart de niet-UI-velden (loop/bezel/effects) per id.
 */
function leesFasesVanUi() {
  const gridEl = $("states-grid");
  const kaarten = gridEl
    ? [...gridEl.querySelectorAll(":scope > .card[data-fase]")]
    : [];
  return kaarten.map((kaart) => {
    const id = kaart.dataset.fase;
    const basis = (state.uiStates || []).find((s) => s.id === id) || {};
    const st = kloon(basis);
    st.id = id;
    st.name = ($(`fase-naam-${id}`)?.value || "").trim() || id;
    if (st.loop === undefined) st.loop = true;
    st.canvas_mode = leesCanvasMode(id);
    // alleen de velden van de ACTIEVE canvas-modus meesturen; de andere
    // keys opruimen zodat de round-trip schoon blijft
    if (st.canvas_mode === "wide") {
      const sel = $(`media-${id}-wide`);
      st.media = sel ? { wide: sel.value || "" } : {};
    } else {
      st.media = {
        "0": ($(`media-${id}-0`)?.value) || "",
        "1": ($(`media-${id}-1`)?.value) || "",
      };
    }
    const adv = st.advance = {};
    // motion-edge: alleen als de schakelaar aan EN een geldig doel gekozen is
    // (kaarten zonder doel mogen geen lege edge achterlaten)
    const mAan = $(`adv-motion-aan-${id}`)?.checked;
    const mTo = $(`adv-to-${id}`)?.value || "";
    if (mAan && mTo) {
      const m = { to: mTo };
      const rq = $(`adv-rq-${id}`)?.value;
      if (rq !== undefined && rq !== "") m.require_motion_secs = num(rq);
      const kt = $(`adv-kindto-${id}`)?.value;
      if (kt) m.kind_to = kt;
      const cd = $(`adv-cd-${id}`)?.value;
      if (cd !== undefined && cd !== "") m.cooldown = num(cd);
      const iTo = $(`adv-idle-to-${id}`)?.value;
      if (iTo) {
        m.idle = { to: iTo };
        const iSecs = $(`adv-idle-secs-${id}`)?.value;
        if (iSecs !== undefined && iSecs !== "") m.idle.still_secs = num(iSecs);
      }
      adv.motion = m;
    }
    // timeout-edge: idem — aan + secs + doel, anders geen edge
    const tAan = $(`adv-timeout-aan-${id}`)?.checked;
    const tsecs = $(`adv-tsecs-${id}`)?.value;
    const tto = $(`adv-tto-${id}`)?.value;
    if (tAan && tsecs !== undefined && tsecs !== "" && tto) {
      adv.timeout = { secs: num(tsecs), to: tto };
    }
    return st;
  });
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
    const meedraai = $(`ovl-meedraai-${i}`)?.checked ?? true;
    item.overlay.rotate = meedraai
      ? (num($(`ovl-hoek-${i}`).value, 0) || "auto")
      : 0;
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

  // per fase — de kaartvolgorde in de UI is leidend (herordenen/nieuw/verwijder
  // muteren de DOM; state.uiStates bewaart de niet-UI-velden zoals bezel/effects)
  c.states = leesFasesVanUi();
  return c;
}

function checkOpslaan() {
  zetDirty(!gelijk(uiNaarConfig(), state.config));
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
    const r = ov.rotate;
    const meedraai = !(typeof r === "number" && r === 0);
    if ($(`ovl-meedraai-${i}`)) {
      $(`ovl-meedraai-${i}`).checked = meedraai;
      $(`ovl-hoek-veld-${i}`).hidden = meedraai && !((typeof r === "number") && r !== 0);
      $(`ovl-hoek-veld-${i}`).hidden = !(meedraai && typeof r === "number");
      $(`ovl-hoek-${i}`).value = (typeof r === "number") ? r : 0;
    }
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

  // fase-kaarten herbouwen (states kunnen gewijzigd zijn), daarna de
  // kaartvelden zetten; de niet-UI-velden (bezel/effects/loop) leven in
  // state.uiStates en blijven zo behouden bij het opslaan.
  // bouwFaseKaarten() zet zelf alle kaartwaarden + schema.
  syncUiStates();
  bouwFaseKaarten();
  zetDirty(false);
}

/* ============================================================
 * TAB 1 — Schermen
 * ============================================================ */

const PREVIEWS = {
  "0": { file: "../media/loops/frames/rust/f0021.png", tekst: "placeholder rust (Scherm 1 — gezicht)" },
  "1": { file: "../media/loops/frames/jump/f0031.png", tekst: "placeholder jump (Scherm 2 — orb)" },
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
      <legend>Scherm ${i + 1}</legend>
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
      </div>
      <div class="row">
        <label class="switch" title="Tekst draait mee met de schermrotatie (leesbaar in portret)">
          <input type="checkbox" id="ovl-meedraai-${i}" checked><span class="track"></span>Draait mee met scherm
        </label>
        <label class="field inline" id="ovl-hoek-veld-${i}" hidden>Eigen hoek
          <input type="number" id="ovl-hoek-${i}" min="0" max="359" step="1" value="0"> °
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
    for (const id of [`ovl-aan-${i}`, `ovl-tekst-${i}`, `ovl-kleur-${i}`, `ovl-size-${i}`, `ovl-x-${i}`, `ovl-y-${i}`,
                     `ovl-meedraai-${i}`, `ovl-hoek-${i}`]) {
      $(id).addEventListener("input", () => { updateOverlayEl(i); updateDndTekst(i); checkOpslaan(); });
    }
    // hoekveld tonen/verbergen bij de meedraai-toggle
    $(`ovl-meedraai-${i}`).addEventListener("change", () => {
      const aan = $(`ovl-meedraai-${i}`).checked;
      $(`ovl-hoek-veld-${i}`).hidden = !(aan && num($(`ovl-hoek-${i}`).value, 0) !== 0);
    });
    $(`ovl-hoek-${i}`).addEventListener("input", () => {
      $(`ovl-hoek-veld-${i}`).hidden = false;
    });

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
  const prev = $(`prev-${i}`);
  if (prev) prev.classList.toggle("portret", Math.abs(graden % 180) === 90);
  if ($(`ovl-el-${i}`)) updateOverlayEl(i);   // overlay draait mee
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

    // mini-preview in het blok getrouw roteren; 90/270 -> blok wordt portret
    const rot = [r0, r1][i];
    const img = blok.querySelector(".placeholder");
    img.style.transform = `rotate(${rot}deg)`;
    blok.classList.toggle("portret", Math.abs(rot % 180) === 90);

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
  blok.classList.toggle("portret", Math.abs(graden % 180) === 90);
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
  // meedraaien: totale hoek = schermrotatie (+ eigen hoek als die is gezet)
  const meedraai = $(`ovl-meedraai-${i}`)?.checked ?? true;
  const eigen = meedraai ? num($(`ovl-hoek-${i}`).value, 0) : 0;
  const schermrot = meedraai ? num($(`rot-${i}`).dataset.val, 0) : 0;
  el.style.transform = `translate(-50%, -50%) rotate(${(schermrot + eigen) % 360}deg)`;
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
  const builtins = (state.libraryBuiltin || []).filter((it) => !!it.wide === wiltWide);
  if (builtins.length) {
    html += `<optgroup label="Ingebouwd">`;
    for (const it of builtins) {
      const sel = it.id === geselecteerd ? " selected" : "";
      html += `<option value="${esc(it.id)}"${sel}>${esc(it.name)}</option>`;
    }
    html += `</optgroup>`;
  }
  if (items.length) {
    html += `<optgroup label="Bibliotheek">`;
    for (const it of items) {
      const sel = it.id === geselecteerd ? " selected" : "";
      const res = it.width && it.height ? ` (${it.width}×${it.height})` : "";
      html += `<option value="${esc(it.id)}"${sel}>${esc(it.name)}${res}</option>`;
    }
    html += `</optgroup>`;
  }
  return html;
}

/** canvas_mode-radio's lezen/schrijven (radio's hebben een name, geen id). */
function leesCanvasMode(st_id) {
  const groep = $(`cm-groep-${st_id}`);
  if (!groep) return "split";
  for (const r of groep.querySelectorAll("input")) {
    if (r.checked) return r.value;
  }
  return "split";
}

function zetCanvasMode(st_id, mode) {
  for (const r of $(`cm-groep-${st_id}`).querySelectorAll("input")) {
    r.checked = r.value === mode;
  }
}

/* ---------- layout-voorkeur (verticaal/horizontaal) ---------- */

const FASE_LAYOUT_KEY = "grimoria-fase-layout";

function leesFaseLayout() {
  return localStorage.getItem(FASE_LAYOUT_KEY) === "horizontaal" ? "horizontaal" : "verticaal";
}

function zetFaseLayout(modus) {
  const grid = $("states-grid");
  if (grid) grid.dataset.layout = modus;
  for (const [m, knop] of [["verticaal", $("fase-layout-verticaal")],
                           ["horizontaal", $("fase-layout-horizontaal")]]) {
    if (knop) {
      knop.classList.toggle("active", m === modus);
      knop.setAttribute("aria-pressed", String(m === modus));
    }
  }
}

/* ---------- niet-UI-velden per fase (loop/bezel/effects) ---------- */

/**
 * Houd state.uiStates bij als bron voor velden zonder UI-besturing:
 * loop, bezel, effects en eventueel de advance-vorm van states zonder
 * kaart. Wordt gesynchroniseerd bij config-load, save en discard.
 */
function syncUiStates(bron = state.config) {
  const uit = [];
  for (const st of bron?.states || []) {
    uit.push({
      id: st.id,
      ...(st.loop !== undefined ? { loop: st.loop } : {}),
      ...(st.bezel ? { bezel: kloon(st.bezel) } : {}),
      ...(st.effects ? { effects: kloon(st.effects) } : {}),
    });
  }
  state.uiStates = uit;
}

/* ---------- kaartvolgorde + ids ---------- */

/** fase-ids in UI-volgorde (uit de kaarten in #states-grid). */
function faseIdsVanUi() {
  const gridEl = $("states-grid");
  if (!gridEl) return [];
  return [...gridEl.querySelectorAll(":scope > .card[data-fase]")]
    .map((k) => k.dataset.fase);
}

/** Nog vrije fase-id: slug van de naam, anders fase-N (eerst vrije N). */
function nieuweFaseId(naam) {
  const bezet = new Set(faseIdsVanUi());
  let slug = String(naam || "").toLowerCase()
    .normalize("NFKD").replace(/[\u0300-\u036f]/g, "")
    .replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40);
  if (slug && !bezet.has(slug)) return slug;
  let n = 1;
  while (bezet.has(`fase-${n}`)) n++;
  return `fase-${n}`;
}

/**
 * Bouw de fase-kaarten volledig opnieuw (initiële load, save, revert).
 * Waarden komen uit state.config.states; niet-UI-velden staan in
 * state.uiStates. Select-waarden worden ná vulStateDropdowns gezet.
 */
function bouwFaseKaarten() {
  const grid = $("states-grid");
  if (!grid) return;
  const states = state.config?.states || [];
  grid.innerHTML = "";
  for (const st of states) {
    grid.appendChild(bouwFaseKaart({
      id: st.id,
      naam: st.name || st.id,
      canvas_mode: st.canvas_mode,
      media: st.media || {},
      motion: st.advance?.motion || null,
      timeout: st.advance?.timeout || null,
    }));
  }
  // pas nu de kaarten in het DOM hangen: waarden zetten + events koppelen
  vulStateDropdowns();
  for (const st of states) {
    zetCanvasMode(st.id, st.canvas_mode === "wide" ? "wide" : "split");
    koppelFaseKaart(grid.querySelector(`:scope > .card[data-fase="${CSS.escape(st.id)}"]`), st.id);
    zetKaartWaarden(st);
  }
  tekenKetenSchema();
}

/**
 * Eén fase-kaart-element bouwen: structuur + statische defaults + events.
 * Select-waarden (adv-*) worden pas geldig na vulStateDropdowns(); zet ze
 * via zetKaartWaarden() (gedaan door bouwFaseKaarten).
 */
function bouwFaseKaart(w, idx) {
  const st = Object.assign({ id: w.id }, w);   // compat: veldtoegang via st.
  const med = st.media || {};
  const m = st.motion || {};
  const t = st.timeout;

  const kaart = document.createElement("fieldset");
  kaart.className = "card fase-kaart";
  kaart.dataset.fase = st.id;
  kaart.innerHTML = `
    <div class="fase-header">
      <span class="fase-grip" title="Fase verslepen om te herordenen (of Alt+pijl op de kaart)">⠿</span>
      <div class="fase-titel">
        <input type="text" class="fase-naam" id="fase-naam-${esc(st.id)}" value="${esc(st.name ?? st.id)}" maxlength="60" aria-label="Fasenaam">
        <span class="fase-id" id="fase-id-${esc(st.id)}">id: ${esc(st.id)}</span>
      </div>
      <label class="field inline">Canvas-modus
        <span class="row" id="cm-groep-${esc(st.id)}">
          <label class="radio"><input type="radio" name="cm-${esc(st.id)}" value="split"> split</label>
          <label class="radio"><input type="radio" name="cm-${esc(st.id)}" value="wide"> wide</label>
        </span>
      </label>
      <button type="button" class="btn btn-klein" data-goto="${esc(st.id)}" title="Zet de machine direct in deze fase">▶</button>
      <button type="button" class="btn btn-klein btn-danger fase-del" data-del-fase="${esc(st.id)}" title="Deze fase verwijderen">🗑</button>
    </div>
    <div class="fase-media-rij">
      <label class="field inline">Film Scherm 1
        <select id="media-${esc(st.id)}-0">${mediaOpties("split", med["0"])}</select>
      </label>
      <label class="field inline">Film Scherm 2
        <select id="media-${esc(st.id)}-1">${mediaOpties("split", med["1"])}</select>
      </label>
      <label class="field inline" id="media-wide-rij-${esc(st.id)}" hidden>Breed-canvas film
        <select id="media-${esc(st.id)}-wide">${mediaOpties("wide", med.wide)}</select>
      </label>
    </div>
    <div class="edge-regels">
      <div class="edge-regel">
        <label class="switch" title="Overgang bij beweging aan/uit">
          <input type="checkbox" id="adv-motion-aan-${esc(st.id)}"><span class="track"></span>
        </label>
        <span class="edge-label">Bij beweging →</span>
        <select id="adv-to-${esc(st.id)}" class="edge-doel"></select>
        <button type="button" class="btn btn-klein edge-extra-knop" data-extra="${esc(st.id)}" aria-expanded="false" title="Extra bewegings-instellingen">⋯</button>
      </div>
      <div class="edge-extra" id="edge-extra-${esc(st.id)}" hidden>
        <label class="field inline">persoon aanwezig
          <input type="number" id="adv-rq-${esc(st.id)}" min="0" step="0.5" value="${m.require_motion_secs ?? ""}" placeholder="—"> s
        </label>
        <label class="field inline">in kind-modus →
          <select id="adv-kindto-${esc(st.id)}" class="edge-doel"></select>
        </label>
        <label class="field inline">cooldown
          <input type="number" id="adv-cd-${esc(st.id)}" min="0" step="1" value="${m.cooldown ?? ""}" placeholder="—"> s
        </label>
        <div class="edge-subregel" id="edge-idle-rij-${esc(st.id)}">
          <span class="edge-label">als beweging stopt →</span>
          <select id="adv-idle-to-${esc(st.id)}" class="edge-doel"></select>
          <label class="field inline">na
            <input type="number" id="adv-idle-secs-${esc(st.id)}" min="0" step="0.5" value="${m.idle?.still_secs ?? ""}" placeholder="—"> s
          </label>
        </div>
      </div>
      <div class="edge-regel">
        <label class="switch" title="Timeout-overgang aan/uit">
          <input type="checkbox" id="adv-timeout-aan-${esc(st.id)}"><span class="track"></span>
        </label>
        <span class="edge-label">Na</span>
        <input type="number" id="adv-tsecs-${esc(st.id)}" min="0" step="0.5" value="${t ? (t.secs ?? "") : ""}" class="edge-secs"> s
        <span class="edge-label">→</span>
        <select id="adv-tto-${esc(st.id)}" class="edge-doel"></select>
      </div>
    </div>`;

  return kaart;   // nog niet gekoppeld: gebeurt na appendChild (ids zijn
                  // pas via getElementById vindbaar als de kaart in het DOM zit)
}

/**
 * Alle velden van één kaart zetten vanuit een config-vormige state
 * ({id, name, canvas_mode, media, advance:{motion, timeout}}). Na het
 * bouwen (selects hebben dan opties via vulStateDropdowns).
 */
function zetKaartWaarden(st) {
  const id = st.id;
  const naamEl = $(`fase-naam-${id}`);
  if (naamEl) naamEl.value = st.name || st.id;
  const med = st.media || {};
  for (const k of ["0", "1", "wide"]) {
    const sel = $(`media-${id}-${k}`);
    if (sel) sel.value = med[k] || "";
  }
  const mode = st.canvas_mode === "wide" ? "wide" : "split";
  zetCanvasMode(id, mode);
  pasCanvasModeToe(id, mode === "wide");

  const m = st.advance?.motion || {};
  const t = st.advance?.timeout;
  if ($(`adv-motion-aan-${id}`)) $(`adv-motion-aan-${id}`).checked = !!m.to;
  const toEl = $(`adv-to-${id}`);
  if (toEl) toEl.value = m.to || "";
  const rqEl = $(`adv-rq-${id}`);
  if (rqEl) rqEl.value = m.require_motion_secs ?? "";
  const ktEl = $(`adv-kindto-${id}`);
  if (ktEl) ktEl.value = m.kind_to || "";
  const cdEl = $(`adv-cd-${id}`);
  if (cdEl) cdEl.value = m.cooldown ?? "";
  const iToEl = $(`adv-idle-to-${id}`);
  if (iToEl) iToEl.value = m.idle?.to || "";
  const iSecsEl = $(`adv-idle-secs-${id}`);
  if (iSecsEl) iSecsEl.value = m.idle?.still_secs ?? "";
  if ($(`adv-timeout-aan-${id}`)) $(`adv-timeout-aan-${id}`).checked = !!t;
  const tsEl = $(`adv-tsecs-${id}`);
  if (tsEl) tsEl.value = t ? (t.secs ?? "") : "";
  const ttEl = $(`adv-tto-${id}`);
  if (ttEl) ttEl.value = t ? (t.to || "") : "";
  zetEdgeZichtbaarheid(id);
}

/** Media-velden tonen naar aanleiding van de canvas-modus. */
function pasCanvasModeToe(id, wide) {
  const rij = $(`media-wide-rij-${id}`);
  if (!rij) return;
  rij.hidden = !wide;
  const f0 = $("media-" + id + "-0")?.closest(".field.inline");
  const f1 = $("media-" + id + "-1")?.closest(".field.inline");
  if (f0) f0.hidden = wide;
  if (f1) f1.hidden = wide;
}

/** Events koppelen op één kaart (structuur staat al in het DOM). */
function koppelFaseKaart(kaart, id) {
  // canvas_mode-wissel → scherm-dropdowns en wide-rij bijwerken
  for (const radio of kaart.querySelectorAll(`input[name="cm-${CSS.escape(id)}"]`)) {
    radio.addEventListener("change", () => {
      if (!radio.checked) return;
      const wide = radio.value === "wide";
      if (wide) {
        $(`media-${id}-wide`).innerHTML = mediaOpties("wide", $(`media-${id}-wide`).value);
      } else {
        const cur0 = $(`media-${id}-0`).value, cur1 = $(`media-${id}-1`).value;
        $(`media-${id}-0`).innerHTML = mediaOpties("split", cur0);
        $(`media-${id}-1`).innerHTML = mediaOpties("split", cur1);
      }
      pasCanvasModeToe(id, wide);
      checkOpslaan();
    });
  }
  pasCanvasModeToe(id, leesCanvasMode(id) === "wide");

  // naam wijzigen → dirty + verwijzingen (dropdowns/schema) live verversen
  $(`fase-naam-${id}`).addEventListener("input", () => {
    checkOpslaan();
    vulStateDropdowns();
    tekenKetenSchema();
  });

  // edge-aan/uit → velden in/uitschakelen + dirty
  for (const soort of ["motion", "timeout"]) {
    $(`adv-${soort}-aan-${id}`).addEventListener("change", () => {
      zetEdgeZichtbaarheid(id);
      checkOpslaan();
      tekenKetenSchema();
    });
  }
  zetEdgeZichtbaarheid(id);

  // uitklapbare extra's (⋯)
  const extraKnop = kaart.querySelector(`.edge-extra-knop[data-extra="${CSS.escape(id)}"]`);
  const zetExtraOpen = (open) => {
    const paneel = $(`edge-extra-${id}`);
    if (!paneel) return;
    paneel.hidden = !open;
    extraKnop.setAttribute("aria-expanded", String(open));
    extraKnop.classList.toggle("active", open);
  };
  extraKnop.addEventListener("click", () => zetExtraOpen($(`edge-extra-${id}`).hidden));
  // bij het aanzetten van motion: extra's meteen openen als er al waarden
  // in staan (of altijd bij een NIEUWE fase, zodat alles vindbaar is)
  $(`adv-motion-aan-${id}`).addEventListener("change", () => {
    // beweging aan = extra-instellingen meteen tonen (hold/kind/cooldown/stil);
    // gebruiker kan ze altijd inclappen via ⋯
    if ($(`adv-motion-aan-${id}`).checked) zetExtraOpen(true);
  });

  // edge-velden → dirty + schema live
  for (const veld of [`adv-to-${id}`, `adv-rq-${id}`, `adv-kindto-${id}`, `adv-cd-${id}`,
                      `adv-idle-to-${id}`, `adv-idle-secs-${id}`,
                      `adv-tsecs-${id}`, `adv-tto-${id}`]) {
    $(veld).addEventListener("change", () => { checkOpslaan(); tekenKetenSchema(); });
  }
  for (const veld of [`media-${id}-0`, `media-${id}-1`, `media-${id}-wide`]) {
    $(veld).addEventListener("change", checkOpslaan);
  }

  // ▶ testknop
  kaart.querySelector(`[data-goto="${CSS.escape(id)}"]`).addEventListener("click", async () => {
    const st = leesFasesVanUi().find((s) => s.id === id);
    try {
      await postGoto(id);
      toast(`Gaat naar fase "${st?.name || id}"`);
    } catch (e) {
      toast("Naar fase gaan mislukt: " + e.message, "err");
    }
  });

  // 🗑 verwijderen
  kaart.querySelector(`[data-del-fase="${CSS.escape(id)}"]`).addEventListener("click", () => {
    verwijderFase(id);
  });

  // herordenen: sleep de grip (pointer events) + alt+pijl op de kaart
  koppelFaseDrag(kaart, id);
  kaart.addEventListener("keydown", (e) => faseToets(id, e));
  kaart.tabIndex = 0;
  return kaart;
}

/** motion/timeout-velden enable/disable naar de aan-standen. */
function zetEdgeZichtbaarheid(id) {
  const mAan = $(`adv-motion-aan-${id}`)?.checked;
  const tAan = $(`adv-timeout-aan-${id}`)?.checked;
  for (const veld of ["adv-to", "adv-rq", "adv-kindto", "adv-cd",
                      "adv-idle-to", "adv-idle-secs"]) {
    const el = $(`${veld}-${id}`);
    if (el) el.disabled = !mAan;
  }
  const extra = $(`edge-extra-${id}`);
  if (extra && !mAan) {
    extra.hidden = true;
    const knop = document.querySelector(`.edge-extra-knop[data-extra="${CSS.escape(id)}"]`);
    if (knop) { knop.setAttribute("aria-expanded", "false"); knop.classList.remove("active"); }
  }
  const idleRij = $(`edge-idle-rij-${id}`);
  if (idleRij) idleRij.classList.toggle("uit", !mAan);
  for (const veld of ["adv-tsecs", "adv-tto"]) {
    const el = $(`${veld}-${id}`);
    if (el) el.disabled = !tAan;
  }
  const mr = $(`adv-motion-aan-${id}`)?.closest(".edge-regel");
  if (mr) mr.classList.toggle("edge-uit", !mAan);
  const tr = $(`adv-timeout-aan-${id}`)?.closest(".edge-regel");
  if (tr) tr.classList.toggle("edge-uit", !tAan);
}

/** Alt+pijl: gefocuste kaart verplaatsen (omhoog/omlaag; in horizontale
 *  modus ook links/rechts). */
function faseToets(id, e) {
  if (!e.altKey) return;
  const horizontaal = leesFaseLayout() === "horizontaal";
  const map = horizontaal
    ? { ArrowLeft: -1, ArrowRight: 1 }
    : { ArrowUp: -1, ArrowDown: 1 };
  if (!(e.key in map)) return;
  e.preventDefault();
  const ids = faseIdsVanUi();
  const van = ids.indexOf(id);
  const naar = van + map[e.key];
  if (van < 0 || naar < 0 || naar >= ids.length) return;
  verplaatsFase(id, naar);
}

/**
 * Fase-kaart verplaatsen naar index `naar` (0-gebaseerd) en alles
 * verversen wat van de volgorde afhangt. Richt de drop-focus op de kaart.
 */
function verplaatsFase(id, naar, focusKaart = true) {
  const grid = $("states-grid");
  const kaart = grid.querySelector(`:scope > .card[data-fase="${CSS.escape(id)}"]`);
  if (!kaart) return;
  const broers = [...grid.querySelectorAll(":scope > .card[data-fase]")];
  const van = broers.indexOf(kaart);
  if (van < 0) return;
  naar = Math.max(0, Math.min(broers.length - 1, naar));
  if (naar === van) return;
  broers.splice(van, 1);
  const ref = broers[naar] || null;                 // kaart die er nu op `naar` staat
  if (naar === broers.length) grid.appendChild(kaart);
  else grid.insertBefore(kaart, ref);
  checkOpslaan();
  tekenKetenSchema();
  if (focusKaart) kaart.focus();
}

/* ---------- drag & drop herordenen ---------- */

const faseDrag = {
  actief: null,   // {id, kaart, pointerId}
};

/** Sleep de grip van een kaart naar een andere positie (pointer events). */
function koppelFaseDrag(kaart, id) {
  const grip = kaart.querySelector(".fase-grip");
  if (!grip) return;

  const start = (e) => {
    if (faseDrag.actief) return;
    e.preventDefault();
    faseDrag.actief = { id, kaart, pointerId: e.pointerId };
    kaart.classList.add("sleep");
    try { grip.setPointerCapture(e.pointerId); } catch { /* synthetisch event */ }
    const rect = kaart.getBoundingClientRect();
    faseDrag.greep = { dx: e.clientX - rect.left, dy: e.clientY - rect.top };
    faseDrag.pointerBeweeg = false;
    faseDragLaat(e);                       // indicator meteen goed zetten
  };

  grip.addEventListener("pointerdown", start);
  grip.addEventListener("pointermove", (e) => {
    if (!faseDrag.actief || e.pointerId !== faseDrag.actief.pointerId) return;
    faseDrag.pointerBeweeg = true;
    faseDragLaat(e);
  });
  const einde = (e) => {
    if (!faseDrag.actief || e.pointerId !== faseDrag.actief.pointerId) return;
    const { id: sid, kaart: sk } = faseDrag.actief;
    faseDrag.actief = null;
    sk.classList.remove("sleep");
    try { grip.releasePointerCapture(e.pointerId); } catch { /* al los */ }
    const doel = faseDragDoel;
    faseDragVerwijderIndicator();
    if (doel && faseDrag.pointerBeweeg && doel.id !== sid) {
      verplaatsFase(sid, doel.index, false);
    } else {
      checkOpslaan();
    }
  };
  grip.addEventListener("pointerup", einde);
  grip.addEventListener("pointercancel", einde);
}

/** Huidige drop-positie bepalen uit de pointer-positie (midden vd kaart). */
function faseDragLaat(e) {
  const grid = $("states-grid");
  const horizontaal = leesFaseLayout() === "horizontaal";
  const kaarten = [...grid.querySelectorAll(":scope > .card[data-fase]")];
  const cx = e.clientX - (faseDrag.greep?.dx ?? 0);
  const cy = e.clientY - (faseDrag.greep?.dy ?? 0);
  const rects = kaarten.map((k) => k.getBoundingClientRect());
  let index = kaarten.length - 1;           // default: helemaal onderaan
  let id = kaarten[kaarten.length - 1]?.dataset.fase;
  for (let i = 0; i < rects.length; i++) {
    if (horizontaal ? cx < rects[i].left + rects[i].width / 2
                    : cy < rects[i].top + rects[i].height / 2) {
      index = i;
      id = kaarten[i].dataset.fase;
      break;
    }
  }
  faseDragDoel = { index, id };
  faseDragTekenIndicator(index);
}

/** Visuele drop-indicator (lijn tussen kaarten) op index-positie. */
function faseDragTekenIndicator(index) {
  faseDragVerwijderIndicator();
  const grid = $("states-grid");
  const kaarten = [...grid.querySelectorAll(":scope > .card[data-fase]")];
  if (!kaarten.length) return;
  const ref = kaarten[index];
  const lijn = document.createElement("div");
  lijn.className = "fase-drop-lijn";
  lijn.dataset.index = String(index);
  if (index >= kaarten.length) grid.appendChild(lijn);
  else grid.insertBefore(lijn, ref);
}

function faseDragVerwijderIndicator() {
  document.querySelectorAll(".fase-drop-lijn").forEach((l) => l.remove());
}

let faseDragDoel = null;   // {index, id} tijdens een sleep

/* ---------- nieuwe fase / verwijderen / keten ---------- */

/** + Nieuwe fase: kaart onderaan toevoegen en de naam focussen. */
function nieuweFase() {
  const grid = $("states-grid");
  if (!grid) return;
  syncUiStatesVanUi();
  const id = nieuweFaseId("Nieuwe fase");
  state.uiStates.push({ id, loop: true });
  grid.appendChild(bouwFaseKaart({
    id, naam: "Nieuwe fase", canvas_mode: "split",
    media: { "0": "", "1": "", wide: "" },
    motion: null, timeout: null,
  }));
  zetCanvasMode(id, "split");
  koppelFaseKaart(grid.querySelector(`:scope > .card[data-fase="${CSS.escape(id)}"]`), id);
  vulStateDropdowns();
  tekenKetenSchema();
  checkOpslaan();
  const naam = $(`fase-naam-${id}`);
  // naar het midden scrollen: onder de sticky statusbar is de kaart niet
  // aanklikbaar/sleepbaar
  grid.querySelector(`:scope > .card[data-fase="${CSS.escape(id)}"]`)
    ?.scrollIntoView({ block: "center" });
  naam.focus();
  naam.select();
  toast(`Fase "${id}" toegevoegd — geef hem een naam`);
}

/** uiStates.actualiseren met de actuele UI-waarden (vóór een mutatie). */
function syncUiStatesVanUi() {
  const fases = leesFasesVanUi();
  const perId = new Map(fases.map((s) => [s.id, s]));
  state.uiStates = faseIdsVanUi().map((id) => {
    const st = perId.get(id) || { id };
    const uit = { id };
    if (st.loop !== undefined) uit.loop = st.loop;
    if (st.bezel) uit.bezel = kloon(st.bezel);
    if (st.effects) uit.effects = kloon(st.effects);
    return uit;
  });
}

/** Fase verwijderen (met confirm; laatste fase weigeren; doorverwijzingen melden). */
function verwijderFase(id) {
  const ids = faseIdsVanUi();
  if (ids.length <= 1) {
    toast("Minstens één fase is verplicht — verwijderen geweigerd", "err");
    return;
  }
  const fases = leesFasesVanUi();
  const slachtoffer = fases.find((s) => s.id === id);
  const verwijzingen = [];
  for (const s of fases) {
    if (s.id === id) continue;
    const m = s.advance?.motion, t = s.advance?.timeout;
    if (m?.to === id) verwijzingen.push(`Beweging → ${naamVanFase(id)} vervalt (${s.name})`);
    if (m?.kind_to === id) verwijzingen.push(`Kind-doel → ${naamVanFase(id)} vervalt (${s.name})`);
    if (m?.idle?.to === id) verwijzingen.push(`Bij stilte → ${naamVanFase(id)} vervalt (${s.name})`);
    if (t?.to === id) verwijzingen.push(`Timeout → ${naamVanFase(id)} vervalt (${s.name})`);
  }
  let bericht = `Fase "${slachtoffer?.name || id}" verwijderen?`;
  if (verwijzingen.length) bericht += "\n\n" + verwijzingen.join("\n");
  if ($("ph-entry").value === id) bericht += `\n\nDe startfase wordt ${naamVanFase(fases.find((s) => s.id !== id)?.id)} (eerste overgebleven fase).`;
  if (!confirm(bericht)) return;

  syncUiStatesVanUi();
  state.uiStates = state.uiStates.filter((s) => s.id !== id);
  const grid = $("states-grid");
  grid.querySelector(`:scope > .card[data-fase="${CSS.escape(id)}"]`)?.remove();
  // entry_state kan naar een verdwenen fase wijzen → eerste overgebleven fase
  // (pas na de save echt; revert herbouwt alles vanuit de originele config)
  if (!$("ph-entry").value || !faseIdsVanUi().includes($("ph-entry").value)) {
    $("ph-entry").value = faseIdsVanUi()[0] || "";
  }
  vulStateDropdowns();
  tekenKetenSchema();
  checkOpslaan();
  toast(`Fase "${slachtoffer?.name || id}" verwijderd — nog opslaan!`);
}

/** Weergavenaam van een fase-id (uit de UI-kaarten). */
function naamVanFase(id) {
  const el = $(`fase-naam-${id}`);
  if (el && el.value.trim()) return el.value.trim();
  const st = (state.uiStates || []).find((s) => s.id === id);
  return st?.name || id;
}

/** Verbind als keten: timeout-edges langs de kaartvolgorde, laatste → entry. */
function verbindKeten() {
  const ids = faseIdsVanUi();
  if (ids.length < 2) {
    toast("Een keten heeft minstens twee fases nodig", "err");
    return;
  }
  if (!confirm("Alle timeout-overgangen worden herschreven naar één keten " +
    "langs de volgorde van de kaarten. Doorgaan?")) return;
  const entry = ids[ids.length - 1];   // laatste kaart wordt de entry (lus terug)
  for (let i = 0; i < ids.length; i++) {
    const id = ids[i];
    const doel = i + 1 < ids.length ? ids[i + 1] : entry;
    // ph-entry meezetten zodat de save de nieuwe entry bevat
    if ($("ph-entry")) $("ph-entry").value = entry;
    const secs = $(`adv-tsecs-${id}`);
    const aan = $(`adv-timeout-aan-${id}`);
    const tto = $(`adv-tto-${id}`);
    if (!secs || !aan || !tto) continue;
    const huidig = secs.value;
    aan.checked = true;
    if (huidig === "" || huidig === undefined || num(huidig, 0) <= 0) {
      secs.value = "10";
    }
    tto.value = doel;
    zetEdgeZichtbaarheid(id);
  }
  checkOpslaan();
  tekenKetenSchema();
  toast(`Keten verbonden: ${ids.map(naamVanFase).join(" → ")} → ${naamVanFase(entry)}`);
}

/** motion.to-/kind_to/idle.to- en timeout.to-dropdowns vullen met staten. */
function vulStateDropdowns() {
  const ids = faseIdsVanUi();
  for (const id of ids) {
    for (const veld of ["adv-to", "adv-kindto", "adv-idle-to", "adv-tto"]) {
      const sel = $(`${veld}-${id}`);
      if (!sel) continue;
      const cur = sel.value;
      sel.innerHTML = `<option value="">— geen —</option>` +
        ids.map((s) => `<option value="${esc(s)}"${s === cur ? " selected" : ""}>${esc(naamVanFase(s))}</option>`).join("");
    }
  }
  // entry-dropdown (ph-entry) via vulEntryDropdown (ids + namen)
  const entry = $("ph-entry");
  if (entry) {
    const cur = entry.value;
    entry.innerHTML = ids.map((s) => `<option value="${esc(s)}"${s === cur ? " selected" : ""}>${esc(naamVanFase(s))}</option>`).join("");
  }
}

/** Media-dropdowns van alle fase-kaarten verversen (na import/upload),
 *  huidige selectie behoudend. */
function verversMediaDropdowns() {
  for (const id of faseIdsVanUi()) {
    for (const key of ["0", "1", "wide"]) {
      const sel = $(`media-${id}-${key}`);
      if (!sel) continue;
      const cur = sel.value;
      sel.innerHTML = mediaOpties(key === "wide" ? "wide" : "split", cur);
      sel.value = cur;   // behoud (kan nu in Ingebouwd- of Bibliotheek-groep zitten)
    }
  }
}

/* ---------- keten-schema (read-only visualisatie) ---------- */

/**
 * Keten-schema tekenen: chips in kaartvolgorde, direct gevolgd door de
 * pijlen (—label→) van die fase. Pijlen verwijzen naar chips via
 * data-van/data-naar; entry-chip gemarkeerd. Live bij elke wijziging.
 */
function tekenKetenSchema() {
  const hold = $("keten-schema");
  if (!hold) return;
  const fases = leesFasesVanUi();
  const entry = $("ph-entry")?.value || "";
  hold.innerHTML = "";
  if (!fases.length) {
    hold.innerHTML = `<span class="hint">Geen fases.</span>`;
    return;
  }

  const chips = new Map();   // id → element (voor pijl-ankers)
  for (const st of fases) {
    const chip = document.createElement("span");
    chip.className = "keten-chip" + (st.id === entry ? " entry" : "");
    chip.dataset.fase = st.id;
    chip.title = `id: ${st.id}` + (st.id === entry ? " — startfase" : "");
    chip.textContent = st.name || st.id;
    hold.appendChild(chip);
    chips.set(st.id, chip);

    // pijlen van deze fase direct erachter (leesbaar als "A —beweging→ B")
    const m = st.advance?.motion, t = st.advance?.timeout;
    const pijlen = [];
    if (m?.to && chips.has(m.to)) {
      const lbl = "beweging" +
        (m.require_motion_secs ? ` +${fmt(m.require_motion_secs, 1)} s` : "");
      pijlen.push({ naar: m.to, lbl, soort: "motion" });
      if (m.idle?.to && chips.has(m.idle.to)) {
        pijlen.push({ naar: m.idle.to, lbl: "geen beweging", soort: "idle" });
      }
    }
    if (t?.to && chips.has(t.to)) {
      pijlen.push({ naar: t.to, lbl: `${fmt(t.secs, 1)} s`, soort: "timeout" });
    }
    for (const p of pijlen) {
      const pijl = document.createElement("span");
      pijl.className = `keten-pijl ${p.soort}`;
      pijl.dataset.van = st.id;
      pijl.dataset.naar = p.naar;
      pijl.title = `${st.name || st.id} → ${naamVanFase(p.naar)} (${p.lbl})`;
      pijl.textContent = `—${p.lbl}→`;
      hold.appendChild(pijl);
    }
  }
  const uitleg = document.createElement("span");
  uitleg.className = "keten-uitleg hint";
  uitleg.textContent = "—label→ = overgang vanaf de fase links ervan · gemarkeerd = startfase";
  hold.appendChild(uitleg);
}

/* ============================================================
 * TAB 3 — Bibliotheek
 * ============================================================ */

function bouwBibliotheekTabel() {
  const tb = $("lib-tbody");
  const imp = $("lib-import");
  if (imp) {
    imp.innerHTML = (state.libraryBuiltin || []).map((b) =>
      `<button type="button" class="btn btn-klein" data-import-builtin="${esc(b.id)}">⬇ ${esc(b.name.replace(" (ingebouwd)", ""))} importeren</button>`
    ).join(" ");
  }
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

function koppelImport() {
  const rij = $("lib-import");
  if (!rij) return;
  rij.addEventListener("click", async (e) => {
    const knop = e.target.closest("[data-import-builtin]");
    if (!knop) return;
    knop.disabled = true;
    try {
      const r = await api("/api/library/import/" + encodeURIComponent(knop.dataset.importBuiltin), { method: "POST" });
      if (!r.ok) throw new Error(r.error || "import mislukt");
      toast(`Geïmporteerd: ${r.item.name}`);
      const lib = await getLibrary();
      state.library = lib.items || [];
      state.libraryBuiltin = lib.builtin || state.libraryBuiltin;
      bouwBibliotheekTabel();
      verversMediaDropdowns();
    } catch (err) {
      toast("Importeren mislukt: " + err.message, "err");
    } finally {
      knop.disabled = false;
    }
  });
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
    $(id).addEventListener("change", () => { checkOpslaan(); tekenKetenSchema(); });
  }

  // fase-werkbalk: nieuwe fase, layout, keten
  $("fase-nieuw")?.addEventListener("click", nieuweFase);
  $("fase-keten")?.addEventListener("click", verbindKeten);
  for (const [modus, knopId] of [["verticaal", "fase-layout-verticaal"],
                                 ["horizontaal", "fase-layout-horizontaal"]]) {
    $(knopId)?.addEventListener("click", () => {
      localStorage.setItem(FASE_LAYOUT_KEY, modus);
      zetFaseLayout(modus);
    });
  }
  zetFaseLayout(leesFaseLayout());

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
      syncUiStates();           // niet-UI-velden (loop/bezel/effects) bijwerken
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
  koppelImport();

  try {
    // EERST de config ophalen, DAARNA de kaarten bouwen: anders kan de
    // gebruiker klikken op een UI waarvan de serverwaarden nog niet
    // geladen zijn, en veegt configNaarUi() zijn wijzigingen weg (race)
    const [cfg, lib] = await Promise.all([getConfig(), getLibrary()]);
    state.config = cfg;
    state.library = lib.items || [];
    state.libraryBuiltin = lib.builtin || [];
    bouwSchermKaarten();
    bouwFaseKaarten();
    configNaarUi();
    bouwBibliotheekTabel();
  } catch (e) {
    toonOffline(`config/library laden mislukt (${e.message})`);
  }
  toonOnline();

  pollOnce(); // start de 1 Hz-statusbalk-poll
}

start();
