#!/usr/bin/env python3
"""Grimoria GL-renderer smoke-test: states doorlopen, CPU meten, screenshots."""
import os, sys, time

os.environ.setdefault("SDL_VIDEODRIVER", "x11")
os.environ.setdefault("DISPLAY", ":0")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pygame

import renderer  # noqa: E402

FULL = (3840, 1080)
STATES = ["SLEEP", "WAKING", "AWAKE", "TALKING", "SCARE", "LAUGH", "RETURN"]


class FakeGrim:
    def __init__(self):
        self.state = "SLEEP"
        self.t0 = time.time()
        self.eye = (0.0, 0.0)
        self.blink = -1.0
        self.mouth = 0.0
        self.flash = 0.0
        self.caption = "Ik voorspel... dat er vanavond snoep in je zak verdwijnt."


def main():
    pygame.init()
    pygame.display.set_mode((640, 360), pygame.OPENGL)
    r = renderer.Renderer(FULL)
    print("GL:", r.gl_version)
    g = FakeGrim()
    shots_dir = "/mnt/media/halloween/logs/shots"
    os.makedirs(shots_dir, exist_ok=True)
    import subprocess
    for st in STATES:
        g.state = st
        g.flash = 1.0 if st == "WAKING" else 0.0
        g.mouth = 0.7 if st == "TalkING" else 0.0
        g.mouth = 0.7 if st == "TALKING" else 0.0
        for _ in range(8):
            r.render(g)
            pygame.display.flip()
            time.sleep(1 / 30)
        png = r.png_bytes()
        path = f"{shots_dir}/gl_{st.lower()}.png"
        open(path, "wb").write(png)
        print(f"  {st:8s} -> {path} ({len(png)} bytes)")
    # CPU-meting: 10s @30fps software-achtig draaien
    g.state = "AWAKE"
    import resource
    t0 = time.process_time()
    n = 0
    t_end = time.time() + 10
    while time.time() < t_end:
        r.render(g)
        pygame.display.flip()
        n += 1
        time.sleep(1 / 30)
    cpu = resource.getrusage(resource.RUSAGE_SELF).ru_utime * 1000 / n
    print(f"perf: {n} frames, ~{cpu:.1f} ms CPU per frame "
          f"(budget 33ms; incl. sleep-correctie: {1000*resource.getrusage(resource.RUSAGE_SELF).ru_utime/10:.0f}% kern)")
    r.release()
    pygame.quit()
    print("OK")


if __name__ == "__main__":
    main()
