#!/usr/bin/env python3
"""Grimoria v2 — pure fase-machine (FSM).

Puur stdlib, geen I/O, geen pygame/numpy/GL: volledig deterministisch
unit-testbaar (tests/test_engine.py geeft de klok mee). De beller (main-loop
van grimoria_pi.py) roostert per frame:

    events = engine.tick(time.time(), person_present)

en krijgt bij een overgang een eventlijst terug:

    [{"event": "transition", "from": "rust", "to": "langzaam",
      "reason": "motion"}]

redenen: "init" (eerste tick: None -> entry_state), "motion" (persoonsrand
zonder hold-tijd), "hold" (persoonsrand ná require_motion_secs aaneengesloten
aanwezig), "timeout" (puur tijdsbasis) en "motion" ook voor de idle-tak
(persoon weg). "goto" is voor handmatige overgangen.

State-model (gevalideerd/genormaliseerd door schemas.py):

    advance: {
      "motion": {"to": ..., "require_motion_secs"?: f, "kind_to"?: str,
                 "cooldown"?: f,
                 "idle"?: {"to": str, "still_secs"?: f}},
      "timeout"?: {"secs": f, "to": str},
      "idle"?:   {"to": str}            # alias voor motion.idle
    }

Semantiek (besluiten uit het ontwerpdoc):

* motion-edge  : vuurt als de persoon aanwezig is (en bij
  `require_motion_secs` pas ná zoveel seconden *aaneengesloten* aanwezig).
  Target = `kind_to` als cfg["kind_mode"] aan staat én kind_to geldig is,
  anders `to`. Kind_mode laat de keten dus doorlopen naar kalm i.p.v. de
  jumpscare (besluit 6); handmatig goto() negeert kind_mode altijd.
* cooldown     : na een overgang over een motion-edge is díé edge
  (sleutel: <state>|motion) gedurende `cooldown` seconden geblokkeerd;
  de timer start op het moment van de overgang. Tijdens de blokkade blijft
  de hold-timer gewoon lopen — na afloop van de cooldown mag dezelfde
  aanwezigheid de overgang alsnog triggeren. Default 0.0 (schemas.py zet
  voor scare-doelen timing.scare_cooldown).
* timeout-edge : vuurt na `secs` seconden in de state, onafhankelijk van
  aanwezigheid.
* idle-tak     : persoon weg gedurende `still_secs` (default:
  cfg["motion"]["still_secs"], override via motion.idle.still_secs)
  -> overgang naar idle.to. Geïmplementeerd als motion.idle (met alias
  advance.idle), dus aan het motion-edge-concept; reden "motion".
* Prioriteit per tick: timeout > motion/hold > idle, en er wordt maar
  één overgang per tick uitgevoerd. Na elke overgang beginnen de
  aanwezigheidsklokken opnieuw (hold telt dus per state), zodat er nooit
  een keten rust→langzaam→jumpscare in één tick ontstaat.

`loop` wordt alleen opgeslagen (de renderer/mediakant doet er iets mee);
voor de engine is een loop-state niets bijzonders en timeout werkt gewoon.
"""

from __future__ import annotations

import copy

__all__ = ["Engine"]


