#!/usr/bin/env python3
"""Headless test van de Wan-videolus-integratie (EGL, geen scherm nodig).
Simuleert grimoria_pi's state-machine en schrijft screenshots naar logs/shots_gl/."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
import pygame  # noqa: E402

import renderer  # noqa: E402

FULL = (3840, 1080)
STATES = ["SLEEP", "WAKING", "AWAKE", "TALKING", "SCARE", "LAUGH", "RETURN"]


class FakeGrim:
    def __init__(self):
        self.state = "AWAKE"
        self.t0 = time.time()
        self.eye = (0.0, 0.0)
        self.blink = -1.0
        self.mouth = 0.0
        self.flash = 0.0
        self.caption = ""


def main():
    pygame.init()
    r = renderer.Renderer(FULL)          # standalone-EGL-context via moderngl
    print("GL:", r.gl_version)
    assert r.video is not None, "video-lusmodus niet actief!"
    print(f"video: {len(r.video['rust'])} rust + {len(r.video['jump'])} jump frames")
    g = FakeGrim()
    shots = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "..", "shots_gl")
    os.makedirs(shots, exist_ok=True)

    def shot(naam):
        png = r.png_bytes()
        pad = os.path.join(shots, naam)
        open(pad, "wb").write(png)
        print(f"  {naam}: {len(png)} bytes")

    # rust-lus: 3 s doorlopen, screenshot op 1 s en bij wrap
    g.state = "AWAKE"
    for i in range(90):
        r.render(g)
        time.sleep(1 / 30)
    shot("video_rust.png")

    # jumpscare: direct na state-wissel moet frame ~0 tonen, na 1 s ~frame 16
    g.state = "SCARE"; g.t0 = time.time()
    for i in range(15):
        r.render(g)
    shot("video_scare_start.png")
    for i in range(45):
        r.render(g)
        time.sleep(1 / 30)
    shot("video_scare_mid.png")

    # LAUGH -> blijft jump-lus (demonisch lachen) tot state voorbij is
    g.state = "LAUGH"; g.t0 = time.time()
    for i in range(30):
        r.render(g)
        time.sleep(1 / 30)
    shot("video_lach.png")

    # terug naar AWAKE -> rust-lus hervat naadloos
    g.state = "AWAKE"
    for i in range(30):
        r.render(g)
        time.sleep(1 / 30)
    shot("video_terug_rust.png")

    # perf: 300 frames
    t0 = time.process_time()
    for _ in range(300):
        r.render(g)
    dt = (time.process_time() - t0) / 300 * 1000
    print(f"perf: {dt:.2f} ms CPU/frame (budget 33 ms)")
    r.release()
    pygame.quit()
    print("OK")


if __name__ == "__main__":
    main()
