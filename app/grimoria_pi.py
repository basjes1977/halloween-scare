#!/usr/bin/env python3
"""
Madame Grimoria — interactieve Halloween-geest voor easy-as-pi (Pi 5).
Twee 1080p schermen (HDMI1+HDMI2 als één 3840x1080 canvas), HA-triggers,
LLM-brein (Ornith via pop-os), Piper NL-stem, procedurele animatie.

States: SLEEP -> WAKING -> AWAKE -> TALKING -> (SCARE -> LAUGH) -> RETURN -> SLEEP
"""
import json, math, os, queue, random, subprocess, sys, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import traceback
import pygame
try:
    import numpy as np
except ImportError:
    np = None

BASE   = "/mnt/media/halloween"
_CFG_PATH = f"{BASE}/app/config.json"
import shutil

def _load_cfg():
    try:
        return json.load(open(_CFG_PATH))
    except Exception as e:
        print(f"config-leesfout ({e}) — defaults")
        return {}

_CFG_RAW = _load_cfg()

# v2-fase-machine: aan tenzij expliciet uit (env GRIMORIA_V2=0)
V2 = os.environ.get("GRIMORIA_V2", "1") != "0"

# v2: config normaliseren (schemas); bij fouten valt hij terug op defaults
try:
    from schemas import validate_config, DEFAULT_CONFIG
    CFG, _cfg_fouten = validate_config(_CFG_RAW)
    if _cfg_fouten:
        print("config-waarschuwingen:", *_cfg_fouten, sep="\n  ")
except Exception as _e:
    print(f"schemas niet beschikbaar ({_e}) — legacy-config")
    CFG = _CFG_RAW

try:
    from engine import Engine
except Exception as _e:
    print(f"engine niet beschikbaar ({_e}) — V2 uit")
    Engine = None
    V2 = False

def _persist_cfg(cfg):
    """Config atomair wegschrijven (webui PUT /api/config)."""
    try:
        tmp = _CFG_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cfg, f, indent=2)
        shutil.move(tmp, _CFG_PATH)
    except Exception as e:
        print(f"config-schrijffout: {e}")

W, H   = 1920, 1080          # per scherm
# één canvas over beide schermen; vertical-layout (boven/onder) vraagt
# 1920x2160 — bij een layoutwijziging herstart de service (server meldt dit)
_LAYOUT = ((CFG.get("screens", {}) or {}).get("layout", {}) or {})
FULL   = (W, H * 2) if _LAYOUT.get("mode") == "vertical" else (W * 2, H)
RENDER_GL = CFG.get("render_gl", True)   # False = software-fallback
if not RENDER_GL:
    V2 = False
FPS    = 30

# ----------------------------------------------------------------voices
def piper_wav(text, path, voice=None):
    voice = voice or CFG.get("voice", "ronnie")
    model = f"{BASE}/voices/nl_NL-{voice}-medium.onnx"
    ls    = CFG.get("length_scale", 1.12)
    p = subprocess.run([f"{BASE}/piper/piper/piper", "--model", model,
                        "--length_scale", str(ls), "--output_file", path],
                       input=text.encode(), capture_output=True, timeout=30)
    if p.returncode != 0 or not os.path.exists(path):
        raise RuntimeError(p.stderr.decode()[:200])

