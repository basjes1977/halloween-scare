# Grimoria installeren op een verse Raspberry Pi 5

Stappenplan voor een verse Pi (Raspberry Pi OS Bookworm, 64-bit) met twee 1080p-schermen, cameramodule en de codemap `/mnt/media/halloween`. Installeer als de gebruiker die later ook de dienst draait (in de unit: UID 1000, `basjes`).

> Na de installatie is Grimoria bereikbaar op `http://<pi-ip>:8077/` — LAN-only, **geen authenticatie**. Niet port-forwarden.

## 1. Packages

```bash
sudo apt update
sudo apt install -y ffmpeg python3-pip python3-picamera2
sudo apt install -y libatlas-base-dev libjpeg-dev   # numpy/tflite-runtime deps
```

- `ffmpeg` — nodig voor de frame-extractie van de filmbibliotheek (op Raspberry Pi OS vaak al aanwezig).
- `python3-picamera2` — camera-stack (op Raspberry Pi OS met desktop meestal al voorverpakt).
- `python3-pip` + de twee `-dev`-packages — voor de Python-deps hieronder.

## 2. Python-afhankelijkheden

```bash
python3 -m pip install --user pygame moderngl numpy
python3 -m pip install --user tflite_runtime
```

**Opmerking over `tflite_runtime` op Bookworm:** de wheels voor nieuwere Python-versies verschijnen later (of helemaal niet) op PyPI. Lukt de install niet:

- Probeer expliciet een nieuwere versie: `python3 -m pip install --user tflite-runtime==2.14.0` of een wheel van de [tflite-runtime-releasepagina](https://github.com/tensorflow/tensorflow/releases?q=tflite-runtime&expanded=true) die bij je Python-passt.
- **Geen wheel beschikbaar? Dan kun je het laten.** Zonder `tflite_runtime` valt de detector automatisch terug op `frame_diff` (luma-frame-diff); `motion.detector: "frame_diff"` zetten in de config is dan netjes. Grimoria draait ook zo gewoon — alleen is het dan bewegingsdetectie in plaats van echte persoonsdetectie. (Eventueel kan ook volledige `tensorflow` via pip, maar die is zwaar en op de Pi meestal niet nodig.)

## 3. Detectiemodel

De persoonsdetectie gebruikt een gequantiseerd SSD MobileNet-model (COCO-classes, inclusief `person`) plus labelmap. Plaats beide in `/mnt/media/halloween/models/`:

```bash
mkdir -p /mnt/media/halloween/models && cd /mnt/media/halloween/models

# SSD MobileNet v1 (gequantiseerd, COCO) — geteste download:
curl -L -o ssd.zip \
  https://storage.googleapis.com/download.tensorflow.org/models/tflite/coco_ssd_mobilenet_v1_1.0_quant_2018_06_29.zip
unzip ssd.zip && rm ssd.zip
# levert: detect.tflite + labelmap.txt
```

Hernoem naar de namen uit de config (default `motion.model: "ssd_mobilenet_v2.tflite"`):

```bash
mv detect.tflite ssd_mobilenet_v2.tflite
mv labelmap.txt ssd_mobilenet_v2_labels.txt
```

*(Liever de v2-variant? Die staat in de [TF Lite-hosted-modellenlijst](https://www.tensorflow.org/lite/guide/hosted_models) — zelfde procedure, maar zet de bestandsnaam dan wel overeenkomstig in `config.json` → `motion.model`.)*

Alternatieve bron: exporteer zelf via [TensorFlow Object Detection API](https://github.com/tensorflow/models/blob/master/research/object_detection/g3doc/tf2_detection_zoo.md) (`SSD MobileNet v2`, `TFLITE_32x8_INT8`-variant, 300×300 invoer).

Check: de bestanden moeten bestaan waar de config ze verwacht; de detector logt bij het starten of het model geladen is (`journalctl --user -u grimoria`). Zo niet, dan valt hij automatisch terug op frame-diff.

## 4. Code en mappen

```bash
sudo mkdir -p /mnt/media/halloween/app
sudo chown -R $(id -u):$(id -g) /mnt/media/halloween
```

Zet de inhoud van `app/` uit de repo in `/mnt/media/halloween/app/` — handmatig (rsync/scp) of met `deploy_video_pi.sh` vanaf de ontwikkelmachine:

```bash
# vanaf de ontwikkelmachine:
bash deploy_video_pi.sh          # of: bash deploy_video_pi.sh --no-restart
```

## 5. Configuratie

```bash
cd /mnt/media/halloween/app
cp config.example.json config.json
nano config.json    # ha_url + ha_token invullen (en zo nodig llm_url)
```

- `ha_token`: in Home Assistant aanmaken via **Profiel → Beveiliging → Langdurige toegangstokens**. Alleen nodig voor de HA-triggers (deurbel, aanwezigheid); zonder token werken camera, fase-machine en webinterface gewoon.
- `app/config.json` bevat daarna een echt geheim: niet kopiëren naar andere machines, niet in git. Op de ontwikkelmachine staat dit bestand in `.gitignore`.

## 6. systemd user-unit

```bash
loginctl enable-linger $USER                       # dienst draait ook zonder ingelogde sessie
mkdir -p ~/.config/systemd/user
cp /mnt/media/halloween/app/grimoria.service.pi ~/.config/systemd/user/grimoria.service
systemctl --user daemon-reload
systemctl --user enable --now grimoria
```

> De unit verwacht een grafische sessie (`DISPLAY=:0`, `graphical.target`) en PulseAudio (`/run/user/1000/pulse/native`; `ExecStartPre` wacht daar op). Log dus lokaal in op de desktop-sessie (of gebruik een autologin) voordat je Grimoria verwacht te zien.

## 7. Verifiëren

```bash
systemctl --user status grimoria
curl -s http://localhost:8077/api/state
journalctl --user -u grimoria -f
```

Open daarna `http://<pi-ip>:8077/` in een browser op het LAN: upload filmpjes in de **Bibliotheek**-tab en wijs ze per fase toe in de **Fases**-tab (de standaardketen draait ook zonder films: dan ziet het scherm er "leeg"/procedureel uit, afhankelijk van renderer-fallback).

## Veiligheidsnotities

- **LAN-only, geen authenticatie.** De API en webinterface zijn Halloween-speelgoed voor het eigen thuisnet: niemand hoeft in te loggen, dus iedereen op het LAN kan de app besturen en films uploaden. Zet dit niet op internet (geen port-forward/omgekeerde proxy zonder auth).
- **Geen secrets in de UI of API.** `ha_token` wordt uit alle API-responses gefilterd en nergens gelogd; het staat alleen in `app/config.json` op de Pi.
- Uploads zijn beperkt tot mp4/webm/mkv, max ~200 MB, en worden in een tijdelijke map geëxtraheerd (geen shell-uitvoering; mislukte extracts ruimen zichzelf op).
