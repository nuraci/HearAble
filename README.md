# HearAble

**Italian live subtitles on broadcast television, about a second behind the
voice, entirely inside the house.**

Built for one person who is hard of hearing, because Italian broadcasters
caption the evening news and not much else — live regional programmes, sport,
talk shows and most of daytime arrive with nothing at all.

HearAble runs on two consumer machines joined by one Ethernet cable: the
satellite/terrestrial receiver that is already under the television, and a
fanless mini PC. No cloud service, no account, no subscription, and nothing
about what someone watches leaves their living room. The viewer presses one
button on the remote they already had.

It subtitles **live broadcast television and films played from the receiver's
own storage** — a USB stick, an internal drive — through the same chain, with no
second code path.

| | |
|---|---:|
| Subtitle lag behind the spoken word | ~1.0 s |
| Real-time factor, 30 min of live television | 0.4848 |
| Audio between the two machines | 26 kB/s |
| Tuners taken from the television | 0 |

Every figure in this repository is a measurement taken on the hardware named
beside it, or is labelled as not measured.

---

## How it works

```
   DVB-T2
     │
     ▼
┌──────────────────────────────┐          ┌──────────────────────────────┐
│  RECEIVER · Enigma2          │          │  MINI PC · Debian            │
│                              │          │                              │
│  frontend ──▶ demux0         │          │  AAC decode + resample       │
│                  │           │          │            │                 │
│                  ▼           │          │            ▼                 │
│              PID tap         │          │  streaming ASR, CPU only     │
│              (+64 ms)        │          │  RTF 0.4848                  │
│                  │           │          │            │                 │
│                  ▼           │          │            ▼                 │
│               relay ─────────┼──────────┼─▶ stabiliser → two lines     │
│              (ffmpeg)        │ 26 kB/s  │            │                 │
│                              │  AAC     │            │                 │
│  OSD panel ◀─────────────────┼──────────┼────────────┘                 │
│       │                      │ WebSocket│                              │
└───────┼──────────────────────┘          └──────────────────────────────┘
        ▼
   television          private cable · no router, no DHCP, no gateway
```

**The trick is where the audio comes from.** The receiver is already
demultiplexing the audio of the channel you are watching. HearAble reads it
straight off the DVB demux with a standard Linux PID filter — userspace only, no
kernel module, no patched image:

```c
open("/dev/dvb/adapter0/demux0", O_RDWR | O_NONBLOCK)
DMX_SET_PES_FILTER    pid    = <the audio PID the viewer selected>
                      input  = DMX_IN_FRONTEND
                      output = DMX_OUT_TSDEMUX_TAP
                      flags  = DMX_IMMEDIATE_START
read()                /* 188-byte MPEG-TS packets */
```

`DMX_IN_FRONTEND` reads the stream *entering* the box rather than what the
decoder is presenting. That costs no tuner, re-encodes nothing, and puts the
recogniser **64 ms ahead** of the picture instead of behind it.

Only compressed audio goes out, and only two lines of text come back.

---

## What you need

| Part | Used here | What matters |
|---|---|---|
| Receiver | Octagon SF8008 V3, openATV 7.5.1 | Any Enigma2 box with root and a working `/dev/dvb`. The tap is standard Linux DVB, not vendor-specific |
| Inference host | T9 Plus, Intel N95, fanless, 15 GiB | x86 with AVX2, four cores, and a governor you may change. No GPU needed — and on this workload, not wanted |
| Link | One Ethernet cable, `10.77.0.0/24` static | No router, no DHCP, no gateway on the critical path |
| Model | Nemotron 3.5 ASR Streaming 0.6B `q8_0` | 707 MB; see [`models/README.md`](models/README.md). Any streaming ASR with a cache-aware encoder fits |
| Runtime | NeMo-Speech.cpp on ggml | Compiles natively on both machines. No CUDA, no Python inference |

**[DEPLOY.md](DEPLOY.md)** has the full procedure: first-time setup of both
machines, the routine update, verification and rollback.

Before anything else, tell the repository where your machines are:

```bash
cp config/site.example.json config/site.json   # then fill it in
```

That file is gitignored. No household's addresses or MAC are in this tree.

---

## What is worth reading

| Document | Answers |
|---|---|
| [`docs/hearable_project.md`](docs/hearable_project.md) | **Start here.** The whole project: purpose, the investigation, what failed, and both machines in full |
| [`DEPLOY.md`](DEPLOY.md) | How it gets onto the two machines, and how to roll back |
| [`WHY.md`](WHY.md) | Why this exists, who it was built for, and what I hope someone else does with it |
| [`HANDOVER.md`](HANDOVER.md) | **Continuing the work**: the state, how fixing is done here, the release order, and what is open |
| [`docs/INDEX.md`](docs/INDEX.md) | What every other document is, and which ones are current |
| [`docs/enigma2_hearable_protocol.md`](docs/enigma2_hearable_protocol.md) | The subtitle wire format |
| [`docs/enigma2_hearable_audio_transport.md`](docs/enigma2_hearable_audio_transport.md) | The audio relay protocol |
| [`benchmark/results/T9_N95_cpu.md`](benchmark/results/T9_N95_cpu.md) | The appliance campaign: 19 injected faults, 19 unattended recoveries |

