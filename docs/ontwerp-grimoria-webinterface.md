# Grimoria webinterface en fase-machine — ontwerp

> Status: concept v1 (2026-10-16) · Obsidian-kopie: `vault/_staging/2026-10-16-grimoria-web-interface-fases.md`

## Summary

Technisch ontwerp voor de herziening van het Halloween-project Grimoria (Pi5, 2×1080p, kamerornament): een webinterface op poort 8077 voor scherm-instellingen (rotatie, positie, overlay-tekst met drag&drop), filmaanbeheer per fase, en een generieke, configureerbare fase-machine aangestuurd door camerabeweging. Code wordt gepubliceerd op GitHub (zonder secrets).

## Context

Grimoria draait als systemd user-service (`grimoria`) op de Pi5 (192.168.178.85), code op de Pi in `/mnt/media/halloween/app`, mirror in workspace `Home Assistant/halloween/`. Huidige opzet:

- Eén randloos X11-venster 3840×1080 over beide HDMI-schermen; pygame + moderngl (GL 3.1, V3D); splitsing links (gezicht) / rechts (orb) is **hard gecodeerd** op `uv.x = 0.5`.
- Video-lusmodus: geëxtraheerde frameseries (480×272 PNG, 16 fps bron, crossfade naar 30 fps render) uit `media/loops/frames/{rust,jump}` — 81 rust-frames, 98 jump-frames (palindroom).
- Camera: picamera2, 320×240 @ 8 fps, bewegingsdetectie via luma-frame-diff (`diff_thr` 25, `min_frac` 1,2 %). Geen persoonsdetectie.
- API op :8077: `GET /state`, `POST /say|/wake|/scare|/mode`. Geen CORS, geen statische bestanden, geen config-endpoints.
- Fase-logica versnipperd over `update()`, `handle()`, `say()` en de main-loop in `grimoria_pi.py`; state-tijden en cooldowns hardcoded.
- Config: `app/config.json` (bevat **HA-token — gevoelig**), geladen bij start; `config_pi.json` identieke kopie.

## Doel

Van een hardcoded demo naar een **configureerbaar platform**: states (fases) met eigen filmpjes, ketens van states die door triggers (beweging) worden afgedraaid, en een webinterface om alles in te stellen zonder code-aanpassingen.

### Scope v1 (nu)

1. **Per scherm**: rotatie 0/90/180/270 (arbitraire graden ondersteund in het model), overlay-tekst aan/uit met drag&drop-positie.
2. **Schermen onderling**: rol-wissel (welk scherm toont welke "canvas-positie") plus instelbare offset/scale.
3. **Filmbibliotheek**: mp4-upload via de webinterface; ffmpeg op de Pi extraheert frames (480×272, 16 fps) naar `media/library/<id>/frames/`.
4. **Per fase per scherm een loop-filmpje** instellen (keuze uit bibliotheek).
5. **Fase-machine** (configureerbare tijden, defaults tussen haakjes):

```text
rust ──beweging──▶ langzaam ──beweging houdt aan (3 s)──▶ jumpscare ──(2,5 s)──▶ kalm ──(20 s)──▶ rust
                     └──beweging stopt (5 s)──▶ rust
```

   plus: jumpscare-cooldown (90 s); kind_mode-toggle (springt nooit af).
6. **Webinterface** geserveerd door de app zelf op :8077 (LAN, geen auth — Halloween-speelgoed op het thuisnet).

### Scope v2 (toekomst, meegenomen in het datamodel)

- Meerdere states definiëren, ieder met eigen filmpje(s); **ketens** van states die als geheel getriggerd worden en sequentieel afspelen.
- **Keten-editor met drag&drop** in de webinterface (states als knopen, overgangen als pijlen).
- Meerdere loops per fase (afwisselend/random), audio per fase, audio-bronnen toewijzen inclusief Bluetooth-koppeling.

## Architectuur

### Nieuwe module-indeling (`app/`)

| Bestand | Rol |
|---|---|
| `engine.py` (nieuw) | Pure fase-machine: states, overgangen, timers, cooldowns. Geen GL/pygame — unit-testbaar op elke machine. |
| `compositor.py` (nieuw) | Per-scherm rendering: rotatie-transform, overlay-tekstquads, offset/scale. |
| `library.py` (nieuw) | Filmbibliotheek: upload, ffmpeg-extractie, metadata (`library.json`), verwijderen. |
| `webui/` (nieuw) | Statische webinterface (`index.html`, `app.js`, `style.css`), geserveerd door de API-server. |
| `grimoria_pi.py` | Wordt dunner: main-loop, camera-thread en API-server blijven; state-logica verhuist naar `engine.py`. |
| `renderer.py` | Blijft voor shaders/texture-beheer; krijgt per-scherm uniformen (rotatie, overlay-pos). |

### Config-model v2 (`config.json`, achterwaarts-compatibel: `ha_*`-sleutels blijven)

