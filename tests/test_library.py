#!/usr/bin/env python3
"""Unittests voor app/library.py.

Gebruikt een tmpdir; de ffmpeg-tests skippen graceful als ffmpeg ontbreekt
(op deze dev-machine is dat vaak zo; op de Pi is ffmpeg er wél). De
testvideo's worden door ffmpeg zelf gegenereerd (lavfi testsrc2), zodat de
tests geen binaire fixtures nodig hebben.

Run: cd <repo> && python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

# app-map achteraan toevoegen (niet vooraan: de oude app/test_*.py mogen
# de modules in tests/ niet schaduwen)
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/app")

from library import Library, LibraryError, ffmpeg_available  # noqa: E402

FFMPEG = ffmpeg_available()
SKIPReason = "ffmpeg niet beschikbaar op deze machine" if not FFMPEG else None


def make_test_video(tmpdir: str) -> bytes:
    """Laat ffmpeg een kleine testvideo maken (testsrc2, 1 s, 640x360,
    16 fps) en lees de bytes; dit is de bron voor de extractietests."""
    out = os.path.join(tmpdir, "src.mp4")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=16:duration=1",
           "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
           out]
    proc = subprocess.run(cmd, capture_output=True, timeout=120)
    if proc.returncode != 0:
        raise unittest.SkipTest("ffmpeg kon geen testvideo maken: %s"
                                % proc.stderr.decode()[:200])
    with open(out, "rb") as f:
        return f.read()


class LibraryTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="glib-test-")
        self.lib = Library(os.path.join(self.tmp, "lib"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


@unittest.skipUnless(FFMPEG, SKIPReason)
class TestExtraction(LibraryTestBase):
    def test_add_from_bytes_split(self):
        data = make_test_video(self.tmp)
        item = self.lib.add_from_bytes("Rust-loop.mp4", data, wide=False)
        self.assertTrue(item["id"].startswith("rust-loop-"))
        self.assertFalse(item["wide"])
        self.assertEqual(item["width"], 480)
        self.assertEqual(item["height"], 272)
        self.assertEqual(item["fps"], 16)
        self.assertGreater(item["frames"], 0)
        self.assertAlmostEqual(item["duration"],
                               round(item["frames"] / 16.0, 3))
        self.assertIsNone(item["warning"])
        frames = self.lib.frames(item["id"])
        self.assertGreater(len(frames), 0)
        self.assertTrue(all(f.endswith(".jpg") for f in frames))
        self.assertTrue(os.path.basename(frames[0]).startswith("f"))
        # metadata staat in library.json
        with open(os.path.join(self.lib.base_dir, "library.json")) as f:
            meta = json.load(f)
        self.assertEqual([it["id"] for it in meta["items"]], [item["id"]])
        # naam = bestandsnaam zonder extensie
        self.assertEqual(item["name"], "Rust-loop")

    def test_add_from_bytes_wide(self):
        data = make_test_video(self.tmp)
        item = self.lib.add_from_bytes("Breed.mp4", data, wide=True)
        self.assertTrue(item["wide"])
        self.assertEqual((item["width"], item["height"]), (1920, 540))

    def test_list_and_delete(self):
        data = make_test_video(self.tmp)
        item = self.lib.add_from_bytes("film.mp4", data)
        self.assertEqual([it["id"] for it in self.lib.list()],
                         [item["id"]])
        self.lib.delete(item["id"])
        self.assertEqual(self.lib.list(), [])
        self.assertFalse(os.path.isdir(self.lib.item_dir(item["id"])))
        with self.assertRaises(LibraryError):
            self.lib.get(item["id"])

    def test_max_frames_guard_truncates(self):
        # bibliotheek met strenge frame-limiet (1 s @16 fps = 16 frames)
        lib = Library(os.path.join(self.tmp, "lib2"), max_frames=3)
        data = make_test_video(self.tmp)
        item = lib.add_from_bytes("lang.mp4", data)
        self.assertEqual(item["frames"], 3)
        self.assertIn("afgekapt", item["warning"])
        self.assertEqual(len(lib.frames(item["id"])), 3)


class TestInputValidation(LibraryTestBase):
    """Extensie-/size-guards; die komen vóór de ffmpeg-aanroep en werken dus
    ook zonder ffmpeg op de machine."""

    def test_bad_extension_rejected(self):
        with self.assertRaises(LibraryError):
            self.lib.add_from_bytes("script.exe", b"x")
        with self.assertRaises(LibraryError):
            self.lib.add_from_bytes("geenext", b"x")

    def test_empty_data_rejected(self):
        with self.assertRaises(LibraryError):
            self.lib.add_from_bytes("film.mp4", b"")
        with self.assertRaises(LibraryError):
            self.lib.add_from_bytes("film.mp4", "geen bytes")

    def test_oversize_rejected(self):
        # niet écht 200 MB maken: een bytes-subclass met gefingeerde lengte
        class FakeBig(bytes):
            def __len__(self):
                return 201 * 1024 * 1024
        with self.assertRaises(LibraryError):
            self.lib.add_from_bytes("groot.mp4", FakeBig())

    @unittest.skipIf(FFMPEG, "ffmpeg wél aanwezig: geen fallback-pad")
    def test_without_ffmpeg_raises(self):
        with self.assertRaises(LibraryError):
            self.lib.add_from_bytes("film.mp4", b"x" * 100)


class TestMetaRobustness(LibraryTestBase):
    def test_broken_library_json_is_ignored(self):
        os.makedirs(self.lib.base_dir, exist_ok=True)
        with open(os.path.join(self.lib.base_dir, "library.json"), "w") as f:
            f.write("{kapot")
        self.assertEqual(self.lib.list(), [])
        # en een nieuw item registreren lukt gewoon
        self.lib._register({"id": "test-item-0001", "name": "x"})
        self.assertEqual(len(self.lib.list()), 1)

    def test_id_guard(self):
        with self.assertRaises(LibraryError):
            self.lib.check_id("../evil")
        with self.assertRaises(LibraryError):
            self.lib.frames("../evil")

    def test_frames_unknown_id_empty(self):
        self.assertEqual(self.lib.frames("niets-zulks-1234"), [])

    def test_duplicate_register_keeps_single_rule(self):
        self.lib._register({"id": "dubbel-0001", "name": "x"})
        self.lib._register({"id": "dubbel-0001", "name": "y"})
        items = self.lib.list()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["name"], "y")


if __name__ == "__main__":
    unittest.main()
