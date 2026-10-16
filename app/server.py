"""Grimoria v2 — HTTP-server: webui + API op één poort (default 8077).

Dient drie doelen:
  1. Statische webinterface (app/webui/) en media bestanden serveren.
  2. API voor de webui: /api/state, /api/config, /api/library, /api/goto, /api/kind.
  3. Legacy-endpoints behouden (/state, /say, /wake, /scare, /mode) zodra de
     integratie die hooks aanlevert.

De server kent de app zelf NIET: hij praat via een ServerContext. Daarmee is hij
apart testbaar (`python3 server.py --port 8078 --config ../config.json` draait een
zelfstandige dev-modus waarin de engine vanzelf doortikt).

Beveiliging:
  - ha_token wordt nóóit teruggegeven (filter_config) en nooit gelogd.
  - Uploads: alleen mp4/webm/mkv, max UPLOAD_MAX bytes, via library.Library.
  - Statische files: realpath-check tegen pad-ontsnapping.
"""

from __future__ import annotations

import email
import json
import os
import re
import shutil
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Zodat dit bestand zowel als `python3 app/server.py` als module werkt.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from schemas import DEFAULT_CONFIG, validate_config  # noqa: E402
from engine import Engine  # noqa: E402
from library import Library, LibraryError, ffmpeg_available  # noqa: E402

UPLOAD_MAX = 200 * 1024 * 1024          # 200 MB
UPLOAD_EXT_OK = (".mp4", ".webm", ".mkv")
STATIC_MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".mp4": "video/mp4",
}
SENSITIVE_KEYS = {"ha_token"}


def filter_config(cfg: dict) -> dict:
    """Kopie van config zonder geheime sleutels (voor de webui)."""
    def clean(node):
        if isinstance(node, dict):
            return {k: clean(v) for k, v in node.items() if k not in SENSITIVE_KEYS}
        if isinstance(node, list):
            return [clean(x) for x in node]
        return node
    return clean(cfg)


def parse_multipart(content_type: str, body: bytes) -> dict:
    """Minimale multipart/form-data-parser (cgi bestaat niet meer in 3.13+).

    Geeft terug: {veldnaam: {"filename": str|None, "data": bytes}}.
    """
    msg = email.message_from_bytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode()
        + body
    )
    out = {}
    for part in msg.get_payload() if msg.is_multipart() else []:
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        out[name] = {
            "filename": part.get_filename(),
            "data": part.get_payload(decode=True) or b"",
        }
    return out


class ServerContext:
    """Koppelpunt tussen server en de draaiende app.

    De app (grimoria_pi.py) vult de callbacks/attributen in; in dev-modus doet
    server.py dat zelf met een zelftikende engine.
    """

    def __init__(self, webui_dir: str, media_dir: str | None = None):
        self.webui_dir = os.path.abspath(webui_dir)
        self.media_dir = os.path.abspath(media_dir) if media_dir else None
        self.engine: Engine | None = None          # verplicht vóór serve()
        self.library: Library | None = None        # verplicht vóór uploads
        self.detector = None                       # object met .backend
        self.person_present = False
        self.person_conf = None
        self.person_last_seen = None               # epoch seconds
        # callbacks (door de app ingevuld):
        self.get_cfg = lambda: {}                  # VOLLEDIGE config (met token)
        self.persist_cfg = lambda cfg: None        # config weg schrijven
        self.on_cfg_applied = lambda cfg: None     # hot-apply in renderer/app
        self.legacy = {}                           # {"say":fn,"wake":fn,"scare":fn,"mode":fn}
        self.lock = threading.RLock()

    # ---------- helpers ----------

    def _states(self) -> dict:
        return {s["id"]: s for s in self.get_cfg().get("states", [])}

    def _state_payload(self) -> dict:
        eng = self.engine
        return {
            "state": eng.state_id,
            "name": eng.state_name,
            "since_secs": round(max(0.0, self._now() - eng.since), 1),
            "person": {
                "present": bool(self.person_present),
                "last_seen": (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(
                    self.person_last_seen)) if self.person_last_seen else None),
                "conf": self.person_conf,
            },
            "detector": getattr(self.detector, "backend", "onbekend"),
            "kind_mode": bool(eng.kind_mode),
        }

    @staticmethod
    def _now() -> float:
        return time.time()

    def goto(self, state_id: str) -> tuple[bool, str]:
        if state_id not in self._states():
            return False, f"onbekende state: {state_id}"
        self.engine.goto(state_id, self._now())
        return True, "ok"

    def set_kind(self, kind: bool) -> None:
        self.engine.kind_mode = bool(kind)
        cfg = self.get_cfg()
        cfg["kind_mode"] = bool(kind)
        self.persist_cfg(cfg)

    def apply_config(self, new_public_cfg: dict) -> tuple[bool, list[str]]:
        """Valideer + samenvoegen + wegschrijven + hot-apply.

        De webui stuurt de config ZONDER token; we mergen op de volledige
        in-memory config zodat secrets bewaard blijven.
        """
        with self.lock:
            full = self.get_cfg()
            merged = json.loads(json.dumps(full))  # deep copy
            for key, val in new_public_cfg.items():
                merged[key] = val
            clean, errors = validate_config(merged)
            if errors:
                return False, errors
            # secrets + onbekende app-keys terugzetten vanuit het origineel
            for key in SENSITIVE_KEYS:
                if key in full:
                    clean[key] = full[key]
            self.persist_cfg(clean)
            # engine opnieuw opzetten als de states veranderen; anders alleen params
            old_ids = [s["id"] for s in full.get("states", [])]
            new_ids = [s["id"] for s in clean.get("states", [])]
            if old_ids != new_ids:
                cur = self.engine.state_id
                self.engine = Engine(clean, self._now())
                if cur in new_ids:
                    self.engine.goto(cur, self._now(), reason="init")
            else:
                self.engine.kind_mode = bool(clean.get("kind_mode", False))
            self.on_cfg_applied(clean)
            return True, []


