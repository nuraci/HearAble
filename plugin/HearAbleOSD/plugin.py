"""HearAble subtitle overlay for Enigma2.

Renders the two subtitle lines HearAble sends over the LAN on top of whatever
the box is showing. It is a renderer and nothing else: it never decodes, never
touches the tuner, never takes a key press, and holds no state that outlives a
reconnection.

Everything here is wrapped defensively on purpose. This runs inside enigma2, so
an unhandled exception is not a stack trace in a terminal — it is the user's
television going black. The plugin logs and degrades; it never raises into the
host.

The moving parts are deliberately elsewhere: `wsserver.py` holds the transport
and `subtitle_session.py` holds the protocol behaviour, both testable on a PC.
What is left here is the part that genuinely needs a set-top box — the screen.
"""
from __future__ import annotations

import fcntl
import json
import os
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from Components.Label import Label
from Plugins.Plugin import PluginDescriptor
from Screens.Screen import Screen
from enigma import eTimer, getDesktop

from relay import AudioRelay, live_without_a_reference, source_kind
from subtitle_session import SubtitleSession
from wsserver import WebSocketServer

LISTEN_PORT = int(os.environ.get("HEARABLE_OSD_PORT", "8770"))
LISTEN_PATH = "/hearable"
CONTROL_PATH = "/control"
RELAY_PORT = int(os.environ.get("HEARABLE_RELAY_PORT", "9010"))
# Only the fallback, for a receiver that does not say what it wants. The figure
# that matters lives in the receiver's own config file, where it can be changed
# without touching the decoder, and reaches the relay on every control poll.
RELAY_LEAD_MS = int(os.environ.get("HEARABLE_RELAY_LEAD_MS", "1500"))
RELAY_POLL_MS = 400

# The screen is repainted at most this often. The producer may send far faster —
# the stress case is 200 states a second — and painting each one would spend the
# box's CPU redrawing text no one can read. Coalescing to the newest state at a
# fixed cadence is what keeps the UI responsive under that load.
RENDER_INTERVAL_MS = 40      # 25 FPS
POLL_INTERVAL_MS = 20

LOG_PATH = "/tmp/hearable_osd.log"
# Counters are published to tmpfs once a second rather than answered over the
# wire. The gate harness reads them over SSH, which keeps the measurement out of
# the path being measured: asking the renderer for its own statistics through
# the same socket carrying the subtitles would change what it is reporting on.
STATUS_PATH = "/tmp/hearable_osd_status.json"
STATUS_INTERVAL_MS = 1000

# Enigma2 keeps play positions in 90 kHz units, the MPEG presentation clock.
PTS_PER_SECOND = 90000.0

# How the two lines look, and the only place it is decided.
#
# Kept outside the code because the answer is a matter of eyesight and of the
# television it is seen on, not of engineering: the viewer picks it in front of
# the screen and the file remembers. Invalid or missing values fall back to
# what shipped, so a bad edit cannot leave someone without subtitles.
LOOK_CONFIG = "/etc/enigma2/hearable_look.json"
LOOK_DEFAULTS = {
    # 1.0 is the size that shipped. The band grows with the text, so a larger
    # font does not crop it — it moves the band up, not off the screen.
    "font_scale": 1.0,
    # The black band behind the words. Opacity 1.0 is the opaque band that
    # shipped; 0.0 hides it completely, which leaves white text alone over the
    # picture and is legible only over dark scenes.
    "background_enabled": True,
    "background_opacity": 1.0,
    # Slack left around the text inside the band, as a fraction of the line.
    "padding": 0.0,
    # The air between the two lines, as a fraction of the type size. Each line
    # sits vertically centred in a box this much taller than the letters, so
    # the gap the eye sees is the sum of two half-boxes. 0.61 is what shipped;
    # halving it brings the lines together without touching their size.
    #
    # It cannot go to nothing: ascenders and descenders need about a fifth of
    # the type size each, and «perché già giù, prqjgy» is the string that says
    # so. The lower limit leaves room for them.
    "line_gap": 0.613,
}
LOOK_LIMITS = {"font_scale": (0.8, 1.6), "background_opacity": (0.0, 1.0),
               "padding": (0.0, 0.5), "line_gap": (0.25, 1.0)}


def load_look() -> dict:
    """What the viewer chose, or what shipped if the file says something odd."""
    look = dict(LOOK_DEFAULTS)
    try:
        with open(LOOK_CONFIG) as handle:
            stored = json.load(handle)
    except Exception:
        return look
    if not isinstance(stored, dict):
        return look
    for key, default in LOOK_DEFAULTS.items():
        if key not in stored:
            continue
        value = stored[key]
        if isinstance(default, bool):
            if isinstance(value, bool):
                look[key] = value
            continue
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        low, high = LOOK_LIMITS[key]
        if low <= value <= high:
            look[key] = value
    return look


def _alpha(opacity: float) -> str:
    """An Enigma2 colour byte from an opacity anyone would recognise.

    The high byte is *transparency*, so it runs the other way: 0x00 is opaque
    and 0xff is invisible. Writing the conversion once, here, is what keeps the
    rest of the code talking about opacity like a person.
    """
    clamped = max(0.0, min(1.0, opacity))
    return f"{int(round(255 * (1.0 - clamped))):02x}"


