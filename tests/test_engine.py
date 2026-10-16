#!/usr/bin/env python3
"""Unittests voor app/engine.py (pure fase-machine).

Dekt: de hele flow rust->langzaam->jumpscare->kalm->rust, kind_mode-route
naar kalm, cooldown-blokkade, hold/still-gedrag (incl. reset bij wegkomst)
en goto. De klok wordt expliciet meegegeven -> deterministisch.

Run: cd <repo> && python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import copy
import os
import sys
import unittest

# app-map achteraan toevoegen (niet vooraan: de oude app/test_*.py mogen
# de modules in tests/ niet schaduwen)
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/app")

import engine as engine_mod   # noqa: E402
import schemas                # noqa: E402


def default_cfg(kind_mode: bool = False) -> dict:
    """Geldige default-config (via de validatie, zoals de server die ook
    laadt), met kind_mode expliciet ingesteld."""
    cfg, errors = schemas.validate_config(copy.deepcopy(schemas.DEFAULT_CONFIG))
    assert errors == [], errors
    cfg["kind_mode"] = kind_mode
    return cfg


class EngineTestBase(unittest.TestCase):
    def mk(self, cfg=None, now=0.0):
        e = engine_mod.Engine(cfg if cfg is not None else default_cfg(), now)
        # init-event consumeren (eerste tick)
        ev = e.tick(now, False)
        self.assertEqual([x["reason"] for x in ev], ["init"])
        self.assertEqual(e.state_id, "rust")
        return e

    def reasons(self, events):
        return [x["reason"] for x in events]


class TestInitAndAttributes(EngineTestBase):
    def test_init_event_once(self):
        e = engine_mod.Engine(default_cfg(), 0.0)
        ev = e.tick(0.0, False)
        self.assertEqual(ev, [{"event": "transition", "from": None,
                               "to": "rust", "reason": "init"}])
        self.assertEqual(e.tick(1.0, False), [])      # geen tweede init

    def test_attributes(self):
        e = self.mk(now=100.0)
        self.assertEqual(e.state_id, "rust")
        self.assertEqual(e.state_name, "Rust")
        self.assertEqual(e.since, 100.0)
        self.assertAlmostEqual(e.since_secs, 0.0)
        self.assertFalse(e.kind_mode)
        e.kind_mode = True                            # settable
        self.assertTrue(e.kind_mode)

    def test_invalid_config_raises(self):
        with self.assertRaises(ValueError):
            engine_mod.Engine({"states": []})
        with self.assertRaises(ValueError):
            engine_mod.Engine("geen dict")

    def test_person_present_none_keeps_last_value(self):
        e = self.mk()
        self.assertEqual(e.tick(1.0, True), [
            {"event": "transition", "from": "rust", "to": "langzaam",
             "reason": "motion"}])
        self.assertTrue(e.person_present)
        e.tick(2.0, None)                             # onveranderd
        self.assertTrue(e.person_present)


class TestFullFlow(EngineTestBase):
    def test_rust_langzaam_jumpscare_kalm_rust(self):
        e = self.mk()
        # rust --beweging--> langzaam (geen hold vereist)
        self.assertEqual(e.tick(1.0, True), [
            {"event": "transition", "from": "rust", "to": "langzaam",
             "reason": "motion"}])
        # langzaam: persoon moet 3 s aaneengesloten aanwezig zijn
        self.assertEqual(e.tick(2.0, True), [])       # 1 s gehouden
        self.assertEqual(e.tick(3.9, True), [])       # 2,9 s gehouden
        self.assertEqual(e.tick(4.0, True), [
            {"event": "transition", "from": "langzaam", "to": "jumpscare",
             "reason": "hold"}])
        # jumpscare: timeout 2,5 s, ongeacht de persoon
        self.assertEqual(e.tick(6.4, None), [])
        self.assertEqual(e.tick(6.5, None), [
            {"event": "transition", "from": "jumpscare", "to": "kalm",
             "reason": "timeout"}])
        # kalm: timeout 20 s
        self.assertEqual(e.tick(26.4, None), [])
        self.assertEqual(e.tick(26.5, None), [
            {"event": "transition", "from": "kalm", "to": "rust",
             "reason": "timeout"}])
        self.assertEqual(e.state_id, "rust")

    def test_no_chained_transitions_in_one_tick(self):
        # In rust met persoon aanwezig: maar één overgang per tick; de keten
        # loopt nooit rust->langzaam->jumpscare in één puls.
        e = self.mk()
        events = e.tick(5.0, True)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["to"], "langzaam")

    def test_cooldown_blocks_rust_motion(self):
        # flow als hierboven; na terugkeer in rust (t=26,5) blokkeert de
        # rust-motion-cooldown (90 s, gestart op t=1) een nieuwe trigger.
        e = self.mk()
        e.tick(1.0, True)          # rust -> langzaam (rust|motion fires)
        e.tick(4.0, True)          # -> jumpscare
        e.tick(6.5, None)          # -> kalm
        e.tick(26.5, None)         # -> rust
        self.assertEqual(e.tick(30.0, True), [])      # 29 s < 90 s: geblokkeerd
        self.assertEqual(e.state_id, "rust")
        # na 90 s (t=91) mag dezelfde aanwezigheid wél triggeren
        self.assertEqual(e.tick(91.0, True), [
            {"event": "transition", "from": "rust", "to": "langzaam",
             "reason": "motion"}])

    def test_cooldown_is_per_edge(self):
        # rust|motion en langzaam|motion hebben onafhankelijke cooldowns.
        e = self.mk()
        e.tick(1.0, True)                    # rust|motion fires (t=1)
        e.tick(4.0, True)                    # langzaam|motion fires (t=4)
        e.tick(6.5, None)
        e.tick(26.5, None)                   # -> rust
        e.tick(91.0, True)                   # rust|motion weer vrij -> langzaam
        # langzaam|motion fired op t=4; op t=94,1 is die 90 s voorbij en vuurt
        # de hold (3 s aanwezig sinds t=91) opnieuw:
        self.assertEqual(e.tick(94.1, True), [
            {"event": "transition", "from": "langzaam", "to": "jumpscare",
             "reason": "hold"}])
        # rust|motion fired opnieuw op t=91 -> nog 90 - (94,1-91) = 86,9 s;
        # langzaam|motion vuurde zójuist zelf (t=94,1) -> weer 90 s gelatcht.
        # Beide edges zijn dus onafhankelijk:
        self.assertAlmostEqual(e.cooldown_remaining("rust"), 86.9, places=6)
        self.assertAlmostEqual(e.cooldown_remaining("langzaam"), 90.0)


class TestKindMode(EngineTestBase):
    def test_kind_mode_routes_to_kalm(self):
        e = self.mk(default_cfg(kind_mode=True))
        e.tick(1.0, True)                    # rust -> langzaam (geen kind_to)
        ev = e.tick(4.0, True)               # hold: kind_to kalm i.p.v. jumpscare
        self.assertEqual(ev, [{"event": "transition", "from": "langzaam",
                               "to": "kalm", "reason": "hold"}])

    def test_kind_mode_toggle_live(self):
        e = self.mk()                        # kind_mode uit
        e.tick(1.0, True)                    # -> langzaam
        e.kind_mode = True                   # toggle tijdens de run
        ev = e.tick(4.0, True)
        self.assertEqual(ev[0]["to"], "kalm")

    def test_goto_ignores_kind_mode(self):
        e = self.mk(default_cfg(kind_mode=True))
        ev = e.goto("jumpscare", 10.0)       # handmatig mag wél de scare
        self.assertEqual(ev, [{"event": "transition", "from": "rust",
                               "to": "jumpscare", "reason": "goto"}])
        self.assertEqual(e.state_id, "jumpscare")


class TestHoldAndStill(EngineTestBase):
    def make_langzaam_cfg(self, require=3.0, still=5.0, idle_still=None):
        """Compacte config: rust->langzaam met hold; langzaam: hold naar
        jumpscare, idle terug naar rust, ruime timeout zodat de idle-tak
        eerst komt."""
        motion = {"to": "jumpscare", "require_motion_secs": require,
                  "idle": {"to": "rust"}}
        if idle_still is not None:
            motion["idle"]["still_secs"] = idle_still
        cfg = {
            "motion": {"still_secs": still},
            "kind_mode": False,
            "states": [
                {"id": "rust", "advance": {
                    "motion": {"to": "langzaam", "cooldown": 0.0}}},
                {"id": "langzaam", "advance": {
                    "motion": motion,
                    "timeout": {"secs": 60.0, "to": "rust"}}},
                {"id": "jumpscare", "advance": {
                    "timeout": {"secs": 1.0, "to": "rust"}}},
            ],
            "entry_state": "rust",
        }
        return cfg

    def test_hold_timer_resets_when_person_leaves(self):
        e = self.mk(self.make_langzaam_cfg(require=3.0))
        e.tick(1.0, True)                    # rust -> langzaam; hold start
        e.tick(2.5, True)                    # 1,5 s gehouden
        e.tick(3.0, False)                   # weg: hold-timer reset
        e.tick(4.0, True)                    # terug: hold start opnieuw (t=4)
        self.assertEqual(e.tick(6.5, True), [])       # pas 2,5 s
        self.assertEqual(e.tick(7.0, True), [
            {"event": "transition", "from": "langzaam", "to": "jumpscare",
             "reason": "hold"}])

    def test_person_leaving_returns_to_rust_via_idle(self):
        e = self.mk(self.make_langzaam_cfg(require=3.0, still=5.0))
        e.tick(1.0, True)                    # -> langzaam
        e.tick(2.0, True)                    # 1 s gehouden (nog geen hold)
        e.tick(3.0, False)                   # persoon weg
        self.assertEqual(e.tick(7.0, False), [])      # 4 s stil: nog niets
        self.assertEqual(e.tick(8.0, False), [
            {"event": "transition", "from": "langzaam", "to": "rust",
             "reason": "motion"}])           # 5 s stil -> idle-tak
        self.assertEqual(e.state_id, "rust")

    def test_idle_still_secs_override(self):
        e = self.mk(self.make_langzaam_cfg(idle_still=2.0))
        e.tick(1.0, True)
        e.tick(2.0, False)
        self.assertEqual(e.tick(3.9, False), [])
        self.assertEqual(e.tick(4.0, False), [
            {"event": "transition", "from": "langzaam", "to": "rust",
             "reason": "motion"}])

    def test_absent_in_rust_does_nothing(self):
        e = self.mk()
        self.assertEqual(e.tick(50.0, False), [])
        self.assertEqual(e.state_id, "rust")


class TestGoto(EngineTestBase):
    def test_goto_known_and_unknown(self):
        e = self.mk()
        ev = e.goto("kalm", 5.0)
        self.assertEqual(ev, [{"event": "transition", "from": "rust",
                               "to": "kalm", "reason": "goto"}])
        self.assertEqual(e.since, 5.0)
        self.assertEqual(e.state_name, "Kalmeren")
        # onbekende state: geen event, geen crash
        self.assertEqual(e.goto("bestaatniet", 6.0), [])
        self.assertEqual(e.state_id, "kalm")

    def test_goto_same_state_is_noop(self):
        e = self.mk()
        self.assertEqual(e.goto("rust", 5.0), [])

    def test_goto_resets_hold_clocks(self):
        e = self.mk()
        e.goto("langzaam", 5.0)
        e.tick(6.0, True)                    # aanwezigheid start op t=6
        self.assertEqual(e.tick(8.9, True), [])       # 2,9 s < 3 s
        self.assertEqual(e.tick(9.0, True), [
            {"event": "transition", "from": "langzaam", "to": "jumpscare",
             "reason": "hold"}])

    def test_goto_emits_init_when_first_call(self):
        e = engine_mod.Engine(default_cfg(), 0.0)
        ev = e.goto("kalm", 3.0)             # nog nooit getickt
        self.assertEqual([x["reason"] for x in ev], ["init", "goto"])


class TestLoad(EngineTestBase):
    def test_load_keeps_current_state(self):
        e = self.mk()
        e.goto("kalm", 5.0)
        e.load(default_cfg(), now=6.0)
        self.assertEqual(e.state_id, "kalm")

    def test_load_falls_back_to_entry(self):
        e = self.mk()
        e.goto("kalm", 5.0)
        cfg = default_cfg()
        cfg["states"] = [s for s in cfg["states"] if s["id"] != "kalm"]
        cfg["states"][0]["advance"]["motion"]["to"] = "rust"  # zelf-loop dicht
        e.load(cfg, now=6.0)
        self.assertEqual(e.state_id, "rust")

    def test_custom_states_and_entry(self):
        cfg = {"kind_mode": False,
               "states": [
                   {"id": "a", "name": "A", "loop": True,
                    "canvas_mode": "wide", "media": "breed-id",
                    "advance": {"timeout": {"secs": 1.0, "to": "b"}}},
                   {"id": "b", "name": "B",
                    "advance": {"motion": {"to": "a"}}},
               ],
               "entry_state": "b"}
        e = engine_mod.Engine(cfg, 0.0)
        e.tick(0.0, False)
        self.assertEqual(e.state_id, "b")
        self.assertEqual(e.state_name, "B")
        # b --motion--> a (persoon aanwezig, geen hold)
        self.assertEqual(e.tick(1.0, True)[0]["to"], "a")
        # a --timeout 1 s--> b (loop-state: timeout werkt gewoon)
        self.assertEqual(e.tick(2.0, False)[0]["reason"], "timeout")
        self.assertEqual(e.state_id, "b")


if __name__ == "__main__":
    unittest.main()
