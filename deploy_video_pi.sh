#!/usr/bin/env bash
# Deploy Grimoria-app naar de Pi5 (192.168.178.85).
# Kopieert app/*.py + webui, maakt een getimestamped backup van de vorige
# versie op de Pi, herstart de user-dienst en verifieert via /api/state.
#
# Gebruik: bash deploy_video_pi.sh [--no-restart]
set -euo pipefail
PI=basjes@192.168.178.85
HQ="/home/basjes/Home Assistant/halloween"
RESTART=1
[ "${1:-}" = "--no-restart" ] && RESTART=0

APP=/mnt/media/halloween/app
STAMP=$(date +%Y%m%dT%H%M%S)
PYFILES="grimoria_pi.py renderer.py engine.py library.py detector.py schemas.py server.py"

echo "== 1. connectivity check =="
ping -c 1 -W 2 192.168.178.85 >/dev/null || { echo "Pi offline — probeer later opnieuw"; exit 1; }

echo "== 2. backup huidige app op de Pi (.bak-$STAMP) =="
ssh "$PI" "cd $APP && for f in $PYFILES index.html app.js style.css; do \\
  [ -e \$f ] && cp \$f \$f.bak-$STAMP; done; ls -1 $APP/*.bak-$STAMP 2>/dev/null || echo '(geen eerdere bestanden om te backuppen)'"

echo "== 3. app overzetten =="
# Klassieke v1-media (webp/frames) alleen meesturen als ze lokaal bestaan;
# bij de v2-structuur komen films via de bibliotheek-upload in media/library/.
if [ -d "$HQ/media/loops/frames/rust" ]; then
  ssh "$PI" 'mkdir -p /mnt/media/halloween/media/loops'
  for f in grimoria_loop_rust_00001_.webp grimoria_loop_jumpscare_raw_00001_.webp \
           grimoria_loop_rust.mp4 grimoria_loop_jumpscare.mp4; do
    [ -e "$HQ/media/loops/$f" ] && cat "$HQ/media/loops/$f" | ssh "$PI" "cat > /mnt/media/halloween/media/loops/$f"
  done
  (cd "$HQ/media/loops/frames" && tar cf - rust jump) | \
    ssh "$PI" 'cd /mnt/media/halloween/media/loops && rm -rf frames && mkdir frames && cd frames && tar xf -'
else
  echo "   (geen media/loops/frames lokaal — media overslagen)"
fi
[ -e "$HQ/app/test_video_mode.py" ] && \
  cat "$HQ/app/test_video_mode.py" | ssh "$PI" "cat > $APP/test_video_mode.py"
for f in $PYFILES; do
  [ -e "$HQ/app/$f" ] || { echo "   ontbreekt lokaal, overslagen: app/$f"; continue; }
  cat "$HQ/app/$f" | ssh "$PI" "cat > $APP/$f"
done
# webui (hele map, zodat toekomstige extra bestanden meekomen)
ssh "$PI" "mkdir -p $APP/webui"
(cd "$HQ/app/webui" && tar cf - .) | ssh "$PI" "cd $APP/webui && tar xf -"
# bibliotheek-map voor uploads (bestaat mogelijk nog niet op de Pi)
ssh "$PI" "mkdir -p /mnt/media/halloween/media/library /mnt/media/halloween/logs"

echo "== 4. herstart grimoria =="
if [ "$RESTART" = 1 ]; then
  ssh "$PI" 'systemctl --user restart grimoria && sleep 8 && systemctl --user is-active grimoria'
else
  echo "   --no-restart: dienst NIET herstart; wijzigingen activeren bij de volgende herstart"
fi

echo "== 5. verificatie =="
ssh "$PI" 'curl -s -m 5 http://localhost:8077/api/state; echo; journalctl --user -u grimoria --since "-30s" --no-pager 2>/dev/null | grep -E "video-lus|renderer|gestart|engine|API" | tail -5; echo "frames rust/jump: $(ls /mnt/media/halloween/media/loops/frames/rust 2>/dev/null | wc -l) / $(ls /mnt/media/halloween/media/loops/frames/jump 2>/dev/null | wc -l)"'
echo "Klaar. Terugrollen kan met de .bak-$STAMP-bestanden in $APP op de Pi."
