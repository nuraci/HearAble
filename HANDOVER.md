# Handover

For whoever picks this up next — including the person who built it, six months
from now. It says what state the system is in, how work is done here, and how a
release actually reaches the two machines.

`README.md` says what HearAble is. `DEPLOY.md` says how it gets installed. This
says how to keep working on it.

---

## 1. Where things stand

```text
version          1.5.3
tests            254 in the source repository
                 179 pass / 23 skip / 0 fail on a fresh clone of this one
in service       yes — daily, on live broadcast television
```

Nothing is waiting on hardware. The defect that was open at 1.5.1 — the last
word of a sentence appearing with the next one — is **partly solved and fully
documented**, which is a different state from fixed and worth stating as such.

What changed: the held word is released when the model puts a full stop on it,
not after a pause. Two faults sat behind that. The flush timer measured "no
event arrived" instead of "the hypothesis stopped changing", which was a real
defect and is fixed. Then the commit guard refused the release 202 times out of
203, because the recogniser had committed a half-finished word and completed it
afterwards, and nothing resets the committer — `is_final` never fires on this
model.

The obvious repair was tried on live television and **made it worse**: releasing
on a timer put a word on screen that the model then changed about half the time.
Changing the signal rather than the threshold is what worked.

**What is still open:** sentences the model does not punctuate show the original
symptom, unchanged. And two causes were never excluded — that the word is never
produced by the recogniser, or produced and not displayed for a reason below the
committer. The limit is stated in the README.

`COMMIT_TAIL_RELEASE` chooses `off`, `time` or `punctuation`. The default is
`off`; this installation runs `punctuation`; one line of configuration and a
restart changes it, without waiting for whoever can redeploy.

---

## 2. How work is done here

Five habits explain most of the decisions in this repository.

**Measure, do not deduce.** Three output modes of the same DVB ioctl behave
identically in the header file and differently on real silicon: one of them
opens without error and delivers zero bytes for ever. Try it on the hardware you
have.

**Do the real thing, not its convenient imitation.** `ip link down` does not
reproduce what unplugging the cable does, and a software power-off does not
reproduce a mains cut. Where a fault can be injected physically, inject it
physically.

**A missing measurement is not a result.** Anything reported here is labelled
`MEASURED`, `DERIVED` or `NOT_MEASURED`, and a gate with too little evidence
reports `NON_MISURATO` rather than passing. A gate that says PASS on zero
observations is decoration.

**Watch it for longer than feels necessary.** A ten-second window answers a
narrower question than a two-minute one, and the narrower answer can look
identical while being useless. Decide what the window has to cover before
reading the number.

**When the instrument and the person disagree, suspect the instrument.** The tap
runs 64 ms ahead of the picture, measured by reading two clocks in the same
instant — `AUDIO_GET_PTS` against the PTS of the packet the filter is handing
over. A screenshot-based estimate of the same quantity is wrong by a factor that
matters, and the viewer's ear was right before either instrument was.

---

## 3. Fixing something

1. **Reproduce it offline first.** Most of this system is testable without
   hardware — the protocol, the renderer's state machine, the stabiliser, the
   commit rules, the clocks. The last-word defect was reproduced in a script
   before a line was changed, which is what made the diagnosis certain rather
   than plausible.
2. **Write the test that fails.** Then fix it. Then **put the defect back** and
   check the test fails, and names the file and line. An untested test is a
   comment.
3. **Keep the test honest about what it measures.** `t9_last_word_gate.py`
   measures the *mechanism* — that the timer fires — and says in its own output
   that it cannot judge whether the subtitles read well. Do not let a gate claim
   more than it observes.
4. **Say what was not confirmed.** If the hardware was not connected, the commit
   message says so. `NOT YET CONFIRMED ON HARDWARE` is a normal thing to write.

### Beyond the tests: build it from a clean clone

The suite says the pieces fit together. It does not say a clean clone produces a
working system — and the first time that was actually tried, it did not. Three
defects only a clean clone could show: `models/README.md` was gitignored, so the
link from the README led nowhere; `scripts/build_cpu.sh` did not fetch the
runtime it claimed to reproduce; and once it did, the checkout aborted on a
machine without `git-lfs`, because the upstream repository's filters were being
disabled one step too late.

All three are fixed and the path is verified end to end: fetch, build, and
Italian recognised from a WAV through this repository's own pipeline. **Do it
again after any change to the build scripts, the package layout or
`.gitignore`** — none of those three failures is visible to a test.

### The test suite

```bash
python3 -m pytest tests/ -q
```

Tests that read an artefact this clone does not carry — the reference audio, the
707 MB model, the compiled ASR runtime — **skip with a reason**. That is
deliberate: a suite that is red for a known reason teaches people to ignore red.
If you add a test that needs one of those, make it skip too.

