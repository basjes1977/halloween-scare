# Grimoria

Interactieve Halloween-"spiegelgeest" op een Raspberry Pi 5: twee 1080p-HDMI-schermen (links een gezicht, rechts een orb), een camera die personen detecteert, een configureerbare fase-machine die daarop reageert (van rustige beweging via een jumpscare naar kalm), en een webinterface op poort 8077 voor alles wat je daaraan wilt instellen.

## Wat het doet

- **Dual-screen rendering**: één randloos X11-venster van 3840×1080 over beide HDMI-uitgangen; rendering met pygame + moderngl (OpenGL 3.1 op de V3D-GPU). Per scherm instelbare rotatie, overlay-tekst (drag&drop-positie) en rol-wissel/offset.
- **Fase-machine**: generiek, uit `config.json` configureerbaar. Standaardketen:

  ```text
  rust ──beweging──▶ langzaam ──beweging houdt aan (3 s)──▶ jumpscare ──(2,5 s)──▶ kalm ──(20 s)──▶ rust
                       └──beweging stopt (5 s)──▶ rust
  ```

  met een jumpscare-cooldown (90 s) en een `kind_mode`-toggle: bij aan wordt de automatische sprong naar de jumpscare overgeslagen (handmatig testen blijft altijd mogelijk). States en overgangen zijn vrij definieerbaar; de standaardketen is een config, geen hardcoded logica.
- **Filmbibliotheek**: mp4's uploaden via de webinterface; ffmpeg extraheert frames (480×272, 16 fps) naar `media/library/<id>/frames/`. Per fase kies je per scherm een loop-filmpje, of één breed 3840×1080-filmpje over beide schermen (`canvas_mode: split | wide`).
- **Persoonsdetectie**: picamera2 (320×240 @ 8 fps) met TFLite SSD MobileNet-herkenning (~3 fps, confidence-drempel instelbaar, ~1 s geheugen). Als het model niet laadt, valt de code automatisch terug op luma-frame-diff (pure bewegingsdetectie).
- **Optioneel**: Home Assistant-triggers (deurbel, aanwezigheid) via een langlopende token, LLM-zinnen via een lokale Ollama-server en NL-stem via Piper.

Volledig ontwerp (config-model v2, API v2, besluiten): [docs/ontwerp-grimoria-webinterface.md](docs/ontwerp-grimoria-webinterface.md). Installatie op de Pi: [docs/installatie-pi.md](docs/installatie-pi.md).

## Architectuur (`app/`)

| Bestand | Rol |
|---|---|
| `grimoria_pi.py` | Main-loop: camera-thread, audio (Piper), HA-triggers, LLM-strook; start de API-server en roept de engine ~30×/s aan. |
| `engine.py` | Pure fase-machine (states, motion/timeout/still-edges, cooldowns, `kind_mode`, `goto`). Geen pygame/GL — unit-testbaar op elke machine. |
| `library.py` | Filmbibliotheek: upload, ffmpeg-extractie (veilig: geen shell, timeouts, atomaire renames), metadata. |
| `api_v2.py` | HTTP-server op :8077: statische webui + `/api/*`-endpoints. Filtert `ha_token` uit alle responses. |
| `webui/` | Statische webinterface (`index.html`, `app.js`, `style.css`), vanilla JS zonder build-chain. |
| `renderer.py` | GL-renderer: shaders, texture-beheer, per-scherm uniformen (rotatie, overlay-positie), videolussen. |
| `detector.py` | *(gepland, zie ontwerp)* Persoonsdetectie-backend: TFLite SSD MobileNet op picamera2-frames, frame-diff als fallback. |
| `config.json` | Runtime-config op de Pi (**geheim: bevat `ha_token` — staat in `.gitignore`**). |
| `grimoria.service.pi` | systemd user-unit zoals die op de Pi hoort. |
| `grimoria.py` | Oudere versie (procedurele animatie, hardcoded fases). Draait niet meer, blijft als referentie. |

## Installatie op de Pi 5

Kort — het complete stappenplan voor een verse Pi staat in [docs/installatie-pi.md](docs/installatie-pi.md). De verwachte map is `/mnt/media/halloween` (pas aan als je ergens anders werkt):

