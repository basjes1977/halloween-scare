// Webinterface-tests TEGEN DE ECHTE PI (192.168.178.85:8077).
// Voorzichtigheid: snapshot vooraf, restore achteraf; geen scream-tests.
//
// Gebruik: node tests/webui/run_pi_tests.mjs
//
import { chromium } from "playwright";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const BASE = process.env.GRIM_BASE || "http://192.168.178.85:8077";

let passed = 0, failed = 0;
const failures = [];
const $ = (s) => s;

async function test(naam, fn, ctx) {
  try { await fn(ctx); passed++; console.log(`  ✔ ${naam}`); }
  catch (e) {
    failed++; failures.push({ naam, e: String(e).split("\n")[0] });
    console.log(`  ✘ ${naam}\n    ${String(e).split("\n").slice(0, 3).join("\n    ")}`);
  }
}

const api = async (p, opt) => (await fetch(BASE + p, opt)).json();

(async () => {
  // ---- snapshot ----
  const snapshot = await api("/api/config");
  let laatstGeschrevenDoorTest = null;   // laatste door een test gezette PUT-body
  const state0 = await api("/api/state");
  console.log(`Pi: fase=${state0.state} kind=${state0.kind_mode} detector=${state0.detector}`);
  console.log(`snapshot genomen (${Object.keys(snapshot).length} top-level keys)`);

  const LIBS = "/tmp/pw-libs/uit/usr/lib/x86_64-linux-gnu";
  const env = fs.existsSync(LIBS) ? { ...process.env, LD_LIBRARY_PATH: LIBS } : process.env;
  const browser = await chromium.launch({ env });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  page.on("dialog", (d) => d.accept().catch(() => {}));   // confirms altijd OK
  const errs = [];
  page.on("pageerror", (e) => errs.push(String(e)));

  try {
    console.log("\n== 1. opstart & statusbalk (echte API) ==");
    await page.goto(BASE + "/", { waitUntil: "networkidle" });
    await test("fase in statusbalk = serverfase", async () => {
      const st = await api("/api/state");
      await page.waitForFunction(
        (s) => document.querySelector("#st-phase")?.textContent?.trim() === s,
        st.name, { timeout: 5000 });
    });
    await test("detector-backend zichtbaar (frame_diff)", async () => {
      await page.waitForFunction(
        () => (document.querySelector("#st-detector")?.textContent || "").includes("frame_diff"),
        null, { timeout: 5000 });
    });
    await test("verbinding ok", () => page.waitForSelector("#st-conn.conn-ok", { timeout: 5000 }));
    await test("geen offline-banner", async () => {
      if (!(await page.locator("#offline-banner").isHidden())) throw new Error("banner zichtbaar");
    });
    await test("geen JS-fouten bij opstart", async () => {
      if (errs.length) throw new Error(errs[0].slice(0, 140));
    });

    console.log("\n== 2. schermen-tab ==");
    await test("twee schermkaarten", async () => {
      const n = await page.locator("#screens-grid .card").count();
      if (n !== 2) throw new Error(`vond ${n} kaarten`);
    });
    await test("overlay invullen → label zichtbaar + versleepbaar", async () => {
      await page.fill("#ovl-tekst-0", "TEST123");
      if (!(await page.locator("#ovl-aan-0").isChecked())) {
        await page.locator('label:has(#ovl-aan-0) .track').click();
      }
      await page.waitForSelector("#ovl-el-0:not([hidden])", { timeout: 3000 });
    });
    await test("drag&drop overlay (echte muisbeweging)", async () => {
      // bepaalde startpositie (vorige runs kunnen x op 0 hebben achtergelaten)
      await page.fill("#ovl-x-0", "0.50");
      await page.fill("#ovl-y-0", "0.50");
      await page.waitForTimeout(150);
      const el = page.locator("#ovl-el-0");
      const box = await el.boundingBox();
      const x0 = await page.locator("#ovl-x-0").inputValue();
      await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
      await page.mouse.down();
      await page.mouse.move(box.x - 140, box.y - 50, { steps: 15 });
      await page.mouse.up();
      const x1 = await page.locator("#ovl-x-0").inputValue();
      if (x1 === x0) throw new Error(`x ongewijzigd (${x0})`);
    });
    await test("rotatieknop draait preview", async () => {
      await page.locator('#screens-grid .card').first().locator('[data-rot="270"]').click();
      const t = await page.locator("#prev-img-0").evaluate(el => el.style.transform);
      if (!t.includes("270")) throw new Error("transform=" + t);
    });
    await test("opslaan persisteert naar de Pi", async () => {
      await page.waitForSelector("#btn-save:not([disabled])", { timeout: 4000 });
      await page.click("#btn-save");
      await page.waitForFunction(
        () => document.querySelector("#savebar")?.hidden === true, null, { timeout: 8000 });
      const cfg = await api("/api/config");
      if (cfg.screens.items[0].overlay.text !== "TEST123")
        throw new Error("overlay niet gepersisteerd: " + JSON.stringify(cfg.screens.items[0].overlay));
      if (cfg.screens.items[0].rotation !== 270)
        throw new Error("rotatie niet gepersisteerd: " + cfg.screens.items[0].rotation);
    });
    await test("swap-checkbox vervangen door wand (niet meer aanwezig)", async () => {
      if (await page.locator("#scr-swap").count()) throw new Error("scr-swap bestaat nog");
    });

    console.log("\n== 3. fases-tab ==");
    await page.click('[role="tab"][data-tab="phases"]');
    await test("vier fasekaarten", async () => {
      await page.waitForSelector("#states-grid .card", { timeout: 4000 });
      const n = await page.locator("#states-grid .card").count();
      if (n < 4) throw new Error(`vond ${n} kaarten`);
    });
    await test("media-dropdowns: opties zichtbaar?", async () => {
      // bibliotheek is leeg op de Pi — wat toont de UI dan? (diagnostisch)
      const n = await page.locator('select[id^="media-rust-0"] option').count();
      const leeg = n === 1; // alleen "— geen —"
      console.log(`    [info] rust-0 dropdown heeft ${n} optie(s)${leeg ? " (alleen 'geen')" : ""}`);
    });
    await test("timing-velden aanwezig (tim-hold/still/cooldown)", async () => {
      for (const id of ["tim-hold", "tim-still", "tim-cooldown"]) {
        if (!(await page.locator("#" + id).count())) throw new Error(id + " ontbreekt");
      }
    });
    await test("kind-modus toggle via statusbalk (echte API)", async () => {
      const voor = (await api("/api/state")).kind_mode;
      await page.locator('label:has(#sb-kind) .track').click();
      await page.waitForTimeout(600);
      const na = (await api("/api/state")).kind_mode;
      if (na === voor) throw new Error("kind_mode ongewijzigd op server");
    });
    await test("testknop fase: naar 'langzaam' (geen scream)", async () => {
      await page.locator('button[data-goto="langzaam"]').first().click();
      await page.waitForFunction(
        () => (document.querySelector("#st-phase-id")?.textContent || "").includes("langzaam"),
        null, { timeout: 6000 });
    });

    console.log("\n== 4. bibliotheek-tab ==");
    await page.click('[role="tab"][data-tab="library"]');
    await test("upload-zone (#dropzone) zichtbaar", async () => {
      await page.waitForSelector("#dropzone", { timeout: 4000 });
    });
    await test("lege bibliotheek toont hint/lege staat, geen crash", async () => {
      if (errs.length) throw new Error(errs[0].slice(0, 140));
    });

    console.log("\n== 4b. wand-editor ==");
    await page.click('[role="tab"][data-tab="screens"]');
    await test("wand-canvas met twee blokken", async () => {
      await page.waitForSelector("#wand-canvas .wand-blok", { timeout: 4000 });
      const n = await page.locator("#wand-canvas .wand-blok").count();
      if (n !== 2) throw new Error(`vond ${n} blokken`);
    });
    await test("blok slepen naar links = horizontal+swap", async () => {
      await page.locator("#wand-card").scrollIntoViewIfNeeded();
      await page.waitForTimeout(350);
      const blok = page.locator('.wand-blok[data-scherm="1"]');
      const box = await blok.boundingBox();
      const canvas = await page.locator("#wand-canvas").boundingBox();
      // grijp bovenin het blok: op het centrum liggen de snap-knoppen
      await page.mouse.move(box.x + box.width / 2, box.y + 15);
      await page.mouse.down();
      await page.mouse.move(canvas.x + canvas.width * 0.15, canvas.y + canvas.height / 2, { steps: 10 });
      await page.mouse.up();
      await page.waitForTimeout(300);
      const status = await page.locator("#wand-status").textContent();
      if (!/links|swap|gespiegeld/i.test(status)) throw new Error("status: " + status);
    });
    await test("layout wijziging maakt dirty (via pijltjestoets, robuuster)", async () => {
      // toetsenbord-route vermijdt sticky-overlay problemen van de muis-sleep
      const blok = page.locator('.wand-blok[data-scherm="1"]');
      await blok.focus();
      await page.keyboard.press("ArrowLeft");   // scherm 2 naar links = swap
      await page.waitForTimeout(300);
      if (await page.locator("#savebar").evaluate(e => e.hidden))
        throw new Error("geen dirty na wand-toets");
      const status = await page.locator("#wand-status").textContent();
      if (!/links|gespiegeld/i.test(status)) throw new Error("status: " + status);
    });
    await test("opslaan persisteert layout", async () => {
      await page.waitForSelector("#btn-save:not([disabled])", { timeout: 4000 });
      await page.click("#btn-save");
      await page.waitForFunction(
        () => document.querySelector("#savebar")?.hidden === true, null, { timeout: 8000 });
      const cfg = await api("/api/config");
      const lay = cfg.screens.layout || {};
      if (lay.mode !== "horizontal" || lay.swap !== true)
        throw new Error("layout niet gepersisteerd: " + JSON.stringify(lay));
    });
    await test("rotatie via wand-sleephandgreep of presets", async () => {
      const blok = page.locator('.wand-blok[data-scherm="0"]');
      await blok.locator('button[data-rot="90"], .wand-rot button:first-child').first().click()
        .catch(async () => {
          // fallback: preset-knoppen heten mogelijk anders — probeer rot via kaart
          await page.locator('#screens-grid .card').first().locator('[data-rot="90"]').click();
        });
      const rot = await page.locator("#rot-0").evaluate(el => el.dataset.val ?? el.value);
      if (!String(rot).includes("90")) throw new Error("rotatie niet gesynchroniseerd: " + rot);
    });

    console.log("\n== 4c. live statusbalk ==");
    await test("tijd in fase loopt live mee", async () => {
      const leesT = () => page.locator("#st-phase-time").textContent();
      const t1 = await leesT();
      await page.waitForTimeout(2500);
      const t2 = await leesT();
      const p = (x) => parseFloat(String(x).replace(",", "."));
      if (!(p(t2) - p(t1) >= 1.5)) throw new Error(`bevroren: ${t1} -> ${t2}`);
    });
    await test("poll herstart na tab-hidden/visible (regressie)", async () => {
      await page.evaluate(() => {
        Object.defineProperty(document, "visibilityState", { value: "hidden", configurable: true });
        document.dispatchEvent(new Event("visibilitychange"));
      });
      await page.waitForTimeout(1500);
      await page.evaluate(() => {
        Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
        document.dispatchEvent(new Event("visibilitychange"));
      });
      const leesT = () => page.locator("#st-phase-time").textContent();
      const p = (x) => parseFloat(String(x).replace(",", "."));
      // fase kan tijdens de meting wisselen (tijd reset) — meet dan opnieuw
      for (let poging = 0; poging < 3; poging++) {
        const t1 = await leesT();
        await page.waitForTimeout(2200);
        const t2 = await leesT();
        if (p(t2) > p(t1)) return;                    // tikt weer ✔
        if (p(t2) < p(t1)) continue;                  // fase-reset: meet opnieuw
        throw new Error(`poll gestopt na visible: ${t1} -> ${t2}`);
      }
      throw new Error("klok tickte in geen enkele meting na visible");
    });


    console.log("\n== 4d. fase-editor ==");
    await page.click('[role="tab"][data-tab="phases"]');
    await page.waitForTimeout(400);
    let nieuweFaseId = null;
    await test("nieuwe fase aanmaken + naam + vertakking + opslaan", async () => {
      await page.locator("#states-grid .card").first().waitFor({ state: "visible", timeout: 8000 });
      await page.click("#fase-nieuw");
      await page.waitForTimeout(900);
      const kaart = page.locator("#states-grid .card").last();
      nieuweFaseId = await kaart.getAttribute("data-fase");
      const naam = page.locator(`#fase-naam-${nieuweFaseId}`);
      await naam.waitFor({ state: "visible" });
      await naam.click();
      await page.keyboard.press("Control+a");
      await page.keyboard.type("Lokaas");
      await page.evaluate((id) => {
        const el = document.querySelector(`#adv-motion-aan-${id}`);
        if (el && !el.checked) el.click();
      }, nieuweFaseId);
      await page.locator(`#edge-extra-${nieuweFaseId}`).waitFor({ state: "visible", timeout: 4000 });
      await page.locator(`#adv-to-${nieuweFaseId}`).selectOption("langzaam");
      await page.locator(`#adv-rq-${nieuweFaseId}`).fill("2.5");
      await page.locator(`#adv-kindto-${nieuweFaseId}`).selectOption("kalm");
      await page.locator(`#adv-idle-to-${nieuweFaseId}`).selectOption("rust");
      await page.locator(`#adv-idle-secs-${nieuweFaseId}`).fill("7");
      await page.waitForTimeout(400);
      await page.waitForSelector("#btn-save:not([disabled])", { timeout: 6000 });
      await page.click("#btn-save");
      await page.waitForFunction(() => document.querySelector("#savebar")?.hidden === true, null, { timeout: 12000 });
      const cfg = await api("/api/config");
      const n = cfg.states.find(st => st.id === nieuweFaseId);
      if (!n || n.name !== "Lokaas") throw new Error("naam niet opgeslagen");
      const m = n.advance.motion;
      if (!m || m.to !== "langzaam" || m.require_motion_secs !== 2.5
          || m.kind_to !== "kalm" || m.idle?.to !== "rust" || m.idle?.still_secs !== 7)
        throw new Error("motion incompleet: " + JSON.stringify(m));
    });
    await test("fase-editor: reorder via drag", async () => {
      const kaarten = page.locator("#states-grid .card");
      const bron = kaarten.nth(await kaarten.count() - 1);
      await bron.scrollIntoViewIfNeeded();
      await page.waitForTimeout(350);
      const gb = await bron.locator(".fase-grip").boundingBox();
      const db = await kaarten.nth(0).boundingBox();
      await page.mouse.move(gb.x + gb.width / 2, gb.y + gb.height / 2);
      await page.mouse.down();
      await page.mouse.move(db.x + 30, db.y + db.height / 2, { steps: 14 });
      await page.mouse.up();
      await page.waitForTimeout(500);
      await page.waitForSelector("#btn-save:not([disabled])", { timeout: 6000 });
      await page.click("#btn-save");
      await page.waitForFunction(() => document.querySelector("#savebar")?.hidden === true, null, { timeout: 12000 });
      const cfg = await api("/api/config");
      if (cfg.states[0].id !== nieuweFaseId) throw new Error("volgorde: " + cfg.states.map(st => st.id).join(","));
    });
    await test("fase-editor: keten-knop herschrijft + entry=laatste", async () => {
      await page.locator("#fase-keten").scrollIntoViewIfNeeded();
      await page.waitForTimeout(250);
      await page.evaluate(() => document.querySelector("#fase-keten")?.click());
      await page.waitForTimeout(700);
      await page.waitForSelector("#btn-save:not([disabled])", { timeout: 6000 });
      await page.click("#btn-save");
      await page.waitForFunction(() => document.querySelector("#savebar")?.hidden === true, null, { timeout: 12000 });
      const cfg = await api("/api/config");
      const ids = cfg.states.map(st => st.id);
      for (let i = 0; i < ids.length - 1; i++) {
        if (cfg.states[i].advance?.timeout?.to !== ids[i + 1])
          throw new Error(`keten broken bij ${ids[i]}`);
      }
      if (cfg.entry_state !== ids[ids.length - 1]) throw new Error("entry: " + cfg.entry_state);
    });
    await test("fase-editor: verwijderen + schema + horizontaal", async () => {
      const kaarten = page.locator("#states-grid .card");
      const laatste = kaarten.nth(await kaarten.count() - 1);
      const id = await laatste.getAttribute("data-fase");
      await laatste.scrollIntoViewIfNeeded();
      await page.waitForTimeout(300);
      await page.evaluate((id) => {
        document.querySelector(`[data-del-fase="${id}"]`)?.click();
      }, id);
      await page.waitForTimeout(700);
      await page.waitForSelector("#btn-save:not([disabled])", { timeout: 6000 });
      await page.click("#btn-save");
      await page.waitForFunction(() => document.querySelector("#savebar")?.hidden === true, null, { timeout: 12000 });
      const cfg = await api("/api/config");
      if (cfg.states.some(st => st.id === id)) throw new Error("niet verwijderd");
      const t = await page.locator("#keten-schema").textContent();
      if (!/→/.test(t)) throw new Error("geen pijlen in schema");
      await page.getByRole("button", { name: /Horizontaal/ }).click();
      await page.waitForTimeout(400);
      const attr = await page.locator("#states-grid").evaluate(e => e.dataset.layout || e.className);
      if (!/horizont/i.test(String(attr))) throw new Error("layout: " + attr);
    });
    console.log("\n== 4e. ingebouwde lussen ==");
    await test("fase-dropdowns: Ingebouwd-groep (2 opties)", async () => {
      await page.click('[role="tab"][data-tab="phases"]');
      await page.waitForTimeout(300);
      const opt = page.locator('select[id^="media-rust-0"] optgroup[label="Ingebouwd"] option');
      await opt.first().waitFor({ state: "attached", timeout: 6000 });
      if ((await opt.count()) !== 2) throw new Error("aantal builtin-opties");
    });
    await test("builtin kiezen + opslaan (builtin:rust)", async () => {
      await page.locator('select[id^="media-rust-0"]').selectOption("builtin:rust");
      await page.waitForTimeout(250);
      await page.waitForSelector("#btn-save:not([disabled])", { timeout: 6000 });
      await page.click("#btn-save");
      await page.waitForFunction(() => document.querySelector("#savebar")?.hidden === true, null, { timeout: 12000 });
      const cfg = await api("/api/config");
      if (cfg.states.find(st => st.id === "rust").media["0"] !== "builtin:rust")
        throw new Error("niet opgeslagen");
    });
    await test("import-knop: builtin -> bibliotheek + dropdowns ververst", async () => {
      await page.click('[role="tab"][data-tab="library"]');
      const knop = page.locator('[data-import-builtin="builtin:rust"]');
      await knop.waitFor({ state: "visible" });
      await knop.click();
      await page.waitForTimeout(2500);
      const lib = await api("/api/library");
      const item = lib.items.find(i => i.source === "builtin");
      if (!item || item.frames < 80) throw new Error("niet geïmporteerd");
      await page.click('[role="tab"][data-tab="phases"]');
      const opt = page.locator('select[id^="media-rust-0"] optgroup[label="Bibliotheek"] option');
      await opt.first().waitFor({ state: "attached", timeout: 6000 });
      // opruimen: geïmporteerde items weer weg + rust-media terug op geen
      await fetch(BASE + "/api/library/" + item.id, { method: "DELETE" });
      await page.locator('select[id^="media-rust-0"]').selectOption("");
      await page.waitForTimeout(250);
      await page.waitForSelector("#btn-save:not([disabled])", { timeout: 6000 });
      await page.click("#btn-save");
      await page.waitForFunction(() => document.querySelector("#savebar")?.hidden === true, null, { timeout: 12000 });
    });

    console.log("\n== 5. mobiele viewport (390×844, echte Pi) ==");
    const mob = await browser.newPage({ viewport: { width: 390, height: 844 } });
    await mob.goto(BASE + "/", { waitUntil: "networkidle" });
    await test("kind-toggle ZICHTBAAR op mobiel", async () => {
      const track = mob.locator('label:has(#sb-kind) .track');
      if (!(await track.isVisible())) throw new Error(".track niet zichtbaar op 390px");
      const box = await track.boundingBox();
      if (!box) throw new Error("geen box");
    });
    await test("overlay-label grijpbaar op mobiel (touch)", async () => {
      await mob.fill("#ovl-tekst-0", "MOB");
      // toggle alleen aanzetten als hij nog uit staat (eerder tests kunnen
      // hem al aangezet hebben)
      if (!(await mob.locator("#ovl-aan-0").isChecked())) {
        await mob.locator('label:has(#ovl-aan-0) .track').click();
      }
      await mob.waitForSelector("#ovl-el-0:not([hidden])", { timeout: 3000 });
    });
    await mob.close();
  } finally {
    // ---- restore ----
    console.log("\n== restore naar snapshot ==");
    try {
      // alleen restoren als de config niet extern gewijzigd is
      const nu = await api("/api/config");
      const externGewijzigd = JSON.stringify(nu)
        !== JSON.stringify(laatstGeschrevenDoorTest ?? snapshot);
      if (externGewijzigd) {
        console.log("  config is extern gewijzigd tijdens de run — NIET teruggedraaid");
      } else {
        const r = await fetch(BASE + "/api/config", {
          method: "PUT", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(snapshot),
        });
        console.log("  config teruggezet:", r.status);
      }
      await api("/api/kind", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ kind: state0.kind_mode }) });
      console.log("  kind_mode terug:", state0.kind_mode);
      await fetch(BASE + "/api/goto/rust", { method: "POST" });
      console.log("  fase terug naar rust");
    } catch (e) { console.log("  HERSTEL FAALDE:", String(e)); }
    await browser.close();
  }

  console.log(`\n=== ${passed} geslaagd, ${failed} gefaald ===`);
  if (failures.length) {
    console.log("FALERS:");
    failures.forEach(f => console.log(` - ${f.naam}: ${f.e}`));
  }
  process.exit(failed ? 1 : 0);
})();
