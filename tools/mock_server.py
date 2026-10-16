#!/usr/bin/env python3
"""
Grimoria mock-server — uitsluitend Python-stdlib.

Serveert de webinterface (app/webui/) op http://localhost:8088 en
beantwoordt /api/* met realistische nepdata in het vaste contract:

  GET    /api/state          live status (since_secs tikt op, persoon wisselt)
  GET    /api/config         config-json (zonder ha_token)
  PUT    /api/config         config opslaan (in-memory)
  GET    /api/library        {"items": [...]}  (3 fake items)
  POST   /api/library        multipart-upload, veld "file" (+ "wide")
  DELETE /api/library/<id>   item verwijderen -> 204
  POST   /api/goto/<id>      {"ok": true} — zet de nep-fasemachine voort
  POST   /api/kind           {"kind": bool} — kind-mode toggle

Alles leeft in het geheugen; na herstart is de oorspronkelijke fake-data
terug. Starten:

  python3 tools/mock_server.py            # http://localhost:8088
  python3 tools/mock_server.py --port 9000
"""

from __future__ import annotations

import argparse
import copy
import json
import random
import re
import sys
import time
from datetime import datetime, timezone
from email.parser import BytesParser
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

BASE = Path(__file__).resolve().parents[1]   # .../halloween
WEBUI = BASE / "app" / "webui"
MEDIA = BASE / "media"

MAX_UPLOAD = 200 * 1024 * 1024              # ~200 MB, zelfde limiet als de echte API
TOEGESTANE_EXT = {".mp4", ".webm", ".mkv"}

# ---------------------------------------------------------------------------
# Fake-data
# ---------------------------------------------------------------------------

FAKE_CONFIG: dict = {
    "screens": {
        "swap": False,
        "layout": {"mode": "horizontal", "swap": False},
        "offset": {"dx": 0.0, "dy": 0.0},
        "items": [
            {
                "rotation": 0,
                "overlay": {"enabled": False, "text": "", "x": 0.5, "y": 0.85,
                            "size": 48, "color": "#ffaa00"},
            },
            {
                "rotation": 90,
                "overlay": {"enabled": True, "text": "Grimoria kijkt...", "x": 0.5,
                            "y": 0.85, "size": 48, "color": "#ffaa00"},
            },
        ],
    },
    "playback": {"fps": 16, "render_fps": 30, "crossfade": True},
    "motion": {
        "enabled": True,
        "detector": "tflite",
        "model": "ssd_mobilenet_v2.tflite",
        "detect_fps": 3,
        "person_conf": 0.5,
        "person_hold_secs": 3.0,
        "present_memory_secs": 1.0,
        "still_secs": 5.0,
        "fallback_frame_diff": True,
    },
    "timing": {"scare_cooldown": 90},
    "kind_mode": False,
    "states": [
        {
            "id": "rust",
            "name": "Rust",
            "loop": True,
            "canvas_mode": "split",
            "media": {"0": "rust_gezicht", "1": "rust_orb", "wide": ""},
            "advance": {"motion": {"to": "langzaam", "cooldown": 90}, "timeout": None},
        },
        {
            "id": "langzaam",
            "name": "Langzame beweging",
            "loop": True,
            "canvas_mode": "split",
            "media": {"0": "rust_gezicht", "1": "rust_orb", "wide": ""},
            "advance": {"motion": {"to": "jumpscare", "require_motion_secs": 3.0},
                        "timeout": {"secs": 20, "to": "rust"}},
        },
        {
            "id": "jumpscare",
            "name": "Jumpscare",
            "loop": False,
            "canvas_mode": "split",
            "media": {"0": "jump_gezicht", "1": "jump_gezicht", "wide": ""},
            "advance": {"motion": None, "timeout": {"secs": 2.5, "to": "kalm"}},
        },
        {
            "id": "kalm",
            "name": "Kalmeren",
            "loop": True,
            "canvas_mode": "wide",
            "media": {"0": "", "1": "", "wide": "park_breed"},
            "advance": {"motion": None, "timeout": {"secs": 20, "to": "rust"}},
        },
    ],
    "entry_state": "rust",
}

FAKE_LIBRARY: list[dict] = [
    {
        "id": "rust_gezicht",
        "name": "rust — gezicht (81 frames)",
        "wide": False,
        "width": 480,
        "height": 272,
        "frames": 81,
        "fps": 16,
        "duration": 5.06,
        "created": "2026-10-14T18:22:05Z",
    },
    {
        "id": "jump_gezicht",
        "name": "jumpscare — gezicht (98 frames)",
        "wide": False,
        "width": 480,
        "height": 272,
        "frames": 98,
        "fps": 16,
        "duration": 6.13,
        "created": "2026-10-14T18:24:11Z",
    },
    {
        "id": "park_breed",
        "name": "spookpark — breed canvas",
        "wide": True,
        "width": 3840,
        "height": 1080,
        "frames": 120,
        "fps": 16,
        "duration": 7.5,
        "created": "2026-10-15T09:03:47Z",
    },
]


