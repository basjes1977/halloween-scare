#!/usr/bin/env python3
"""Grimoria v2 — config-model en validatie.

Converteert/valideert ruwe config (uit config.json of de webinterface) naar
het v2-model uit docs/ontwerp-grimoria-webinterface.md. Contract:

    clean, errors = validate_config(ruw_dict)

    * clean  : complete, genormaliseerde config (defaults aangevuld). Bij
               niet-fatale fouten zo veel mogelijk van de invoer behouden;
               bij fatale fouten (states onbruikbaar) terugvallen op de
               DEFAULT_CONFIG-states zodat de server altijd kan opstarten.
    * errors : lijst menselijk-leesbare foutmeldingen (Nederlandse teksten).

`validate_config` is puur en gooit nooit — een webinterface kan de functie
dus aanroepen zonder try/except.

Backward-compat: onbekende top-level keys (ha_url, ha_token, llm_url,
camera, jumpscare_kans, api_port, ...) worden onaangetast doorgegeven in
`clean`; de server filtert zelf secrets weg bij API-responses.

States-model (v2, zie ontwerpdoc):

    { "id", "name", "loop", "canvas_mode": "split"|"wide",
      "media": {"0": "<lib-id>", "1": "<lib-id>"} | "<lib-id>" (wide),
      "advance": {
        "motion": {"to", "require_motion_secs"?, "kind_to"?, "cooldown"?,
                   "idle": {"to"?} }?,
        "timeout": {"secs", "to"} | None } }

Semantiek van de overgangen staat in engine.py (docstring).
"""

from __future__ import annotations

import copy

# --------------------------------------------------------------------------
# Defaults (uit het ontwerpdoc; besluit 4: alle tijden in de UI instelbaar)
# --------------------------------------------------------------------------

DEFAULT_CONFIG: dict = {
    "screens": {
        "swap": False,
        "offset": {"dx": 0.0, "dy": 0.0},
        "items": [
            {
                "rotation": 0,
                "overlay": {"enabled": False, "text": "", "x": 0.5,
                            "y": 0.85, "size": 48, "color": "#ffaa00"},
            },
            {
                "rotation": 0,
                "overlay": {"enabled": False, "text": "", "x": 0.5,
                            "y": 0.85, "size": 48, "color": "#ffaa00"},
            },
        ],
    },
    "playback": {"fps": 16, "render_fps": 30, "crossfade": True},
    "motion": {
        "enabled": True,
        "detector": "tflite",
        "model": "models/ssd_mobilenet_v2.tflite",
        "detect_fps": 3,
        "person_conf": 0.5,
        "person_hold_secs": 3.0,
        "present_memory_secs": 1.0,
        "still_secs": 5.0,
        "fallback_frame_diff": True,
        # drempels voor de frame-diff-fallback (gelijk aan grimoria_pi.py)
        "diff_thr": 25,
        "min_frac": 0.012,
    },
    "timing": {"scare_cooldown": 90.0},
    "kind_mode": True,
    "states": [
        {
            "id": "rust",
            "name": "Rust",
            "loop": True,
            "canvas_mode": "split",
            "media": {"0": "", "1": ""},
            "advance": {
                "motion": {"to": "langzaam", "cooldown": 90.0},
                "timeout": None,
            },
        },
        {
            "id": "langzaam",
            "name": "Langzame beweging",
            "loop": True,
            "canvas_mode": "split",
            "media": {"0": "", "1": ""},
            "advance": {
                "motion": {"to": "jumpscare", "require_motion_secs": 3.0,
                           "kind_to": "kalm",
                           "idle": {"to": "rust"}},
                "timeout": {"secs": 20.0, "to": "rust"},
            },
        },
        {
            "id": "jumpscare",
            "name": "Jumpscare",
            "loop": False,
            "canvas_mode": "split",
            "media": {"0": "", "1": ""},
            "advance": {"timeout": {"secs": 2.5, "to": "kalm"}},
        },
        {
            "id": "kalm",
            "name": "Kalmeren",
            "loop": True,
            "canvas_mode": "split",
            "media": {"0": "", "1": ""},
            "advance": {"timeout": {"secs": 20.0, "to": "rust"}},
        },
    ],
    "entry_state": "rust",
}

# Toegestane waarden per veld (voor validatie)
_CANVAS_MODES = ("split", "wide")
_ROTATIONS = (0, 90, 180, 270)


def _is_num(v) -> bool:
    """Alle getallen behalve bool (bool is een subclass van int)."""
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(v)))


