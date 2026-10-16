#!/usr/bin/env python3
"""
Grimoria v2 — API + webui-server (poort 8077).

Zelfstandig uitbreidbaar vanuit grimoria_pi.py: alle toestand komt binnen via
één context-object (ApiContext), er is géén globale state. Integratie in de
bestaande main-loop:

    from api_v2 import ApiContext, make_server
    ctx = ApiContext(
        base_dir=BASE + "/app",
        config_path=BASE + "/app/config.json",
        config_getter=lambda: dict(CFG),          # volledige config (mét filter)
        config_setter=_schrijf_config_atomair,    # schrijft CFG weg
        engine_getter=lambda: engine,             # Engine uit engine.py (of None)
        library=library,                          # module library.py
    )
    srv = make_server(ctx, port=CFG.get("api_port", 8077))
    threading.Thread(target=srv.serve_forever, daemon=True).start()

Endpoints (contract uit docs/ontwerp-grimoria-webinterface.md):
  GET    /                    webui/index.html
  GET    /webui/<bestand>     statische bestanden (path-traversal-beschermd)
  GET    /api/state           {state, name, since_secs, motion:{last_seen, active}, kind_mode}
  GET    /api/config          config, gefilterd (geen ha_token of andere token-velden)
  PUT    /api/config          valideert, schrijft atomisch, geeft opgeschoonde config
  GET    /api/library         bibliotheeklijst
  POST   /api/library         multipart-upload → ffmpeg-extractie (max 200 MB)
  DELETE /api/library/<id>    media verwijderen
  POST   /api/goto/<state_id> handmatige trigger
  POST   /api/kind            {"kind": bool} kind_mode-toggle

Geen auth (LAN-speelgoed), maar wél: nooit secrets in responses, body-limieten
overal, geen shell-uitvoering, geen chunked encoding.
"""

from __future__ import annotations

import functools
import html
import json
import mimetypes
import os
import re
import shutil
import tempfile
import time
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional

MAX_JSON_BYTES = 64 * 1024
MAX_UPLOAD_BYTES = 200 * 1024 * 1024          # 200 MB, gelijk aan library.py
_SECRET_KEYS = {"ha_token"}
_SECRET_HINTS = ("token", "secret", "password", "api_key", "apikey")
_REDACTED = "[gefilterd]"                     # herkenbare round-trip-marker
_UPLOAD_CHUNK = 64 * 1024


class _HTTPError(Exception):
    """Intern afbreken van een request met een nette JSON-fout."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class ApiContext:
    """Alle toestand die de server nodig heeft. Aanroeper (grimoria_pi.py)
    levert getters/setters; de handler kent geen globals."""
    base_dir: str                                   # app-map met webui/
    config_path: str                                # app/config.json
    config_getter: Callable[[], dict]               # volledige config (kopie!)
    config_setter: Callable[[dict], Any]            # schrijft config atomisch
    engine_getter: Optional[Callable[[], Any]] = None   # -> engine.Engine | None
    library: Any = None                             # module library.py (of None)
    on_config_written: Optional[Callable[[dict], Any]] = None  # hot-apply hook
    extra: dict = field(default_factory=dict)       # vrij veld voor de app


def make_server(ctx: ApiContext, host: str = "0.0.0.0", port: int = 8077):
    """Bouw een (nog niet startende) ThreadingHTTPServer. Start met
    threading.Thread(target=srv.serve_forever, daemon=True).start()."""
    handler = functools.partial(_SecFileHandler, ctx)
    ThreadingHTTPServer.allow_reuse_address = True
    ThreadingHTTPServer.daemon_threads = True
    return ThreadingHTTPServer((host, port), handler)


# ============================================================ helpers
def _scrub(obj: Any) -> Any:
    """Verwijder alle token-achtige velden (recursief) voor API-responses."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            kl = str(k).lower()
            if kl in _SECRET_KEYS or any(h in kl for h in _SECRET_HINTS):
                out[k] = _REDACTED if v not in (None, "", [], {}) else v
            else:
                out[k] = _scrub(v)
        return out
    if isinstance(obj, list):
        return [_scrub(v) for v in obj]
    return obj


