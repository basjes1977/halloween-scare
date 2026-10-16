#!/usr/bin/env bash
# Deploy Wan-videolus-modus naar de Pi (192.168.178.85).
# Gebruik: bash deploy_video_pi.sh
set -euo pipefail
PI=basjes@192.168.178.85
HQ="/home/basjes/Home Assistant/halloween"

echo "== 1. connectivity check =="
ping -c 1 -W 2 192.168.178.85 >/dev/null || { echo "Pi offline — probeer later opnieuw"; exit 1; }

echo "== 2. backup huidige renderer =="
ssh "$PI" 'cp /mnt/media/halloween/app/renderer.py /mnt/media/halloween/app/renderer.py.bak-video-$(date +%Y%m%dT%H%M%S)'

echo "== 3. media + app overzetten =="
ssh "$PI" 'mkdir -p /mnt/media/halloween/media/loops'
cat "$HQ/media/loops/grimoria_loop_rust_00001_.webp"       | ssh "$PI" 'cat > /mnt/media/halloween/media/loops/grimoria_loop_rust_00001_.webp'
cat "$HQ/media/loops/grimoria_loop_jumpscare_raw_00001_.webp" | ssh "$PI" 'cat > /mnt/media/halloween/media/loops/grimoria_loop_jumpscare_raw_00001_.webp'
cat "$HQ/media/loops/grimoria_loop_rust.mp4"              | ssh "$PI" 'cat > /mnt/media/halloween/media/loops/grimoria_loop_rust.mp4'
cat "$HQ/media/loops/grimoria_loop_jumpscare.mp4"         | ssh "$PI" 'cat > /mnt/media/halloween/media/loops/grimoria_loop_jumpscare.mp4'
cd "$HQ/media/loops/frames" && tar cf - rust jump | ssh "$PI" 'cd /mnt/media/halloween/media/loops && rm -rf frames && mkdir frames && cd frames && tar xf -'
cat "$HQ/app/renderer.py"      | ssh "$PI" 'cat > /mnt/media/halloween/app/renderer.py'
cat "$HQ/app/test_video_mode.py" | ssh "$PI" 'cat > /mnt/media/halloween/app/test_video_mode.py'

echo "== 4. herstart grimoria =="
ssh "$PI" 'systemctl --user restart grimoria && sleep 8 && systemctl --user is-active grimoria'

echo "== 5. verificatie =="
ssh "$PI" 'curl -s -m 5 http://localhost:8077/state; echo; journalctl --user -u grimoria --since "-30s" --no-pager 2>/dev/null | grep -E "video-lus|renderer|gestart" | tail -5; ls /mnt/media/halloween/media/loops/frames/rust | wc -l; ls /mnt/media/halloween/media/loops/frames/jump | wc -l'
echo "Klaar. Check log op 'video-lusmodus AAN'."
