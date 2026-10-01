# What every document is

This repository carries the system as it runs, and the documents that describe
it. It does not carry the measurement archive the figures were taken from —
around 100 MB of audio and per-run data — which stayed with the working copy it
was produced in.

Italian documents are marked **IT**.

## The whole project

| Document | | What it is |
|---|---|---|
| [`hearable_project.md`](hearable_project.md) | | **Start here.** What it is for, where the audio comes from, and both machines in full |
| [`hearable_progetto.md`](hearable_progetto.md) | **IT** | The same document in Italian |

## The wire

| Document | | What it is |
|---|---|---|
| [`enigma2_hearable_protocol.md`](enigma2_hearable_protocol.md) | | The subtitle protocol: messages, and the two rules that shape it |
| [`enigma2_hearable_audio_transport.md`](enigma2_hearable_audio_transport.md) | | The audio relay: control and media, in opposite directions |

## Building and running

| Document | | What it is |
|---|---|---|
| [`t9_appliance_prerequisites.md`](t9_appliance_prerequisites.md) | | Every package the mini PC needs, and what breaks without it |
| [`configuration.md`](configuration.md) | | The JSON configuration files under `config/` |
| [`audio_sources.md`](audio_sources.md) | | The pluggable audio input layer |
| [`subtitle_sinks.md`](subtitle_sinks.md) | | The pluggable subtitle output layer |
| [`two_line_subtitle_ui.md`](two_line_subtitle_ui.md) | | How two lines are composed, and when a line is promoted |

## Published pages

`docs/site/` holds the source of the reader-facing pages, with its own README
mapping each file to where it is published.
