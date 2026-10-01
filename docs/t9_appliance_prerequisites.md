# What the mini PC needs

A Debian netinst is deliberately spartan, and every package below is here
because something broke without it. The list is split by what is needed to put
subtitles on the screen, what is only needed to build, and what is only needed
to measure.

## Required at runtime — without these there are no subtitles

```bash
sudo apt install -y ffmpeg python3-numpy python3-aiohttp curl
```

| package | why |
|---|---|
| **`ffmpeg`** | decodes the transport stream's audio into PCM. Without it the receiver's tap opens the connection, sends 4.5 kB and gets a *broken pipe* — a symptom that never names its cause, and cost a round of diagnosis |
| `python3-numpy` | the pipeline's audio chunks |
| `python3-aiohttp` | the server in `hearable.realtime_server` |
| `curl` | the launcher switches HearAble on at the receiver; without it that call failed silently, because it was guarded by `\|\| true` |

## Required for the private link and for waking

```bash
sudo apt install -y ethtool
```

`ethtool` re-applies Wake-on-LAN at every boot: the `r8169` driver clears it, and
without that line the magic packet stops working after the first reboot.

## Required for the status ring (optional hardware)

```bash
sudo apt install -y python3-serial
```

If it is missing, `hearable-rgb` says so once in the journal and carries on
without a ring. The subtitles never notice, which is the point.

## Required to build the ASR runtime

```bash
sudo apt install -y build-essential cmake ninja-build pkg-config git
```

`libsentencepiece-dev` is **not** needed: `scripts/build_t9_n95_cpu.sh` builds
sentencepiece from a pinned commit, as the reference host does.

## Optional — useful for measuring, useless for working

```bash
sudo apt install -y rsync time linux-perf lm-sensors
```

| package | why |
|---|---|
| `rsync` | moving sources and the model from a workstation |
| `time` | `/usr/bin/time -v` for peak RSS and CPU% |
| `linux-perf`, `lm-sensors` | profiling and temperatures |

## Installed and then removed

`dnsmasq` was useful for **two minutes**, to bring the receiver back when a moved
cable had left it with no network and its Wi-Fi was not yet configured: its
`eth0` was on DHCP, it was given `10.77.0.1`, and from there it could be
configured statically. Then it was stopped and the configuration removed.
**There is no DHCP on the critical path**, deliberately.

## Quick check

```bash
for p in ffmpeg curl ethtool; do command -v $p >/dev/null || echo "missing $p"; done
python3 -c "import numpy, aiohttp, serial" || echo "a python module is missing"
```
