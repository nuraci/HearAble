#!/usr/bin/env python3
"""Did the held last word come out during the pause, or at the head of the next sentence?

The defect reported from real use: a sentence would end one word short, and if a
pause followed, that word reappeared in front of the next sentence. The cause was
that the tail-flush timer measured "no event arrived" instead of "the hypothesis
stopped changing", and a streaming recogniser emits a result for every chunk even
while nobody is speaking — so the timer was restamped forever and never expired.

This gate looks for the *witness*. Every word released by the timer carries
`reason: "TAIL_FLUSH_TIMEOUT"` in its commit event, and those events are written
to the run's events file. With the defect present that reason essentially never
appears, because the deadline is never reached. With it fixed, it appears once
per pause.

So the question this answers is narrow and checkable:

    did the tail get released by the timer, during live television,
    without the next utterance having to arrive first?

It does not judge the subtitles' quality, and it cannot: only the viewer can say
whether sentences now end where they should. This measures the mechanism.

    tools/t9_last_word_gate.py                 # run a session and judge it
    tools/t9_last_word_gate.py --seconds 240
    tools/t9_last_word_gate.py --events FILE   # judge a run that already happened

`--events` takes a file produced by a previous run and makes no network calls at
all, which is also how this script is tested without a T9 in the room.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from pathlib import Path as _Path
import sys as _sys
_ROOT = _Path(__file__).resolve().parent.parent
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))
from hearable import site

T9 = site.get("t9_host")
T9_USER = "hearable"
REMOTE_ROOT = "/home/hearable/hearable"
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]

# Below this many flushes the run says nothing either way: live television with
# no pause in it produces no evidence, and reporting PASS on zero observations
# is how a gate becomes decoration.
MIN_FLUSHES_FOR_A_VERDICT = 3


def read_events(path: Path) -> list[dict]:
    records = []
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def commit_reasons(records: list[dict]) -> list[dict]:
    """Every commit event in the run, flattened, in order."""
    events = []
    for record in records:
        for event in record.get("commit_events") or []:
            if isinstance(event, dict) and event.get("reason"):
                events.append(event)
    return events


def judge(records: list[dict]) -> dict:
    events = commit_reasons(records)
    counts: dict[str, int] = {}
    for event in events:
        counts[str(event["reason"])] = counts.get(str(event["reason"]), 0) + 1

    flushes = counts.get("TAIL_FLUSH_TIMEOUT", 0)
    flushed_words = [str(e.get("word", "")) for e in events
                     if e.get("reason") == "TAIL_FLUSH_TIMEOUT"]

    if not events:
        verdict = "NON_MISURATO"
        why = ("nessun evento di commit nella corsa: o non e' passato parlato, "
               "o il file degli eventi non e' quello giusto")
    elif flushes == 0:
        verdict = "FAIL"
        why = ("nessun rilascio per scadenza in tutta la corsa. Con del parlato "
               "e delle pause, il timer dovrebbe scattare: se non scatta mai, "
               "l'ultima parola sta ancora aspettando la frase successiva")
    elif flushes < MIN_FLUSHES_FOR_A_VERDICT:
        verdict = "NON_MISURATO"
        why = (f"solo {flushes} rilasci: troppo pochi per distinguere una "
               f"correzione che funziona da una coincidenza. Serve una corsa "
               f"piu' lunga, o del parlato con piu' pause")
    else:
        verdict = "PASS"
        why = (f"{flushes} parole rilasciate dal timer durante la corsa, cioe' "
               f"durante le pause e non all'arrivo della frase dopo")

    return {
        "class": "MEASURED",
        "LAST_WORD_RELEASED_ON_SILENCE": verdict,
        "why": why,
        "tail_flush_timeouts": flushes,
        "commit_events_total": len(events),
        "commit_reasons": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
        "sample_flushed_words": flushed_words[:12],
        "subtitle_records": len(records),
    }


def run_session(seconds: int, label: str) -> Path:
    """Start a live run on the T9 and bring its events file back."""
    remote_events = f"{REMOTE_ROOT}/benchmark/results/T9_N95_cpu/{label}_events.jsonl"
    print(f"== corsa dal vivo sul T9, {seconds} s (label {label})")
    print("   parla la televisione: servono frasi con delle pause in mezzo.")
    command = (f"cd {REMOTE_ROOT} && LABEL={label} DURATION={seconds} "
               f"ON=0 scripts/hearable_t9_live.sh")
    result = subprocess.run(SSH + [f"{T9_USER}@{T9}", command],
                            capture_output=True, text=True, timeout=seconds + 180)
    if result.returncode != 0:
        print(result.stdout[-2000:])
        print(result.stderr[-2000:], file=sys.stderr)
        raise SystemExit(f"la corsa e' fallita (rc {result.returncode})")

    local = Path(f"/tmp/{label}_events.jsonl")
    fetch = subprocess.run(
        ["scp", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
         f"{T9_USER}@{T9}:{remote_events}", str(local)],
        capture_output=True, text=True)
    if fetch.returncode != 0:
        raise SystemExit(f"non ho potuto recuperare {remote_events}: {fetch.stderr.strip()}")
    return local


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=180,
                        help="durata della corsa dal vivo (default 180)")
    parser.add_argument("--label", default="lastword",
                        help="nome della corsa, usato per i file di uscita")
    parser.add_argument("--events", type=Path,
                        help="giudica un file di eventi gia' esistente, senza toccare la rete")
    parser.add_argument("--json", action="store_true", help="solo il verdetto, in JSON")
    args = parser.parse_args()

    events_path = args.events or run_session(args.seconds, args.label)
    if not events_path.exists():
        raise SystemExit(f"{events_path} non esiste")

    report = judge(read_events(events_path))
    report["events_file"] = str(events_path)

    if args.json:
        print(json.dumps(report, indent=1, ensure_ascii=False))
        return 0 if report["LAST_WORD_RELEASED_ON_SILENCE"] == "PASS" else 1

    print()
    print("=" * 66)
    print(f"  LAST_WORD_RELEASED_ON_SILENCE: {report['LAST_WORD_RELEASED_ON_SILENCE']}")
    print("=" * 66)
    print(f"  {report['why']}")
    print()
    print(f"  rilasci per scadenza : {report['tail_flush_timeouts']}")
    print(f"  eventi di commit     : {report['commit_events_total']}")
    print(f"  stati di sottotitolo : {report['subtitle_records']}")
    if report["commit_reasons"]:
        print("  motivi di commit     :")
        for reason, count in report["commit_reasons"].items():
            print(f"      {count:6d}  {reason}")
    if report["sample_flushed_words"]:
        print(f"  parole rilasciate    : {', '.join(report['sample_flushed_words'])}")
    print()
    print("  Questo misura il meccanismo, non la qualita'. Se il verdetto e' PASS")
    print("  ma guardando la televisione le frasi finiscono ancora corte, il difetto")
    print("  e' altrove e va riaperto il mandato forense.")
    print()
    return 0 if report["LAST_WORD_RELEASED_ON_SILENCE"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
