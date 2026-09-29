#!/usr/bin/env bash
# Install (or update) the HearAble OSD plugin on an Enigma2 box.
#
# The plugin renders subtitles on the decoder's screen; there is no way to do
# that from the LAN, so this is the one component of the gate that has to live
# on the box. It only ever listens and draws — no tuner, no decoding, no key
# handling — and it can be removed with tools/sf8008_remove_plugin.sh.
#
# enigma2 loads session-start plugins when it starts, so installing one means
# restarting enigma2. That blanks the television for a few seconds and stops
# whatever is playing.
set -euo pipefail

# Where this installation's decoder is. config/site.json holds it; the
# repository carries only placeholders.
HOST="${1:-$(python3 -c "import sys;sys.path.insert(0,'$(cd "$(dirname "$0")/.." && pwd)');from hearable import site;print(site.get('decoder_host'))" 2>/dev/null)}"
if [ -z "$HOST" ]; then
  echo "no address: pass one as an argument, or fill in config/site.json" >&2
  exit 2
fi
HERE="$(cd "$(dirname "$0")/.." && pwd)"
TARGET=/usr/lib/enigma2/python/Plugins/Extensions/HearAbleOSD
SSH_OPTS=(-o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=8 -o UserKnownHostsFile=/dev/null)

# The version in the tree, checked before anything is copied. A plugin whose
# version disagrees with the package it came from answers "which one is on the
# box?" wrongly, which is worse than not answering.
TREE_VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "${HERE}/hearable/__init__.py")
PLUGIN_VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "${HERE}/plugin/HearAbleOSD/__init__.py")
if [ -z "$TREE_VERSION" ] || [ "$TREE_VERSION" != "$PLUGIN_VERSION" ]; then
  echo "versione incoerente: hearable=${TREE_VERSION:-nessuna} plugin=${PLUGIN_VERSION:-nessuna}" >&2
  echo "allinea hearable/__init__.py e plugin/HearAbleOSD/__init__.py prima di installare" >&2
  exit 2
fi

echo "installing HearAble ${TREE_VERSION} on ${HOST}:${TARGET}"
ssh "${SSH_OPTS[@]}" "root@${HOST}" "mkdir -p ${TARGET}"
scp "${SSH_OPTS[@]}" -q \
    "${HERE}/plugin/HearAbleOSD/__init__.py" \
    "${HERE}/plugin/HearAbleOSD/plugin.py" \
    "${HERE}/plugin/HearAbleOSD/wsserver.py" \
    "${HERE}/plugin/HearAbleOSD/subtitle_session.py" \
    "${HERE}/plugin/HearAbleOSD/relay.py" \
    "${HERE}/plugin/HearAbleOSD/dvb_demux_tap.py" \
    "root@${HOST}:${TARGET}/"

# The protocol module is deployed from the project rather than kept as a second
# copy in the plugin folder, so the two ends cannot drift apart.
scp "${SSH_OPTS[@]}" -q "${HERE}/hearable/enigma2_protocol.py" "root@${HOST}:${TARGET}/"

ssh "${SSH_OPTS[@]}" "root@${HOST}" \
    "rm -f ${TARGET}/*.pyc /tmp/hearable_osd_status.json; rm -rf ${TARGET}/__pycache__"

# The plugin carries no default MAC any more: it reads WOL_CONFIG on the box.
# Provision that file if the box has not got one, from the same config this
# script took the address from. Never overwrite an existing one.
WOL_MAC=$(python3 -c "import sys;sys.path.insert(0,'${HERE}');from hearable import site;print(site.get('t9_mac'))" 2>/dev/null || true)
WOL_BCAST=$(python3 -c "import sys;sys.path.insert(0,'${HERE}');from hearable import site;print(site.get('link_broadcast'))" 2>/dev/null || true)
if [ -n "$WOL_MAC" ] && [ "$WOL_MAC" != "00:00:00:00:00:00" ]; then
  ssh "${SSH_OPTS[@]}" "root@${HOST}" \
      "test -f /etc/enigma2/hearable_wol.json || printf '%s\\n' \
       '{\"enabled\": true, \"mac\": \"${WOL_MAC}\", \"broadcast\": \"${WOL_BCAST}\", \"ports\": [9, 7]}' \
       > /etc/enigma2/hearable_wol.json"
  echo "wake-on-lan: /etc/enigma2/hearable_wol.json present (mac ${WOL_MAC})"
else
  echo "wake-on-lan: no MAC in config/site.json — F4 will not wake the mini PC" >&2
fi

# Deleting the status file is not proof the new code is running: the plugin that
# is already loaded rewrites it once a second, so the wait below was satisfied
# within a second by the *old* build. Worse, `init 4; sleep 3; init 3` in one
# command can return before enigma2 has actually gone, and restarting it then
# does nothing at all — which is how an install once reported success while the
# previous build stayed loaded for hours.
#
# So: remember the process, wait for it to go, bring it back, and only believe
# the plugin is new when a *different* process is reporting a small uptime.
BEFORE=$(ssh "${SSH_OPTS[@]}" "root@${HOST}" "pidof enigma2" 2>/dev/null || echo none)
echo "installed. restarting enigma2 (pid ${BEFORE}) — the picture will go for a few seconds"
ssh "${SSH_OPTS[@]}" "root@${HOST}" "init 4" || true

for _ in $(seq 40); do
  NOW=$(ssh "${SSH_OPTS[@]}" "root@${HOST}" "pidof enigma2" 2>/dev/null || echo none)
  [ "$NOW" != "$BEFORE" ] && break
  sleep 2
done
ssh "${SSH_OPTS[@]}" "root@${HOST}" "init 3" || true

for _ in $(seq 60); do
  sleep 3
  STATUS=$(ssh "${SSH_OPTS[@]}" "root@${HOST}" "cat /tmp/hearable_osd_status.json 2>/dev/null" || true)
  AFTER=$(ssh "${SSH_OPTS[@]}" "root@${HOST}" "pidof enigma2" 2>/dev/null || echo none)
  [ -z "$STATUS" ] && continue
  [ "$AFTER" = "$BEFORE" ] && continue
  if python3 -c "import json,sys; sys.exit(0 if json.loads(sys.argv[1]).get('uptime_s', 1e9) < 120 else 1)" "$STATUS"; then
    ON_BOX=$(python3 -c "import json,sys; print(json.loads(sys.argv[1]).get('version','sconosciuta'))" "$STATUS")
    if [ "$ON_BOX" != "$TREE_VERSION" ]; then
      echo "il box dichiara ${ON_BOX}, l'albero ${TREE_VERSION}: qualcosa non è stato sostituito" >&2
      exit 3
    fi
    echo "plugin ${ON_BOX} in esecuzione (enigma2 pid ${AFTER}):"
    echo "$STATUS"
    exit 0
  fi
done
echo "the plugin did not come up fresh within three minutes; check /tmp/hearable_osd.log" >&2
ssh "${SSH_OPTS[@]}" "root@${HOST}" "tail -20 /tmp/hearable_osd.log 2>/dev/null" || true
exit 1
