#!/usr/bin/env python3
"""
Smoke-test voor de Grimoria v2 filmbibliotheek (library.py).

Draaien:  python3 test_library.py
Maakt in een tijdelijke GRIMORIA_BASE een synthetisch 'video' (via ffmpeg,
indien aanwezig) en doorloopt register_upload → extract → list → delete.
Zonder ffmpeg (bijv. op deze dev-machine): nette SKIP, exit 0.
"""

import os
import shutil
import subprocess
import sys
import tempfile

APP_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP_DIR)

import library                                    # noqa: E402
from library import LibraryError, ExternalToolError  # noqa: E402

_TOT = _OK = 0


def check(naam, cond, extra=""):
    global _TOT, _OK
    _TOT += 1
    if cond:
        _OK += 1
        print(f"  PASSED  {naam}")
    else:
        print(f"  FAILED  {naam}" + (f"  [{extra}]" if extra else ""))


def sectie(titel):
    print(f"\n== {titel} ==")


def maak_testvideo(pad: str, seconden: float = 2.0, fps: int = 8) -> bool:
    """Synthetisch mp4: bewegende kleurbalken via ffmpeg lavfi-testsrc."""
    try:
        p = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i",
             f"testsrc=duration={seconden}:size=320x240:rate={fps}",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", pad],
            capture_output=True, timeout=120)
        return p.returncode == 0 and os.path.getsize(pad) > 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def main() -> int:
    tmp_base = tempfile.mkdtemp(prefix="grimoria-libtest-")
    tmp_in = tempfile.mkdtemp(prefix="grimoria-libtest-in-")
    try:
        # geïsoleerde omgeving: BASE wijst naar tmp
        os.environ["GRIMORIA_BASE"] = tmp_base
        library.BASE = os.path.join(tmp_base)          # module-const opnieuw zetten
        library._FF_CACHE.clear()

        sectie("omgeving")
        if not library._has("ffmpeg"):
            print("  SKIP: geen ffmpeg — extractie-test overgeslagen.")
            print("  (op de Pi is ffmpeg 5.1 aanwezig; hier draait alleen de")
            print("   validatie/list/delete-pad van library.py)")
            ffmpeg_ok = False
        else:
            ffmpeg_ok = maak_testvideo(os.path.join(tmp_in, "Spook film.mp4"))
            if not ffmpeg_ok:
                print("  SKIP: ffmpeg aanwezig maar testvideo maken mislukt "
                      "(geen libx264?).")
        check("tmp-BASE geïsoleerd aangemaakt", os.path.isdir(tmp_base))
        check("lege bibliotheek → lege lijst", library.list_media() == [])

        sectie("validatie bij register_upload")
        nep = os.path.join(tmp_in, "plaatje.gif")
        open(nep, "wb").write(b"GIF89a-nepinhoud")
        try:
            library.register_upload("plaatje.gif", nep)
            check("verkeerde extensie geweigerd", False)
        except LibraryError:
            check("verkeerde extensie geweigerd", True)
        leeg = os.path.join(tmp_in, "leeg.mp4")
        open(leeg, "wb").close()
        try:
            library.register_upload("leeg.mp4", leeg)
            check("leeg bestand geweigerd", False)
        except LibraryError:
            check("leeg bestand geweigerd", True)
        groot = os.path.join(tmp_in, "groot.mp4")
        with open(groot, "wb") as f:
            f.seek(library.MAX_UPLOAD_BYTES + 1)
            f.write(b"\0")
        try:
            library.register_upload("groot.mp4", groot)
            check("te groot bestand geweigerd", False)
        except LibraryError:
            check("te groot bestand geweigerd", True)
        check("na afwijzingen: bibliotheek nog steeds leeg",
              library.list_media() == [])

        if not ffmpeg_ok:
            print(f"\n{'=' * 46}")
            print(f"  {_OK}/{_TOT} checks geslaagd  —  SKIP (geen ffmpeg)")
            return 0

        sectie("upload + extract")
        bron = os.path.join(tmp_in, "Spook film.mp4")
        mid = library.register_upload("Spook film.mp4", bron)
        check("id is veilige slug+hash", mid.startswith("spook-film-")
              and "/" not in mid and len(mid) < 60)
        check("bron verplaatst naar library-map",
              any(os.path.isfile(os.path.join(library.media_dir(mid),
                                              f"source.{e}"))
                  for e in library.ALLOWED_EXT))
        check("upload-tmp is weg (move, geen copy)",
              not os.path.exists(bron))
        meta = library.extract(mid, fps=16, height=272, max_frames=300)
        check("meta: id klopt", meta["id"] == mid)
        check("meta: frames geëxtraheerd (2 s @8fps bron, fps=16)",
              8 <= meta["frames"] <= 32, str(meta))
        check("meta: fps 16", meta["fps"] == 16)
        check("meta: duration > 0", meta["duration"] > 0)
        check("frames bestaan op disk",
              len(library.frames_glob(mid)) == meta["frames"])
        check("meta.json leesbaar via get_meta",
              library.get_meta(mid)["id"] == mid)

        sectie("lijst & dubbel-id")
        items = library.list_media()
        check("list_media toont het item", len(items) == 1
              and items[0]["id"] == mid)
        check("listitem-velden compleet",
              all(k in items[0] for k in
                  ("id", "name", "frames", "fps", "duration", "created")))
        kopie = os.path.join(tmp_in, "kopie.mp4")
        shutil.copy(bron if os.path.exists(bron)
                    else os.path.join(library.media_dir(mid), "source.mp4"), kopie)
        mid2 = library.register_upload("Spook film.mp4", kopie)
        check("tweede upload krijgt ander id", mid2 != mid)

        sectie("extract-fouten ruimen op")
        try:
            kapot = os.path.join(tmp_in, "kapot.mp4")
            open(kapot, "wb").write(b"\x00\x01\x02geenvideo")
            mid3 = library.register_upload("kapot.mp4", kapot)
            library.extract(mid3)
            check("kapotte bron → LibraryError", False)
        except ExternalToolError:
            check("kapotte bron → LibraryError", False, "onverwacht ExternalToolError")
        except LibraryError:
            check("kapotte bron → LibraryError", True)
            check("mislukte extractie: map opgeruimd",
                  not os.path.isdir(library.media_dir(mid3)))

        sectie("delete")
        library.delete(mid)
        check("map weg na delete", not os.path.isdir(library.media_dir(mid)))
        try:
            library.delete(mid)
            check("delete van onbekend id → LibraryError", False)
        except LibraryError:
            check("delete van onbekend id → LibraryError", True)
        library.delete(mid2)
        check("bibliotheek weer leeg", library.list_media() == [])

        sectie("pad-traversie-bescherming")
        for kwaad in ("../../etc", "..%2F..", "a/b", "AB", "x" * 200):
            try:
                library.media_dir(kwaad)
                check(f"onveilig id geweigerd: {kwaad[:20]!r}", False)
            except LibraryError:
                check(f"onveilig id geweigerd: {kwaad[:20]!r}", True)

        print(f"\n{'=' * 46}")
        print(f"  {_OK}/{_TOT} checks geslaagd  —  "
              f"{'ALLES GOED' if _OK == _TOT else 'FALEN!'}")
        return 0 if _OK == _TOT else 1
    finally:
        shutil.rmtree(tmp_base, ignore_errors=True)
        shutil.rmtree(tmp_in, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