---

## 4. Releasing

### The order matters

```text
1. deploy the change to one side
2. judge it against the hardware
3. only then raise the version and tag
```

Raising the version forces a plugin reinstall on the receiver **even when the
plugin did not change**: the consistency test requires the package's copy and the
plugin's copy to match, and the farewell line on screen would otherwise show a
number that is not what is running. That means two changes landing at once while
you are trying to decide whether one of them worked.

So the version number means *confirmed*, not *hoped*.

### Raising it

Three places must agree, and a test enforces the first two:

| Where | File |
|---|---|
| The package | `hearable/__init__.py` |
| The plugin's own copy | `plugin/HearAbleOSD/__init__.py` |
| The running receiver | `version` in `/tmp/hearable_osd_status.json` |

```bash
# 1. both copies, same value
sed -i 's/^__version__ = .*/__version__ = "1.5.2"/' \
    hearable/__init__.py plugin/HearAbleOSD/__init__.py

# 2. the guards must pass before anything else
python3 -m pytest tests/test_version_consistency.py -q

# 3. the whole suite
python3 -m pytest tests/ -q

# 4. commit and tag — annotated, with a short name, as every release here is
git commit -am "Release 1.5.2"
git tag -a v1.5.2 -m "v1.5.2 - <what it is, in three or four words>"

# 5. both machines
tools/t9_deploy.sh
tools/sf8008_install_plugin.sh        # address from config/site.json
```

`tests/test_version_consistency.py` refuses a tree whose two copies disagree, or
whose version is behind the newest tag. The installer refuses to deploy a
mismatch, and refuses to report success if the box then declares something else.

### After deploying

Check that what is running is what you shipped — the status file is rewritten
every second, so it is live rather than a snapshot:

```bash
ssh root@<receiver> 'python3 -c "
import json; d=json.load(open(\"/tmp/hearable_osd_status.json\"))
print(d[\"version\"], d[\"relay\"][\"state\"], d[\"look\"])"'
```

Then press `F4` twice. The greeting stands for four seconds; the farewell shows
`HearAble <version>` — which is the one place a viewer without a terminal can
read what is installed.

### Rolling back

Check out the earlier tag and deploy it again. The receiver's settings files are
not versioned and survive both directions, so the viewer's chosen look is never
lost by rolling back.

---

## 5. The two traps that cost the most time

**A restart that did not happen.** Both deploy scripts verify a *different*
process is running afterwards, because an install once reported success while
the previous build stayed loaded for hours. If you write another deployment
path, verify the same way: the PID must change, not merely the command must
return.

**An address written into code.** The receiver's address used to be a default
argument in forty-odd files. When its Ethernet port became the private link, that
address stopped existing, and every one of those tools reported `No route to
host` — which reads exactly like a switched-off receiver and is not. Addresses
live in `config/site.json`, which is gitignored; `hearable/site.py` layers the
example, that file, and the environment. Do not write one into a file again.

---

## 6. What is open

| Item | State | Note |
|---|---|---|
| The last word of a sentence | Partly solved | Released on the model's full stop, confirmed on live television. Sentences the model does not punctuate still wait for the next one |
| CASE B and CASE C of the same defect | Not excluded | Only one cause was proven and fixed: the word was produced and held. If the gate passes but sentences still end short, the word was either never produced by the recogniser, or produced and not displayed — neither has been ruled out |
| AVX-VNNI | Available, unused | The CPU advertises it; the build does not use it. Expected small next to the governor |
| Full reboot on plugin install | Observed, not investigated | Installing the plugin restarts the whole box, not just Enigma2 as the command claims |

---

## 7. How this repository relates to the original

This is a copy of the working tree at 1.5.3. The **full history — 125 commits,
each explaining why a decision was made — exists only in the original working
copy**, not here. If that history matters to you, it has to come from there; it
was a deliberate choice not to bring it, and the original is currently its only
copy.

This repository is *the system*, not the story of how it was arrived at. The
measurement archive the figures come from — around 100 MB of audio and per-run
data — stayed with the working copy it was produced in.

---

## 8. Who did what

HearAble is **by Nunzio Raciti**, built with **Claude** (Anthropic's Opus) as a
development assistant. He set the goal and the constraints, made every design
decision, owns the hardware, and did the testing that mattered most; Claude
wrote code, tests and measurement tools to his direction.

Anyone continuing this should know which half is which. The code can be read and
changed by anyone. The judgement about whether a subtitle is good enough to
depend on belongs to the person who watches the television — and in this project
that judgement overruled the instruments more than once, and was right each time.
