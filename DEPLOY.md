# Deploying HearAble

HearAble runs on two machines and they are deployed differently. The receiver
gets a plugin copied into Enigma2's flash and needs Enigma2 restarted, which
blanks the television for a few seconds. The mini PC gets the source tree and a
service restart, which nobody watching the television can tell happened.

Almost every deploy is the second kind. Reach for the first only when something
under `plugin/` has actually changed.

```
┌─ receiver ──────────────────┐   ┌─ mini PC ───────────────────┐
│  plugin/HearAbleOSD/*.py    │   │  hearable/  config/         │
│  hearable/enigma2_protocol  │   │  scripts/  systemd/  tools/ │
│                             │   │                             │
│  tools/sf8008_install_      │   │  tools/t9_deploy.sh         │
│        plugin.sh <host>     │   │                             │
│  → restarts Enigma2         │   │  → restarts hearable-t9     │
│  → television goes black    │   │  → nothing visible          │
└─────────────────────────────┘   └─────────────────────────────┘
```

---

## The routine case: updating the mini PC

```bash
tools/t9_deploy.sh
tools/t9_last_word_gate.py      # or whichever gate judges the change
```

### What it does, in order

1. **Checks the mini PC is there** — a ping, then SSH. If it is not, that is
   *not treated as a fault*: the machine powers itself off when nobody is
   watching. The script says how to wake it (press `F4` on the remote, or the
   power button) and stops.
2. **Copies five directories** — `hearable/ config/ scripts/ systemd/ tools/` —
   over SSH with `rsync -az --delete`.
3. **Verifies the copy** by comparing the md5 of `hearable/realtime_server.py`
   on both sides. A mismatch stops the deploy.
4. **Restarts `hearable-t9`** and verifies the restart two ways: the unit must
   report `active`, *and the PID must have changed*. If it did not, the old code
   is still loaded and the deploy failed regardless of what systemd said.

That last check is the reason the script exists. A restart that reports success
while the previous build stays loaded is a failure this project has already met
once, with the plugin installer, and it cost hours of testing code that was not
running.

### What it deliberately does not copy

| Left alone | Why |
|---|---|
| `models/` | 707 MB, already there, and it never changes |
| `upstream/` | The ASR runtime's build directory, compiled *on* the mini PC |
| `runs/` | Written **by** the mini PC. Overwriting it would destroy the record of the previous run |

### Before you run it

- **`rsync --delete` is in force.** Anything inside those five directories that
  exists on the mini PC but not in your working tree is deleted. That is correct
  for an appliance — the two sides should agree — but it will remove files you
  put there by hand.
- **Passwordless `sudo` is required** on the mini PC for `systemctl restart`.
- Look before you touch, if you want to:

```bash
tools/t9_deploy.sh --dry-run    # lists what would be copied and deleted, changes nothing
```

### Exit codes

| Code | Meaning |
|---:|---|
| 0 | Deployed, restarted, verified |
| 1 | Mini PC unreachable, or SSH not answering yet |
| 2 | Sources do not match after the copy |
| 3 | Service not `active`, or the PID did not change |

They are distinct so this can be called from another script without parsing
output.

---

## Updating the receiver's plugin

Only when something under `plugin/` changed.

```bash
tools/sf8008_install_plugin.sh 192.168.1.244
```

**Always pass the address.** The script's built-in default is `192.168.1.250`,
which stopped existing when the receiver's Ethernet port became the private link
to the mini PC; the receiver is on Wi-Fi now. A stale address gives
`No route to host`, which looks exactly like a switched-off receiver and is not.

The script refuses to do anything unsafe:

- if `hearable/__init__.py` and `plugin/HearAbleOSD/__init__.py` disagree about
  the version, it stops (exit 2) before copying a byte;
- after restarting Enigma2 it waits for a **different** process reporting a
  small uptime, so a restart that did not happen cannot be reported as success;
- if the box then declares a version other than the tree's, it stops (exit 3) —
  something was not replaced.

It restarts Enigma2, so **the picture goes for a few seconds**. Deploying to the
receiver while somebody is watching is a choice, not an accident.

### Removing it

```bash
tools/sf8008_remove_plugin.sh 192.168.1.244
```

Deletes the plugin directory and its temporary files, then restarts Enigma2. To
return the box completely to stock, also remove the three settings files it
writes:

```bash
ssh root@192.168.1.244 'rm -f /etc/enigma2/hearable_look.json \
                              /etc/enigma2/hearable_sync.json \
                              /etc/enigma2/hearable_wol.json'
```

Nothing else on the receiver is ever modified: no Enigma2 setting, no init
script, no kernel module.

---

## First-time setup

### Mini PC

Debian 13 netinst, then in order:

