#!/usr/bin/env python3
"""
Unit-tests voor de Grimoria v2 fase-machine (engine.py).

Draaien:  python3 test_engine.py
Geen pytest nodig: mini assert-framework met duidelijke PASSED/FAILED-output.
Testketen = exact de keten uit docs/ontwerp-grimoria-webinterface.md:

  rust ──beweging──▶ langzaam ──beweging houdt aan (3 s)──▶ jumpscare ──(2,5 s)──▶ kalm ──(20 s)──▶ rust
                       └──beweging stopt (5 s)──▶ rust
"""

import sys

from engine import Engine

T0 = 1000.0     # vaste starttijd (epoch-achtig), deterministisch


def keten_config() -> dict:
    """De standaardketen uit het ontwerp, als config-model."""
    return {
        "kind_mode": False,
        "entry_state": "rust",
        "states": [
            {"id": "rust", "name": "Rust", "loop": True,
             "media": {"0": "rust-a", "1": "rust-b"},
             "advance": {"motion": {"to": "langzaam", "cooldown": 90},
                         "timeout": None}},
            {"id": "langzaam", "name": "Langzame beweging", "loop": True,
             "media": {"0": "langzaam-a", "1": "langzaam-b"},
             "advance": {"motion": {"to": "jumpscare", "require_motion_secs": 3.0},
                         "timeout": {"secs": 20, "to": "rust"},
                         "still": {"to": "rust", "still_secs": 5}}},
            {"id": "jumpscare", "name": "Jumpscare", "loop": False, "scare": True,
             "media": {"0": "jump-a", "1": "jump-b"},
             "advance": {"timeout": {"secs": 2.5, "to": "kalm"}}},
            {"id": "kalm", "name": "Kalmeren", "loop": True,
             "media": {"0": "kalm-a", "1": "kalm-b"},
             "advance": {"timeout": {"secs": 20, "to": "rust"}}},
        ],
    }


# ---------------------------------------------------------------- framework
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


# ---------------------------------------------------------------- tests
def test_start():
    sectie("start & eigenschappen")
    e = Engine(keten_config(), now=T0)
    check("start in entry_state 'rust'", e.state_id == "rust")
    check("naam uit config", e.name == "Rust")
    check("since_secs is 0 bij start", abs(e.since_secs) < 1e-9)
    check("kind_mode default False", e.kind_mode is False)
    check("last_motion_ts is None zonder beweging", e.last_motion_ts is None)
    check("geen cooldowns actief bij start",
          all(c["remaining_secs"] == 0.0 for c in e.cooldowns()))


def test_rust_naar_langzaam():
    sectie("rust ──beweging──▶ langzaam (cooldown 90 s)")
    e = Engine(keten_config(), now=T0)
    e.on_motion(True, T0 + 1)
    check("beweging gezien: last_motion_ts gezet", e.last_motion_ts == T0 + 1)
    check("motion_active True", e.motion_active is True)
    wissel = e.tick(T0 + 1.5)
    check("rust→langzaam meteen bij beweging (hold=0)", wissel and e.state_id == "langzaam")
    check("naam 'Langzame beweging'", e.name == "Langzame beweging")
    cd = {c["state"]: c for c in e.cooldowns()}
    check("cooldown op rust|motion gestart",
          cd["rust"]["last_fired"] == T0 + 1.5 and cd["rust"]["blocked"])
    check("cooldown-remaining ~90", 89 < cd["rust"]["remaining_secs"] <= 90)


def test_langzaam_hold_en_jumpscare():
    sectie("langzaam ──beweging houdt aan (3 s)──▶ jumpscare")
    e = Engine(keten_config(), now=T0)
    e.on_motion(True, T0 + 1)
    e.tick(T0 + 1.5)                      # rust → langzaam
    check("nog in langzaam vóór hold verstreken",
          e.tick(T0 + 3.9) is False and e.state_id == "langzaam")
    check("motion_held_secs loopt op (state-ingang herstart klok)",
          2.3 < e.motion_held_secs < 2.5)
    wissel = e.tick(T0 + 1 + 4.5)
    check("na 3 s aanhoudende beweging (sinds state-ingang) → jumpscare",
          wissel and e.state_id == "jumpscare")

    sectie("langzaam ──beweging stopt (5 s)──▶ rust")
    e2 = Engine(keten_config(), now=T0)
    e2.on_motion(True, T0 + 1)
    e2.tick(T0 + 1.5)                     # in langzaam
    e2.on_motion(False, T0 + 4)           # beweging stopt
    check("still_secs telt vanaf stop", e2.tick(T0 + 8) is False
          and e2.state_id == "langzaam")
    wissel = e2.tick(T0 + 9)              # 5 s stilte na T0+4
    check("na 5 s stilte → rust", wissel and e2.state_id == "rust")


def test_jumpscare_kalm_rust_timeouts():
    sectie("jumpscare ──2,5 s──▶ kalm ──20 s──▶ rust")
    e = Engine(keten_config(), now=T0)
    e.goto("jumpscare", T0, force=True)
    check("nog in jumpscare bij 2,4 s", e.tick(T0 + 2.4) is False
          and e.state_id == "jumpscare")
    check("na 2,5 s → kalm", e.tick(T0 + 2.5) and e.state_id == "kalm")
    check("kalm blijft bij 19,9 s", e.tick(T0 + 22.4) is False
          and e.state_id == "kalm")
    check("na 20 s → rust", e.tick(T0 + 22.5) and e.state_id == "rust")