---

## Where the second goes

The delay is mostly a decision, not a limit. More than half of it is spent
deliberately *not* showing a word that might still change — a subtitle that
rewrites itself in front of the reader is worse than one that arrives later and
stays put, and much worse for someone depending on the text rather than glancing
at it.

| Component | Cost | What it buys |
|---|---:|---|
| Chunking, 160 ms | ~80 ms | Audio arrives in blocks |
| RNN-T right context, 1 | ~160 ms | The model sees ahead before committing |
| ASR compute | 74 ms | The inference itself |
| Stabiliser confirmations | 320–480 ms | Words that do not rewrite themselves |
| Tail hold | ≤450 ms | A last word the model has finished with |
| Repaint cap, 6 fps | ≤166 ms | Corrections that do not strobe |
| Publication delay | 0 ms | Nothing — the tap is already ahead |

---

## Tests

```bash
python3 -m pytest tests/ -q
```

Tests that read artefacts this repository does not carry — 97 MB of reference
audio, the 707 MB model, the compiled ASR runtime — **skip with a reason**
rather than fail. Build the runtime with `scripts/build_cpu.sh` and put the
model in `models/`, and they run.

---

## What this repository does not contain

* **The model** — 707 MB and not ours to redistribute. `models/README.md` has
  the size and checksum to verify against.
* **The ASR runtime** — `upstream/NeMo-Speech.cpp` is a build directory.
  `scripts/build_cpu.sh` fetches and builds it at the revision every
  measurement here was taken with. Verified from a clean clone on 29 September
  2026: fetch, build, and Italian recognised from a WAV through this
  repository's own pipeline.
* **The record of how it got here** — the campaign reports of closed lines, the
  work orders they were run from, and around 100 MB of raw artefacts. Three
  reports are kept as evidence for how the system behaves *today*; the rest
  stayed with the original working copy, together with the commit history.
  That is deliberate: an archive that cannot be verified is worse than no
  archive. **The full record exists and can be shared — ask.**
* **Any address or MAC of the installation it was built for.**

---

## How this was built

HearAble was **orchestrated by Nunzio Raciti and written by Claude** (Anthropic's
Opus model) working to his direction. He set the goal and the constraints, made
every design decision, owned the hardware, and did the testing that mattered
most; Claude was the material executor — the code, the tests, the measurement
tools and the reports.

The division showed up in the results. Several of the defects that mattered were
found not by a test but by the person using the real thing: pressing buttons on
the remote found two faults a nineteen-case automated matrix had passed;
physically unplugging a cable exposed a dead socket that `ip link down` could not
reproduce; cutting mains power answered a Wake-on-LAN question that months of
software-off testing could not; and reading an RGB status ring by eye corrected a
colour map that scored 0 out of 5 against the documentation it came from.

**Every phase was gated by tests and measurements, and nothing advanced on an
opinion.** That is the working rule of this project, not a description added
afterwards:

* **246 automated tests** covering the protocol, the receiver-side renderer, the
  stabiliser, the commit rules, the configuration and the version consistency —
  all runnable without a set-top box in the room.
* **Purpose-built gates** for everything that needs real hardware: the LAN
  gate, the live session, the UI lifecycle, boot cycles, Wake-on-LAN, the idle
  timer, the file player, and a nineteen-case failure matrix. Each one produces
  a verdict and a machine-readable block, not a log to read.
* **Campaign reports** under `benchmark/results/`, each labelling every line
  **MEASURED**, **DERIVED** or **NOT_MEASURED** — so a missing measurement can
  never be mistaken for a result.
* **Verdicts that can say "no"**: two investigations were closed by the numbers
  that closed them rather than quietly dropped, and a gate that produced too
  little evidence reports `NON_MISURATO` instead of passing.

When a measurement disagreed with the instrument that produced it, the
instrument lost. The tap was believed to run 2.4 s ahead of the picture until it
was measured properly and turned out to be 64 ms — a factor of forty, corrected
by asking the decoder directly instead of correlating screenshots.

---

## Status

Version 1.5.2. In daily use on real broadcast television.

Two lines of investigation are closed and documented with the numbers that
closed them: hosting the recogniser on a €50 single-board computer (it needs
7.33×, the memory bandwidth allows 2.79×), and delaying the picture so the
subtitles catch up (the delay is reachable, but drains back to live in about two
minutes). Both are in `docs/hearable_project.md`, because a measured dead end is
worth more than an unmeasured hope.

Orchestrated by Nunzio Raciti. Written by Claude (Opus) to his direction.