1. Code naar `/mnt/media/halloween/app/` zetten (handmatig of met `deploy_video_pi.sh`).
2. Packages: `ffmpeg` en `python3-picamera2` via apt (op Raspberry Pi OS meestal al aanwezig), plus `pygame`, `moderngl`, `numpy`, `tflite_runtime` via pip.
3. Detectiemodel (SSD MobileNet, gequantiseerd `.tflite` + labelmap) neerzetten in `/mnt/media/halloween/models/`.
4. Config aanmaken: `app/config.example.json` → `app/config.json` invullen.
5. systemd user-unit installeren: `grimoria.service.pi` → `~/.config/systemd/user/grimoria.service`, daarna `systemctl --user daemon-reload && systemctl --user enable --now grimoria`.
6. Controleren: `curl http://<pi-ip>:8077/api/state` en de webinterface openen.

## Configuratie

Sjabloon: [`config.example.json`](config.example.json) (repo-root) — kopiëren naar `app/config.json` en invullen. Belangrijkste sleutels:

| Sleutel | Betekenis |
|---|---|
| `ha_url`, `ha_token` | Home Assistant-endpoint + langlopende token (**geheim — nooit committen**; optioneel: zonder token werken camera, fases en webui gewoon) |
| `ha_entities` | Entity-id's voor deurbel/motion/aanwezigheid |
| `api_port` | Poort van API + webinterface (standaard 8077) |
| `camera` | Aan/uit, resolutie, fps, flip |
| `screens` | Per scherm rotatie + overlay-tekst, `swap` en offset tussen de schermen |
| `playback` | Bron-fps, render-fps, crossfade |
| `motion` | Detector (`tflite` of frame-diff), modelbestand, `detect_fps`, confidence-drempel, `person_hold_secs`, `still_secs`, fallback-instellingen |
| `timing` | `scare_cooldown` |
| `states` + `entry_state` | De fase-machine: id, naam, `loop`, `canvas_mode`, `media` (per scherm een bibliotheek-id), `advance`-randen (`motion`/`still`/`timeout` met eigen tijden en cooldowns) |
| `kind_mode` | Springt nooit automatisch af |
| `llm_url`, `llm_model`, `voice`, … | Optionele spraak/LLM-koppeling |

> **Waarschuwing:** `app/config.json` bevat een echt `ha_token`. Dat bestand staat in `.gitignore` en mag **nooit** worden gecommit of gedeeld — gebruik altijd `config.example.json` als sjabloon. De API filtert het token uit alle responses.

## Webinterface

`http://<pi-ip>:8077/` (LAN-only, geen authenticatie — Halloween-speelgoed op het eigen net). Eén pagina, vier tabs:

- **Schermen** — rotatie per scherm, preview-canvas, overlay-tekst (aan/uit, grootte, kleur) met drag&drop-positie, rol-wissel en offset.
- **Fases** — per fase per scherm filmkeuze, alle tijden (aanhoudingsdrempel, timeouts, cooldowns), `kind_mode` en een testknop per fase.
- **Bibliotheek** — mp4-upload (drag&drop), lijst met duur/framecount, verwijderen.
- **Statusbalk** — live fase, tijd in fase, laatste beweging, kind-mode-toggle.

## API (poort 8077)

| Endpoint | Methode | Functie |
|---|---|---|
| `/` en `/webui/*` | GET | Statische webinterface |
| `/api/state` | GET | `{state, name, since_secs, motion:{last_seen, active}, kind_mode}` (1 Hz poll door de UI) |
| `/api/config` | GET/PUT | Config lezen/schrijven (validatie, hot-apply waar mogelijk; **filtert `ha_token`**) |
| `/api/library` | GET/POST | Bibliotheeklijst / mp4-upload → ffmpeg-extractie |
| `/api/library/{id}` | DELETE | Media verwijderen |
| `/api/goto/{state_id}` | POST | Handmatige trigger (testknop per fase) |
| `/api/kind` | POST | `{kind: bool}` kind-mode-toggle |
| `/state`, `/say`, `/wake`, `/scare`, `/mode` | GET/POST | Legacy v1-endpoints (nog aanwezig) |

## Ontwikkelen

Pure onderdelen (engine, bibliotheek) werken op elke machine — geen camera of GL nodig:

```bash
cd app
python3 -m unittest discover -s tests
```

Voor de webinterface/API los van de Pi: `tools/mock_server.py` start de API met een nep-engine en -bibliotheek. Deployen naar de Pi kan met `deploy_video_pi.sh` (backup op de Pi, kopieer, herstart, verify via `/api/state`); met `--no-restart` zonder herstart.

## Licentie

MIT — zie [LICENSE](LICENSE).