# ----------------------------------------------------------------brain
def llm_reply(system, user, hist=None):
    if not CFG.get("llm_url"):
        return None
    msgs = [{"role": "system", "content": system}]
    for h in (hist or [])[-6:]:
        msgs.append({"role": "assistant", "content": h[0]})
        msgs.append({"role": "user", "content": h[1]})
    msgs.append({"role": "user", "content": user})
    body = json.dumps({"model": CFG["llm_model"], "messages": msgs,
                       "temperature": 0.95, "max_tokens": 260}).encode()
    req = urllib.request.Request(CFG["llm_url"] + "/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            out = json.load(r)
            return out["choices"][0]["message"]["content"].strip().strip('"“”')
    except Exception as e:
        log(f"LLM-fout: {e}")
        return None

PERSONA = (
    "Jij bent Madame Grimoria, een eeuwenoude geest die woont in een etalage "
    "achter een raam. Op halloween blijven voorbijgangers en kinderen voor het "
    "raam staan om naar je te kijken. Je spreekt Nederlands. Je antwoorden zijn "
    "KORT: maximaal 2 zinnen, samen hooguit 35 woorden. Je toon is griezelig "
    "maar vriendelijk voor kinderen: theatraal, ouderwets, mysterieus. Je nodigt "
    "voorbijgangers uit dichterbij te komen en voorspelt speels hun toekomst "
    "(grappig met een griezelig randje, nooit echt eng of verdrietig). Je houdt "
    "van snoep. Stel nooit vragen om persoonlijke gegevens. Antwoord ALLEEN met "
    "wat Grimoria zegt."
)

GREET_DING = [
    "De klok slaat… iemand waagt het aan mijn deur te rammelen.",
    "Ah… een kloppend hart nadert mijn spiegel.",
    "De bel… muziek in mijn graf.",
]
GREET_MOTION = [
    "Wie sluipt daar door de nacht… mijn ruiten voelen je blik.",
    "Nader, reiziger… het glas scheidt ons niet.",
    "Ik voel voetstappen… de nacht fluistert je naam.",
]
PREFETCH = [
    ("MOTION", "Kinderen in halloweenpakjes stoppen voor het raam en kijken naar binnen. Nodig ze uit dichterbij te komen en voorspel iets griezeligs over snoep."),
    ("MOTION", "Iemand loopt langzaam langs het raam en werpt een blik naar binnen. Roep die persoon speels aan vanuit het glas."),
    ("MOTION", "Een kind met een lantaarntje staat nu vlak voor het raam. Geef een korte griezelige voorspelling."),
    ("MOTION", "Een groepje verklede kinderen verdringt zich lachend voor het raam. Verwelkom ze theatraal."),
    ("MOTION", "Iemand maakt een foto van het raam met een telefoon. Reageer geheimzinnig op de lens."),
]
FALLBACK_FORTUNES = [
    "Ik voorspel… dat er vanavond snoep in je zak verdwijnt. Vreemde krachten.",
    "De sterren fluisteren… jij wordt rijk. Rijk aan snoep.",
    "Ik zie… een kat. Drie keer zwart. Volg haar niet.",
    "Voorzichtig met trappen vanavond… de geesten houden van grapjes.",
    "Jouw toekomst is helder… helder als een pompoen in de nacht.",
    "Er staat iemand achter je. Nee—nu weer niet. Geesten zijn snel.",
]

# ----------------------------------------------------------------senses
events = queue.Queue()
_last_state = {}

# gedeelde camera-status (geschreven door cam_watcher, gelezen door main-loop)
CAM = {"present": False, "last_seen": None, "conf": None}

class _DetInfo:
    backend = "onbekend"
DET = _DetInfo()

# v2-bibliotheek (filmframes); op een dev-machine zonder /mnt/media blijft
# hij None en valt de renderer terug op de ingebouwde frame-series
try:
    from library import Library
    LIB = Library(f"{BASE}/media/library")
except Exception as _e:
    print(f"bibliotheek niet beschikbaar ({_e})")
    LIB = None

def _v2_frames_source(mid):
    """media-id -> frame-padlijst. 'builtin:rust'/'builtin:jump' gebruikt de
    oorspronkelijke Wan-frame-series als er nog geen media is toegewezen."""
    if not mid:
        return None
    if mid.startswith("builtin:"):
        import glob as _glob
        d = f"{BASE}/media/loops/frames/{mid.split(':', 1)[1]}"
        hits = sorted(_glob.glob(os.path.join(d, "f*.png")))
        return hits or None
    if LIB is None:
        return None
    try:
        return LIB.frames(mid) or None
    except Exception:
        return None

# hergebruikte tekenbuffers (voorkomt gigabyte/s aan allocaties op de Pi)
_SCRATCH: list = []

def scratch():
    """Lege fullscreen SRCALPHA-buffer uit de pool (max 2 gelijktijdig in gebruik)."""
    if _SCRATCH:
        s = _SCRATCH.pop()
    else:
        s = pygame.Surface((W, H), pygame.SRCALPHA)
    s.fill((0, 0, 0, 0))
    return s

def scratch_done(s):
    _SCRATCH.append(s)

def ha_get(entity):
    token = CFG.get("ha_token", "")
    req = urllib.request.Request(f"{CFG['ha_url']}/api/states/{entity}",
                                 headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=6) as r:
            return json.load(r)["state"]
    except Exception:
        return None

def cam_watcher():
    """Camera-thread. v2: persoonsdetectie (TFLite, fallback frame-diff) +
    bliktracking. Legacy: bewegingsdetectie zoals voorheen."""
    try:
        from picamera2 import Picamera2
    except Exception as e:
        log(f"picamera2 niet beschikbaar: {e}")
        return
    c = CFG.get("camera", {})
    m = CFG.get("motion", {})
    W_, H_ = c.get("w", 320), c.get("h", 240)
    try:
        p = Picamera2()
        p.configure(p.create_video_configuration(
            main={"size": (W_, H_), "format": "YUV420"}))
        p.start()
    except Exception as e:
        log(f"camera-start faalt: {e}")
        return
    time.sleep(1.5)                      # auto-exposure laten settelen
    thr      = c.get("diff_thr", 25)
    motion_px = int(W_ * H_ * c.get("min_frac", 0.012))
    flip_x = c.get("flip_x", False)
    flip_y = c.get("flip_y", False)
    detect_elk = max(1, int(round((c.get("fps", 8)) /
                                  max(0.5, m.get("detect_fps", 3)))))
    det = mem = None
    if V2 and m.get("enabled", True):
        try:
            from detector import PersonDetector, PresentMemory
            det = PersonDetector(CFG)
            mem = PresentMemory(m.get("present_memory_secs", 1.0))
            log(f"persoonsdetectie actief (backend={det.backend})")
        except Exception as e:
            log(f"detector-start faalt: {e} — frame-diff-blik alleen")
    prev = None
    frame_n = 0
    last_track = 0
    log(f"camera actief ({W_}x{H_}, thr={thr}, min_px={motion_px}, "
        f"v2={bool(det)})")
    while True:
        try:
            req = p.capture_request()
            luma = req.make_array("main")[:H_].astype(np.float32)
            req.release()
        except Exception as e:
            log(f"cam-fout: {e}")
            time.sleep(2)
            continue
        frame_n += 1
        nu = time.time()
        # ---- persoonsdetectie (v2) ----
        if det is not None and frame_n % detect_elk == 0:
            try:
                d = det.detect(luma)
                present = mem.update(d, nu)
                if d.get("person"):
                    CAM["last_seen"] = nu
                    CAM["conf"] = d.get("conf")
                CAM["present"] = present
                DET.backend = det.backend
            except Exception as e:
                log(f"detect-fout: {e}")
        # ---- bliktracking (frame-diff zwaartepunt; ook in v2) ----
        if prev is not None:
            mask = np.abs(luma - prev) > thr
            n = int(mask.sum())
            if n > motion_px:
                ys, xs = np.nonzero(mask)
                nx = float(xs.mean()) / W_ * 2 - 1
                ny = float(ys.mean()) / H_ * 2 - 1
                if flip_x: nx = -nx
                if flip_y: ny = -ny
                if nu - last_track > 0.5:
                    events.put(("CAM_TRACK", (nx, ny)))
                    last_track = nu
        prev = luma
        time.sleep(1.0 / c.get("fps", 8))


def ha_watcher():
    global _last_state
    ents = CFG["ha_entities"]
    while True:
        cur = {}
        for key, eid in ents.items():
            if eid:
                cur[key] = ha_get(eid)
        # edges
        prev = _last_state
        if cur.get("ding") == "on" and prev.get("ding") != "on":
            events.put(("DING", None))
        if cur.get("motion") == "on" and prev.get("motion") != "on" \
           and cur.get("ding") != "on":
            events.put(("MOTION", None))
        for who in ("bas", "tanja", "kristel"):
            k = f"home_{who}"
            if k in cur and cur.get(k) == "home" and prev.get(k) == "not_home":
                events.put(("HOME", who))
        _last_state = cur
        time.sleep(2)

# ----------------------------------------------------------------logging
LOG_PATH = f"{BASE}/logs/grimoria.log"
def log(msg):
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 2_000_000:
            os.replace(LOG_PATH, LOG_PATH + ".old")
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass

# ----------------------------------------------------------------venster-guard
# LXDE/openbox begroef het venster bij eerdere herstarts (Iconic/IsUnMapped).
# Deze guard controleert de X-state en herstelt + logt als dat weer gebeurt.
_WIN_ID = None

def _x(cmd):
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=5,
                           env={**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":0")})
        return p.stdout.decode().strip()
    except Exception:
        return ""