def build_skin() -> str:
    """Lay the two subtitle lines out for whatever OSD this box actually has.

    Two things about Enigma2 skins are easy to get wrong and expensive to get
    wrong on someone's television:

    * The OSD is not the video resolution. This box decodes 1080p but draws its
      OSD at 1280x720, so coordinates written for 1920x1080 fall off the screen.
      The desktop is therefore measured rather than assumed.
    * In a skin colour the high byte is *transparency*, not opacity — the stock
      skin defines `transparent` as `#ffffffff`. Three consequences, all of which
      had to be seen on a television to be believed:
      - `#ff000000` is an invisible background, which is what the full-screen
        container wants.
      - The subtitle band is the opposite case and wants `#00000000`, fully
        opaque. At 80% opacity it looked fine over the dark parts of the picture
        and let white text vanish into a bright yellow one.
      - White text is `#00ffffff`, not `#ffffffff`. The latter is the skin's own
        definition of "transparent", so the glyphs were drawn see-through and
        took the colour of whatever was behind them — the words came out green
        over the green bar and blue over the blue one.

    The band is sized by broadcast practice rather than by what fits: two lines
    at roughly 7.5% of screen height each, touching, so they read as one block
    from across a room.
    """
    try:
        size = getDesktop(0).size()
        width, height = size.width(), size.height()
    except Exception:
        width, height = 1280, 720

    look = load_look()
    scale = look["font_scale"]

    margin = int(width * 0.04)
    line_width = width - 2 * margin
    # The type first, then the box around it: a bigger font in a fixed box is
    # how descenders get their tails cut off.
    font_size = max(22, int(height * 0.075 * scale * 0.62))
    line_height = max(34, int(font_size * (1.0 + look["line_gap"])
                              * (1.0 + look["padding"])))
    # The band keeps its distance from the bottom edge and grows upwards, so it
    # stays inside the safe area whatever the size.
    lower_top = height - int(height * 0.07) - line_height
    upper_top = lower_top - line_height

    background = ("#" + _alpha(look["background_opacity"]) + "000000"
                  if look["background_enabled"] else "#ff000000")

    def widget(name: str, top: int) -> str:
        return (f'<widget name="{name}" position="0,{top}" '
                f'size="{line_width},{line_height}" font="Regular;{font_size}" '
                'halign="center" valign="center" transparent="0" '
                f'foregroundColor="#00ffffff" backgroundColor="{background}"/>')

    # The window is the band, not the desktop.
    #
    # It used to be full screen and transparent, which looked identical and was
    # not: a full-screen window sitting on top took the remote with it. Volume
    # still worked, because that is handled globally, but MENU did nothing at
    # all — the television looked broken while HearAble was showing subtitles.
    # A window the size of what it actually draws leaves the rest of the screen,
    # and the rest of the remote, to Enigma2.
    return (f'<screen name="HearAbleOSDScreen" position="{margin},{upper_top}" '
            f'size="{line_width},{2 * line_height}" '
            'flags="wfNoBorder" backgroundColor="#ff000000" zPosition="2" title="HearAble">'
            + widget("upper", 0) + widget("lower", line_height) + "</screen>")


# Where the sync settings live on the box, and what they mean.
#
# This is a *publication* delay: the subtitle waits before being painted. It is
# not `lead_ms`, which is how far ahead of the viewer the file relay reads, and
# it does not touch audio, video, the recogniser or the stabiliser.
#
# It starts at zero, and that is a correction. It was built at 1500 ms because a
# `/grab` measurement said the tap ran 2.4 s ahead of the picture, so HearAble
# beat the image and had to be held back. Asking the decoder directly —
# AUDIO_GET_PTS against the PTS the PID filter is handing over at that same
# instant — the tap is **64 ms** ahead, not 2400: a factor of forty, and the
# viewer's own ear agreed with the smaller number. What is left over the picture
# is the recogniser's own second, which no delay can give back. So the delay is
# there for the viewer to add if their box needs it, and it adds nothing by
# default.
# The keys, as Linux input codes. The handset has F1 to F4 and the kernel's
# input device declares all four, so HearAble takes the three the design asked
# for and touches nothing else: F1 still opens the help, SUBTITLE still opens
# the subtitle menu, TEXT still opens teletext, FAV still opens the favourites.
KEY_SYNC_DOWN = 60         # F2
KEY_SYNC_UP = 61           # F3
KEY_TOGGLE = 62            # F4

SYNC_PATH = "/etc/enigma2/hearable_sync.json"
SYNC_DEFAULTS = {"file_delay_ms": 0, "dvb_delay_ms": 0,
                 "step_ms": 100, "min_ms": 0, "max_ms": 3000}
# Long enough that holding the key does not write the flash on every repeat.
SYNC_WRITE_AFTER_S = 2.0
# What the viewer is told, and for how long.
try:
    from . import __version__ as HEARABLE_VERSION
except (ImportError, ValueError):        # loaded as a loose file, not a package
    HEARABLE_VERSION = "unknown"

SYNC_NOTICE_S = 2.5

# The two lifecycle messages, and the only place either the credit or the
# version appears: never during speech, where the panel belongs to what is being
# said. The credit greets, the version says goodbye — so switching the subtitles
# off is also how you ask the decoder which build it is running, without a
# terminal and without knowing there is one.
CREDIT_LINE = "By Nunzio Raciti"
START_LOWER = "HearAble \u00b7 Avvio..."
# Just "Arrivederci": the line above already says HearAble, with the version
# after it, and the panel is two lines of forty characters — a word repeated on
# both of them is a word spent twice.
STOP_LOWER = "Arrivederci"
# How long the goodbye stands before the panel is cleared; how long the greeting
# stands *at least*, even when a subtitle is already waiting to take the panel;
# and how long it may stand at most if nothing ever comes to replace it.
GOODBYE_S = 4.0
GREETING_MIN_S = 4.0
GREETING_MAX_S = 120.0

# -- diagnostics ------------------------------------------------------------
# Off unless asked for, and bounded when on. It writes one JSON object per
# painted update to tmpfs, which is where a measurement can be fetched from
# without an SD card write, and stops rather than filling the box's memory.
DIAG_PATH = "/tmp/hearable_diag.jsonl"
DIAG_LIMIT = 20000
# The audio decoder's own clock, in 90 kHz ticks. Read with the packet in hand
# it is the only timestamp on this box that belongs to the broadcast rather than
# to a process, and it is what tells a render that moved in software from a
# render that moved in the stream.
AUDIO_DEVICE = "/dev/dvb/adapter0/audio0"
AUDIO_GET_PTS = 0x80086F13              # _IOR('o', 19, __u64)
PTS_HZ = 90000

def _pts_gap_ms(before, after):
    """Milliseconds between two 90 kHz stamps, or None if either is missing.

    A PTS is thirty-three bits and wraps roughly every twenty-six hours; a gap
    that comes out negative or absurd is a wrap, a discontinuity or a zap, and
    is reported as None rather than as a number somebody might average.
    """
    if before is None or after is None:
        return None
    gap = after - before
    if gap < 0:
        gap += 1 << 33
    if gap > 30 * PTS_HZ:
        return None
    return round(gap / PTS_HZ * 1000, 1)


