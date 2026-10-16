// Unit/integratie-tests voor de Grimoria-webinterface.
// Draait in echte Chromium (Playwright) tegen de mock-server (zelfde
// API-contract als de Pi op :8077). Start zelf de mock via python3.
//
// Gebruik:
//   python3 tools/mock_server.py &          # of laat de runner dit doen
//   node tests/webui/run_webui_tests.mjs    # draait alles + rapport
//
import { chromium } from "playwright";
import { spawn } from "child_process";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const REPO = path.resolve(__dirname, "..", "..");
const MOCK = { cmd: "python3", args: [path.join(REPO, "tools", "mock_server.py")] };
const BASE = "http://localhost:8088";

let passed = 0, failed = 0;
const failures = [];

async function test(naam, fn, page) {
  try {
    await fn(page);
    passed++;
    console.log(`  ✔ ${naam}`);
  } catch (e) {
    failed++;
    failures.push({ naam, e });
    console.log(`  ✘ ${naam}\n    ${String(e).split("\n").slice(0, 4).join("\n    ")}`);
  }
}

async function wachtOpTekst(page, selector, tekst, timeout = 4000) {
  await page.waitForSelector(selector, { timeout });
  await page.waitForFunction(
    ([sel, tx]) => document.querySelector(sel)?.textContent?.includes(tx),
    [selector, tekst], { timeout });
}