def test_cooldown():
    sectie("cooldown blokkeert tweede motion-trigger")
    e = Engine(keten_config(), now=T0)
    e.on_motion(True, T0 + 1)
    e.tick(T0 + 1.5)                      # rust → langzaam, cooldown gestart
    e.on_motion(False, T0 + 2)
    e.tick(T0 + 8)                        # 5 s stil → terug naar rust
    check("weer in rust", e.state_id == "rust")
    e.on_motion(True, T0 + 10)            # nieuwe beweging binnen 90 s-cooldown
    wissel = e.tick(T0 + 10.5)
    check("rust→langzaam geblokkeerd door cooldown",
          wissel is False and e.state_id == "rust")
    e.on_motion(False, T0 + 12)
    e.on_motion(True, T0 + 91.6)          # na afloop van de 90 s (laatste vuur T0+1.5)
    wissel = e.tick(T0 + 92)
    check("na cooldown-verloop werkt de trigger weer",
          wissel and e.state_id == "langzaam")
    cd = {c["state"]: c for c in e.cooldowns()}
    check("cooldown-overzicht toont nieuwe vuurtijd",
          cd["rust"]["last_fired"] == T0 + 92 and cd["rust"]["blocked"])


def test_kind_mode():
    sectie("kind_mode slaat jumpscare over")
    cfg = keten_config()
    cfg["kind_mode"] = True
    e = Engine(cfg, now=T0)
    e.on_motion(True, T0 + 1)
    e.tick(T0 + 1.5)                      # rust → langzaam
    wissel = e.tick(T0 + 5)               # 3,5 s aanhoudende beweging
    check("jumpscare overgeslagen → direct kalm",
          wissel and e.state_id == "kalm")
    e.kind_mode = False                   # setter werkt
    e2 = Engine(keten_config(), now=T0)   # vers: geen cooldown-voorgeschiedenis
    e2.goto("langzaam", T0 + 6, force=True)
    e2.on_motion(True, T0 + 6)
    wissel = e2.tick(T0 + 9.5)            # hold 3 s verstreken sinds state-ingang
    check("kind_mode uit: jumpscare wordt wél genomen",
          wissel and e2.state_id == "jumpscare")
    # force=True op de scare-state zelf negeert kind_mode altijd
    e2 = Engine(keten_config(), now=T0)
    e2.kind_mode = True
    e2.goto("jumpscare", T0 + 1, force=True)
    check("goto force=True gaat ook mét kind_mode naar jumpscare",
          e2.state_id == "jumpscare")
    # force=False respecteert kind_mode: springt door naar timeout-doel
    e2.goto("jumpscare", T0 + 2, force=False)
    check("goto force=False + kind_mode → skip naar 'kalm'",
          e2.state_id == "kalm")


def test_goto():
    sectie("goto forceert handmatige overgang")
    e = Engine(keten_config(), now=T0)
    check("goto onbekende state → False", e.goto("nietbestaand", T0) is False)
    check("goto jumpscare → True", e.goto("jumpscare", T0, force=True) is True)
    check("nu in jumpscare", e.state_id == "jumpscare")
    check("goto dezelfde state geeft True (herstart-timer)",
          e.goto("jumpscare", T0 + 0.1, force=True) is True)
    check("state-timer herstart", abs(e.since_secs - 0.0) < 1e-6)


def test_onbekend_doel_en_valide():
    sectie("robustheid: kapotte edge-doelen & invalid config")
    cfg = keten_config()
    cfg["states"][1]["advance"]["motion"]["to"] = "bestaatniet"
    e = Engine(cfg, now=T0)
    e.on_motion(True, T0 + 1)
    e.tick(T0 + 1.5)                      # rust → langzaam
    check("kapot motion-doel → edge vervalt, geen crash",
          e.tick(T0 + 5) is False and e.state_id == "langzaam")
    for bad in ({}, {"states": []}, {"states": [{"id": "x"}], "entry_state": "jij"},
                {"states": [{"id": "a"}, {"id": "a"}]}):
        try:
            Engine(bad, now=T0)
            ok = False
        except ValueError:
            ok = True
        check(f"ongeldig model geweigerd: {bad!r:.60}", ok)


def test_keten_eind_aan_eind():
    sectie("volledige keten end-to-end (kind_mode uit)")
    e = Engine(keten_config(), now=T0)
    stappen = [
        (T0 + 1.0, lambda: e.on_motion(True, T0 + 1.0), None),
        (T0 + 1.5, lambda: e.tick(T0 + 1.5), "langzaam"),    # beweging → langzaam
        (T0 + 4.5, lambda: e.tick(T0 + 4.5), "jumpscare"),   # 3 s aangehouden
        (T0 + 7.0, lambda: e.tick(T0 + 7.0), "kalm"),        # 2,5 s scare
        (T0 + 27.0, lambda: e.tick(T0 + 27.0), "rust"),      # 20 s kalm
    ]
    for nu, act, verwacht in stappen:
        act()
        if verwacht:
            check(f"t={nu - T0:5.1f}s → {verwacht}", e.state_id == verwacht,
                  f"zit in {e.state_id}")
    check("keten roundtrip: terug in rust",
          e.state_id == "rust")
    e.on_motion(False, T0 + 28)
    check("motion-status netjes afgesloten (last_motion_ts blijft staan)",
          e.motion_active is False and e.last_motion_ts == T0 + 1.0)


def main() -> int:
    for test in (test_start, test_rust_naar_langzaam, test_langzaam_hold_en_jumpscare,
                 test_jumpscare_kalm_rust_timeouts, test_cooldown, test_kind_mode,
                 test_goto, test_onbekend_doel_en_valide, test_keten_eind_aan_eind):
        test()
    print(f"\n{'=' * 46}")
    status = "ALLES GOED" if _OK == _TOT else "FALEN!"
    print(f"  {_OK}/{_TOT} checks geslaagd  —  {status}")
    return 0 if _OK == _TOT else 1


if __name__ == "__main__":
    sys.exit(main())
