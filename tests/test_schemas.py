#!/usr/bin/env python3
"""Unittests voor app/schemas.py (config-validatie).

Run: cd <repo> && python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import sys
import unittest

# app-map achteraan toevoegen (niet vooraan: de oude app/test_*.py mogen
# de modules in tests/ niet schaduwen)
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/app")

import schemas  # noqa: E402


class TestDefaults(unittest.TestCase):
    """DEFAULT_CONFIG is zelfstandig geldig."""

    def test_default_config_is_valid(self):
        clean, errors = schemas.validate_config(
            schemas.DEFAULT_CONFIG)
        self.assertEqual(errors, [])
        self.assertEqual(clean["entry_state"], "rust")
        self.assertEqual(len(clean["states"]), 4)

    def test_defaults_present(self):
        m = schemas.DEFAULT_CONFIG["motion"]
        self.assertEqual(m["person_hold_secs"], 3.0)
        self.assertEqual(m["present_memory_secs"], 1.0)
        self.assertEqual(m["still_secs"], 5.0)
        self.assertEqual(m["detect_fps"], 3)
        self.assertEqual(m["person_conf"], 0.5)
        self.assertEqual(schemas.DEFAULT_CONFIG["timing"]["scare_cooldown"],
                         90.0)
        scare = next(s for s in schemas.DEFAULT_CONFIG["states"]
                     if s["id"] == "jumpscare")
        self.assertEqual(scare["advance"]["timeout"]["secs"], 2.5)
        kalm = next(s for s in schemas.DEFAULT_CONFIG["states"]
                    if s["id"] == "kalm")
        self.assertEqual(kalm["advance"]["timeout"]["secs"], 20.0)


class TestEmptyAndUnknownKeys(unittest.TestCase):
    def test_empty_config_gets_defaults(self):
        clean, errors = schemas.validate_config({})
        self.assertEqual(errors, [])          # niets mis: gewoon defaults
        self.assertEqual(len(clean["states"]), 4)
        self.assertEqual(clean["motion"]["detect_fps"], 3)

    def test_none_config_is_defaults_with_warning(self):
        clean, errors = schemas.validate_config(None)
        self.assertEqual(len(clean["states"]), 4)
        self.assertTrue(errors)

    def test_backward_compat_keys_pass_through(self):
        cfg = {"ha_url": "http://192.168.178.51:8123",
               "ha_token": "geheim", "llm_url": "http://x:11434",
               "llm_model": "test", "api_port": 8077,
               "camera": {"w": 320}, "jumpscare_kans": 0.0}
        clean, errors = schemas.validate_config(cfg)
        self.assertEqual(errors, [])
        self.assertEqual(clean["ha_token"], "geheim")   # server scrubbt zelf
        self.assertEqual(clean["camera"]["w"], 320)
        self.assertEqual(clean["api_port"], 8077)


class TestScreensPlaybackMotion(unittest.TestCase):
    def test_screens_overlay_fixes(self):
        cfg = {"screens": {"swap": "ja", "offset": {"dx": "links", "dy": 0.5},
                           "items": [
                               {"rotation": 90,
                                "overlay": {"enabled": True, "text": "Boo!",
                                            "x": 0.2, "y": 0.9, "size": 64,
                                            "color": "#FFAA00"}},
                               {"rotation": 45},
                               {"rotation": 0}  # derde scherm -> geweigerd
                           ]}}
        clean, errors = schemas.validate_config(cfg)
        self.assertTrue(any("swap" in e for e in errors) or
                        any("screens" in e for e in errors))
        s = clean["screens"]
        self.assertFalse(s["swap"])                    # hersteld naar default
        self.assertEqual(s["offset"], {"dx": 0.0, "dy": 0.5})
        self.assertEqual(len(s["items"]), 2)
        it0 = s["items"][0]
        self.assertEqual(it0["rotation"], 90)
        self.assertTrue(it0["overlay"]["enabled"])
        self.assertEqual(it0["overlay"]["color"], "#ffaa00")  # lowercase
        self.assertEqual(it0["overlay"]["size"], 64.0)
        self.assertTrue(any("maximaal 2" in e for e in errors))

    def test_motion_value_ranges(self):
        cfg = {"motion": {"detect_fps": 99, "person_conf": 1.7,
                          "present_memory_secs": -4, "still_secs": "strak",
                          "detector": "yolo"}}
        clean, errors = schemas.validate_config(cfg)
        m = clean["motion"]
        self.assertEqual(m["detect_fps"], 30.0)        # geclamped
        self.assertEqual(m["person_conf"], 1.0)        # geclamped
        self.assertEqual(m["present_memory_secs"], 0.0)
        self.assertEqual(m["still_secs"], 5.0)         # default terug
        self.assertEqual(m["detector"], "tflite")      # onbekende waarde
        self.assertTrue(any("detector" in e for e in errors))


class TestStatesValidation(unittest.TestCase):
    def test_state_and_edge_errors(self):
        cfg = {"states": [
            {"id": "rust", "advance": {
                "motion": {"to": "niet_bestaand"},
                "timeout": {"secs": "veel", "to": "rust"}}},
            {"id": "rust", "name": "Dubbel"},              # dubbel -> skip
            {"id": "", "name": "Lege id"},                 # -> skip
            "geen dict",                                    # -> skip
            {"id": "kalm", "canvas_mode": "driedelig",     # -> split
             "media": {"0": 5, "1": "ok", "2": "weg"},
             "advance": {"timeout": {"secs": 2.5, "to": "rust"}}},
        ], "entry_state": "onzichtbaar"}
        clean, errors = schemas.validate_config(cfg)
        ids = [s["id"] for s in clean["states"]]
        self.assertEqual(ids, ["rust", "kalm"])
        rust = clean["states"][0]
        # motion.to bestond niet -> edge weg
        self.assertNotIn("motion", rust["advance"])
        # timeout.secs was geen getal -> edge weg
        self.assertNotIn("timeout", rust["advance"])
        kalm = clean["states"][1]
        self.assertEqual(kalm["canvas_mode"], "split")
        self.assertEqual(kalm["media"], {"0": "", "1": "ok"})
        self.assertTrue(errors)                       # 6+ meldingen
        self.assertEqual(clean["entry_state"], "kalm")  # eerste beschikbare

    def test_fatal_states_falls_back_to_defaults(self):
        cfg = {"states": [], "kind_mode": True}
        clean, errors = schemas.validate_config(cfg)
        self.assertEqual(len(clean["states"]), 4)
        self.assertTrue(any("default states" in e for e in errors))

    def test_wide_media_forms(self):
        cfg = {"states": [
            {"id": "a", "canvas_mode": "wide", "media": "breed.mp4-id"},
            {"id": "b", "canvas_mode": "wide", "media": {"0": "x"}},
            {"id": "c", "canvas_mode": "split", "media": "id"},
        ]}
        clean, errors = schemas.validate_config(cfg)
        a, b, c = clean["states"]
        self.assertEqual(a["media"], {"all": "breed.mp4-id"})
        self.assertEqual(b["media"], {"all": ""})
        self.assertTrue(any("wide" in e for e in errors))
        self.assertEqual(c["media"], {"0": "id", "1": "id"})

    def test_cooldown_default_for_scare_edge(self):
        cfg = {"timing": {"scare_cooldown": 120.0},
               "states": [{"id": "rust", "advance": {
                   "motion": {"to": "jumpscare"}}},
                   {"id": "jumpscare"}]}
        clean, _ = schemas.validate_config(cfg)
        motion = clean["states"][0]["advance"]["motion"]
        self.assertEqual(motion["cooldown"], 120.0)   # erft scare_cooldown

    def test_kind_to_and_idle_kept(self):
        cfg = {"states": [
            {"id": "langzaam", "advance": {
                "motion": {"to": "jumpscare", "require_motion_secs": 3.0,
                           "kind_to": "kalm",
                           "idle": {"to": "rust", "still_secs": 4}},
                "timeout": {"secs": 20, "to": "rust"}}},
            {"id": "jumpscare"}, {"id": "kalm"}, {"id": "rust"},
        ]}
        clean, errors = schemas.validate_config(cfg)
        self.assertEqual(errors, [])
        m = clean["states"][0]["advance"]["motion"]
        self.assertEqual(m["kind_to"], "kalm")
        self.assertEqual(m["idle"], {"to": "rust", "still_secs": 4})


if __name__ == "__main__":
    unittest.main()