class Engine:
    """Configureerbare, pure fase-machine. Single-threaded gebruiken."""

    def __init__(self, cfg: dict, now: float = 0.0):
        self._states: dict[str, dict] = {}
        self._entry: str = ""
        self._kind_mode: bool = False
        self._still_secs_default: float = 5.0
        self._load(cfg)

        # klok + toestand
        self._t0: float = float(now)          # begin van de huidige state
        self._last_now: float = float(now)
        self._state_id: str = self._entry
        self._init_sent: bool = False         # init-event nog niet gegeven

        # aanwezigheid (wordt door tick bijgehouden)
        self._person_present: bool = False
        self._person_since: float | None = None   # start aaneengesloten aanwezig
        self._absent_since: float | None = None   # start aaneengesloten afwezig
        self._last_person_ts: float | None = None

        # cooldowns: "<state-id>|motion" -> epoch van laatste overgang
        self._edge_fired: dict[str, float] = {}

    # ------------------------------------------------------------ config
    def _load(self, cfg: dict) -> None:
        """Lees cfg in. Ongeldige configs -> ValueError (schemas.py voorkomt
        dat normaal; de engine is zelf ook defensief)."""
        if not isinstance(cfg, dict):
            raise ValueError("cfg moet een dict zijn")
        states = cfg.get("states")
        if not isinstance(states, list) or not states:
            raise ValueError("cfg['states'] moet een niet-lege lijst zijn")
        parsed: dict[str, dict] = {}
        for st in states:
            if not isinstance(st, dict):
                continue
            sid = st.get("id")
            if not isinstance(sid, str) or not sid or sid in parsed:
                continue
            adv_in = st.get("advance") or {}
            adv = adv_in if isinstance(adv_in, dict) else {}
            motion = adv.get("motion")
            parsed[sid] = {
                "id": sid,
                "name": st.get("name") if isinstance(st.get("name"), str)
                        and st.get("name") else sid,
                "loop": bool(st.get("loop", True)),
                "canvas_mode": st.get("canvas_mode", "split"),
                "media": copy.deepcopy(st.get("media")) if isinstance(
                    st.get("media"), (dict, str)) else {},
                "advance": {
                    "motion": copy.deepcopy(motion) if isinstance(motion, dict) else None,
                    "timeout": copy.deepcopy(adv.get("timeout"))
                               if isinstance(adv.get("timeout"), dict) else None,
                    "idle": copy.deepcopy(adv.get("idle"))
                            if isinstance(adv.get("idle"), dict) else None,
                },
            }
        if not parsed:
            raise ValueError("cfg['states'] bevat geen bruikbare state")

        entry = cfg.get("entry_state")
        if not isinstance(entry, str) or entry not in parsed:
            entry = next(iter(parsed))
        self._states = parsed
        self._entry = entry
        self._kind_mode = bool(cfg.get("kind_mode", False))
        motion_cfg = cfg.get("motion")
        self._still_secs_default = 5.0
        if isinstance(motion_cfg, dict):
            v = motion_cfg.get("still_secs", 5.0)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                self._still_secs_default = float(v)
        # dode overgangen (doel bestaat niet) neutraliseren
        ids = set(parsed)
        for st in parsed.values():
            adv = st["advance"]
            m = adv["motion"]
            if m is not None and m.get("to") not in ids:
                adv["motion"] = None
            t = adv["timeout"]
            if t is not None and t.get("to") not in ids:
                adv["timeout"] = None
            i = adv["idle"]
            if i is not None and i.get("to") not in ids:
                adv["idle"] = None

    def load(self, cfg: dict, now: float | None = None) -> None:
        """(Her)laad config. Huidige state blijft staan als die nog bestaat,
        anders terug naar entry_state (t0 wordt opnieuw gezet)."""
        current = self._state_id
        self._load(cfg)
        if now is None:
            now = self._last_now
        now = float(now)
        if current in self._states:
            self._state_id = current
        else:
            self._state_id = self._entry
            self._t0 = now
        self._last_now = now

    # ------------------------------------------------------------ attributen
    @property
    def state_id(self) -> str:
        return self._state_id

    @property
    def state_name(self) -> str:
        return self._state()["name"]

    @property
    def name(self) -> str:
        """Compat-alias voor state_name (api_v2 leest eng.name)."""
        return self.state_name

    @property
    def since(self) -> float:
        """t0 (epoch) waarop de huidige state inging."""
        return self._t0

    @property
    def since_secs(self) -> float:
        """Seconden in de huidige state (t.o.v. de laatst verwerkte klok)."""
        return max(0.0, self._last_now - self._t0)

    @property
    def kind_mode(self) -> bool:
        return self._kind_mode

    @kind_mode.setter
    def kind_mode(self, value: bool) -> None:
        self._kind_mode = bool(value)

    @property
    def person_present(self) -> bool:
        return self._person_present

    @property
    def last_person_ts(self) -> float | None:
        """Epoch van de laatste keer dat de persoon gezien werd (None = nooit)."""
        return self._last_person_ts

    @property
    def entry_state(self) -> str:
        return self._entry

    # compat-aliassen voor api_v2 (/api/state leest eng.last_motion_ts en
    # eng.motion_active); inhoudelijk hetzelfde als de person_*-velden
    @property
    def last_motion_ts(self) -> float | None:
        return self._last_person_ts

    @property
    def motion_active(self) -> bool:
        return self._person_present

    def state(self) -> dict:
        """Kopie van de huidige state-definitie (voor /api/state e.d.)."""
        st = self._state()
        return {
            "id": st["id"],
            "name": st["name"],
            "loop": st["loop"],
            "canvas_mode": st["canvas_mode"],
            "media": copy.deepcopy(st["media"]),
            "advance": copy.deepcopy(st["advance"]),
        }

    def cooldown_remaining(self, state_id: str | None = None) -> float:
        """Resterende cooldown (s) op de motion-edge van de gegeven state
        (default: huidige state). 0.0 = niet geblokkeerd."""
        sid = state_id if state_id is not None else self._state_id
        fired = self._edge_fired.get(f"{sid}|motion")
        if fired is None:
            return 0.0
        cd = self._cooldown_secs(sid)
        return max(0.0, cd - (self._last_now - fired))

    def _cooldown_secs(self, state_id: str) -> float:
        m = self._states[state_id]["advance"]["motion"]
        if not m:
            return 0.0
        v = m.get("cooldown", 0.0)
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0

    def _state(self) -> dict:
        return self._states[self._state_id]

    # ------------------------------------------------------------ tick/goto
    def tick(self, now: float, person_present: bool | None = None) -> list[dict]:
        """Verwerk één klok-puls. person_present: is er nú iemand gezien?
        None = onveranderd (laatste waarde blijven gebruiken). Retourneert
        0 of 1 transition-event."""
        now = float(now)
        self._last_now = now
        events: list[dict] = []
        if not self._init_sent:
            self._init_sent = True
            events.append({"event": "transition", "from": None,
                           "to": self._state_id, "reason": "init"})
            return events

        # --- aanwezigheid bijwerken (aaneengesloten klokken)
        if person_present is not None:
            person_present = bool(person_present)
            if person_present:
                self._last_person_ts = now
                if not self._person_present:
                    self._person_present = True
                    self._person_since = now
                    self._absent_since = None
            else:
                if self._person_present:
                    self._person_present = False
                    self._absent_since = now
                    self._person_since = None

        st = self._state()
        adv = st["advance"]

        # --- 1) timeout (tijdsbasis, ongeacht persoon)
        timeout = adv["timeout"]
        if timeout:
            secs = self._num(timeout.get("secs"), 0.0)
            if now - self._t0 >= secs:
                return events + self._transition(timeout.get("to"), now,
                                                 "timeout")

        # --- 2) motion/hold + cooldown + kind_to
        motion = adv["motion"]
        if motion:
            target = self._resolve_target(motion)
            if target is not None:
                require = self._num(motion.get("require_motion_secs"), 0.0)
                if self._person_present and self._person_since is not None:
                    held = now - self._person_since
                    if held >= require:
                        key = f"{self._state_id}|motion"
                        fired = self._edge_fired.get(key)
                        cooldown = self._cooldown_secs(self._state_id)
                        if fired is None or now - fired >= cooldown:
                            return events + self._transition(
                                target, now,
                                "hold" if require > 0.0 else "motion",
                                edge_key=key)

        # --- 3) idle-tak: persoon aaneengesloten weg -> idle.to
        idle = adv["motion"]["idle"] if (motion and
                                         isinstance(motion.get("idle"), dict)) \
            else adv["idle"]
        if idle and not self._person_present and self._absent_since is not None:
            still = self._num(idle.get("still_secs"),
                              self._still_secs_default)
            if now - self._absent_since >= still:
                return events + self._transition(idle.get("to"), now,
                                                 "motion")
        return events

    def goto(self, state_id: str, now: float, reason: str = "goto",
             force: bool = True) -> list[dict]:
        """Handmatige overgang (testknop / API): forceert, negeert cooldowns,
        hold-tijden én kind_mode (besluit 6). Onbekende state -> geen event.
        `force` wordt geaccepteerd voor api-compat (api_v2 roept aan met
        force=True); handmatig gaat ALWAYS vóór kind_mode."""
        now = float(now)
        self._last_now = now
        events: list[dict] = []
        if not self._init_sent:
            self._init_sent = True
            events.append({"event": "transition", "from": None,
                           "to": self._state_id, "reason": "init"})
        if state_id not in self._states:
            return events
        return events + self._transition(state_id, now, reason)

    # ------------------------------------------------------------ intern
    @staticmethod
    def _num(v, default: float) -> float:
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
        return default

    def _resolve_target(self, motion: dict) -> str | None:
        """Doel van de motion-edge: kind_to bij kind_mode, anders to."""
        to = motion.get("to")
        if self._kind_mode:
            kt = motion.get("kind_to")
            if isinstance(kt, str) and kt in self._states:
                return kt
        if isinstance(to, str) and to in self._states:
            return to
        return None

    def _transition(self, target, now: float, reason: str,
                    edge_key: str | None = None) -> list[dict]:
        """Voer overgang uit (met zelf-guard en cooldown-registratie)."""
        if not isinstance(target, str) or target not in self._states:
            return []
        if target == self._state_id:
            # self-transition is een no-op (voorkomt event-storms)
            return []
        if edge_key:
            self._edge_fired[edge_key] = now
        event = {"event": "transition", "from": self._state_id,
                 "to": target, "reason": reason}
        self._state_id = target
        self._t0 = now
        # aanwezigheidsklokken herstarten: hold/still tellen per state
        if self._person_present:
            self._person_since = now
        else:
            self._absent_since = now
        return [event]
