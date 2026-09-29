#!/usr/bin/env bash
# Remove the HearAble OSD plugin and put the box back as it was.
set -euo pipefail
HOST="${1:-$(python3 -c "import sys;sys.path.insert(0,'$(cd "$(dirname "$0")/.." && pwd)');from hearable import site;print(site.get('decoder_host'))" 2>/dev/null)}"
SSH_OPTS=(-o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=8 -o UserKnownHostsFile=/dev/null)
ssh "${SSH_OPTS[@]}" "root@${HOST}" \
    "rm -rf /usr/lib/enigma2/python/Plugins/Extensions/HearAbleOSD /tmp/hearable_osd*.json /tmp/hearable_osd.log"
echo "removed. restarting enigma2"
ssh "${SSH_OPTS[@]}" "root@${HOST}" "init 4; sleep 3; init 3" || true