(async () => {
  // ---- mock-server starten (mocht hij nog niet draaien) ----
  let mock = null, eigen = false;
  try {
    await fetch(BASE + "/api/state");
    console.log("mock-server draaide al");
  } catch {
    mock = spawn(MOCK.cmd, MOCK.args, { stdio: "ignore", detached: true });
    eigen = true;
    await new Promise(r => setTimeout(r, 1500));
    console.log("mock-server gestart (pid " + mock.pid + ")");
  }

  // lokaal uitgepakte systeemlibs voor chromium (zie tests/webui/README)
  const LIBS = "/tmp/pw-libs/uit/usr/lib/x86_64-linux-gnu";
  const env = fs.existsSync(LIBS)
    ? { ...process.env, LD_LIBRARY_PATH: LIBS }
    : process.env;
  const browser = await chromium.launch({ env });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  page.on("pageerror", (e) => console.log("  [pageerror]", String(e).slice(0, 120)));

  console.log("\n== 1. opstart & statusbalk ==");
  await page.goto(BASE + "/", { waitUntil: "networkidle" });
  await test("statusbalk toont fase", () => wachtOpTekst(page, "#st-phase", "Rust"));
  await test("statusbalk toont detector", () => wachtOpTekst(page, "#st-detector", "tflite"));
  await test("verbindingindicator groen", async () => {
    await page.waitForSelector("#st-conn.conn-ok", { timeout: 4000 });
  });
  await test("geen offline-banner", async () => {
    const hidden = await page.locator("#offline-banner").isHidden();
    if (!hidden) throw new Error("offline-banner zichtbaar terwijl API up is");
  });

  console.log("\n== 2. schermen-tab ==");
  await test("twee schermkaarten", async () => {
    const n = await page.locator("#screens-grid .card").count();
    if (n !== 2) throw new Error(`verwacht 2 kaarten, vond ${n}`);
  });
  await test("overlay-tekst invullen maakt label zichtbaar", async () => {
    await page.fill("#ovl-tekst-0", "BOO");
    await page.locator("#ovl-aan-0").check();
    if (await page.locator("#ovl-el-0").isHidden()) throw new Error("overlay blijft hidden na tekst+aan");
  });
  await test("rotatie-knop verandert preview-transform", async () => {
    await page.click('[data-rot="90"][data-scherm="0"]');
    const t = await page.locator("#prev-img-0").evaluate(el => el.style.transform);
    if (!t.includes("90")) throw new Error("transform=" + t);
  });
  await test("drag&drop overlay werkt (echte muis)", async () => {
    const label = page.locator("#ovl-el-0");
    const before = await page.locator("#ovl-x-0").inputValue();
    const box = await label.boundingBox();
    if (!box) throw new Error("geen bounding box (label hidden?)");
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.down();
    await page.mouse.move(box.x - 120, box.y - 60, { steps: 12 });
    await page.mouse.up();
    const after = await page.locator("#ovl-x-0").inputValue();
    if (before === after) throw new Error(`x niet gewijzigd: ${before}`);
  });
  await test("savebalk verschijnt na wijziging", async () => {
    await page.waitForSelector("#savebar:not(.verborgen)", { timeout: 3000 });
  });
  await test("opslaan PUT persisteert overlay", async () => {
    await page.click("#btn-save");
    await page.waitForSelector("#savebar.verborgen", { timeout: 5000 });
    const cfg = await (await fetch(BASE + "/api/config")).json();
    if (cfg.screens.items[0].overlay.text !== "BOO") throw new Error("tekst niet gepersisteerd");
  });
  await test("swap & offset velden bestaan en zijn bedienbaar", async () => {
    await page.check("#scr-swap");
    await page.fill("#scr-dx", "0.05");
    if (await page.locator("#savebar").evaluate(el => el.classList.contains("verborgen")))
      throw new Error("geen dirty na swap/dx");
  });

  console.log("\n== 3. fases-tab ==");
  await page.click('[role="tab"][data-tab="phases"]');
  await test("vier fasekaarten", async () => {
    const n = await page.locator("#phases-grid .card, #tab-phases .card").count();
    if (n < 4) throw new Error(`verwacht >=4 kaarten, vond ${n}`);
  });
  await test("media-dropdowns gevuld (split + wide)", async () => {
    const splitOpts = await page.locator('select[id^="media-rust-0"] option').count();
    if (splitOpts < 1) throw new Error("geen opties in rust-0 dropdown");
  });
  await test("media kiezen maakt dirty", async () => {
    const sel = page.locator('select[id^="media-rust-0"]');
    const waarde = await sel.inputValue();
    if (!waarde) {
      const opts = await sel.locator("option").evaluateAll(os => os.map(o => o.value));
      const keus = opts.find(v => v && v !== "");
      if (!keus) throw new Error("geen niet-lege optie om te kiezen (bibliotheek leeg?)");
      await sel.selectOption(keus);
    }
    if (await page.locator("#savebar").evaluate(el => el.classList.contains("verborgen")))
      throw new Error("geen dirty na media-keuze");
  });
  await test("testknop 'naar deze fase' roept goto aan", async () => {
    await page.locator('button[data-goto="jumpscare"]').first().click();
    await wachtOpTekst(page, "#st-phase", "Jumpscare");
  });
  await test("kind-modus toggle (statusbalk)", async () => {
    const voor = await page.locator("#sb-kind").isChecked();
    await page.locator("#sb-kind").click();
    await page.waitForTimeout(400);
    const api = await (await fetch(BASE + "/api/state")).json();
    if (api.kind_mode === voor) throw new Error("kind_mode niet gewijzigd op server");
  });
  await test("timing-velden staan in de UI", async () => {
    for (const id of ["ti-person-hold", "ti-still", "ti-cooldown"]) {
      if (!(await page.locator("#" + id).count())) throw new Error(id + " ontbreekt");
    }
  });

  console.log("\n== 4. bibliotheek-tab ==");
  await page.click('[role="tab"][data-tab="library"]');
  await test("bibliotheektabel toont mock-items", async () => {
    await page.waitForSelector("#lib-tbody tr", { timeout: 4000 });
    const n = await page.locator("#lib-tbody tr").count();
    if (n < 1) throw new Error("geen items");
  });
  await test("upload-zone bestaat", async () => {
    await page.waitForSelector("#lib-drop", { timeout: 3000 });
  });

  console.log("\n== 5. mobiele viewport (390x844) ==");
  const mob = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await mob.goto(BASE + "/", { waitUntil: "networkidle" });
  await test("kind-toggle zichtbaar op mobiel", async () => {
    const visible = await mob.locator("#sb-kind").isVisible();
    if (!visible) throw new Error("sb-kind niet zichtbaar op 390px");
  });
  await test("tabs bereikbaar/scrollbaar op mobiel", async () => {
    const box = await mob.locator("#tabs").boundingBox();
    if (!box || box.width > 390) throw new Error("tabs steken buiten viewport");
  });
  await mob.close();

  console.log(`\n=== ${passed} geslaagd, ${failed} gefaald ===`);
  if (failures.length) {
    console.log("\nFALERS:");
    for (const f of failures) console.log(` - ${f.naam}: ${String(f.e).split("\n")[0]}`);
  }
  await browser.close();
  if (mock && eigen) process.kill(-mock.pid, "SIGTERM");
  process.exit(failed ? 1 : 0);
})();
