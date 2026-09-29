#!/usr/bin/env bash
# HearAble on the T9: the real chain, fed by the SF8008's direct PID tap.
#
# Same pipeline as the PC's, with three differences that belong to this machine:
# the runtime lives in the T9's own build directory, the governor is the one
# phase 8 chose, and the model path is local.
#
#   scripts/hearable_t9_live.sh                 # segue il canale sintonizzato
#   LABEL=live10 DURATION=600 scripts/hearable_t9_live.sh
#
# The decoder needs nothing done to it beyond HearAble being on (F4 on the
# handset, or the control call this script makes when ON=1).
set -uo pipefail

ROOT="${ROOT:-/home/hearable/hearable}"
# The decoder on the private link. Nothing in the subtitle path goes through the
# house network any more: no router, no DHCP, no name resolution.
BOX="${BOX:-10.77.0.1}"
LIB_DIR="${LIB_DIR:-$ROOT/upstream/NeMo-Speech.cpp/build/t9_cpu_baseline/bin}"
LABEL="${LABEL:-live}"
DURATION="${DURATION:-0}"
OUTPUT_DIR="${OUTPUT_DIR:-$ROOT/benchmark/results/T9_N95_cpu}"
GOVERNOR="${GOVERNOR:-performance}"
ON="${ON:-1}"

mkdir -p "$OUTPUT_DIR"

# Phase 8 chose `performance`: on this part the recogniser's bursty duty cycle
# never keeps intel_pstate busy enough to clock up on its own, and the
# difference is 28% of the RTF. Set it explicitly rather than hoping.
if [ -w /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor ] || sudo -n true 2>/dev/null; then
  for c in /sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_governor; do
    echo "$GOVERNOR" | sudo tee "$c" >/dev/null 2>&1 || true
  done
fi
echo "governor: $(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor)"

if [ "$ON" = "1" ]; then
  curl -s -m 10 "http://${BOX}:8770/control?hearable=on" >/dev/null || true
  echo "HearAble sul decoder: acceso"
fi

export LD_LIBRARY_PATH="$LIB_DIR${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

exec python3 -m hearable.realtime_server \
  --audio-source-config "${AUDIO_CONFIG:-$ROOT/config/audio_source_sf8008_relay_private.json}" \
  --subtitle-sink-config "${SINK_CONFIG:-$ROOT/config/subtitle_sink_sf8008_private.json}" \
  --model "$ROOT/models/nemotron-3.5-asr-streaming-0.6b.q8_0.gguf" \
  --lib-dir "$LIB_DIR" \
  --gpu -1 \
  --language it-IT \
  --chunk-ms 160 \
  --duration "$DURATION" \
  --metrics-output "$OUTPUT_DIR/${LABEL}_metrics.json" \
  --events-output "$OUTPUT_DIR/${LABEL}_events.jsonl" \
  "$@"