def _fix_num(container: dict, key: str, default, lo=None, hi=None) -> None:
    """Normaliseer container[key] naar een getal binnen [lo, hi]."""
    v = container.get(key, default)
    if not _is_num(v):
        container[key] = default
    else:
        v = float(v)
        if lo is not None:
            v = max(lo, v)
        if hi is not None:
            v = min(hi, v)
        container[key] = v


def _fix_bool(container: dict, key: str, default) -> None:
    v = container.get(key, default)
    container[key] = default if not isinstance(v, bool) else v


# --------------------------------------------------------------------------
# screens / playback / motion / timing
# --------------------------------------------------------------------------

def _validate_screens(screens, errors: list) -> dict:
    clean = copy.deepcopy(DEFAULT_CONFIG["screens"])
    if screens is None:
        return clean
    if not isinstance(screens, dict):
        errors.append("screens: moet een object zijn — defaults gebruikt")
        return clean

    _fix_bool(screens, "swap", clean["swap"])
    clean["swap"] = bool(screens["swap"])

    off = screens.get("offset")
    if isinstance(off, dict):
        _fix_num(off, "dx", 0.0, -2.0, 2.0)
        _fix_num(off, "dy", 0.0, -2.0, 2.0)
        clean["offset"] = {"dx": float(off["dx"]), "dy": float(off["dy"])}
    elif off is not None:
        errors.append("screens.offset: moet een object zijn — default gebruikt")

    items = screens.get("items")
    if items is not None:
        if not isinstance(items, list):
            errors.append("screens.items: moet een lijst zijn — defaults gebruikt")
        else:
            out = []
            for i, it in enumerate(items[:2]):      # twee schermen
                out.append(_validate_screen_item(it, i, errors))
            if len(items) > 2:
                errors.append("screens.items: maximaal 2 schermen — "
                              "extra items genegeerd")
            clean["items"] = out
    return clean


def _validate_screen_item(item, idx: int, errors: list) -> dict:
    template = copy.deepcopy(DEFAULT_CONFIG["screens"]["items"][0])
    if not isinstance(item, dict):
        errors.append(f"screens.items[{idx}]: moet een object zijn — "
                      "default gebruikt")
        return template
    rot = item.get("rotation", template["rotation"])
    if _is_num(rot) and int(rot) in _ROTATIONS:
        template["rotation"] = int(rot)
    elif _is_num(rot):
        # arbitraire graden zijn in het model toegestaan (ontwerp besluit 1);
        # normaliseren naar 0..359 en de preset-check door de UI laten doen
        template["rotation"] = int(rot) % 360
    else:
        errors.append(f"screens.items[{idx}].rotation: geen getal — "
                      "default gebruikt")

    ov = item.get("overlay")
    if ov is None:
        return template
    if not isinstance(ov, dict):
        errors.append(f"screens.items[{idx}].overlay: moet een object zijn — "
                      "default gebruikt")
        return template
    t = template["overlay"]
    _fix_bool(ov, "enabled", t["enabled"])
    t["enabled"] = bool(ov["enabled"])
    txt = ov.get("text", t["text"])
    t["text"] = txt if isinstance(txt, str) else t["text"]
    for key in ("x", "y"):
        _fix_num(ov, key, t[key], 0.0, 1.0)
        t[key] = float(ov[key])
    _fix_num(ov, "size", t["size"], 6.0, 400.0)
    t["size"] = float(ov["size"])
    col = ov.get("color", t["color"])
    if isinstance(col, str) and len(col) == 7 and col.startswith("#"):
        t["color"] = col.lower()
    else:
        errors.append(f"screens.items[{idx}].overlay.color: "
                      "verwacht '#rrggbb' — default gebruikt")
    return template


def _validate_playback(playback, errors: list) -> dict:
    clean = copy.deepcopy(DEFAULT_CONFIG["playback"])
    if playback is None:
        return clean
    if not isinstance(playback, dict):
        errors.append("playback: moet een object zijn — defaults gebruikt")
        return clean
    _fix_num(playback, "fps", clean["fps"], 1.0, 60.0)
    _fix_num(playback, "render_fps", clean["render_fps"], 1.0, 120.0)
    _fix_bool(playback, "crossfade", clean["crossfade"])
    clean["fps"] = float(playback["fps"])
    clean["render_fps"] = float(playback["render_fps"])
    clean["crossfade"] = bool(playback["crossfade"])
    return clean


