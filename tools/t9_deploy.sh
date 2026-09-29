#!/usr/bin/env bash
# Put the current working tree on the T9 and restart the pipeline.
#
# Until now the source reached the mini PC by whatever rsync line was in the
# shell history at the time. That is fine while someone is watching and wrong
# the first time it matters: a deploy that silently copies nothing, or copies
# the right files and leaves the old process running, looks exactly like a fix
# that did not work.
#
# So this copies, restarts, and then *checks* — it compares the checksum of the
# file it cares about on both sides and refuses to report success if the running
# service is not the one it just installed.
#
#   tools/t9_deploy.sh                 # the usual: deploy and restart
#   tools/t9_deploy.sh --dry-run       # show what would be copied, change nothing
#   T9=10.0.0.9 tools/t9_deploy.sh
#
# The T9 switches itself off when nobody is watching, so it may simply not be
# there. That is not a failure of this script and it says so.
set -uo pipefail

T9="${T9:-$(python3 -c "import sys;sys.path.insert(0,'$(cd "$(dirname "$0")/.." && pwd)');from hearable import site;print(site.get('t9_host'))" 2>/dev/null)}"
USER_AT="${T9_USER:-hearable}@${T9}"
REMOTE_ROOT="${T9_ROOT:-/home/hearable/hearable}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=8)
DRY=""
[ "${1:-}" = "--dry-run" ] && DRY="--dry-run"

# What belongs on the appliance. Deliberately not the whole tree: the model is
# 707 MB and already there, `upstream/` is a build directory, and benchmark
# results are written *by* the T9 — copying ours over its own would destroy the
# evidence of the last run.
INCLUDE=(hearable config scripts systemd tools)

say() { printf '%s\n' "$*"; }

if ! ping -c 1 -W 3 "$T9" >/dev/null 2>&1; then
  say "Il T9 non risponde su ${T9}."
  say ""
  say "Non e' necessariamente un guasto: si spegne da solo quando nessuno guarda."
  say "Per svegliarlo, una delle due:"
  say "  - premi F4 sul telecomando del decoder (manda il magic packet), oppure"
  say "  - premi il pulsante di accensione del T9."
  say "Poi rilancia questo comando."
  exit 1
fi

if ! ssh "${SSH_OPTS[@]}" "$USER_AT" true 2>/dev/null; then
  say "Il T9 risponde al ping ma non all'ssh: forse si sta ancora avviando."
  say "Riprova fra una trentina di secondi."
  exit 1
fi

say "== deploy su ${USER_AT}:${REMOTE_ROOT}"
for d in "${INCLUDE[@]}"; do
  [ -d "${HERE}/${d}" ] || continue
  rsync -az --delete $DRY \
        --exclude '__pycache__' --exclude '*.pyc' \
        -e "ssh ${SSH_OPTS[*]}" \
        "${HERE}/${d}/" "${USER_AT}:${REMOTE_ROOT}/${d}/"
  say "   ${d}/"
done

if [ -n "$DRY" ]; then
  say "== dry run: niente e' stato cambiato"
  exit 0
fi

# The check. A restart that reports success while the old code is still loaded
# is the failure mode worth spending ten lines on.
LOCAL_SUM=$(md5sum "${HERE}/hearable/realtime_server.py" | cut -d' ' -f1)
REMOTE_SUM=$(ssh "${SSH_OPTS[@]}" "$USER_AT" "md5sum ${REMOTE_ROOT}/hearable/realtime_server.py | cut -d' ' -f1")
if [ "$LOCAL_SUM" != "$REMOTE_SUM" ]; then
  say "ERRORE: dopo la copia i sorgenti non coincidono."
  say "  qui:    $LOCAL_SUM"
  say "  sul T9: $REMOTE_SUM"
  exit 2
fi
say "== sorgenti allineati (md5 ${LOCAL_SUM:0:12})"

BEFORE=$(ssh "${SSH_OPTS[@]}" "$USER_AT" "systemctl show -p MainPID --value hearable-t9 2>/dev/null || echo 0")
say "== riavvio hearable-t9 (pid ${BEFORE})"
ssh "${SSH_OPTS[@]}" "$USER_AT" "sudo systemctl restart hearable-t9"
sleep 6
AFTER=$(ssh "${SSH_OPTS[@]}" "$USER_AT" "systemctl show -p MainPID --value hearable-t9")
STATE=$(ssh "${SSH_OPTS[@]}" "$USER_AT" "systemctl is-active hearable-t9")

if [ "$STATE" != "active" ]; then
  say "ERRORE: hearable-t9 non e' attivo (${STATE}). Ultime righe:"
  ssh "${SSH_OPTS[@]}" "$USER_AT" "sudo journalctl -u hearable-t9 -n 20 --no-pager -o cat"
  exit 3
fi
if [ "$AFTER" = "$BEFORE" ]; then
  say "ERRORE: stesso pid dopo il riavvio (${AFTER}): il servizio non e' ripartito."
  exit 3
fi

say "== hearable-t9 attivo, pid ${AFTER}"
say ""
say "Versione dell'albero: $(sed -n 's/^__version__ = \"\(.*\)\"/\1/p' "${HERE}/hearable/__init__.py")"
say "Per verificare la parola finale:  tools/t9_last_word_gate.py"