class MockState:
    """In-memory toestand van de nep-backend."""

    def __init__(self) -> None:
        self.config: dict = copy.deepcopy(FAKE_CONFIG)
        self.library: list[dict] = copy.deepcopy(FAKE_LIBRARY)
        self.huidige_fase: str = self.config["entry_state"]
        self.fase_start: float = time.monotonic()
        # nep-persoon: wisselt volgens een vast ritme (6 s aan, 8 s uit)
        self.person_cyclus: float = time.monotonic()
        self.person_present: bool = False
        self.last_seen: str | None = None

    # --- fase ---------------------------------------------------------------
    def goto(self, state_id: str) -> bool:
        if not any(s["id"] == state_id for s in self.config["states"]):
            return False
        self.huidige_fase = state_id
        self.fase_start = time.monotonic()
        return True

    def fase_naam(self) -> str:
        for s in self.config["states"]:
            if s["id"] == self.huidige_fase:
                return s["name"]
        return self.huidige_fase

    # --- persoon ------------------------------------------------------------
    def update_person(self) -> None:
        """Persoon wisselt elke 14 s: 6 s aanwezig, 8 s afwezig."""
        nu = time.monotonic()
        pos = (nu - self.person_cyclus) % 14.0
        self.person_present = pos < 6.0
        if self.person_present:
            self.last_seen = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def api_state(self) -> dict:
        self.update_person()
        conf = round(random.uniform(0.72, 0.95), 2) if self.person_present else None
        return {
            "state": self.huidige_fase,
            "name": self.fase_naam(),
            "since_secs": round(time.monotonic() - self.fase_start, 1),
            "person": {
                "present": self.person_present,
                "last_seen": self.last_seen,
                "conf": conf,
            },
            "detector": self.config.get("motion", {}).get("detector", "tflite"),
            "kind_mode": bool(self.config.get("kind_mode", False)),
        }

    # --- bibliotheek ----------------------------------------------------------
    def nieuw_lib_id(self, filename: str) -> str:
        stam = Path(filename or "film").stem.lower()
        stam = re.sub(r"[^a-z0-9_]+", "_", stam).strip("_") or "film"
        bestaand = {it["id"] for it in self.library}
        if stam not in bestaand:
            return stam
        n = 2
        while f"{stam}_{n}" in bestaand:
            n += 1
        return f"{stam}_{n}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_multipart(body: bytes, content_type: str) -> dict[str, tuple[str | None, bytes]]:
    """Parse een multipart/form-data-body met de stdlib email-parser.

    Geeft {veldnaam: (filename|None, bytes)} terug.
    """
    parser = BytesParser()
    msg = parser.parsebytes(
        b"Content-Type: " + content_type.encode("latin-1")
        + b"\r\nMIME-Version: 1.0\r\n\r\n" + body
    )
    velden: dict[str, tuple[str | None, bytes]] = {}
    if not msg.is_multipart():
        return velden
    for part in msg.get_payload():
        naam = part.get_param("name", header="content-disposition")
        if naam is None:
            continue
        filename = part.get_filename()
        payload = part.get_payload(decode=True)
        if isinstance(payload, str):        # kan niet, maar zeker is zeker
            payload = payload.encode("utf-8", "surrogateescape")
        velden[naam] = (filename, payload or b"")
    return velden


def iso_nu() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# HTTP-handler
# ---------------------------------------------------------------------------