def _validate_motion(motion, errors: list) -> dict:
    clean = copy.deepcopy(DEFAULT_CONFIG["motion"])
    if motion is None:
        return clean
    if not isinstance(motion, dict):
        errors.append("motion: moet een object zijn — defaults gebruikt")
        return clean
    _fix_bool(motion, "enabled", clean["enabled"])
    clean["enabled"] = bool(motion["enabled"])
    det = motion.get("detector", clean["detector"])
    if det in ("tflite", "frame_diff"):
        clean["detector"] = det
    else:
        errors.append("motion.detector: moet 'tflite' of 'frame_diff' zijn "
                      "— default gebruikt")
    model = motion.get("model", clean["model"])
    clean["model"] = model if isinstance(model, str) and model else clean["model"]
    _fix_num(motion, "detect_fps", clean["detect_fps"], 1.0, 30.0)
    clean["detect_fps"] = float(motion["detect_fps"])
    _fix_num(motion, "person_conf", clean["person_conf"], 0.0, 1.0)
    clean["person_conf"] = float(motion["person_conf"])
    _fix_num(motion, "person_hold_secs", clean["person_hold_secs"], 0.0, 300.0)
    clean["person_hold_secs"] = float(motion["person_hold_secs"])
    _fix_num(motion, "present_memory_secs", clean["present_memory_secs"],
             0.0, 30.0)
    clean["present_memory_secs"] = float(motion["present_memory_secs"])
    _fix_num(motion, "still_secs", clean["still_secs"], 0.0, 300.0)
    clean["still_secs"] = float(motion["still_secs"])
    _fix_bool(motion, "fallback_frame_diff", clean["fallback_frame_diff"])
    clean["fallback_frame_diff"] = bool(motion["fallback_frame_diff"])
    _fix_num(motion, "diff_thr", clean["diff_thr"], 1.0, 255.0)
    clean["diff_thr"] = float(motion["diff_thr"])
    _fix_num(motion, "min_frac", clean["min_frac"], 0.0, 1.0)
    clean["min_frac"] = float(motion["min_frac"])
    return clean


def _validate_timing(timing, errors: list) -> dict:
    clean = copy.deepcopy(DEFAULT_CONFIG["timing"])
    if timing is None:
        return clean
    if not isinstance(timing, dict):
        errors.append("timing: moet een object zijn — defaults gebruikt")
        return clean
    _fix_num(timing, "scare_cooldown", clean["scare_cooldown"], 0.0, 86400.0)
    clean["scare_cooldown"] = float(timing["scare_cooldown"])
    return clean


# --------------------------------------------------------------------------
# states
# --------------------------------------------------------------------------

def _validate_media(media, canvas_mode: str, sid: str, errors: list) -> dict:
    """Normaliseer media. split -> {"0": id, "1": id}; wide -> {"all": id}.
    Andere vormen worden als fout teruggewezen (defaults/leeg)."""
    if canvas_mode == "wide":
        default = {"all": ""}
        if isinstance(media, str):
            return {"all": media}
        if isinstance(media, dict) and isinstance(media.get("all"), str):
            return {"all": media["all"]}
        if isinstance(media, dict) and media:
            errors.append(f"state {sid!r}: media bij canvas_mode 'wide' is "
                          "één string of {\"all\": id} — leeg gebruikt")
        return default
    # split
    default = {"0": "", "1": ""}
    if media is None:
        return default
    if isinstance(media, str):
        # toegeeflijk: één id voor beide schermen
        return {"0": media, "1": media}
    if not isinstance(media, dict):
        errors.append(f"state {sid!r}: media moet een object zijn — "
                      "leeg gebruikt")
        return default
    out = dict(default)
    for k in ("0", "1"):
        v = media.get(k)
        if v is None:
            continue
        if isinstance(v, str):
            out[k] = v
        else:
            errors.append(f"state {sid!r}: media[{k!r}] moet een string zijn "
                          "— leeg gebruikt")
    extra = set(media) - {"0", "1"}
    if extra:
        errors.append(f"state {sid!r}: onbekende media-keys {sorted(extra)} "
                      "genegeerd")
    return out