class GrimoriaHandler(BaseHTTPRequestHandler):
    ctx: ServerContext = None  # wordt door serve() gezet

    # ---------- basis ----------

    def log_message(self, fmt, *args):  # rustig logboek
        pass

    def _json(self, obj, code: int = 200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes | None:
        n = int(self.headers.get("Content-Length") or 0)
        if n > UPLOAD_MAX:
            self._json({"error": "body te groot"}, 413)
            return None
        return self.rfile.read(n) if n else b""

    def _static(self, rel: str) -> None:
        base = self.ctx.webui_dir if not rel.startswith("media/") else self.ctx.media_dir
        if base is None:
            self._json({"error": "not found"}, 404)
            return
        path = os.path.realpath(os.path.join(base, rel[len("media/"):] if rel.startswith("media/") else rel))
        if not path.startswith(base) or not os.path.isfile(path):
            self._json({"error": "not found"}, 404)
            return
        ext = os.path.splitext(path)[1].lower()
        with open(path, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", STATIC_MIME.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---------- routing ----------

    def do_GET(self):
        ctx = self.ctx
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        try:
            if path == "/api/state":
                return self._json(ctx._state_payload())
            if path == "/api/config":
                return self._json(filter_config(ctx.get_cfg()))
            if path == "/api/library":
                items = ctx.library.list() if ctx.library else []
                return self._json({"items": items, "ffmpeg": ffmpeg_available()})
            if path.startswith("/webui/"):
                return self._static(path[len("/webui/"):])
            if path.startswith("/media/"):
                return self._static(path[1:])
            if path == "/":
                return self._static("index.html")
            if path == "/state":  # legacy
                return self._json(ctx._state_payload())
            return self._json({}, 404)
        except Exception as exc:  # nooit een crash naar de client lekken
            return self._json({"error": str(exc)}, 500)

    def do_PUT(self):
        ctx = self.ctx
        path = self.path.split("?", 1)[0].rstrip("/")
        try:
            if path == "/api/config":
                body = self._read_body()
                if body is None:
                    return
                try:
                    data = json.loads(body.decode("utf-8") or "{}")
                except (ValueError, UnicodeDecodeError):
                    return self._json({"error": "ongeldige json"}, 400)
                ok, errors = ctx.apply_config(data)
                return self._json({"ok": ok, "errors": errors}, 200 if ok else 400)
            return self._json({}, 404)
        except Exception as exc:
            return self._json({"error": str(exc)}, 500)

    def do_POST(self):
        ctx = self.ctx
        path = self.path.split("?", 1)[0].rstrip("/")
        try:
            if path == "/api/library":
                return self._upload()
            if path == "/api/kind":
                body = self._read_body()
                if body is None:
                    return
                try:
                    kind = bool(json.loads(body.decode() or "{}").get("kind"))
                except (ValueError, UnicodeDecodeError):
                    return self._json({"error": "ongeldige json"}, 400)
                ctx.set_kind(kind)
                return self._json({"ok": True, "kind_mode": bool(ctx.engine.kind_mode)})
            m = re.fullmatch(r"/api/goto/([A-Za-z0-9_\-]+)", path)
            if m:
                ok, msg = ctx.goto(m.group(1))
                return self._json({"ok": ok, "msg": msg}, 200 if ok else 404)
            if path == "/say" and "say" in ctx.legacy:      # legacy hooks
                body = self._read_body() or b""
                ctx.legacy["say"](json.loads(body.decode() or "{}"))
                return self._json({"ok": True})
            if path == "/wake" and "wake" in ctx.legacy:
                ctx.legacy["wake"]()
                return self._json({"ok": True})
            if path == "/scare" and "scare" in ctx.legacy:
                ctx.legacy["scare"]()
                return self._json({"ok": True})
            if path == "/mode" and "mode" in ctx.legacy:
                body = self._read_body() or b""
                ctx.legacy["mode"](json.loads(body.decode() or "{}"))
                return self._json({"ok": True})
            return self._json({}, 404)
        except Exception as exc:
            return self._json({"error": str(exc)}, 500)

    def do_DELETE(self):
        ctx = self.ctx
        path = self.path.split("?", 1)[0].rstrip("/")
        try:
            m = re.fullmatch(r"/api/library/([A-Za-z0-9_\-]+)", path)
            if m:
                if ctx.library is None:
                    return self._json({"error": "geen bibliotheek"}, 500)
                try:
                    ctx.library.delete(m.group(1))
                except LibraryError:
                    return self._json({"error": "onbekend item"}, 404)
                return self._json({"ok": True}, 200)
            return self._json({}, 404)
        except Exception as exc:
            return self._json({"error": str(exc)}, 500)

    # ---------- upload ----------

    def _upload(self):
        ctx = self.ctx
        if ctx.library is None:
            return self._json({"error": "geen bibliotheek"}, 500)
        if not ffmpeg_available():
            return self._json({"error": "ffmpeg ontbreekt op deze machine"}, 503)
        ctype = self.headers.get("Content-Type", "")
        if not ctype.startswith("multipart/form-data"):
            return self._json({"error": "verwacht multipart/form-data"}, 400)
        body = self._read_body()
        if body is None:
            return
        parts = parse_multipart(ctype, body)
        file_part = parts.get("file")
        if not file_part or not file_part["filename"]:
            return self._json({"error": "ontbrekend veld 'file'"}, 400)
        fname = file_part["filename"]
        if not fname.lower().endswith(UPLOAD_EXT_OK):
            return self._json({"error": "alleen mp4/webm/mkv toegestaan"}, 400)
        wide = (parts.get("wide", {}).get("data", b"") in (b"true", b"1", b"on"))
        try:
            item = ctx.library.add_from_bytes(fname, file_part["data"], wide=wide)
        except Exception as exc:
            return self._json({"error": f"extractie mislukt: {exc}"}, 500)
        return self._json({"ok": True, "item": item}, 201)


def serve(ctx: ServerContext, port: int, bind_retries: int = 30) -> ThreadingHTTPServer:
    """Start de server; wacht geduldig op een vrije poort (patroon uit v1)."""
    GrimoriaHandler.ctx = ctx
    httpd = None
    for attempt in range(bind_retries):
        try:
            httpd = ThreadingHTTPServer(("0.0.0.0", port), GrimoriaHandler)
            break
        except OSError:
            if attempt == bind_retries - 1:
                raise
            time.sleep(2)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


# ---------------------------------------------------------------------------
# Dev-modus: zelfstandig draaien zonder de volledige app (voor tests).
# ---------------------------------------------------------------------------

def _dev_main(argv: list[str]) -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Grimoria server (dev-modus)")
    ap.add_argument("--port", type=int, default=8078)
    ap.add_argument("--config", default=os.path.join(os.path.dirname(__file__), "config.json"))
    ap.add_argument("--webui", default=os.path.join(os.path.dirname(__file__), "webui"))
    ap.add_argument("--media", default=None, help="media-map om te serveren onder /media/")
    ap.add_argument("--library", default=None, help="bibliotheek-map (default <repo>/media/library)")
    args = ap.parse_args(argv)

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg_path = args.config
    if os.path.isfile(cfg_path):
        with open(cfg_path, encoding="utf-8") as fh:
            cfg = json.load(fh)
        print(f"config geladen: {cfg_path}")
    else:
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        print("geen config gevonden — DEFAULT_CONFIG gebruikt")

    clean, errors = validate_config(cfg)
    if errors:
        print("config-waarschuwingen:", errors)
    cfg.update({k: v for k, v in clean.items() if k in DEFAULT_CONFIG})  # v2-keys genormaliseerd

    ctx = ServerContext(webui_dir=args.webui, media_dir=args.media or os.path.join(repo_root, "media"))
    ctx.engine = Engine(cfg, time.time())
    lib_dir = args.library or os.path.join(repo_root, "media", "library")
    ctx.library = Library(lib_dir)
    ctx.get_cfg = lambda: cfg
    ctx.persist_cfg = lambda c: (cfg.update(c), _atomic_write(cfg_path, c)) and None
    ctx.on_cfg_applied = lambda c: cfg.update(c)

    # engine zelf laten doortikken (zonder echte camera: persoon = uit)
    def _ticker():
        while True:
            time.sleep(0.2)
            try:
                ctx.engine.tick(time.time(), False)
            except Exception as exc:
                print("tick-fout:", exc)
    threading.Thread(target=_ticker, daemon=True).start()

    print(f"Grimoria dev-server op http://0.0.0.0:{args.port} (state: {ctx.engine.state_id})")
    serve(ctx, args.port, bind_retries=1)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


def _atomic_write(path: str, cfg: dict) -> None:
    if not path or not os.path.isfile(path):
        return  # in dev zonder config-bestand niet wegschrijven
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2, ensure_ascii=False)
    shutil.move(tmp, path)


if __name__ == "__main__":
    _dev_main(sys.argv[1:])