def _win_state(wid):
    wm = _x(["xprop", "-id", wid, "WM_STATE", "_NET_WM_STATE"])
    return " | ".join(l.strip() for l in wm.splitlines() if l) or "onbekend"

def window_geom(wid):
    """Lees absolute geometrie via xwininfo."""
    out = _x(["xwininfo", "-id", wid])
    g = {}
    for line in out.splitlines():
        for key in ("Absolute upper-left X", "Absolute upper-left Y", "Width", "Height"):
            if line.strip().startswith(key):
                g[key] = int(line.split(":")[1].strip())
    return g

def window_ok(wid):
    """True als venster de verwachte layout-afmeting heeft op (0,0) en niet
    geminimaliseerd is. Vertical-layout vraagt 1920x2160 i.p.v. 3840x1080."""
    if not wid:
        return False
    g = window_geom(wid)
    if not g:
        return False
    mode = ((CFG.get("screens", {}) or {}).get("layout", {})
            or {}).get("mode", "horizontal")
    w_verw, h_verw = (1920, 2160) if mode == "vertical" else (3840, 1080)
    ok_geom = (g.get("Width") == w_verw and g.get("Height") == h_verw
               and g.get("Absolute upper-left X", -1) == 0
               and g.get("Absolute upper-left Y", -1) == 0)
    return ok_geom and "Iconic" not in _win_state(wid)

_venster_formaat = None    # (breedte, hoogte) volgens huidige layout

def venster_formaat():
    """Venster-afmeting volgens screens.layout (global, bij herstart nodig)."""
    global _venster_formaat
    if _venster_formaat is None:
        mode = ((CFG.get("screens", {}) or {}).get("layout", {})
                or {}).get("mode", "horizontal")
        _venster_formaat = (1920, 2160) if mode == "vertical" else (3840, 1080)
    return _venster_formaat

def find_win_id():
    ids = _x(["xdotool", "search", "--pid", str(os.getpid())]).split()
    if not ids:
        ids = _x(["xdotool", "search", "--name", "grimoria"]).split()
    for wid in reversed(ids):
        cls = _x(["xprop", "-id", wid, "WM_CLASS"])
        if "grimoria" in cls or not cls:
            return wid
    return ids[-1] if ids else None

def enforce_window(reason):
    """Pin venster op 0,0 in de layout-afmeting, boven-alles; log voor/na."""
    global _WIN_ID
    if not os.environ.get("DISPLAY"):
        return
    wid = _WIN_ID if window_ok(_WIN_ID) else (find_win_id() or _WIN_ID)
    if not wid:
        log(f"venster-guard[{reason}]: GEEN window-id gevonden (xdotool/wmctrl aanwezig?)")
        return
    _WIN_ID = wid
    bw, hh = venster_formaat()
    voor = f"geom={window_geom(wid)} | {_win_state(wid)[:120]}"
    _x(["xdotool", "windowactivate", wid])
    # fullscreen-state juist WEGHALEN: openbox maakt daar 1920x1080 van (1 monitor)
    _x(["wmctrl", "-i", "-r", wid, "-b", "remove,fullscreen"])
    _x(["wmctrl", "-i", "-r", wid, "-e", f"0,0,0,{bw},{hh}"])
    _x(["wmctrl", "-i", "-r", wid, "-b", "add,above"])
    time.sleep(0.3)
    na = f"geom={window_geom(wid)}"
    log(f"venster-guard[{reason}] win={wid}: voor=({voor}) na=({na})")

# ----------------------------------------------------------------speech
mixer_busy = lambda: pygame.mixer.get_busy()
sfx = {}

def make_sfx():
    """Procedurele geluidseffecten (geen assets nodig)."""
    import numpy as np
    sr = 22050
    def snd(arr):
        arr = np.clip(arr, -1, 1)
        stereo = np.column_stack([arr, arr])
        return pygame.sndarray.make_sound((stereo * 32767).astype(np.int16))
    # donder: bruine ruis met trage decay
    n = sr * 3
    noise = np.cumsum(np.random.uniform(-1, 1, n)) / 400
    noise /= np.abs(noise).max()
    env = np.exp(-np.linspace(0, 6, n))
    sfx["thunder"] = snd(noise * env * 0.9)
    # whisper-scherie: ruis met bandje-achtige modulatie
    n = sr * 2
    m = np.random.uniform(-1, 1, n) * np.sin(np.linspace(0, 40 * math.pi, n))
    m /= np.abs(m).max()
    sfx["whisper"] = snd(m * 0.25)
    # screamer: opgaande dissonante zaagtand+ruis
    n = sr * 2
    t = np.linspace(0, 2, n)
    f = 300 + 900 * t**1.5
    saw = 2 * ((f * t) % 1) - 1
    harsh = 0.6 * saw + 0.4 * np.random.uniform(-1, 1, n)
    env = np.minimum(1, 6 * t) * np.exp(-1.2 * t)
    sfx["scream"] = snd(harsh * env * 0.95)
    # gegiechel: pulserende tonen
    n = sr * 2
    t = np.linspace(0, 2, n)
    gig = np.sin(2 * math.pi * (380 + 120 * np.sin(2 * math.pi * 7 * t)) * t)
    env = (0.5 + 0.5 * np.sin(2 * math.pi * 7 * t)) * np.exp(-1.0 * t)
    sfx["laugh"] = snd(gig * env * 0.5)