# A subtitle nobody has refreshed for this long is stale. Long enough that a
# pause between sentences never blanks the panel, short enough that a producer
# which dies mid-sentence does not leave its last words on the television.
SUBTITLE_IDLE_S = 8.0


def log(message: str) -> None:
    """Append one line to a log on tmpfs. Never raises, never fills the flash."""
    try:
        with open(LOG_PATH, "a") as handle:
            handle.write(f"{time.strftime('%H:%M:%S')} {message}\n")
    except Exception:
        pass


class HearAbleOSDScreen(Screen):
    def __init__(self, session):
        self.skin = build_skin()
        Screen.__init__(self, session)
        self["upper"] = Label("")
        self["lower"] = Label("")
        self._visible = True

    def show_lines(self, upper: str, lower: str) -> None:
        self["upper"].setText(upper)
        self["lower"].setText(lower)
        # The panel is opaque, so an empty panel is a black band across somebody
        # else's television. It belongs on screen only while there is something
        # to read, and nowhere at all the rest of the time.
        self.set_visible(bool(upper.strip() or lower.strip()))

    def set_visible(self, visible: bool) -> None:
        if visible == self._visible:
            return
        self._visible = visible
        try:
            self.show() if visible else self.hide()
        except Exception:
            pass


class HearAbleOSD:
    """Owns the server, the session and the two timers."""

    def __init__(self, session) -> None:
        self.session = session
        self.screen = None
        self.state = SubtitleSession(stamp=lambda: self._audio_pts() if self.diag_on else None)
        self.relay = AudioRelay(self.playback, port=RELAY_PORT,
                                lead_ms=RELAY_LEAD_MS, log=log)
        self.server = WebSocketServer(
            port=LISTEN_PORT, path=LISTEN_PATH,
            on_message=self._on_message,
            on_event=lambda kind, detail: log(f"{kind}: {detail}"),
            on_http=self._on_http,
        )
        self.poll_timer = eTimer()
        self.render_timer = eTimer()
        self.status_timer = eTimer()
        self.relay_timer = eTimer()
        self.marker_timer = eTimer()
        self.renders = 0
        self.acks_sent = 0
        self._acked_seq: tuple | None = None
        self.render_times: list[float] = []
        self.sync = SYNC_DEFAULTS.copy()
        self.sync_writes = 0
        # Off at every start, deliberately and without memory of the last
        # session. Enigma2 restarts for reasons that have nothing to do with
        # HearAble — an image update, a watchdog, a crash — and a viewer who
        # did not ask for subtitles should never find the tap taken and a black
        # band across the picture because of a decision made days ago. F4 turns
        # it on; the relay stays off until it does.
        self.enabled = False
        self.relay.enabled = False
        # "start" while the greeting stands, "stop" while the goodbye does.
        # A lifecycle message is a UI state, not a subtitle: it never passes
        # through the stabiliser or the formatter, and it never becomes text
        # the producer believes it sent.
        self._lifecycle = None
        self._lifecycle_since = 0.0
        self._lifecycle_until = 0.0
        self.panel = {"upper": "", "lower": "", "source": "start", "at": 0.0}
        self._sync_dirty_since: float | None = None
        self._notice_until = 0.0
        self._keys_bound = False
        # What the remote did and what HearAble did about it. A pixel cannot
        # answer "was this key consumed" — semi-transparent menus sit over
        # moving video — and this can, exactly.
        self.keys_seen = 0
        self.keys_consumed = 0
        self.last_key: dict = {}
        self.diag_on = False
        self.diag_written = 0
        self.diag_dropped = 0
        self._diag_file = None
        self._audio_fd = None
        self.marker_on = False
        self.marker_count = 0
        self._marker_seq = 0
        # Elapsed time is measured against the monotonic clock, never the
        # wall clock. This box sets its clock *after* enigma2 has started, so a
        # cold boot moves the wall clock forward under a running plugin: the
        # first cold start after the A/V delay work had this reporting an
        # uptime of 78 minutes on a decoder that had been on for 12.
        self.started_at = time.monotonic()

    def start(self) -> None:
        try:
            self.screen = self.session.instantiateDialog(HearAbleOSDScreen)
            # Instantiated but not shown. HearAble is a thing the viewer turns
            # on; until subtitles actually arrive the television must look
            # exactly as it would without this plugin installed.
            self.screen.set_visible(False)
            log(f"skin: {build_skin()[:120]}")
            self.server.start()
        except Exception as error:
            log(f"start failed: {type(error).__name__}: {error}")
            return
        self.sync = self._load_sync()
        self._apply_sync()
        self._bind_keys()
        self._watch_service_changes()
        self.poll_timer.callback.append(self._poll)
        self.render_timer.callback.append(self._render)
        self.status_timer.callback.append(self._write_status)
        self.relay_timer.callback.append(self._relay_tick)
        self.marker_timer.callback.append(self._marker_tick)
        self.poll_timer.start(POLL_INTERVAL_MS, False)
        self.render_timer.start(RENDER_INTERVAL_MS, False)
        self.status_timer.start(STATUS_INTERVAL_MS, False)
        self.relay_timer.start(RELAY_POLL_MS, False)
        log(f"started on port {LISTEN_PORT}{LISTEN_PATH}")

    def stop(self) -> None:
        # Enigma2 is going: a shutdown, a deep standby, or a restart for an
        # image update. Tell the receiver, and let it decide what to do.
        #
        # Measured on openATV 7.5.1: this is never reached. Neither `init 4` nor
        # OpenWebif's clean restart calls a WHERE_SESSIONSTART plugin back with
        # reason 1 — the log simply stops. It is left here because it costs
        # nothing and is correct where the hook does fire, but nothing depends
        # on it: the receiver works out that the decoder has gone by asking it,
        # which also covers the case where the power is pulled and no message
        # could ever be sent.
        try:
            self.relay.announce_going_away()
        except Exception:
            pass
        for timer in (self.poll_timer, self.render_timer, self.status_timer,
                      self.relay_timer, self.marker_timer):
            try:
                timer.stop()
            except Exception:
                pass
        try:
            self.server.stop()
        except Exception:
            pass
        try:
            self.relay._kill()
        except Exception:
            pass
        self._diag_set(False)
        try:
            if self._audio_fd not in (None, -1):
                os.close(self._audio_fd)
        except Exception:
            pass
        self._audio_fd = None
        self._unbind_keys()
        original = getattr(self, "_original_play_service", None)
        if original is not None:
            try:
                self.session.nav.playService = original
            except Exception:
                pass
        if self.screen is not None:
            try:
                self.screen.hide()
            except Exception:
                pass
        log("stopped")

    # -- callbacks ---------------------------------------------------------
    def _on_message(self, text: str, client) -> None:
        self.state.handle_text(text)

    def _on_http(self, path: str, query: str = ""):
        """The control plane. Polled by the receiver; never carries audio.

        The receiver announces itself in the query string, which doubles as its
        heartbeat: the relay only runs while someone is asking.
        """
        if path != CONTROL_PATH:
            return None
        for part in query.split("&"):
            name, _, value = part.partition("=")
            if name == "receiver" and value:
                # The colon arrives percent-encoded. Handed on as it came, ffmpeg
                # is given tcp://host%3Aport and cannot open it.
                self.relay.announce(urllib.parse.unquote(value))
            elif name == "set_sync_ms" and value:
                # For the bench: the same path the keys take, without a remote.
                # Including the refusal — the FILE delay has not been measured,
                # so nothing may write it. Without this the endpoint wrote 1500
                # into `file_delay_ms` whenever it was called in a moment when
                # the decoder was between services, and a file played later
                # would have been held back by a number nobody had measured.
                try:
                    key = self._active_delay_key()
                    if key == "dvb_delay_ms":
                        self.sync[key] = self._clamp_sync(int(value), self.sync)
                        self._apply_sync()
                        self._sync_dirty_since = time.monotonic()
                except ValueError:
                    pass
            elif name == "diag" and value in ("on", "off"):
                self._diag_set(value == "on")
            elif name == "marker" and value in ("on", "off"):
                self._marker_set(value == "on")
            elif name == "diag_note" and value:
                # A label the campaign can drop into the log to mark a step.
                self._diag({"event": "note", "text": str(value)[:120],
                            "audio_pts": self._audio_pts(),
                            "delay_ms": self.state.publish_delay_ms})
            elif name in LOOK_DEFAULTS and value:
                self._set_look(name, value)
            elif name == "hearable" and value in ("on", "off"):
                wanted = value == "on"
                if wanted != self.enabled:
                    self.toggle_hearable()
            elif name == "want_lead_ms" and value:
                try:
                    self.relay.set_lead_ms(int(value))
                except ValueError:
                    pass
            elif name == "lead_s" and value:
                try:
                    self.relay.report_lead(float(value))
                except ValueError:
                    pass
        return json.dumps(self.relay.control())

    def _relay_tick(self) -> None:
        self.relay.poll()

    # -- the remote ---------------------------------------------------------
    def _bind_keys(self) -> None:
        """Watch the remote for our three keys, and get out of the way for the rest.

        F2, F3 and F4, which the handset has and the kernel's input device
        declares (codes 60, 61 and 62). openATV maps them to the bare actions
        `f2`, `f3` and `f4`, which nothing on this image claims while the
        viewer is watching television: measured on the box, each did nothing to
        the picture. F1 keeps the help, and no other key is touched.

        The box's own description of the handset, `rc_models/sf8008/
        rcpositions.xml`, draws only F1 and F2. It is wrong about this remote,
        and the remote is the authority.

        Every other key returns 0 and passes straight through. That matters more
        than it sounds: a full-screen overlay once swallowed the whole remote in
        this project, and a binding that consumed everything would do it again.
        """
        try:
            from enigma import eActionMap
            self._action_map = eActionMap.getInstance()
            self._action_map.bindAction("", -0x7FFFFFFF, self._on_key)
            self._keys_bound = True
            log("remote keys bound: F2 sync-, F3 sync+, F4 on/off")
        except Exception as error:
            log(f"remote keys unavailable: {type(error).__name__}: {error}")

    def _unbind_keys(self) -> None:
        if not self._keys_bound:
            return
        try:
            self._action_map.unbindAction("", self._on_key)
        except Exception:
            pass
        self._keys_bound = False

    def _on_key(self, key: int, flag: int) -> int:
        """0 means "not mine, carry on". Only our three keys ever return 1."""
        taken = 0
        try:
            if flag != 1:                       # only the make, never repeats or breaks
                return 0
            self.keys_seen += 1
            if key == KEY_TOGGLE:
                self.toggle_hearable()
                taken = 1
            elif not self.enabled:
                # HearAble is off: the sync keys do nothing and are handed
                # straight back, so the box behaves exactly as it would without
                # the plugin installed.
                taken = 0
            elif key == KEY_SYNC_DOWN:
                self.adjust_sync(-self.sync["step_ms"])
                taken = 1
            elif key == KEY_SYNC_UP:
                self.adjust_sync(+self.sync["step_ms"])
                taken = 1
        except Exception as error:
            log(f"key handling failed: {type(error).__name__}: {error}")
        if flag == 1:
            self.keys_consumed += taken
            self.last_key = {"code": key, "consumed": bool(taken),
                             "at": round(time.monotonic() - self.started_at, 2)}
        return taken

    # -- sync settings ------------------------------------------------------
    def _load_sync(self) -> dict:
        """The saved settings, or the defaults, and never an exception.

        A corrupt or half-written file must not stop HearAble from starting:
        the defaults are a working configuration, and the file is rewritten the
        next time the viewer changes anything.
        """
        settings = dict(SYNC_DEFAULTS)
        try:
            with open(SYNC_PATH) as handle:
                saved = json.load(handle)
            if isinstance(saved, dict):
                for key, value in saved.items():
                    if key in settings and isinstance(value, (int, float)):
                        settings[key] = int(value)
        except Exception as error:
            log(f"sync settings unreadable, using defaults: {type(error).__name__}")
        # Not a setting yet: no measurement exists of how far the subtitle sits
        # from the picture on the FILE path, and a delay invented rather than
        # measured is worse than none. The field stays in the file so the shape
        # of the configuration is the one the design asked for.
        settings["file_delay_ms"] = 0
        settings["min_ms"] = max(0, settings["min_ms"])
        settings["max_ms"] = max(settings["min_ms"], settings["max_ms"])
        for key in ("file_delay_ms", "dvb_delay_ms"):
            settings[key] = self._clamp_sync(settings[key], settings)
        settings["step_ms"] = max(10, min(1000, settings["step_ms"]))
        return settings

    @staticmethod
    def _clamp_sync(value: int, settings: dict) -> int:
        return max(settings["min_ms"], min(settings["max_ms"], int(value)))

    def _save_sync(self) -> None:
        """Write the settings where they will survive a reboot, atomically."""
        try:
            temporary = SYNC_PATH + ".tmp"
            with open(temporary, "w") as handle:
                json.dump(self.sync, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, SYNC_PATH)
            self.sync_writes += 1
        except Exception as error:
            log(f"sync settings not saved: {type(error).__name__}: {error}")

    def _current_source_kind(self) -> str:
        info = self.relay.playback()
        kind = source_kind(info.get("service_reference"))
        if kind == "unknown" and live_without_a_reference(info):
            return "dvb"
        return kind

    def _active_delay_key(self) -> str:
        return "dvb_delay_ms" if self._current_source_kind() == "dvb" else "file_delay_ms"

    def _apply_sync(self) -> None:
        key = self._active_delay_key()
        self.state.set_publish_delay_ms(self.sync[key])

    def adjust_sync(self, step_ms: int) -> None:
        """One press of the sync keys."""
        key = self._active_delay_key()
        if key == "file_delay_ms":
            # The file path's own delay has not been qualified, so the keys say
            # so rather than quietly moving a number nobody has measured.
            self._notice("HearAble · sync FILE", "non ancora qualificato")
            return
        before = self.sync[key]
        self.sync[key] = self._clamp_sync(before + step_ms, self.sync)
        self._apply_sync()
        self._sync_dirty_since = time.monotonic()
        self._notice("HearAble · sync DVB", f"{self.sync[key]} ms")
        log(f"sync {key}: {before} -> {self.sync[key]}")

    def toggle_hearable(self) -> None:
        """On or off, for real: off lets go of the tap and everything with it."""
        self.enabled = not self.enabled
        if not self.enabled:
            self.state.drop_pending("hearable_off")
            self.state.upper_line = ""
            self.state.lower_line = ""
            self.relay.release("hearable_off")
            self.relay.enabled = False
            log("HearAble off: tap released, subtitles cleared")
            self._lifecycle_message("stop")
        else:
            # A new epoch, and an empty line: what was true before the switch
            # was thrown belongs to a stream nobody is watching any more.
            self.state.drop_pending("hearable_on")
            self.relay.enabled = True
            self.relay.epoch += 1
            # F4 is the viewer asking for subtitles, and the machine that makes
            # them may be asleep: give the wake a fresh budget of attempts and
            # send the first packet now rather than on the next poll.
            self.relay.reset_wake()
            self.relay.wake_receiver()
            log(f"HearAble on: epoch {self.relay.epoch}")
            self._lifecycle_message("start")

    def _set_look(self, name: str, value: str) -> None:
        """Change how the subtitles look, now, without restarting enigma2.

        Comparing two sizes of type on a television means seeing them minutes
        apart otherwise, which is no comparison at all: by the time the box has
        restarted, nobody remembers the first one well enough to prefer it.
        """
        look = load_look()
        default = LOOK_DEFAULTS[name]
        if isinstance(default, bool):
            if value.lower() not in ("0", "1", "true", "false", "on", "off"):
                return
            parsed = value.lower() in ("1", "true", "on")
        else:
            try:
                parsed = float(value)
            except ValueError:
                return
            low, high = LOOK_LIMITS[name]
            if not low <= parsed <= high:
                log(f"look: {name}={parsed} fuori dai limiti {low}..{high}")
                return
        look[name] = parsed
        try:
            with open(LOOK_CONFIG, "w") as handle:
                json.dump(look, handle)
        except Exception as error:
            log(f"look: scrittura fallita: {type(error).__name__}: {error}")
            return
        log(f"look: {name} = {parsed}")
        self._rebuild_screen()

    def _rebuild_screen(self) -> None:
        """Throw the panel away and build it again from the new skin.

        A skin is read once, when the screen is instantiated, so there is no way
        to change type size in place: the dialog goes and another takes its
        place. What is on screen is put back afterwards, so a change made while
        someone is reading does not blank the line they were reading.
        """
        if self.session is None:
            return
        upper, lower = self.panel.get("upper", ""), self.panel.get("lower", "")
        visible = bool(upper.strip() or lower.strip())
        try:
            if self.screen is not None:
                self.screen.set_visible(False)
                self.session.deleteDialog(self.screen)
        except Exception as error:
            log(f"look: rimozione del pannello fallita: {type(error).__name__}: {error}")
        try:
            self.screen = self.session.instantiateDialog(HearAbleOSDScreen)
            self.screen.set_visible(False)
            if visible:
                self.screen.show_lines(upper, lower)
            log(f"look: pannello ricostruito, {build_skin()[:110]}")
        except Exception as error:
            self.screen = None
            log(f"look: ricostruzione fallita: {type(error).__name__}: {error}")

    def _lifecycle_message(self, kind: str) -> None:
        """The greeting and the goodbye, the only lines carrying the credit.

        The greeting does not hold anything up: the pipeline is already being
        started behind it, and the first real subtitle replaces it the moment it
        arrives. The goodbye stands for a few seconds and then the panel is
        cleared, so nothing of the closed session survives on screen.
        """
        self._lifecycle = kind
        self._lifecycle_since = time.monotonic()
        self._lifecycle_until = time.monotonic() + (
            GREETING_MAX_S if kind == "start" else GOODBYE_S)
        # A lifecycle message outranks a sync confirmation that may still stand.
        self._notice_until = 0.0
        upper = CREDIT_LINE if kind == "start" else f"HearAble {HEARABLE_VERSION}"
        self._paint(upper, START_LOWER if kind == "start" else STOP_LOWER,
                    f"lifecycle_{kind}")
        log(f"lifecycle: {kind}")

    def _paint(self, upper: str, lower: str, source: str) -> None:
        """Paint the panel and remember what is on it.

        `state.upper_line` is what the recogniser said; this is what the viewer
        can see, which is a different thing whenever a lifecycle message or a
        notice is standing. Keeping both lets a test read the screen instead of
        inferring it.
        """
        self.panel = {"upper": upper, "lower": lower, "source": source,
                      "at": round(time.time(), 3)}
        if self.screen is not None:
            self.screen.show_lines(upper, lower)

    def _notice(self, upper: str, lower: str) -> None:
        """A short word to the viewer, which never becomes the subtitle."""
        self._notice_until = time.monotonic() + SYNC_NOTICE_S
        self._paint(upper, lower, "notice")

    def _watch_service_changes(self) -> None:
        """Let go of the tuner before Enigma2 needs it, not after.

        This box has one terrestrial tuner, and a client attached to port 8001
        holds the service it is reading. Changing to a channel on another
        multiplex needs that tuner retuned, and while the stream is attached the
        attempt fails and leaves the television with no service at all.

        That is the platform, not this plugin: a plain `curl` on port 8001
        reproduces it exactly, and releasing the stream four seconds before the
        same zap makes it succeed. So the relay lets go when the viewer *asks*
        for another service, which is what playService means, rather than when
        the old one has already ended — by then the tuning has been attempted
        and lost.

        The service events are kept as well: they cover the cases a direct
        playService call does not, such as a service ending on its own.
        """
        try:
            from enigma import iPlayableService
            self._ending_events = {iPlayableService.evEnd, iPlayableService.evStopped}
            self.session.nav.event.append(self._service_event)
        except Exception as error:
            log(f"service events unavailable: {type(error).__name__}: {error}")
        try:
            navigation = self.session.nav
            original = navigation.playService
            if getattr(original, "_hearable_guarded", False):
                return

            def guarded(*args, **kwargs):
                try:
                    self.relay.release("service_change_requested")
                except Exception as error:
                    log(f"release failed: {type(error).__name__}: {error}")
                return original(*args, **kwargs)

            guarded._hearable_guarded = True
            self._original_play_service = original
            navigation.playService = guarded
            log("watching service events and service changes")
        except Exception as error:
            log(f"service change guard unavailable: {type(error).__name__}: {error}")

    def _service_event(self, event) -> None:
        try:
            if event in getattr(self, "_ending_events", ()):
                self.relay.release("service_ending")
        except Exception as error:
            log(f"service event failed: {type(error).__name__}: {error}")

    def _poll(self) -> None:
        try:
            self.server.poll()
            self._retire_stale_subtitles()
            self._settle_sync()
        except Exception as error:
            log(f"poll failed: {type(error).__name__}: {error}")

    def _settle_sync(self) -> None:
        """Write the settings once the viewer has stopped pressing.

        Holding the key repeats it many times a second; writing the flash on
        every repeat would be a write storm for a value that is still moving.
        """
        if self._sync_dirty_since is None:
            return
        if time.monotonic() - self._sync_dirty_since >= SYNC_WRITE_AFTER_S:
            self._sync_dirty_since = None
            self._save_sync()

    def _retire_stale_subtitles(self) -> None:
        """Take the panel down when nobody is asking for it any more.

        Two ways that happens. The producer goes away — the run ends, the
        recogniser is stopped, the laptop is closed — and whatever was on screen
        would otherwise stay there for good; that is how a test message was
        still sitting on the television hours later. Or the producer is still
        connected but has gone quiet, which a subtitle should not survive.
        """
        if self.screen is None:
            return
        showing = bool(self.state.upper_line.strip() or self.state.lower_line.strip())
        if not showing:
            return
        idle_for = time.monotonic() - self.state.last_state_at
        if self.server.client_count == 0 or idle_for > SUBTITLE_IDLE_S:
            self.state.upper_line = ""
            self.state.lower_line = ""
            if self._lifecycle is not None:
                # A lifecycle message is standing, and this rule is about stale
                # subtitles, not about it. Forgetting the old lines is right;
                # painting over the greeting is not — and it happened: F4 was
                # pressed with the machine asleep, the last subtitle of the
                # previous session was still in `state`, and eight seconds later
                # the greeting was wiped. The viewer then watched a blank panel
                # for the twenty seconds it takes the T9 to wake, with nothing
                # to say that anything was happening.
                return
            self._paint("", "", "idle_hidden")
            log("panel hidden: "
                + ("no producer connected" if self.server.client_count == 0
                   else f"nothing for {idle_for:.1f} s"))

    # -- diagnostics --------------------------------------------------------
    def _audio_pts(self):
        """The PTS the audio decoder is playing, in 90 kHz ticks, or None.

        The device is opened read-only and non-blocking and only ever asked a
        question; it is not the decoder's controlling handle and nothing here
        writes to it. Measured on this box, opening it changes nothing on the
        screen. Everything is guarded: a plugin that raises inside the main loop
        is a frozen television.
        """
        if self._audio_fd is None:
            try:
                self._audio_fd = os.open(AUDIO_DEVICE, os.O_RDONLY | os.O_NONBLOCK)
            except OSError:
                self._audio_fd = -1
        if self._audio_fd == -1:
            return None
        try:
            buffer = bytearray(8)
            fcntl.ioctl(self._audio_fd, AUDIO_GET_PTS, buffer, True)
            return int.from_bytes(bytes(buffer), sys.byteorder)
        except OSError:
            return None

    def _diag_set(self, on: bool) -> None:
        if on == self.diag_on:
            return
        if on:
            try:
                self._diag_file = open(DIAG_PATH, "a")
                self.diag_on = True
                self._diag({"event": "diag_on", "limit": DIAG_LIMIT})
                log("diagnostics on")
            except Exception as error:
                log(f"diagnostics could not start: {type(error).__name__}: {error}")
                self._diag_file = None
        else:
            self._diag({"event": "diag_off", "written": self.diag_written})
            self.diag_on = False
            try:
                if self._diag_file is not None:
                    self._diag_file.close()
            except Exception:
                pass
            self._diag_file = None
            log("diagnostics off")

    def _diag(self, record: dict) -> None:
        if self._diag_file is None:
            return
        if self.diag_written >= DIAG_LIMIT:
            self.diag_dropped += 1
            return
        try:
            record["monotonic"] = round(time.monotonic(), 6)
            record["wall"] = round(time.time(), 3)
            self._diag_file.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._diag_file.flush()
            self.diag_written += 1
        except Exception:
            self.diag_dropped += 1

    def _marker_tick(self) -> None:
        """A subtitle nobody spoke.

        The point of it is that no audio, no recogniser and no stabiliser are
        involved: the state is handed straight to the session, so what is
        measured afterwards is the scheduler and the screen and nothing else.
        The text counts, so a camera pointed at the television can read the
        offset off the picture.
        """
        if not self.marker_on:
            return
        try:
            self._marker_seq += 1
            self.marker_count += 1
            born = time.monotonic()
            message = {"protocol_version": 1, "type": "subtitle_state",
                       "seq": 900000 + self._marker_seq, "source_id": "diagnostic",
                       "source_epoch": 9000, "session_id": "diagnostic",
                       "upper_line": f"DELAY TEST {self.marker_count:03d}",
                       "lower_line": f"{time.strftime('%H:%M:%S')}  D={self.state.publish_delay_ms} ms",
                       "generated_monotonic_ms": round(born * 1000, 3)}
            self._diag({"event": "marker_born", "marker": self.marker_count,
                        "seq": message["seq"], "audio_pts": self._audio_pts(),
                        "delay_ms": self.state.publish_delay_ms})
            self.state.handle_text(json.dumps(message))
        except Exception as error:
            log(f"marker failed: {type(error).__name__}: {error}")

    def _marker_set(self, on: bool) -> None:
        self.marker_on = on
        try:
            if on:
                self.state.guard.reset()
                self.marker_timer.start(1000, False)
                log("marker mode on")
            else:
                self.marker_timer.stop()
                # The markers ran at an epoch far above any real producer's, so
                # the guard would reject the genuine stream for the rest of the
                # session. Reset it on the way out.
                self.state.guard.reset()
                self.state.drop_pending("marker_off")
                self.state.upper_line = ""
                self.state.lower_line = ""
                self._paint("", "", "marker_off")
                log("marker mode off")
        except Exception as error:
            log(f"marker mode failed: {type(error).__name__}: {error}")

    def _render(self) -> None:
        try:
            if not self.enabled:
                # Nothing waits through the switch being off. A state that
                # arrived just as HearAble was turned off would otherwise sit in
                # the delay line and be painted on the way back in, fifty
                # seconds stale — which is what the measurement showed.
                #
                # Before the notice, not after: the confirmation the viewer sees
                # stands for two and a half seconds, and states kept arriving
                # through it. Measured, the line still held three or four.
                self.state.drop_pending("hearable_off")
                if self._lifecycle == "stop" and time.monotonic() >= self._lifecycle_until:
                    # The goodbye has had its seconds; leave the panel empty
                    # rather than leaving the last thing said on the screen.
                    self._lifecycle = None
                    self._paint("", "", "cleared_after_goodbye")
                return
            if self._lifecycle == "start" and time.monotonic() >= self._lifecycle_until:
                # Nothing ever came to replace the greeting. Clear it instead of
                # leaving a message that no longer describes anything.
                self._lifecycle = None
                self._paint("", "", "greeting_expired")
            elif (self._lifecycle == "start"
                  and time.monotonic() - self._lifecycle_since < GREETING_MIN_S):
                # The greeting gets its seconds even when a subtitle is already
                # due. Nothing is thrown away to give it them: the states wait
                # in the delay line, which keeps the newest and discards the
                # ones it has overtaken, so what appears afterwards is what is
                # being said then and not what was being said four seconds ago.
                return
            if time.monotonic() < self._notice_until:
                return                      # the viewer is being told something
            wanted = self.sync.get(self._active_delay_key(), 0)
            if wanted != self.state.publish_delay_ms:
                previous = self.state.publish_delay_ms
                self.state.set_publish_delay_ms(wanted)
                self._diag({"event": "delay_change", "old_ms": previous,
                            "new_ms": wanted, "audio_pts": self._audio_pts(),
                            "queue_depth": len(self.state._waiting)})
            render_call = time.monotonic()
            pts_before = self._audio_pts() if self.diag_on else None
            lines = self.state.take_render()
            if lines is None:
                return
            # The first real subtitle is what the greeting was waiting for.
            self._lifecycle = None
            self._paint(lines[0], lines[1], "subtitle")
            if self.diag_on:
                painted = self.state.last_painted or {}
                pts_after = self._audio_pts()
                arrived = painted.get("arrived_monotonic")
                due = painted.get("due_monotonic")
                render_return = time.monotonic()
                self._diag({
                    "event": "render",
                    "seq": painted.get("seq"),
                    "text_hash": hash(lines[0] + "\x1f" + lines[1]) & 0xFFFFFFFF,
                    "preview": lines[0][:28],
                    # Three clocks, kept apart on purpose. `arrived` and the
                    # render stamps are this box's CLOCK_MONOTONIC; `generated`
                    # is the producer's own monotonic on another machine and is
                    # recorded raw, never subtracted from anything here; the PTS
                    # pair belongs to the broadcast at 90 kHz.
                    "arrived_monotonic": arrived,
                    "target_monotonic": due,
                    "render_call_monotonic": round(render_call, 6),
                    "render_return_monotonic": round(render_return, 6),
                    "producer_generated_monotonic_ms":
                        painted.get("generated_monotonic_ms"),
                    "configured_delay_ms": self.sync.get(self._active_delay_key()),
                    "delay_ms_in_force": painted.get("delay_ms_in_force"),
                    "coalesced_with_it": painted.get("coalesced_with_it"),
                    "queue_depth_after": len(self.state._waiting),
                    "audio_pts_before_render": pts_before,
                    "audio_pts_after_render": pts_after,
                    "ready_to_render_ms": (round((render_call - arrived) * 1000, 2)
                                           if arrived is not None else None),
                    "target_to_render_ms": (round((render_call - due) * 1000, 2)
                                            if due is not None else None),
                    "render_duration_ms": round((render_return - render_call) * 1000, 3),
                    "audio_pts_at_arrival": painted.get("stamp_at_arrival"),
                    # The wait measured in the broadcast's own clock rather than
                    # in the box's: if the delay is real, this is the delay.
                    "pts_across_the_wait_ms": _pts_gap_ms(
                        painted.get("stamp_at_arrival"), pts_after),
                    "service": (self.relay.playback() or {}).get("service_reference"),
                    "audio_pid": self.relay.live_pid,
                    "osd_generation": self.renders + 1,
                    "marker_mode": self.marker_on,
                })
            # Tell the producer what reached the screen. It is observational:
            # nothing upstream waits for it, and a producer that never sees one
            # carries on unaffected. It is what makes the network-and-render leg
            # measurable instead of assumed.
            # Keyed by the stream as well as the number: a fresh producer starts
            # its sequence again at one, and an ack suppressed as a duplicate of
            # the previous session's would leave it believing nothing was painted.
            seq = self.state.last_rendered_seq
            if seq is not None and (self.state.session_id, seq) != self._acked_seq:
                self._acked_seq = (self.state.session_id, seq)
                self.server.broadcast(self.state.ack_for(seq).encode("utf-8"))
                self.acks_sent += 1
            self.renders += 1
            self.render_times.append(round(time.time(), 4))
            if len(self.render_times) > 4096:
                del self.render_times[:2048]
        except Exception as error:
            log(f"render failed: {type(error).__name__}: {error}")

    def playback(self) -> dict:
        """Where the decoder actually is, asked of the decoder.

        This is the thing the network cannot be told: OpenWebif on this image has
        no getPosition controller, `statusinfo` reports duration 0 for a 4097:
        reference, and the whole previous gate recovered the position by matching
        a frame off the screen against a bit-identical local copy of the media.
        Inside enigma2 it is simply a method call, and it is exact.

        Everything here is defensive. A plugin that throws inside the main loop
        is a frozen television, and a service that has just ended or has not
        started yet answers with a failure code rather than raising.
        """
        info = {"available": False, "seekable": False}
        try:
            service = self.session.nav.getCurrentService()
            if service is None:
                info["reason"] = "no current service"
                return info

            # What every service can answer, broadcast included. This used to sit
            # below the seek check, so a live channel — which has no play
            # position — made the plugin report nothing at all: no reference, no
            # tracks, `idle`. It knew what was on and said it did not.
            reference = self.session.nav.getCurrentlyPlayingServiceReference()
            if reference is not None:
                info["service_reference"] = reference.toString()

            tracks = service.audioTracks()
            if tracks is not None:
                current = tracks.getCurrentTrack()
                info["audio_track"] = current
                info["audio_track_count"] = tracks.getNumberOfTracks()
                described = tracks.getTrackInfo(current)
                if described is not None:
                    info["audio_track_language"] = described.getLanguage()
                    info["audio_track_description"] = described.getDescription()
                    # The PID is what identifies the selected audio in a
                    # broadcast transport stream, where an index is only ffmpeg's
                    # ordering of what it happened to find.
                    if hasattr(described, "getPID"):
                        info["audio_pid"] = described.getPID()

            info["available"] = True

            # And what only a file can answer.
            seek = service.seek()
            if seek is None:
                info["reason"] = "service is not seekable"
                return info
            ok, position = seek.getPlayPosition()
            if ok:
                info["reason"] = "getPlayPosition reported failure"
                return info
            info["position_s"] = round(position / PTS_PER_SECOND, 3)
            length_ok, length = seek.getLength()
            if not length_ok:
                info["length_s"] = round(length / PTS_PER_SECOND, 3)
            info["sampled_wall_epoch"] = time.time()
            info["seekable"] = True
        except Exception as error:
            info["reason"] = f"{type(error).__name__}: {error}"
        return info

    def _write_status(self) -> None:
        try:
            with open(STATUS_PATH, "w") as handle:
                json.dump(self.status(), handle)
        except Exception as error:
            log(f"status write failed: {type(error).__name__}: {error}")

    def status(self) -> dict:
        counters = self.state.counters()
        counters.update({
            "uptime_s": round(time.monotonic() - self.started_at, 1),
            "renders": self.renders,
            "render_acks_sent": self.acks_sent,
            "render_times": self.render_times[-256:],
            "render_interval_ms": RENDER_INTERVAL_MS,
            "poll_interval_ms": POLL_INTERVAL_MS,
            "playback": self.playback(),
            "relay": {k: v for k, v in self.relay.control().items() if k != "events"},
            "version": HEARABLE_VERSION,
            "look": load_look(),
            "panel": dict(self.panel),
            "lifecycle": {"showing": self._lifecycle,
                          "credit_line": CREDIT_LINE,
                          "version_line": f"HearAble {HEARABLE_VERSION}",
                          "goodbye_s": GOODBYE_S,
                          "greeting_min_s": GREETING_MIN_S,
                          "greeting_max_s": GREETING_MAX_S,
                          "shown_for_s": (round(time.monotonic() - self._lifecycle_since, 2)
                                          if self._lifecycle else None),
                          "clears_in_s": (round(self._lifecycle_until - time.monotonic(), 2)
                                          if self._lifecycle else None)},
            "sync": {**self.sync, "active_key": self._active_delay_key(),
                     "enabled": self.enabled, "writes": self.sync_writes,
                     "keys": {"down": KEY_SYNC_DOWN, "up": KEY_SYNC_UP,
                              "toggle": KEY_TOGGLE, "bound": self._keys_bound,
                              "seen": self.keys_seen,
                              "consumed": self.keys_consumed,
                              "last": self.last_key}},
            "scheduler": self.state.scheduler_state(),
            "diagnostics": {"on": self.diag_on, "written": self.diag_written,
                            "dropped": self.diag_dropped, "limit": DIAG_LIMIT,
                            "path": DIAG_PATH, "marker": self.marker_on,
                            "markers_sent": self.marker_count,
                            "audio_pts": self._audio_pts() if self.diag_on else None},
            "clients": self.server.client_count,
            "connections_accepted": self.server.connections_accepted,
            "messages_received": self.server.messages_received,
            "transport_errors": self.server.protocol_errors,
        })
        return counters


_instance: HearAbleOSD | None = None


def sessionstart(reason, session=None, **kwargs):
    global _instance
    try:
        if reason == 0 and session is not None:
            _instance = HearAbleOSD(session)
            _instance.start()
        elif reason == 1 and _instance is not None:
            _instance.stop()
            _instance = None
    except Exception as error:
        log(f"sessionstart failed: {type(error).__name__}: {error}")


def status_json() -> str:
    """Counters for the gate harness, read over SSH rather than over the wire."""
    return json.dumps(_instance.status() if _instance else {"running": False})


def Plugins(**kwargs):
    return [PluginDescriptor(
        name="HearAble OSD",
        description="Renders HearAble subtitles sent over the LAN",
        where=PluginDescriptor.WHERE_SESSIONSTART,
        fnc=sessionstart,
    )]