class GrimoriaMockHandler(SimpleHTTPRequestHandler):
    """Statische bestanden uit app/webui + nep-/api-eindpunten."""

    server_version = "GrimoriaMock/1.0"
    mock: MockState  # klasse-attribuut, gezet in main()

    # ---------- antwoord-helpers ----------
    def _json(self, obj, status: int = 200) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _lege(self, status: int = 204) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---------- GET ----------
    def do_GET(self) -> None:
        pad = urlparse(self.path).path
        if pad == "/api/state":
            self._json(self.mock.api_state())
            return
        if pad == "/api/config":
            # ha_token bestaat in de mock sowieso niet; filter toch voor de vorm
            cfg = copy.deepcopy(self.mock.config)
            cfg.pop("ha_token", None)
            self._json(cfg)
            return
        if pad == "/api/library":
            self._json({"items": self.mock.library})
            return
        if pad.startswith("/api/"):
            self._json({"error": "onbekend eindpunt"}, 404)
            return
        # placeholders onder /media/ uit de repo (preview-afbeeldingen)
        if pad.startswith("/media/"):
            self._serve_bestand(MEDIA, pad[len("/media/"):])
            return
        super().do_GET()

    # ---------- PUT ----------
    def do_PUT(self) -> None:
        pad = urlparse(self.path).path
        if pad != "/api/config":
            self._json({"error": "onbekend eindpunt"}, 404)
            return
        try:
            lengte = int(self.headers.get("Content-Length") or 0)
            cfg = json.loads(self.rfile.read(lengte).decode("utf-8"))
        except (ValueError, json.JSONDecodeError) as e:
            self._json({"error": f"ongeldige json: {e}"}, 400)
            return
        if not isinstance(cfg, dict) or not isinstance(cfg.get("states"), list):
            self._json({"error": "config mist 'states'-lijst"}, 400)
            return
        cfg.pop("ha_token", None)  # nooit mee laten komen, zelfs niet bij een test
        self.mock.config = cfg
        # hervat de nep-fasemachine in een geldige fase
        if not self.mock.goto(str(cfg.get("entry_state") or "rust")):
            self.mock.fase_start = time.monotonic()
        self._json({"ok": True, "saved": True})

    # ---------- POST ----------
    def do_POST(self) -> None:
        pad = urlparse(self.path).path

        if pad == "/api/kind":
            try:
                lengte = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(lengte).decode("utf-8"))
            except (ValueError, json.JSONDecodeError):
                body = {}
            kind = bool(body.get("kind"))
            self.mock.config["kind_mode"] = kind
            self._json({"ok": True, "kind_mode": kind})
            return

        m = re.fullmatch(r"/api/goto/([^/]+)", pad)
        if m:
            state_id = unquote(m.group(1))
            if self.mock.goto(state_id):
                self._json({"ok": True})
            else:
                self._json({"error": f"onbekende state '{state_id}'"}, 404)
            return

        if pad == "/api/library":
            self._upload()
            return

        self._json({"error": "onbekend eindpunt"}, 404)

    def _upload(self) -> None:
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype:
            self._json({"error": "verwacht multipart/form-data"}, 400)
            return
        try:
            lengte = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            lengte = 0
        if lengte > MAX_UPLOAD:
            self._json({"error": "bestand groter dan 200 MB"}, 413)
            return
        body = self.rfile.read(lengte)

        velden = parse_multipart(body, ctype)
        if "file" not in velden:
            self._json({"error": "ontbrekend veld 'file'"}, 400)
            return
        filename, data = velden["file"]
        wide = str(velden.get("wide", (None, b"false"))[1].decode("utf-8", "replace")).lower() \
            in ("true", "1", "on", "yes")

        ext = Path(filename or "").suffix.lower()
        if ext not in TOEGESTANE_EXT:
            self._json({"error": f"extensie '{ext}' niet toegestaan (mp4/webm/mkv)"}, 400)
            return

        # fake-extractie: frames afleiden uit de bestandsgrootte
        frames = max(1, len(data) // 2048)
        item = {
            "id": self.mock.nieuw_lib_id(filename or "film.mp4"),
            "name": Path(filename or "film").name,
            "wide": wide,
            "width": 3840 if wide else 480,
            "height": 1080 if wide else 272,
            "frames": frames,
            "fps": 16,
            "duration": round(frames / 16.0, 2),
            "created": iso_nu(),
        }
        self.mock.library.append(item)
        self._json({"ok": True, "item": item}, 201)

    # ---------- DELETE ----------
    def do_DELETE(self) -> None:
        pad = urlparse(self.path).path
        m = re.fullmatch(r"/api/library/([^/]+)", pad)
        if not m:
            self._json({"error": "onbekend eindpunt"}, 404)
            return
        lib_id = unquote(m.group(1))
        voor = len(self.mock.library)
        self.mock.library = [it for it in self.mock.library if it["id"] != lib_id]
        if len(self.mock.library) == voor:
            self._json({"error": f"onbekend bibliotheek-item '{lib_id}'"}, 404)
        else:
            self._lege(204)

    # ---------- statisch met basis buiten webui ----------
    def _serve_bestand(self, basis: Path, rel: str) -> None:
        """Serveer een bestand buiten app/webui (bijv. placeholder-media)."""
        doelpad = (basis / unquote(rel).lstrip("/")).resolve()
        if not str(doelpad).startswith(str(basis.resolve())) or not doelpad.is_file():
            self._json({"error": "niet gevonden"}, 404)
            return
        # de statische server gebruikt self.directory bij het vertalen van
        # self.path; tijdelijk omschakelen naar de repo-basis
        oude_dir = self.directory
        self.directory = str(BASE)
        try:
            self.path = "/" + str(doelpad.relative_to(BASE))
            super().do_GET()
        finally:
            self.directory = oude_dir

    def log_message(self, fmt: str, *args) -> None:  # compacte logs
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))


# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Grimoria mock-server (stdlib)")
    ap.add_argument("--port", type=int, default=8088)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    if not (WEBUI / "index.html").is_file():
        sys.exit(f"index.html niet gevonden in {WEBUI}")

    # mock-state als klasse-attribuut (de handler-basisklassen accepteren
    # geen extra kwargs via partial)
    GrimoriaMockHandler.mock = MockState()
    handler = partial(GrimoriaMockHandler, directory=str(WEBUI))
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"Grimoria mock-server op http://{args.host}:{args.port}/  (Ctrl-C om te stoppen)")
    print(f"  webui : {WEBUI}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\ngestopt")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
