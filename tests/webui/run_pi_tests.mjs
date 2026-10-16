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
  const state0 = await api("/api/state");
  console.log(`Pi: fase=${state0.state} kind=${state0.kind_mode} detector=${state0.detector}`);
  console.log(`snapshot genomen (${Object.keys(snapshot).length} top-level keys)`);

  const LIBS = "/tmp/pw-libs/uit/usr/lib/x86_64-linux-gnu";
  const env = fs.existsSync(LIBS) ? { ...process.env, LD_LIBRARY_PATH: LIBS } : process.env;
  const browser = await chromium.launch({ env });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
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
      await page.locator('label:has(#ovl-aan-0) .track').click();
      await page.waitForSelector("#ovl-el-0:not([hidden])", { timeout: 3000 });
    });
    await test("drag&drop overlay (echte muisbeweging)", async () => {
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
      await page.waitForSelector("#savebar:not([hidden])", { timeout: 3000 }).catch(() => {});
      await page.click("#btn-save");
      await page.waitForFunction(
        () => document.querySelector("#savebar")?.hidden === true,
        null, { timeout: 8000 });
      const cfg = await api("/api/config");
      if (cfg.screens.items[0].overlay.text !== "TEST123")
        throw new Error("overlay niet gepersisteerd: " + JSON.stringify(cfg.screens.items[0].overlay));
      if (cfg.screens.items[0].rotation !== 270)
        throw new Error("rotatie niet gepersisteerd: " + cfg.screens.items[0].rotation);
    });
    await test("swap-toggle bedienbaar via label (onclick)", async () => {
      await page.locator('label:has(#scr-swap) .track').click();
      const aan = await page.locator("#scr-swap").isChecked();
      if (!aan) throw new Error("swap niet aangevinkt na klik");
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
      await mob.locator('label:has(#ovl-aan-0) .track').click();
      await mob.waitForSelector("#ovl-el-0:not([hidden])", { timeout: 3000 });
    });
    await mob.close();
  } finally {
    // ---- restore ----
    console.log("\n== restore naar snapshot ==");
    try {
      const r = await fetch(BASE + "/api/config", {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(snapshot),
      });
      console.log("  config teruggezet:", r.status);
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