def _validate_edge_motion(edge, sid: str, errors: list) -> dict | None:
    """Normaliseer advance.motion. Cooldown-override: als de edge 'cooldown'
    mist, krijgt hij cfg['timing']['scare_cooldown'] als de bestemming een
    state met id 'jumpscare' is (default flow); anders geen cooldown (0.0)."""
    if edge is None:
        return None
    if not isinstance(edge, dict):
        errors.append(f"state {sid!r}: advance.motion moet een object of "
                      "null zijn — edge verwijderd")
        return None
    to = edge.get("to")
    if not isinstance(to, str) or not to:
        errors.append(f"state {sid!r}: advance.motion.to ontbreekt — "
                      "edge verwijderd")
        return None
    clean = {"to": to}
    if "require_motion_secs" in edge:
        v = edge.get("require_motion_secs")
        if _is_num(v):
            clean["require_motion_secs"] = _clamp(v, 0.0, 600.0)
        else:
            errors.append(f"state {sid!r}: require_motion_secs moet een "
                          "getal zijn — veld weggehaald")
    if "kind_to" in edge:
        kt = edge.get("kind_to")
        if isinstance(kt, str) and kt:
            clean["kind_to"] = kt
        else:
            errors.append(f"state {sid!r}: kind_to moet een non-lege string "
                          "zijn — veld weggehaald")
    if "cooldown" in edge:
        v = edge.get("cooldown")
        if _is_num(v):
            clean["cooldown"] = _clamp(v, 0.0, 86400.0)
        else:
            errors.append(f"state {sid!r}: cooldown moet een getal zijn — "
                          "veld weggehaald")
    idle = edge.get("idle")
    if idle is not None:
        if isinstance(idle, dict) and isinstance(idle.get("to"), str) \
                and idle["to"]:
            clean["idle"] = {"to": idle["to"]}
            if _is_num(idle.get("still_secs")):
                clean["idle"]["still_secs"] = _clamp(idle["still_secs"],
                                                     0.0, 600.0)
        elif isinstance(idle, str):
            # toegeeflijk: idle als string
            clean["idle"] = {"to": idle}
        else:
            errors.append(f"state {sid!r}: advance.motion.idle moet "
                          "{\"to\": ...} zijn — veld weggehaald")
    return clean


def _validate_edge_timeout(edge, sid: str, errors: list) -> dict | None:
    if edge is None:
        return None
    if not isinstance(edge, dict):
        errors.append(f"state {sid!r}: advance.timeout moet een object of "
                      "null zijn — edge verwijderd")
        return None
    to = edge.get("to")
    secs = edge.get("secs")
    if not isinstance(to, str) or not to or not _is_num(secs):
        errors.append(f"state {sid!r}: advance.timeout moet 'secs' (getal) "
                      "en 'to' (string) bevatten — edge verwijderd")
        return None
    return {"secs": _clamp(secs, 0.0, 86400.0), "to": to}


def _validate_states(states, errors: list,
                     scare_cooldown: float) -> list:
    """Valideer de states-lijst. Bij fatale schade (minder dan 1 bruikbare
    state) valt hij terug op de DEFAULT_CONFIG-states."""
    if states is None:
        # backward-compat: config vóór de fase-machine had nog geen states;
        # dat is geen fout maar de default-keten
        return copy.deepcopy(DEFAULT_CONFIG["states"])
    if not isinstance(states, list) or not states:
        errors.append("states: moet een niet-lege lijst zijn — "
                      "default states gebruikt")
        return copy.deepcopy(DEFAULT_CONFIG["states"])

    seen: dict[str, int] = {}
    clean_states: list[dict] = []
    for i, st in enumerate(states):
        if not isinstance(st, dict):
            errors.append(f"states[{i}]: moet een object zijn — overgeslagen")
            continue
        sid = st.get("id")
        if not isinstance(sid, str) or not sid:
            errors.append(f"states[{i}]: 'id' ontbreekt of is geen string — "
                          "overgeslagen")
            continue
        if sid in seen:
            errors.append(f"states[{i}]: dubbele state-id {sid!r} — "
                          "overgeslagen")
            continue
        seen[sid] = i

        name = st.get("name", sid)
        if not isinstance(name, str) or not name:
            name = sid

        cm = st.get("canvas_mode", "split")
        if cm not in _CANVAS_MODES:
            errors.append(f"state {sid!r}: canvas_mode moet 'split' of "
                          f"'wide' zijn (kreeg {cm!r}) — 'split' gebruikt")
            cm = "split"

        loop = st.get("loop", True)
        if not isinstance(loop, bool):
            loop = True

        adv_in = st.get("advance") or {}
        if not isinstance(adv_in, dict):
            errors.append(f"state {sid!r}: advance moet een object zijn — "
                          "leeg gebruikt")
            adv_in = {}
        motion = _validate_edge_motion(adv_in.get("motion"), sid, errors)
        # cooldown-default: scare-overgang erft timing.scare_cooldown
        if motion is not None and "cooldown" not in motion:
            motion["cooldown"] = (scare_cooldown
                                  if motion["to"] == "jumpscare" else 0.0)
        timeout = _validate_edge_timeout(adv_in.get("timeout"), sid, errors)
        advance: dict = {}
        if motion is not None:
            advance["motion"] = motion
        if timeout is not None:
            advance["timeout"] = timeout

        clean_states.append({
            "id": sid,
            "name": name,
            "loop": loop,
            "canvas_mode": cm,
            "media": _validate_media(st.get("media"), cm, sid, errors),
            "advance": advance,
        })

    if not clean_states:
        errors.append("states: geen enkele bruikbare state — "
                      "default states gebruikt")
        return copy.deepcopy(DEFAULT_CONFIG["states"])

    # alle edge-doelen moeten bestaan
    ids = {s["id"] for s in clean_states}
    dead: list[str] = []
    for s in clean_states:
        adv = s["advance"]
        m = adv.get("motion")
        if m is not None:
            if m["to"] not in ids:
                dead.append(f"state {s['id']!r}: advance.motion.to "
                            f"{m['to']!r} bestaat niet")
                m["to"] = None
            elif m.get("kind_to") is not None and m["kind_to"] not in ids:
                dead.append(f"state {s['id']!r}: kind_to {m['kind_to']!r} "
                            "bestaat niet")
                m["kind_to"] = None
            idle = m.get("idle")
            if idle is not None and idle.get("to") not in ids:
                dead.append(f"state {s['id']!r}: motion.idle.to "
                            f"{idle['to']!r} bestaat niet")
                m.pop("idle", None)
        t = adv.get("timeout")
        if t is not None and t["to"] not in ids:
            dead.append(f"state {s['id']!r}: advance.timeout.to "
                        f"{t['to']!r} bestaat niet")
            t["to"] = None
    for d in dead:
        errors.append(d + " — overgang verwijderd")
    # rond de genormaliseerde edges af: "to": None is nu een dode overgang;
    # ruim die op zodat de engine geen None-doelen ziet
    for s in clean_states:
        adv = s["advance"]
        m = adv.get("motion")
        if m is not None:
            if m.get("to") is None:
                adv.pop("motion", None)
            else:
                if m.get("kind_to") is None:
                    m.pop("kind_to", None)
                if "idle" in m and m["idle"] is None:
                    m.pop("idle", None)
        t = adv.get("timeout")
        if t is not None and t.get("to") is None:
            adv.pop("timeout", None)
        if not adv:
            s["advance"] = {}
    return clean_states