```bash
# 1. packages — see docs/t9_appliance_prerequisites.md for why each one
sudo apt install -y ffmpeg python3-numpy python3-aiohttp curl ethtool python3-serial

# 2. the account and the tree
sudo adduser --disabled-password --gecos '' hearable
sudo -u hearable git clone <this repo> /home/hearable/hearable

# 3. the model — copy it, do not re-download: the checksum must match.
#    -L is not optional: models/ here is a symlink to a directory outside the
#    repository, and without it you transfer a dangling link, not 707 MB.
rsync -PL models/nemotron-3.5-asr-streaming-0.6b.q8_0.gguf \
      hearable@<mini pc>:/home/hearable/hearable/models/

# 4. build the ASR runtime on the machine itself
ssh hearable@<mini pc> 'cd hearable && scripts/build_t9_n95_cpu.sh'
```

The build script fetches the runtime itself, at the revision every measurement
in this repository was taken with, and disables the upstream repository's LFS
filters *before* checking out — without that, a machine with no `git-lfs`
aborts the checkout. That is how a first build from a clean clone fails.

**If the library will not load** with `GLIBCXX_3.4.xx not found`, the Python you
are running bundles an older C++ runtime than the one just built against it.
Anaconda does this. Use the system Python, or preload the system library:

```bash
LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libstdc++.so.6 python3 -m hearable.realtime_server ...
```

Verify the model before going further — a truncated copy fails much later and
much less clearly:

```
sha256  a5c435f294eea8f88ce68dd27b8c3bfea7f777cb2fbba04fcd30eaa555f429ae
bytes   741548352
```

**The private link.** Install the interface stanza and bring it up:

```bash
sudo cp systemd/hearable-private-link.conf /etc/network/interfaces.d/hearable
sudo ifup enp1s0
```

Static `10.77.0.2/24`, deliberately with **no gateway and no DNS** so it can
never become the default route, and it re-arms Wake-on-LAN on every boot because
the `r8169` driver clears it. Check the interface name matches your hardware.

**The services.** There is no installer for these yet; it is four copies and
four enables:

```bash
sudo cp systemd/hearable-*.service /etc/systemd/system/
sudo cp systemd/hearable.default   /etc/default/hearable
sudo cp systemd/99-hearable-ring.rules /etc/udev/rules.d/
sudo systemctl daemon-reload
sudo systemctl enable --now hearable-governor hearable-t9 \
                            hearable-idle-watchdog hearable-rgb
```

`hearable-rgb` is optional — it drives the status ring on the case and degrades
to doing nothing if the hardware or `pyserial` is absent.

**Passwordless sudo** for the `hearable` user, so the deploy script can restart
the service and the launcher can set the CPU governor.

### Receiver

Enigma2 with root SSH access, then:

```bash
tools/sf8008_install_plugin.sh <its address>
```

That is all. The plugin creates its own settings files on first use.

---

## Verifying a deploy

**The mini PC**, from anywhere:

```bash
ssh hearable@192.168.1.30 'systemctl is-active hearable-t9 hearable-governor \
                                              hearable-idle-watchdog'
```

**The receiver** — the status file is rewritten every second, so it is live, not
a snapshot:

```bash
ssh root@192.168.1.244 'python3 -c "
import json; d=json.load(open(\"/tmp/hearable_osd_status.json\"))
print(d[\"version\"], d[\"relay\"][\"state\"], d[\"look\"])"'
```

**End to end**: press `F4`. The greeting stands for four seconds, the mini PC
wakes in about 32 seconds if it was off, and the first subtitle follows about
4 seconds after the pipeline is up.

---

## Rolling back

Both sides roll back the same way they were deployed — check out the earlier
revision and deploy it again.

```bash
git checkout v1.5.2
tools/t9_deploy.sh
tools/sf8008_install_plugin.sh 192.168.1.244   # only if plugin/ differs
```

The receiver's settings files are not versioned and survive both directions, so
the viewer's chosen look is not lost by rolling back.

---

## What version is running

Three places have to agree, and a test enforces the first two:

| Where | How to read it |
|---|---|
| The tree | `hearable/__init__.py` |
| The plugin's own copy | `plugin/HearAbleOSD/__init__.py` |
| The running receiver | `version` in `/tmp/hearable_osd_status.json` |

`tests/test_version_consistency.py` refuses a tree whose two copies disagree, or
whose version is behind the newest tag. The installer refuses to deploy a
mismatch, and refuses to report success if the box then declares something else.

The viewer can read it without a terminal: pressing `F4` to switch HearAble off
shows `HearAble <version>` above `Arrivederci` for four seconds.

---

## Deploying a change you have not confirmed yet

Deploy **one side at a time**, and do not bump the version until the hardware
has agreed with you.

Raising the version forces a plugin reinstall even when `plugin/` did not
change — the consistency test requires the two copies to match, and the farewell
line would otherwise show a number that is not what is running. That means two
changes landing at the same moment, while you are trying to judge whether one of
them worked.

So: deploy, judge, *then* tag. The version number should mean confirmed rather
than hoped.
