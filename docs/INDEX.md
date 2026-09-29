# What every document is

This repository carries the system as it runs, and the documents that describe
it. It does not carry the record of how it got here — the campaign reports of
lines that are closed, the work orders they were run from, and around 100 MB of
raw measurement artefacts.

That was a decision, not an omission. Those things exist intact in the original
working copy, together with 116 commits explaining why each choice was made. An
archive that cannot be verified is worse than no archive: it invites a reader to
check a number and then fails them. **If you want the full record, ask — it has
not been lost, only left where it is whole.**

Italian documents are marked **IT**.

## The whole project

| Document | | What it is |
|---|---|---|
| [`hearable_project.md`](hearable_project.md) | | **Start here.** Purpose, the investigation, what failed, both machines in full |
| [`hearable_progetto.md`](hearable_progetto.md) | **IT** | The same document in Italian |

## The wire

| Document | | What it is |
|---|---|---|
| [`enigma2_hearable_protocol.md`](enigma2_hearable_protocol.md) | | The subtitle protocol: messages, the two rules that shape it |
| [`enigma2_hearable_audio_transport.md`](enigma2_hearable_audio_transport.md) | **IT** | The audio relay: control and media, in opposite directions |

## Building and running

| Document | | What it is |
|---|---|---|
| [`t9_appliance_prerequisites.md`](t9_appliance_prerequisites.md) | **IT** | Every package the mini PC needs, and what broke without it |
| [`configuration.md`](configuration.md) | | The JSON configuration files under `config/` |
| [`audio_sources.md`](audio_sources.md) | | The pluggable audio input layer |
| [`subtitle_sinks.md`](subtitle_sinks.md) | | The pluggable subtitle output layer |
| [`two_line_subtitle_ui.md`](two_line_subtitle_ui.md) | | How two lines are composed and when a line is promoted |

## Open

| Document | | What it is |
|---|---|---|
| [`prova_parola_finale.md`](prova_parola_finale.md) | **IT** | The fix waiting on hardware: what to run, what to look at, why the version was not raised |

## Evidence

Three campaign reports under `benchmark/results/` are kept because they are the
measured evidence for how the system behaves today, not a record of how it was
built:

* `T9_N95_cpu.md` — the appliance: nineteen injected faults, nineteen unattended
  recoveries, thirty minutes of deliberate abuse.
* `sf8008_file_regression_v120.md` — the file playback path.
* `ux_subtitles.md` — how the subtitles came to look the way they do.

## Published pages

`docs/site/` holds the source of the reader-facing pages, with its own README
mapping each file to where it is published.