# --------------------------------------------------------------------------
# publieke entrypoint
# --------------------------------------------------------------------------

def validate_config(cfg) -> tuple[dict, list[str]]:
    """Valideer en normaliseer een config-dict -> (clean, errors).

    clean is altijd een complete config (defaults aangevuld) en bevat alle
    onbekende top-level keys uit cfg onaangetast (backward-compat met de
    huidige config.json: ha_url/ha_token/llm_url/...). errors bevat alleen
    waarschuwingen/herstelde fouten; bij fatale states-schade zijn de
    default states teruggezet (dat staat dan ook in errors).
    """
    errors: list[str] = []
    if cfg is None:
        errors.append("config: leeg — volledige defaults gebruikt")
        return copy.deepcopy(DEFAULT_CONFIG), errors
    if not isinstance(cfg, dict):
        errors.append("config: moet een object zijn — "
                      "volledige defaults gebruikt")
        return copy.deepcopy(DEFAULT_CONFIG), errors

    clean = {k: copy.deepcopy(v) for k, v in cfg.items()
             if k not in ("screens", "playback", "motion", "timing",
                          "kind_mode", "states", "entry_state")}

    kind_mode_in = cfg.get("kind_mode", DEFAULT_CONFIG["kind_mode"])
    kind_mode = bool(kind_mode_in) if isinstance(kind_mode_in, bool) else False
    if not isinstance(kind_mode_in, bool):
        errors.append("kind_mode: moet een boolean zijn — false gebruikt")

    timing = _validate_timing(cfg.get("timing"), errors)
    clean["timing"] = timing
    clean["screens"] = _validate_screens(cfg.get("screens"), errors)
    clean["playback"] = _validate_playback(cfg.get("playback"), errors)
    clean["motion"] = _validate_motion(cfg.get("motion"), errors)
    clean["kind_mode"] = kind_mode
    clean["states"] = _validate_states(cfg.get("states"), errors,
                                       timing["scare_cooldown"])

    entry = cfg.get("entry_state", DEFAULT_CONFIG["entry_state"])
    ids = {s["id"] for s in clean["states"]}
    if not isinstance(entry, str) or entry not in ids:
        fallback = sorted(ids)[0] if ids else "rust"
        errors.append(f"entry_state {entry!r} bestaat niet in states — "
                      f"'{fallback}' gebruikt")
        entry = fallback
    clean["entry_state"] = entry
    return clean, errors