```jsonc
{
  "screens": {
    "swap": false,
    "offset": { "dx": 0.0, "dy": 0.0 },
    "items": [
      { "rotation": 0,
        "overlay": { "enabled": false, "text": "", "x": 0.5, "y": 0.85, "size": 48, "color": "#ffaa00" } },
      { "rotation": 0, "overlay": { "zelfde vorm" } }
    ]
  },
  "playback": { "fps": 16, "render_fps": 30, "crossfade": true },
  "motion":   { "enabled": true, "diff_thr": 25, "min_frac": 0.012,
                "person_hold_secs": 3.0, "still_secs": 5.0 },
  "timing":   { "scare_cooldown": 90 },
  "kind_mode": true,
  "states": [
    { "id": "rust", "name": "Rust", "loop": true,
      "media": { "0": "<bibliotheek-id>", "1": "<bibliotheek-id>" },
      "advance": { "motion": { "to": "langzaam", "cooldown": 90 }, "timeout": null } },
    { "id": "langzaam", "name": "Langzame beweging", "loop": true,
      "media": { "0": "<id>", "1": "<id>" },
      "advance": { "motion": { "to": "jumpscare", "require_motion_secs": 3.0 },
                   "timeout": { "secs": 20, "to": "rust" } } },
    { "id": "jumpscare", "name": "Jumpscare", "loop": false,
      "media": { "0": "<id>", "1": "<id>" },
      "advance": { "timeout": { "secs": 2.5, "to": "kalm" } } },
    { "id": "kalm", "name": "Kalmeren", "loop": true,
      "media": { "0": "<id>", "1": "<id>" },
      "advance": { "timeout": { "secs": 20, "to": "rust" } } }
  ],
  "entry_state": "rust"
}
```

Dit model is al de **generieke ketenvorm** voor v2: een keten is een getriggerde wandeling door `states` via `advance`-randen; v2 voegt benoemde ketens (`chains: [{id, entry, sequence}]`) en de DnD-editor toe, zonder engine-herziening.

### API v2 (poort 8077)

| Endpoint | Methode | Functie |
|---|---|---|
| `/` en `/webui/*` | GET | Statische webinterface |
| `/api/state` | GET | `{state, name, since_secs, motion:{last_seen, active}, kind_mode}` (1 Hz poll door UI) |
| `/api/config` | GET/PUT | Config lezen/schrijven (validatie + hot-apply waar mogelijk; **filtert `ha_token`**) |
| `/api/library` | GET | Bibliotheeklijst `{id, name, frames, fps, duration, created}` |
| `/api/library` | POST | mp4-upload (multipart) → ffmpeg-extractie |
| `/api/library/{id}` | DELETE | Media verwijderen |
| `/api/goto/{state_id}` | POST | Handmatige trigger (testknop per fase) |
| `/api/kind` | POST | `{kind: bool}` kind_mode-toggle |

### Webinterface (v1)

Eén pagina, vanilla JS (geen build-chain):

- **Schermen-tab**: per scherm rotatie-selector, preview-canvas (getrouw geroteerd), overlay-tekst (tekstveld, aan/uit, size, kleur) met **drag&drop binnen de preview** (pointer-events; positie als fracties 0..1), swap- en offset-instellingen.
- **Fases-tab**: per fase per scherm filmkeuze + alle tijden (`person_hold_secs`, timeouts, cooldowns) + kind_mode + testknop "ga naar deze fase".
- **Bibliotheek-tab**: upload (drag&drop mp4), lijst met duur/framecount, verwijderen.
- **Statusbalk**: live fase, tijd in fase, laatste beweging.

## Besluiten (vastgelegd 2026-10-16)

1. Rotatie: 0/90/180/270 als primaire presets; het model ondersteunt arbitraire graden (170° mogelijk, met zwarte hoeken).
2. "Persoon gedetecteerd" = **aanhoudende beweging** (v1). Echte persoonsdetectie is v2-optie; interface (`motion.active`) verandert niet.
3. Screens-positie: rol-wissel + offset/scale; per-scherm-filmpjes blijven leidend (geen doorlopende 3840-px-video in v1).
4. UI op :8077, LAN-only, geen authenticatie. Geen secrets in UI of API-responses.
5. `kind_mode` blijft bestaan als toggle.
6. Defaults: hold 3 s, scare 2,5 s, kalm 20 s, cooldown 90 s, still 5 s — in de UI aanpasbaar.
7. GitHub-publicatie: aparte repo; **`config.json`/`config_pi.json`/`token/`/`data/`/screenshots uitgesloten** via `.gitignore`; `config.example.json` met placeholder-token. Licentie: MIT (tenzij anders gewenst).

## Beveiliging

- HA-token blijft uitsluitend in `config.json` op de Pi (gitignored). Nieuwe code logt het token nooit en neemt het niet op in API-responses (`/api/config` filtert `ha_token` weg).
- Upload-endpoint accepteert alleen mp4/webm/mkv, max ~200 MB, extractie in tijdelijke map; mislukte extracts worden opgeruimd.

## Uitrol en terugrol

1. Ontwikkeling in workspace `halloween/app/` (git-repo; vóór de eerste wijziging een baseline-commit).
2. Rooktest op deze machine: `engine.py` unit-tests (pure FSM); webui via lokale http-server.
3. Deploy via bijgewerkt `deploy_video_pi.sh`: backup op de Pi (getimestamped), kopieer `app/*` + `webui/`, `systemctl --user restart grimoria`, verify via `/api/state` en journalctl.
4. Terugrol: vorige versie staat als `.bak-*` op de Pi én als git-commit.

## Status

- 2026-10-16: ontwerp vastgesteld, baseline-commit, implementatie gestart (backend engine/API en webui parallel). Live dienst op de Pi nog onaangetast.