def _restore_secrets(new: Any, old: Any) -> None:
    """Als de UI een gefilterde waarde ongewijzigd terugstuurt, herstel dan de
    originele secret (de UI kan immers het echte token nooit zien)."""
    if isinstance(new, dict) and isinstance(old, dict):
        for k, v in new.items():
            kl = str(k).lower()
            sensitive = kl in _SECRET_KEYS or any(h in kl for h in _SECRET_HINTS)
            if sensitive and v == _REDACTED:
                new[k] = old.get(k)
            else:
                _restore_secrets(v, old.get(k) if isinstance(old, dict) else None)
    elif isinstance(new, list) and isinstance(old, list):
        for i in range(min(len(new), len(old))):
            _restore_secrets(new[i], old[i])


def _num(v: Any, wat: str) -> None:
    if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float))):
        raise _HTTPError(400, f"{wat}: getal verwacht")


def _validate_config(data: Any) -> None:
    """Lichte structuurvalidatie van een PUT /api/config-body (dict)."""
    if not isinstance(data, dict):
        raise _HTTPError(400, "config moet een JSON-object zijn")
    for sect in ("motion", "timing", "playback", "screens", "ha_entities", "camera"):
        if sect in data and not isinstance(data[sect], dict):
            raise _HTTPError(400, f"'{sect}' moet een object zijn")
    if "kind_mode" in data and not isinstance(data["kind_mode"], bool):
        raise _HTTPError(400, "'kind_mode' moet boolean zijn")
    if "entry_state" in data and not isinstance(data["entry_state"], str):
        raise _HTTPError(400, "'entry_state' moet een string zijn")
    states = data.get("states")
    if states is not None:
        if not isinstance(states, list) or not states:
            raise _HTTPError(400, "'states' moet een niet-lege lijst zijn")
        for st in states:
            if not isinstance(st, dict):
                raise _HTTPError(400, "elke state moet een object zijn")
            sid = st.get("id")
            if not sid or not isinstance(sid, str):
                raise _HTTPError(400, "elke state heeft niet-lege string 'id' nodig")
            media = st.get("media")
            if media is not None:
                if not isinstance(media, dict):
                    raise _HTTPError(400, f"state {sid!r}: 'media' moet een object zijn")
                for scr, mid in media.items():
                    if mid is not None and not isinstance(mid, str):
                        raise _HTTPError(400, f"state {sid!r}: media[{scr!r}] moet string of null zijn")
            adv = st.get("advance")
            if adv is not None:
                if not isinstance(adv, dict):
                    raise _HTTPError(400, f"state {sid!r}: 'advance' moet object of null zijn")
                motion = adv.get("motion")
                if motion is not None:
                    if not isinstance(motion, dict):
                        raise _HTTPError(400, f"state {sid!r}: advance.motion moet object of null zijn")
                    _num(motion.get("require_motion_secs"), f"state {sid!r}: require_motion_secs")
                    _num(motion.get("cooldown"), f"state {sid!r}: cooldown")
                    if "to" in motion and not isinstance(motion.get("to"), str):
                        raise _HTTPError(400, f"state {sid!r}: advance.motion.to moet string zijn")
                timeout = adv.get("timeout")
                if timeout is not None:
                    if not isinstance(timeout, dict):
                        raise _HTTPError(400, f"state {sid!r}: advance.timeout moet object of null zijn")
                    _num(timeout.get("secs"), f"state {sid!r}: timeout.secs")
                    if not isinstance(timeout.get("to"), str):
                        raise _HTTPError(400, f"state {sid!r}: advance.timeout.to moet string zijn")
        ids = {st.get("id") for st in states}
        if data.get("entry_state") and data["entry_state"] not in ids:
            raise _HTTPError(400, f"entry_state {data['entry_state']!r} bestaat niet in states")


