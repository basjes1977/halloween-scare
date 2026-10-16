#!/usr/bin/env python3
"""Grimoria v2 — filmbibliotheek.

Beheert <base_dir>/<id>/frames/f%04d.jpg + metadata in <base_dir>/library.json
onder een fcntl-lock (tegen races tussen server-threads/processen).

ffmpeg-extractie: split-media 480x272, wide-media 1920x540 (beide 16 fps,
jpeg q=2, -q:v 2). ffmpeg-afwezigheid geeft LibraryError; de unittests
skippen graceful als er geen ffmpeg op de machine staat.

Gebruik (server-integratie later centraal):
    lib = Library("/mnt/media/halloween/media/library")
    item = lib.add_from_bytes("Film.mp4", data, wide=False)
    lib.list() / lib.delete(id) / lib.frames(id)
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time

__all__ = ["Library", "LibraryError", "ffmpeg_available", "slugify"]

ALLOWED_EXT = ("mp4", "webm", "mkv")
DEFAULT_FPS = 16
SPLIT_W, SPLIT_H = 480, 272
WIDE_W, WIDE_H = 1920, 540
DEFAULT_MAX_FRAMES = 600
FFMPEG_TIMEOUT = 1800                     # s
_META_NAME = "library.json"


class LibraryError(Exception):
    """Verwijderbare fout (bad request / conflict) — server geeft 4xx."""


def ffmpeg_available() -> bool:
    """Is er een bruikbare ffmpeg op PATH?"""
    try:
        p = subprocess.run(["ffmpeg", "-version"], capture_output=True,
                           timeout=15)
        return p.returncode == 0
    except Exception:
        return False


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "film").lower()).strip("-")
    return slug[:40] or "film"


class Library:
    """Media-bibliotheek: <base_dir>/<id>/frames/f%04d.jpg met een centrale
    library.json (atomair geschreven onder fcntl-lock)."""

    def __init__(self, base_dir: str, max_frames: int = DEFAULT_MAX_FRAMES):
        self._base = os.path.abspath(base_dir)
        self._max_frames = max(1, int(max_frames))
        os.makedirs(self._base, exist_ok=True)

    # ------------------------------------------------------------ paden
    @property
    def base_dir(self) -> str:
        return self._base

    def item_dir(self, mid: str) -> str:
        return os.path.join(self._base, self.check_id(mid))

    def frames_dir(self, mid: str) -> str:
        return os.path.join(self.item_dir(mid), "frames")

    @staticmethod
    def check_id(mid: str) -> str:
        """Ids worden zelf gegenereerd; dit is een pad-safety-guard."""
        if not isinstance(mid, str) or not re.fullmatch(r"[0-9a-z-]{4,80}", mid):
            raise LibraryError("ongeldig media-id")
        return mid

    # ------------------------------------------------------------ metadata
    def _meta_path(self) -> str:
        return os.path.join(self._base, _META_NAME)

    def _read_meta_locked(self) -> dict:
        """Lees library.json en normaliseer. Aanroeper houdt de lock vast
        (de lock-file, niet dit bestand — het meta-bestand zelf wordt alleen
        atoomair vervangen via os.replace)."""
        try:
            with open(self._meta_path(), "rb") as fh:
                raw = fh.read()
        except OSError:
            return {"items": []}
        if not raw:
            return {"items": []}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            # kapotte metadata: niet crashen, met lege lijst verder
            return {"items": []}
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            return {"items": []}
        return data

    def _write_meta(self, data: dict) -> None:
        """Atomair herschrijven van library.json onder een exclusieve lock.
        De lock-file staat naast de meta (blijft bestaan; is onschuldig)."""
        lock_path = self._meta_path() + ".lock"
        os.makedirs(self._base, exist_ok=True)
        with open(lock_path, "a+") as lock_fh:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
            try:
                fd, tmp = tempfile.mkstemp(prefix=".library-", dir=self._base)
                try:
                    with os.fdopen(fd, "w") as f:
                        json.dump(data, f, indent=2)
                        f.flush()
                        os.fsync(f.fileno())
                    os.replace(tmp, self._meta_path())
                except Exception:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                    raise
            finally:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)

    def _read_meta(self) -> dict:
        """Lees library.json (gedeelde lock, voor lezers)."""
        lock_path = self._meta_path() + ".lock"
        if not os.path.exists(self._meta_path()):
            return {"items": []}
        with open(lock_path, "a+") as lock_fh:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_SH)
            try:
                return self._read_meta_locked()
            finally:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)

    # ------------------------------------------------------------ add
    def add_from_bytes(self, filename: str, data: bytes,
                       wide: bool = False) -> dict:
        """Sla upload op als bronbestand en extraheer frames met ffmpeg.
        Retourneert het item-dict uit library.json. Stappen:
        tempmap -> bronbestand -> ffmpeg naar frames/ -> metadata. Mislukte
        pogingen ruimen hun eigen map op."""
        filename = os.path.basename(filename or "film.mp4")
        stem, ext = os.path.splitext(filename)
        ext = ext.lstrip(".").lower()
        if ext not in ALLOWED_EXT:
            raise LibraryError("extensie .%s niet toegestaan (toegestaan: %s)"
                               % (ext or "?", ", ".join(ALLOWED_EXT)))
        if not isinstance(data, (bytes, bytearray)) or len(data) == 0:
            raise LibraryError("leeg bestand")
        if len(data) > 200 * 1024 * 1024:
            raise LibraryError("bestand te groot (limiet 200 MB)")

        digest = hashlib.sha256(
            stem.encode("utf-8", "replace") + str(time.time_ns()).encode()
        ).hexdigest()[:8]
        mid = "%s-%s" % (slugify(stem), digest)

        item_dir = self.item_dir(mid)
        if os.path.exists(item_dir):
            raise LibraryError("id-conflict, probeer opnieuw")
        os.makedirs(item_dir)
        work = None
        try:
            src = os.path.join(item_dir, "source." + ext)
            with open(src, "wb") as f:
                f.write(bytes(data))
            work = os.path.join(item_dir, ".extract")
            os.makedirs(work)
            item = self._extract(src, work, wide)
            # frames op definitieve plek (hernoemen binnen dezelfde map)
            frames = os.path.join(item_dir, "frames")
            os.rename(work, frames)
            work = None
            item["id"] = mid
            item["name"] = stem or mid
            item["wide"] = bool(wide)
            self._register(item)
            return dict(item)
        except Exception:
            shutil.rmtree(item_dir, ignore_errors=True)
            raise

    def _extract(self, src: str, work: str, wide: bool) -> dict:
        """ffmpeg: frames naar work/f%04d.jpg; retourneert mediavelden."""
        if not ffmpeg_available():
            raise LibraryError("ffmpeg niet beschikbaar op deze machine")
        w, h = (WIDE_W, WIDE_H) if wide else (SPLIT_W, SPLIT_H)
        vf = "scale=%d:%d,fps=%d" % (w, h, DEFAULT_FPS)
        # framerate-regeling zit in het fps-filter van -vf; geen -vsync
        # (afgebroken in ffmpeg 7.x, en overbodig)
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               "-i", src, "-vf", vf, "-start_number", "0",
               "-q:v", "2", os.path.join(work, "f%04d.jpg")]
        try:
            proc = subprocess.run(cmd, capture_output=True,
                                  timeout=FFMPEG_TIMEOUT)
        except subprocess.TimeoutExpired as e:
            raise LibraryError("ffmpeg timeout na %ss" % FFMPEG_TIMEOUT) from e
        frames = sorted(n for n in os.listdir(work) if n.endswith(".jpg"))
        if proc.returncode != 0 or not frames:
            detail = proc.stderr.decode("utf-8", "replace")[:300]
            raise LibraryError("ffmpeg-extractie mislukt: %s" % detail)
        warning = None
        if len(frames) > self._max_frames:
            # trunceren: welbegrensde extractie (Pi-schijf/CPU)
            for name in frames[self._max_frames:]:
                os.unlink(os.path.join(work, name))
            frames = frames[:self._max_frames]
            warning = "afgekapt op %d frames" % self._max_frames
        width, height = self._frame_size(os.path.join(work, frames[0]),
                                         w, h)
        return {
            "width": width,
            "height": height,
            "frames": len(frames),
            "fps": DEFAULT_FPS,
            "duration": round(len(frames) / float(DEFAULT_FPS), 3),
            "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "warning": warning,
        }

    @staticmethod
    def _frame_size(first_frame: str, want_w: int, want_h: int) -> tuple:
        """Lees de echte frame-afmeting uit een JPEG-header (stdlib-only).
        Fallback: de gevraagde schaal."""
        try:
            with open(first_frame, "rb") as f:
                data = f.read(64 * 1024)
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3):
                    h, w = ((data[i + 5] << 8) | data[i + 6],
                            (data[i + 7] << 8) | data[i + 8])
                    return w, h
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                seglen = (data[i + 2] << 8) | data[i + 3]
                if seglen < 2:
                    break
                i += 2 + seglen
        except Exception:
            pass
        return want_w, want_h

    def import_builtin(self, naam: str, bron_dir: str, wide: bool = False) -> dict:
        """Importeer een bestaande frames-directory (zoals de ingebouwde
        Wan-lussen media/loops/frames/{rust,jump} — let op: map MET frames)
        als bibliotheek-item.
        Kopieert de frames (geen ffmpeg nodig) en registreert metadata;
        afmetingen worden van het eerste frame gelezen indien PIL
        beschikbaar is, anders conservatief geschat."""
        if not os.path.isdir(bron_dir):
            raise LibraryError("bronmap bestaat niet: %s" % bron_dir)
        frames = sorted(f for f in os.listdir(bron_dir)
                        if f.lower().endswith((".png", ".jpg", ".jpeg")))
        if not frames:
            raise LibraryError("geen frames in %s" % bron_dir)
        mid = self._maak_id(naam)
        item_dir = os.path.join(self._base, mid)
        if os.path.exists(item_dir):
            raise LibraryError("id al in gebruik: %s" % mid)
        os.makedirs(os.path.join(item_dir, "frames"), exist_ok=True)
        for f in frames:
            shutil.copyfile(os.path.join(bron_dir, f),
                            os.path.join(item_dir, "frames", f))
        w, h = (1920 if wide else 480, 540 if wide else 272)
        try:
            from PIL import Image
            with Image.open(os.path.join(item_dir, "frames", frames[0])) as im:
                w, h = im.size
        except Exception:
            pass
        item = {
            "id": mid, "name": naam, "wide": bool(wide),
            "width": w, "height": h,
            "frames": len(frames), "fps": 16,
            "duration": round(len(frames) / 16.0, 2),
            "created": time.time(), "source": "builtin",
        }
        self._register(item)
        return item

    def _maak_id(self, naam: str) -> str:
        import re as _re
        slug = _re.sub(r"[^a-z0-9]+", "-",
                       str(naam).lower()).strip("-")[:32] or "item"
        kandidaat = slug
        n = 1
        while os.path.exists(os.path.join(self._base, kandidaat)) \
                or any(it.get("id") == kandidaat for it in self.list()):
            n += 1
            kandidaat = f"{slug}-{n}"
        return kandidaat

    def _register(self, item: dict) -> None:
        """Voeg item toe aan library.json (onder lock)."""
        lock_path = self._meta_path() + ".lock"
        with open(lock_path, "a+") as lock_fh:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
            try:
                data = self._read_meta_locked()
                items = [it for it in data["items"]
                         if isinstance(it, dict) and it.get("id") != item["id"]]
                items.append(dict(item))
                data["items"] = items
                fd, tmp = tempfile.mkstemp(prefix=".library-", dir=self._base)
                try:
                    with os.fdopen(fd, "w") as f:
                        json.dump(data, f, indent=2)
                        f.flush()
                        os.fsync(f.fileno())
                    os.replace(tmp, self._meta_path())
                except Exception:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                    raise
            finally:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)

    # ------------------------------------------------------------ list/get
    def list(self) -> list[dict]:
        """Alle items uit library.json (gesorteerd op created, nieuwste laatst
        zoals toegevoegd)."""
        data = self._read_meta()
        out = []
        for it in data["items"]:
            if isinstance(it, dict) and it.get("id"):
                out.append(dict(it))
        return out

    def get(self, mid: str) -> dict:
        """Eén item, of LibraryError als onbekend."""
        mid = self.check_id(mid)
        for it in self.list():
            if it.get("id") == mid:
                return it
        raise LibraryError("media %s bestaat niet" % mid)

    def frames(self, mid: str) -> list[str]:
        """Volledige paden van geëxtraheerde frames (voor de renderer)."""
        fdir = self.frames_dir(mid)
        if not os.path.isdir(fdir):
            return []
        return [os.path.join(fdir, n) for n in sorted(os.listdir(fdir))
                if n.endswith(".jpg")]

    # ------------------------------------------------------------ delete
    def delete(self, mid: str) -> None:
        """Verwijder map + metadata-regel. Puur bibliotheek-niveau: of een
        item nog in config.states zit, checkt de server."""
        mid = self.check_id(mid)
        item_dir = self.item_dir(mid)
        if not os.path.isdir(item_dir):
            # toch metadata opruimen (wees-regel)
            self._unregister(mid)
            raise LibraryError("media %s bestaat niet" % mid)
        shutil.rmtree(item_dir, ignore_errors=True)
        self._unregister(mid)

    def _unregister(self, mid: str) -> None:
        lock_path = self._meta_path() + ".lock"
        with open(lock_path, "a+") as lock_fh:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
            try:
                data = self._read_meta_locked()
                before = len(data["items"])
                data["items"] = [it for it in data["items"]
                                 if isinstance(it, dict)
                                 and it.get("id") != mid]
                if len(data["items"]) != before:
                    fd, tmp = tempfile.mkstemp(prefix=".library-",
                                               dir=self._base)
                    try:
                        with os.fdopen(fd, "w") as f:
                            json.dump(data, f, indent=2)
                            f.flush()
                            os.fsync(f.fileno())
                        os.replace(tmp, self._meta_path())
                    except Exception:
                        try:
                            os.unlink(tmp)
                        except OSError:
                            pass
                        raise
            finally:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