# ----------------------------------------------------------------grimoria class
class Grimoria:
    def __init__(self, screen):
        self.scr = screen
        self.font_big  = pygame.font.SysFont("dejavuserifbold", 110)
        self.font_mid  = pygame.font.SysFont("dejavuserifbold", 64)
        self.font_cap  = pygame.font.SysFont("dejavusans", 44)
        self.state = "SLEEP"
        self.t0 = time.time()          # state start
        self.eye = (0, 0)              # pupil -1..1
        self.eye_target = (0, 0)
        self.blink = 0                 # countdown
        self.mouth = 0.0               # 0..1
        self.saying = ""
        self.caption = ""
        self.cap_surfaces = []
        self.scare_frame = 0
        self.flash = 0.0
        self.glow_phase = random.random() * 10
        self.cool = {}                 # event -> tijd
        self.hist = []                 # (grimoria, trigger-context)
        self.stars = [(random.random(), random.random(), random.random() * 2 * math.pi)
                      for _ in range(140)]
        self.idle_scan_next = time.time() + 4
        self.pf = queue.Queue()        # geprefetchte LLM-regels per event-type
        threading.Thread(target=self.prefetcher, daemon=True).start()
        # GPU-renderer (fragment-shader); bij falen software-tekenpad
        self.gl = None
        if RENDER_GL:
            try:
                from renderer import Renderer
                self.gl = Renderer(FULL, v2=V2)
                log(f"GL-renderer actief: {self.gl.gl_version}")
                if V2 and self.gl.v2 is not None:
                    self.gl.set_v2_frames_source(_v2_frames_source)
            except Exception as e:
                log(f"GL-renderer faalt, val terug op software: {e}")
                self.gl = None
        # v2-fase-machine-status (server_ctx wordt in main() gezet)
        self.server_ctx = None
        self._v2_sid = None

    def prefetcher(self):
        # haalt vooraf LLM-reacties op zodat een trigger instant voelt
        while True:
            try:
                if self.pf.qsize() < 10:      # cap: geen onbeperkte stapel
                    kind, ctx = random.choice(PREFETCH)
                    r = llm_reply(PERSONA, ctx, self.hist)
                    if r and 5 < len(r) < 220:
                        self.pf.put_nowait((kind, r))
                        log(f"prefetch klaar ({kind}): {r[:60]}")
            except Exception as e:
                log(f"prefetch-fout: {e}")
            time.sleep(20)

    # ----------------speech
    def say(self, text, caption=True):
        self.saying = text
        self.caption = text if caption else ""
        self.cool["SPRAAK"] = time.time()     # spreek-cooldown-richtpunt
        if V2:
            # de engine bezit de state; alleen praten + onderschrift
            try:
                piper_wav(text, "/tmp/gsay.wav")
                pygame.mixer.music.load("/tmp/gsay.wav")
                pygame.mixer.music.play()
                log(f"ZEGT: {text}")
            except Exception as e:
                log(f"TTS-fout: {e}")
            return
        self.state = "TALKING"
        self.t0 = time.time()
        try:
            piper_wav(text, "/tmp/gsay.wav")
            pygame.mixer.music.load("/tmp/gsay.wav")
            pygame.mixer.music.play()
            log(f"ZEGT: {text}")
        except Exception as e:
            log(f"TTS-fout: {e}; terug naar AWAKE")
            self.state = "AWAKE"; self.t0 = time.time()

    # ----------------director
    last_track = 0.0

    def handle(self, ev):
        kind, who = ev
        now = time.time()
        # cooldowns
        cd = self.cool.get(kind, 0)
        if kind == "DING"  and now - cd < 25: return
        if kind == "MOTION" and now - cd < 90: return
        if kind == "HOME"  and now - cd < 1200: return
        self.cool[kind] = now

        if kind == "CAM_TRACK":
            nx, ny = who
            self.eye_target = (nx * 0.9, ny * 0.45)
            self.last_track = now
            if self.state == "AWAKE":
                self.t0 = now          # blijf wakker zolang er iemand staat
            return
        if kind == "MOTION":
            if self.state in ("SLEEP", "RETURN"):
                self.wake_up(kind)
            elif self.state == "AWAKE":
                # niet eindeloos doorpraten: minstens spreek_cooldown sec rust
                if now - self.cool.get("SPRAAK", 0) < CFG.get("spreek_cooldown", 50):
                    return
                nxt = self._pop_prefetch("MOTION") or random.choice(FALLBACK_FORTUNES)
                self.say(nxt)
        elif kind == "DING":
            if self.state in ("SLEEP", "RETURN"):
                self.wake_up(kind)
            elif self.state in ("AWAKE", "TALKING"):
                self.follow_up(kind)
        elif kind == "HOME":
            naam = {"bas": "Bas", "tanja": "Tanja", "kristel": "Kristel"}.get(who, "")
            self.wake_home(naam)

    def _pop_prefetch(self, kind):
        # zoek een geprefetchte regel van het juiste type (max 4 pogingen)
        stash = []
        found = None
        for _ in range(4):
            try:
                k, r = self.pf.get_nowait()
            except queue.Empty:
                break
            if k == kind and not found:
                found = r
            else:
                stash.append((k, r))
        for it in stash:
            self.pf.put_nowait(it)
        return found

    def wake_up(self, kind):
        self.state = "WAKING"; self.t0 = time.time()
        self.flash = 1.0
        sfx["thunder"].play()
        greet = random.choice(GREET_DING if kind == "DING" else GREET_MOTION)
        self._pend = ("greet", greet)
        self._pend_next = self._pop_prefetch(kind)  # LLM-regel die erna komt
        if not self._pend_next:
            self._pend_next = random.choice(FALLBACK_FORTUNES)

    def follow_up(self, kind):
        ctx = "Iemand drukte opnieuw op de bel terwijl je al wakker bent."
        r = llm_reply(PERSONA, ctx, self.hist) or random.choice(FALLBACK_FORTUNES)
        self.hist.append((r, ctx))
        self.say(r)

    def wake_home(self, naam):
        if self.state == "SLEEP":
            self.state = "WAKING"; self.t0 = time.time()
            self.flash = 0.6
            sfx["thunder"].play()
        ctx = f"{naam} (bewoner) komt net thuis. Verwelkom hem/haar kort in je spiegel."
        r = llm_reply(PERSONA, ctx, self.hist) or f"Welkom thuis, {naam}… de nacht is jong."
        self.hist.append((r, ctx))
        self._pend = ("greet", f"Ah… {naam} keert huiswaarts…")
        self._pend_next = r

    def maybe_scare(self):
        if self.state not in ("AWAKE", "TALKING"): return
        if CFG.get("kind_mode", True): return
        if random.random() < CFG.get("jumpscare_kans", 0.0):
            self.state = "SCARE"; self.t0 = time.time(); self.scare_frame = 0
            pygame.mixer.music.stop()
            sfx["scream"].play()

    # ----------------v2: fase-machine-koppeling
    def update_v2(self, dt, nu):
        """Engine tikken + overgangen vertalen naar effecten; legacy-animatie-
        onderdelen (blik, knipper, flash) gewoon bijhouden."""
        ctx = self.server_ctx
        if ctx is None:
            return
        eng = ctx.engine
        try:
            overgangen = eng.tick(nu, bool(CAM["present"]))
        except Exception as e:
            log(f"engine-tick-fout: {e}")
            overgangen = []
        for tr in overgangen:
            log(f"fase: {tr['from']} -> {tr['to']} ({tr['reason']})")
            st = next((s for s in CFG.get("states", [])
                       if s["id"] == tr["to"]), {})
            fx = st.get("effects", {})
            if fx.get("scare"):
                self.flash = 1.0
                pygame.mixer.music.stop()
                sfx["scream"].play()
        # legacy-state benaderen zodat overige logica/captions kloppen
        if eng.state_id == "jumpscare":
            self.state = "SCARE"
        elif self.caption:
            self.state = "AWAKE"
        else:
            self.state = "SLEEP" if eng.state_id == "rust" else "AWAKE"
        # camera-status doorgeven aan de server (/api/state)
        ctx.person_present = bool(CAM["present"])
        ctx.person_conf = CAM.get("conf")
        ctx.person_last_seen = CAM.get("last_seen")
        # ogen/knipper/flash (zelfde als legacy-tail)
        if nu > self.idle_scan_next and nu - self.last_track > 3:
            self.eye_target = (random.uniform(-.8, .8), random.uniform(-.4, .4))
            self.idle_scan_next = nu + random.uniform(2, 6)
        ex = self.eye_target[0] - self.eye[0]
        ey = self.eye_target[1] - self.eye[1]
        self.eye = (self.eye[0] + ex * min(1, dt * 3),
                    self.eye[1] + ey * min(1, dt * 3))
        self.blink -= dt
        if self.blink < -3 and random.random() < 0.02:
            self.blink = 0.18
        self.glow_phase += dt
        self.flash = max(0, self.flash - dt * 1.5)

    def v2_frame_info(self):
        """Alles wat de renderer per frame nodig heeft: media, rotatie,
        wide/bezel, effecten en overlay-items (uit CFG)."""
        eng = self.server_ctx.engine
        sid = eng.state_id
        reset = sid != self._v2_sid
        self._v2_sid = sid
        st = next((s for s in CFG.get("states", []) if s["id"] == sid), {})
        mode = st.get("canvas_mode", "split")
        media = st.get("media", {}) or {}
        rust_id = "builtin:rust"
        jump_id = "builtin:jump"
        if mode == "wide":
            mids = {"all": media.get("all") or rust_id}
        else:
            fb = jump_id if sid == "jumpscare" else rust_id
            mids = {"0": media.get("0") or fb, "1": media.get("1") or fb}
        scr = CFG.get("screens", {})
        items = scr.get("items", []) or []
        def _item(i):
            return items[i] if i < len(items) else {}
        overlay = []
        for i in range(2):
            ov = (_item(i).get("overlay") or {})
            if ov.get("enabled") and ov.get("text"):
                overlay.append({"text": ov["text"], "x": ov.get("x", 0.5),
                                "y": ov.get("y", 0.85),
                                "size": ov.get("size", 48),
                                "color": ov.get("color", "#ffaa00"),
                                "rotate": ov.get("rotate", "auto"),
                                "scherm_rot": _item(i).get("rotation", 0),
                                "screen": i})
        if self.caption:
            overlay.append({"text": self.caption, "x": 0.5, "y": 0.9,
                            "size": 44, "color": "#e1ebdc", "screen": 0})
        return {
            "mode": mode,
            "mids": mids,
            "effects": st.get("effects", {}),
            "bezel": (st.get("bezel", {}).get("x", 0.0),
                      st.get("bezel", {}).get("y", 0.0)),
            "screens": {"rot0": _item(0).get("rotation", 0),
                        "rot1": _item(1).get("rotation", 0)},
            "layout": (CFG.get("screens", {}) or {}).get("layout",
                          {"mode": "horizontal", "swap": False}),
            "fps": (CFG.get("playback", {}) or {}).get("fps", 16),
            "reset_t0": reset,
            "overlay": overlay,
        }

    # ----------------update
    def update(self, dt):
        # wakker-worden: na donder ~1.2s de groet uitspreken
        if self.state == "WAKING" and time.time() - self.t0 > 1.2:
            g = getattr(self, "_pend", None)
            if g:
                txt = g[1]
                nxt = getattr(self, "_pend_next", None)
                self._pend = None; self._pend_next = None
                self.state = "AWAKE"; self.t0 = time.time()
                self.say(txt)
                if nxt:
                    self._queue_next = nxt
        # pratend -> klaar?
        if self.state == "TALKING":
            if time.time() - self.t0 > 60:      # watchdog: audio bleef hangen
                log("TALKING-watchdog: forceer AWAKE")
                pygame.mixer.music.stop()
                self.state = "AWAKE"; self.t0 = time.time(); self.mouth = 0
            elif mixer_busy():
                self.mouth = max(0.0, min(1.0, self.mouth + random.uniform(-.35, .35)))
            else:
                if time.time() - self.t0 > 1.0:  # buffer na start
                    nxt = getattr(self, "_queue_next", None)
                    if nxt:
                        self._queue_next = None
                        self.say(nxt)
                    else:
                        self.state = "AWAKE"; self.t0 = time.time()
                        self.mouth = 0
                        self.maybe_scare()
        # wakker -> terug in slaap
        if self.state == "AWAKE" and time.time() - self.t0 > CFG.get("awake_secs", 75):
            self.state = "RETURN"; self.t0 = time.time()
        if self.state == "RETURN" and time.time() - self.t0 > 4:
            self.state = "SLEEP"; self.t0 = time.time()
            self.caption = ""
        if self.state == "SCARE" and time.time() - self.t0 > 2.2:
            self.state = "LAUGH"; self.t0 = time.time()
            sfx["laugh"].play()
        if self.state == "LAUGH" and time.time() - self.t0 > 4:
            self.state = "AWAKE"; self.t0 = time.time()

        # ogen: random blik alleen als er geen cameratracking is
        if time.time() > self.idle_scan_next and time.time() - self.last_track > 3:
            self.eye_target = (random.uniform(-.8, .8), random.uniform(-.4, .4))
            self.idle_scan_next = time.time() + random.uniform(2, 6)
        ex = self.eye_target[0] - self.eye[0]
        ey = self.eye_target[1] - self.eye[1]
        self.eye = (self.eye[0] + ex * min(1, dt * 3),
                    self.eye[1] + ey * min(1, dt * 3))
        self.blink -= dt
        if self.blink < -3 and random.random() < 0.02:
            self.blink = 0.18
        self.glow_phase += dt
        self.flash = max(0, self.flash - dt * 1.5)

    # ----------------tekenen
    def draw_ghost(self, surf):
        cx, cy = W // 2, H // 2 + 40
        sleeping = self.state in ("SLEEP", "RETURN")
        waking   = self.state == "WAKING"
        scare    = self.state in ("SCARE", "LAUGH") and self.state == "SCARE"
        scale = 1.0
        if self.state == "SCARE":
            scale = 1.0 + min(1.4, (time.time() - self.t0) * 1.2)
        breathe = math.sin(self.glow_phase * (0.7 if sleeping else 1.6)) * (10 if sleeping else 4)

        # nevel-glow
        glow_col = (30, 60, 45)
        if waking or self.state == "SCARE": glow_col = (90, 20, 20)
        for i in range(6, 0, -1):
            r = int((320 + i * 70 + breathe) * scale)
            g = scratch()
            pygame.draw.circle(g, (*glow_col, max(4, 26 - i * 4)), (cx, cy - 60), r)
            surf.blit(g, (0, 0)); scratch_done(g)

        # gezicht
        fw, fh = int(360 * scale), int(470 * scale)
        face = scratch()
        pale = (170, 185, 165, 235) if not sleeping else (120, 135, 120, 190)
        pygame.draw.ellipse(face, pale, (cx - fw // 2, cy - fh // 2 + int(breathe), fw, fh))
        # holle wangen / schaduwen
        pygame.draw.ellipse(face, (60, 70, 60, 120),
                            (cx - fw // 2 + 30, cy - 40, 90, 140))
        pygame.draw.ellipse(face, (60, 70, 60, 120),
                            (cx + fw // 2 - 120, cy - 40, 90, 140))
        surf.blit(face, (0, 0)); scratch_done(face)

        # ogen
        ew, eh = int(110 * scale), int(80 * scale)
        for sx in (-1, 1):
            ex_ = cx + sx * int(105 * scale)
            ey_ = cy - int(110 * scale) + int(breathe)
            if self.state == "SCARE":
                pygame.draw.ellipse(surf, (10, 5, 5), (ex_ - ew, ey_ - eh, ew * 2, eh * 2))
                pygame.draw.circle(surf, (255, 40, 40), (ex_ + int(self.eye[0] * 30),
                                                          ey_ + int(self.eye[1] * 20)), int(26 * scale))
                continue
            if sleeping:
                pygame.draw.arc(surf, (20, 25, 20), (ex_ - ew // 2, ey_ - 12, ew, 30), math.pi, 2 * math.pi, 6)
                continue
            open_ = 1.0 if self.blink <= 0 else 0.12
            hh = int(eh * open_)
            pygame.draw.ellipse(surf, (12, 14, 12), (ex_ - ew // 2, ey_ - hh, ew, hh * 2))
            px = ex_ + int(self.eye[0] * ew * 0.32)
            py = ey_ + int(self.eye[1] * hh * 0.5)
            col = (215, 255, 200) if self.state != "SCARE" else (255, 60, 60)
            pygame.draw.circle(surf, col, (px, py), int(17 * scale))

        # mond
        mw = int(120 * scale)
        if self.state == "SCARE":
            pygame.draw.ellipse(surf, (5, 5, 5), (cx - mw, cy + 30, mw * 2, int(260 * scale)))
        elif sleeping:
            pygame.draw.arc(surf, (25, 30, 25), (cx - 70, cy + 60, 140, 60), math.pi, 2 * math.pi, 5)
        else:
            mh = int(16 + self.mouth * 120)
            pygame.draw.ellipse(surf, (8, 10, 8), (cx - mw // 2, cy + 55, mw, mh))

        # sluier/haar
        veil = scratch()
        for i in range(9):
            ph = self.glow_phase * 1.2 + i * 0.7
            dx = int(math.sin(ph) * 26)
            x0 = cx - fw // 2 - 60 + i * (fw + 120) // 8
            pts = [(x0 - 40, 0), (x0 + 40, 0),
                   (x0 + 26 + dx, cy + fh // 2 + 200),
                   (x0 - 26 + dx, cy + fh // 2 + 210)]
            pygame.draw.polygon(veil, (18, 28, 22, 160 if not sleeping else 120), pts)
        surf.blit(veil, (0, 0)); scratch_done(veil)

        # caption onderop scherm 1
        if self.caption and self.state in ("TALKING", "AWAKE", "WAKING"):
            self._caption(surf, self.caption, 1)

    def draw_orb(self, surf):
        surf.fill((2, 4, 3))
        ox, oy = W // 2, H // 2 + 120
        talking = self.state == "TALKING"
        pul = 1 + (0.12 if talking else 0.04) * math.sin(self.glow_phase * 6)
        for i in range(5, 0, -1):
            r = int((150 + i * 55) * pul)
            g = scratch()
            col = (25, 90, 45) if not talking else (60, 160, 70)
            pygame.draw.circle(g, (*col, max(3, 20 - i * 3)), (ox, oy), r)
            surf.blit(g, (0, 0)); scratch_done(g)
        pygame.draw.circle(surf, (200, 240, 210), (ox, oy), int(36 * pul))
        # sterren
        for fx, fy, ph in self.stars:
            a = 0.5 + 0.5 * math.sin(self.glow_phase * 2 + ph)
            x = int(fx * W); y = int(fy * H * 0.55)
            pygame.draw.circle(surf, (int(90 * a) + 40, int(140 * a) + 60, int(110 * a) + 50), (x, y), 2)
        # titel (gecached)
        if self.state != "SCARE":
            if getattr(self, "_title", None) is None:
                t1 = self.font_big.render("MADAME GRIMORIA", True, (190, 220, 190))
                t2 = self.font_mid.render("de spiegel zijner zielen", True, (120, 160, 125))
                pad = 12
                bg = pygame.Surface((max(t1.get_width(), t2.get_width()) + 2 * pad,
                                     t1.get_height() + t2.get_height() + 3 * pad), pygame.SRCALPHA)
                bg.blit(t1, ((bg.get_width() - t1.get_width()) // 2, pad))
                bg.blit(t2, ((bg.get_width() - t2.get_width()) // 2, t1.get_height() + 2 * pad))
                self._title = bg
            surf.blit(self._title, self._title.get_rect(midtop=(W // 2, 120)))
        if self.caption:
            self._caption(surf, self.caption, 0)

    def _caption(self, surf, text, which):
        # caption over max 2 regels, onderaan
        words = text.split(); lines, cur = [], ""
        for w_ in words:
            if self.font_cap.size(cur + " " + w_)[0] > W - 200 and cur:
                lines.append(cur); cur = w_
            else:
                cur = (cur + " " + w_).strip()
        lines.append(cur)
        y = H - 120 - len(lines) * 52
        for ln in lines[:2]:
            sh = scratch()
            t = self.font_cap.render(ln, True, (225, 235, 220))
            r = t.get_rect(center=(W // 2, y))
            pygame.draw.rect(sh, (0, 0, 0, 130), r.inflate(40, 18), border_radius=14)
            surf.blit(sh, (0, 0)); surf.blit(t, r); scratch_done(sh)
            y += 52

    def draw_scare_overlay(self, screen):
        f = self.flash
        if f > 0:
            ov = pygame.Surface(FULL); ov.fill((255, 255, 255))
            ov.set_alpha(int(140 * f)); screen.blit(ov, (0, 0))
        if self.state == "SCARE":
            n = random.randint(0, 60)
            ov = pygame.Surface(FULL)
            arr = pygame.surfarray.pixels_red(ov)
            arr[:] = random.randint(0, 40)
            del arr
            screen.blit(ov, (0, 0), special_flags=pygame.BLEND_ADD)

    _buf = None
    def draw(self, screen):
        if self.gl is not None:
            # GPU-pad: hele scene in de fragment-shader (flits/korrel zitten daar)
            self.gl.render(self)
            pygame.display.flip()
            return
        # ---- software-fallback (pygame) ----
        screen.fill((0, 0, 0))
        if Grimoria._buf is None:
            Grimoria._buf = [pygame.Surface((W, H)), pygame.Surface((W, H))]
        left, right = Grimoria._buf
        left.fill((3, 5, 4)); right.fill((2, 4, 3))
        self.draw_ghost(left)
        self.draw_orb(right)
        screen.blit(left, (0, 0)); screen.blit(right, (W, 0))
        self.draw_scare_overlay(screen)
        pygame.display.flip()

# ----------------------------------------------------------------web api
grim = None
class API(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        if self.path == "/state":
            return self._send(200, {"state": grim.state, "caption": grim.caption,
                                    "kind_mode": CFG.get("kind_mode", True)})
        return self._send(404, {})
    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send(400, {"error": "bad json"})
        if self.path == "/say":
            events.put(("SAY", data.get("text", "")))
            return self._send(200, {"ok": True})
        if self.path == "/wake":
            events.put(("DING", None)); return self._send(200, {"ok": True})
        if self.path == "/scare":
            events.put(("SCARE", None)); return self._send(200, {"ok": True})
        if self.path == "/mode":
            CFG["kind_mode"] = bool(data.get("kind", True))
            json.dump(CFG, open(f"{BASE}/app/config.json", "w"), indent=2)
            return self._send(200, {"ok": True, "kind_mode": CFG["kind_mode"]})
        return self._send(404, {})
    def log_message(self, *a): pass

# ----------------------------------------------------------------main
def main():
    global grim
    os.environ.setdefault("SDL_VIDEODRIVER", "x11")
    # KERNFIX: SDL minimaliseert fullscreen-vensters bij focusverlies.
    # Iets (lxpanel/desktop) trekt periodiek de focus → grimoria verdween.
    os.environ["SDL_VIDEO_MINIMIZE_ON_FOCUS_LOSS"] = "0"
    pygame.mixer.pre_init(44100, -16, 2, 512)
    pygame.init()
    pygame.mixer.init()
    make_sfx()
    pygame.mouse.set_visible(False)
    # venster: randloos (NOFRAME), NIET WM-fullscreen — openbox resize't
    # fullscreen naar 1 monitor (1920x1080). De guard pint 0,0 3840x1080.
    flags = pygame.NOFRAME | (pygame.OPENGL | pygame.DOUBLEBUF if RENDER_GL else 0)
    screen = pygame.display.set_mode(FULL, flags)
    pygame.display.set_caption("grimoria")
    grim = Grimoria(screen)
    if RENDER_GL and grim.gl is None:
        # GL-context faalde na venster-open: venster opnieuw zonder GL
        screen = pygame.display.set_mode(FULL, pygame.NOFRAME)

    threading.Thread(target=ha_watcher, daemon=True).start()
    threading.Thread(target=cam_watcher, daemon=True).start()
    # poort-bind met retry: vorige instantie kan de poort nog vasthouden
    ThreadingHTTPServer.allow_reuse_address = True
    port = CFG.get("api_port", 8077)
    srv = None
    for poging in range(1, 31):
        try:
            if V2:
                from server import ServerContext, serve as serve_v2
                ctx = ServerContext(webui_dir=f"{BASE}/app/webui",
                                    media_dir=f"{BASE}/media")
                ctx.engine = Engine(CFG, time.time())
                ctx.library = LIB
                ctx.detector = DET
                ctx.get_cfg = lambda: CFG
                ctx.persist_cfg = _persist_cfg
                def _applied(c):
                    CFG.update({k: v for k, v in c.items()
                                if k in DEFAULT_CONFIG})
                ctx.on_cfg_applied = _applied
                # legacy-hooks (bel/say/scare via oude endpoints)
                ctx.legacy = {
                    "say": lambda d: events.put(("SAY", d.get("text", ""))),
                    "wake": lambda: events.put(("DING", None)),
                    "scare": lambda: events.put(("SCARE", None)),
                    "mode": lambda d: None,     # kind via /api/kind
                }
                grim.server_ctx = ctx
                srv = serve_v2(ctx, port)
                log(f"v2-API + webui actief op :{port} "
                    f"(engine-state: {ctx.engine.state_id})")
                break
            srv = ThreadingHTTPServer(("0.0.0.0", port), API)
            break
        except OSError as e:
            log(f"poort {port} nog in gebruik (poging {poging}/30): {e}")
            time.sleep(2)
    if srv is None:
        log(f"poort {port} kon niet gebonden worden — API uit, animatie draait door")
    elif not V2:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    log(f"Grimoria gestart (pid {os.getpid()}) — API op :{port}, "
        f"HA-poll actief, GL={RENDER_GL}, V2={V2}")

    # venster stabiliseren: herstel op t=1,3,7,15s en daarna elke 30s
    guard_momenten = [1, 3, 7, 15]
    volgende_guard = time.time() + 1
    laatste_silent_ok = 0
    clock = pygame.time.Clock()
    while True:
        dt = clock.tick(FPS) / 1000
        nu = time.time()
        if nu >= volgende_guard:
            if guard_momenten:
                enforce_window(f"startup t+{guard_momenten[0]}s")
                volgende_guard = nu + guard_momenten.pop(0)
            else:
                wid = _WIN_ID or find_win_id()
                if wid and not window_ok(wid):
                    enforce_window("periodiek: venster was weg")
                else:
                    if nu - laatste_silent_ok > 600:
                        log(f"venster-guard: ok ({_win_state(wid)})")
                        laatste_silent_ok = nu
                volgende_guard = nu + 30
        for e in pygame.event.get():
            if e.type == pygame.QUIT or (e.type == pygame.KEYDOWN and e.key == pygame.K_ESCAPE):
                pygame.quit(); sys.exit(0)
        try:
            while True:
                ev = events.get_nowait()
                if V2:
                    ctx = grim.server_ctx
                    if ev[0] == "SAY":
                        grim.say(ev[1])
                    elif ev[0] == "SCARE":
                        ok, msg = ctx.goto("jumpscare")
                        if not ok:
                            log(f"SCARE-knop: {msg}")
                    elif ev[0] == "DING":
                        ok, msg = ctx.goto("langzaam")
                        log(f"DING (v2): naar langzaam ({msg})")
                    elif ev[0] == "HOME":
                        naam = {"bas": "Bas", "tanja": "Tanja",
                                "kristel": "Kristel"}.get(ev[1], "")
                        threading.Thread(
                            target=grim.say,
                            args=(f"Welkom thuis, {naam}… de nacht is jong.",),
                            daemon=True).start()
                    else:
                        grim.handle(ev)      # CAM_TRACK: blik
                    continue
                if ev[0] == "SAY":
                    if grim.state in ("SLEEP", "RETURN"):
                        grim.state = "AWAKE"; grim.t0 = time.time()
                    grim.say(ev[1])
                elif ev[0] == "SCARE":
                    grim.state = "SCARE"; grim.t0 = time.time(); grim.scare_frame = 0
                    sfx["scream"].play()
                else:
                    grim.handle(ev)
        except queue.Empty:
            pass
        if V2 and grim.server_ctx is not None:
            grim.update_v2(dt, nu)
        else:
            grim.update(dt)
        grim.draw(screen)

if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        os._exit(1)   # harde exit: voorkomt achtergebleven threads/poort