def _write_json_atomic(path: str, obj: dict) -> None:
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".cfg-", dir=d)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ============================================================ handler
class _SecFileHandler(BaseHTTPRequestHandler):
    server_version = "Grimoria/2.0"
    protocol_version = "HTTP/1.1"        # keep-alive; we sturen altijd Content-Length
    timeout = 60

    # ctx wordt als extra positioneel argument door functools.partial meegegeven
    def __init__(self, ctx: ApiContext, *args, **kwargs):
        self.ctx = ctx
        super().__init__(*args, **kwargs)

    # -------------------------------------------------- transport
    def log_message(self, fmt, *args):   # rustig op de Pi-console
        pass

    def send_error(self, code, message=None, explain=None):
        # BaseHTTPRequestHandler stuurt anders HTML; wij willen JSON
        try:
            self._send_json(code, {"error": html.escape(str(message or explain or code))})
        except Exception:
            pass

    def _send_json(self, code: int, obj: Any) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_bytes(self, code: int, ctype: str, body: bytes, cache: bool = False) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if cache:
            self.send_header("Cache-Control", "max-age=60")
        else:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _read_body(self, limit: int = MAX_JSON_BYTES) -> bytes:
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            raise _HTTPError(400, "chunked transfer niet ondersteund")
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise _HTTPError(400, "ongeldige Content-Length")
        if n < 0:
            raise _HTTPError(400, "ongeldige Content-Length")
        if n > limit:
            raise _HTTPError(413, f"body te groot (limiet {limit} bytes)")
        data = bytearray()
        while len(data) < n:
            chunk = self.rfile.read(min(_UPLOAD_CHUNK, n - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        return bytes(data)

    def _json_body(self) -> dict:
        raw = self._read_body(MAX_JSON_BYTES)
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise _HTTPError(400, f"ongeldige JSON: {e}")
        if not isinstance(data, dict):
            raise _HTTPError(400, "JSON-object verwacht")
        return data

    # -------------------------------------------------- dispatch
    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("GET")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _dispatch(self, meth: str):
        try:
            parsed = urllib.parse.urlsplit(self.path)
            path = urllib.parse.unquote(parsed.path)
            if path.startswith("/api/"):
                self._api(meth, path)
            elif meth in ("GET", "HEAD"):
                self._serve_static(path)
            else:
                raise _HTTPError(404, "onbekend pad")
        except _HTTPError as e:
            self._send_json(e.code, {"error": e.message})
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as e:            # nooit een stacktrace naar de client
            self._send_json(500, {"error": f"interne fout: {type(e).__name__}"})

    # -------------------------------------------------- router
    def _api(self, meth: str, path: str):
        if path == "/api/state" and meth == "GET":
            return self._api_state()
        if path == "/api/config":
            if meth == "GET":
                return self._api_config_get()
            if meth == "PUT":
                return self._api_config_put()
            raise _HTTPError(405, "methode niet toegestaan")
        if path == "/api/library":
            if meth == "GET":
                return self._api_library_get()
            if meth == "POST":
                return self._api_library_post()
            raise _HTTPError(405, "methode niet toegestaan")
        m = re.fullmatch(r"/api/library/([0-9a-z-]+)", path)
        if m:
            if meth == "DELETE":
                return self._api_library_delete(m.group(1))
            raise _HTTPError(405, "methode niet toegestaan")
        m = re.fullmatch(r"/api/goto/([A-Za-z0-9_-]+)", path)
        if m:
            if meth == "POST":
                return self._api_goto(m.group(1))
            raise _HTTPError(405, "methode niet toegestaan")
        if path == "/api/kind":
            if meth == "POST":
                return self._api_kind()
            raise _HTTPError(405, "methode niet toegestaan")
        raise _HTTPError(404, "onbekend API-endpoint")

    # -------------------------------------------------- engine helper
    def _engine(self):
        eng = self.ctx.engine_getter() if self.ctx.engine_getter else None
        if eng is None:
            raise _HTTPError(503, "engine niet actief")
        return eng

    # -------------------------------------------------- endpoints
    def _api_state(self):
        eng = self._engine()
        self._send_json(200, {
            "state": eng.state_id,
            "name": eng.name,
            "since_secs": round(max(0.0, eng.since_secs), 2),
            "motion": {"last_seen": eng.last_motion_ts, "active": eng.motion_active},
            "kind_mode": eng.kind_mode,
        })

    def _api_config_get(self):
        cfg = self.ctx.config_getter()
        self._send_json(200, _scrub(cfg))

    def _api_config_put(self):
        data = self._json_body()
        _validate_config(data)
        full = self.ctx.config_getter()
        _restore_secrets(data, full)              # round-trip-marker terugzetten
        merged = {**full, **data}
        if self.ctx.config_setter:
            try:
                self.ctx.config_setter(merged)
            except Exception as e:
                raise _HTTPError(500, f"config wegschrijven mislukt: {e}")
        # lichte hot-apply: engine-directe velden direct doorzetten
        eng = None
        try:
            eng = self.ctx.engine_getter() if self.ctx.engine_getter else None
        except Exception:
            eng = None
        if eng is not None:
            try:
                if "kind_mode" in data:
                    eng.kind_mode = bool(merged.get("kind_mode"))
                if "states" in data or "entry_state" in data:
                    eng.load(merged)
            except Exception:
                pass                               # app doet eigen reload/hook
        if self.ctx.on_config_written:
            try:
                self.ctx.on_config_written(merged)
            except Exception:
                pass
        self._send_json(200, {"ok": True, "config": _scrub(merged)})

    def _api_library_get(self):
        if self.ctx.library is None:
            raise _HTTPError(503, "bibliotheek niet actief")
        self._send_json(200, {"items": self.ctx.library.list_media()})

    def _api_library_post(self):
        lib = self.ctx.library
        if lib is None:
            raise _HTTPError(503, "bibliotheek niet actief")
        ctype = self.headers.get("Content-Type", "")
        if not ctype.lower().startswith("multipart/form-data"):
            raise _HTTPError(400, "Content-Type moet multipart/form-data zijn")
        m = re.search(r'boundary="?([^";]+)"?', ctype, re.IGNORECASE)
        if not m:
            raise _HTTPError(400, "multipart-boundary ontbreekt")
        tmp_path, filename = self._recv_multipart(m.group(1).encode(), MAX_UPLOAD_BYTES)
        try:
            try:
                mid = lib.register_upload(filename, tmp_path)
            finally:
                if os.path.exists(tmp_path):
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
            try:
                meta = lib.extract(mid)
            except Exception:
                # mislukte extractie: netjes opruimen (geen half werk in de lijst)
                try:
                    lib.delete(mid)
                except Exception:
                    pass
                raise
        except TypeError as e:                    # library.LibraryError is niet hier
            raise _HTTPError(400, str(e))
        except Exception as e:
            naam = type(e).__name__
            msg = str(e)
            if naam == "ExternalToolError" or "niet beschikbaar" in msg:
                raise _HTTPError(503, msg)
            raise _HTTPError(400, msg)
        self._send_json(201, {"ok": True, "id": mid, "media": meta,
                              "items": lib.list_media()})

    def _api_library_delete(self, mid: str):
        if self.ctx.library is None:
            raise _HTTPError(503, "bibliotheek niet actief")
        try:
            self.ctx.library.delete(mid)
        except Exception as e:
            naam = type(e).__name__
            if naam == "LibraryError" and "bestaat niet" in str(e):
                raise _HTTPError(404, str(e))
            if naam == "LibraryError":
                raise _HTTPError(400, str(e))
            raise
        self._send_json(200, {"ok": True, "id": mid})

    def _api_goto(self, state_id: str):
        eng = self._engine()
        changed = eng.goto(state_id, time.time(), force=True)
        if not changed and eng.state_id != state_id:
            raise _HTTPError(404, f"state {state_id!r} bestaat niet")
        self._send_json(200, {"ok": True, "state": eng.state_id,
                              "name": eng.name, "changed": changed})

    def _api_kind(self):
        eng = self._engine()
        data = self._json_body()
        if "kind" in data:
            val = data["kind"]
        elif "kind_mode" in data:
            val = data["kind_mode"]
        elif not data:
            val = not eng.kind_mode               # lege body = toggle
        else:
            raise _HTTPError(400, "verwacht {\"kind\": bool}")
        if not isinstance(val, bool):
            raise _HTTPError(400, "'kind' moet boolean zijn")
        eng.kind_mode = val
        # persistenteren: via de bestaande config-pad (best-effort)
        try:
            cfg = self.ctx.config_getter()
            if cfg.get("kind_mode") != val:
                cfg["kind_mode"] = val
                if self.ctx.config_setter:
                    self.ctx.config_setter(cfg)
        except Exception:
            pass
        self._send_json(200, {"ok": True, "kind_mode": val})

    # -------------------------------------------------- multipart (licht, streamend)
    def _recv_multipart(self, boundary: bytes, limit: int):
        """Stroomt de multipart-body naar een tmp-bestand zonder alles in
        geheugen te hebben. Retourneert (tmp_path, filename) van het eerste
        bestandsdeel. Gooit _HTTPError(413) boven de limiet."""
        sep = b"\r\n--" + boundary
        fd, tmp_path = tempfile.mkstemp(prefix="grimoria-upload-", suffix=".tmp")
        f = os.fdopen(fd, "wb")
        filename = None
        received = 0
        try:
            # eerste boundary-regel
            buf = b""
            while b"\r\n" not in buf:
                chunk = self.rfile.read1(4096)   # read1: niet wachten op 4096 bytes
                if not chunk:
                    raise _HTTPError(400, "onverwacht einde van multipart-body")
                buf += chunk
            first, _, buf = buf.partition(b"\r\n")
            if first.strip() != b"--" + boundary:
                raise _HTTPError(400, "multipart: opening-boundary ontbreekt")

            while True:
                # part-headers lezen
                while b"\r\n\r\n" not in buf:
                    if len(buf) > 64 * 1024:
                        raise _HTTPError(400, "multipart-headers te groot")
                    chunk = self.rfile.read1(4096)
                    if not chunk:
                        raise _HTTPError(400, "onverwacht einde van multipart-body")
                    buf += chunk
                raw_headers, _, buf = buf.partition(b"\r\n\r\n")
                disp = ""
                for line in raw_headers.split(b"\r\n"):
                    if line[:20].lower() == b"content-disposition:":
                        disp = line.decode("utf-8", "replace")
                is_file = 'filename="' in disp or "filename=UTF-8''" in disp.lower()
                fname = ""
                m = re.search(r'filename="((?:[^"\\]|\\.)*)"', disp)
                if m:
                    fname = m.group(1).replace('\\"', '"')
                else:
                    m = re.search(r"filename\*=UTF-8''([^;]+)", disp, re.IGNORECASE)
                    if m:
                        fname = urllib.parse.unquote(m.group(1))
                if is_file and fname:
                    if filename is not None:
                        raise _HTTPError(400, "meerdere bestandsdelen niet ondersteund")
                    filename = os.path.basename(fname)
                    writing = True
                else:
                    writing = False                # form-veld: alleen consumeren

                # part-inhoud tot volgende boundary
                while True:
                    idx = buf.find(sep)
                    if idx >= 0:
                        if writing:
                            f.write(buf[:idx])
                            received += idx
                        rest = buf[idx + len(sep):]
                        if rest.startswith(b"--"):
                            buf = b""              # laatste part
                            done = True
                            break
                        if rest.startswith(b"\r\n"):
                            buf = rest[2:]
                            done = False
                            break
                        raise _HTTPError(400, "multipart: malformed boundary")
                    keep = len(sep) + 4
                    if len(buf) > keep:
                        deel = buf[:-keep]
                        if writing:
                            f.write(deel)
                            received += len(deel)
                            if received > limit:
                                raise _HTTPError(
                                    413, f"upload te groot (limiet {limit} bytes)")
                        buf = buf[-keep:]
                    chunk = self.rfile.read1(_UPLOAD_CHUNK)
                    if not chunk:
                        raise _HTTPError(400, "onverwacht einde van multipart-body")
                    buf += chunk
                if done:
                    break
            if not filename:
                raise _HTTPError(400, "geen bestandsdeel (filename) gevonden")
            if received == 0:
                raise _HTTPError(400, "leeg bestand")
        except Exception:
            try:
                f.close()
            except Exception:
                pass
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        f.flush()
        os.fsync(f.fileno())
        f.close()
        return tmp_path, filename

    # -------------------------------------------------- statisch
    def _serve_static(self, path: str):
        root = os.path.join(self.ctx.base_dir, "webui")
        if path in ("/", "/index.html", "/webui/", "/webui/index.html"):
            rel = "index.html"
        elif path.startswith("/webui/"):
            rel = path[len("/webui/"):]
        else:
            raise _HTTPError(404, "niet gevonden")
        full = os.path.realpath(os.path.join(root, rel))
        root_real = os.path.realpath(root)
        if full != root_real and not full.startswith(root_real + os.sep):
            raise _HTTPError(403, "verboden pad")          # path-traversal
        if not os.path.isfile(full):
            raise _HTTPError(404, "niet gevonden")
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",
                                                  "application/json"):
            ctype += "; charset=utf-8"
        try:
            with open(full, "rb") as fh:
                body = fh.read()
        except OSError:
            raise _HTTPError(500, "bestand onleesbaar")
        self._send_bytes(200, ctype, body, cache=not full.endswith("index.html"))
