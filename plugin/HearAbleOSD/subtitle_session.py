"""Receiver-side subtitle state, independent of both transport and OSD.

Everything the renderer decides — which state to paint, what to discard, when to
clear — lives here, so it can be tested on a PC without a set-top box in the
room. `plugin.py` supplies the socket and the labels; this file supplies the
behaviour.

The shape follows the producer's: exactly one subtitle state is ever pending, so
a renderer that falls behind loses freshness rather than accumulating a backlog,
and control messages get their own small bounded queue so a clear or a source
change is never overwritten by the subtitle state that follows it.
"""
from __future__ import annotations

import json
import time
from collections import deque

from enigma2_protocol import EpochGuard, ProtocolError, PROTOCOL_VERSION, render_ack, validate

CONTROL_QUEUE_LIMIT = 32
# The most states the delay line will ever hold. At the maximum delay of three
# seconds this is a producer sending twenty a second, well past anything the
# stabiliser emits; past it the oldest is dropped rather than the memory grown.
WAITING_LIMIT = 64


class SubtitleSession:
    """What is on the screen, and when it is allowed to get there.

    The delay lives here and nowhere else. The recogniser never waits for it,
    the stabiliser knows nothing about it, and no thread sleeps: each state
    carries the instant it arrived, and the render tick that already runs every
    forty milliseconds looks at the clock.

    The waiting states form a delay line, not a queue of work. Its depth is set
    by the delay and the producer's own rate — a second and a half of speech at
    six updates a second is nine entries — and it is capped, so a producer gone
    mad cannot grow it without bound. One pending slot was tried first and is
    wrong: while somebody is talking, each new state would push the previous
    one's deadline back, and the subtitle would never be painted at all. That
    was measured on the box, not reasoned about: 197 states waiting and three
    painted in three minutes.

    Coalescing still happens, where it belongs: when several states are due in
    the same tick — a screen that fell behind, a delay that was just lowered —
    only the newest is painted. Nothing obsolete ever reaches the picture.
    """

    def __init__(self, control_queue_limit: int = CONTROL_QUEUE_LIMIT,
                 stamp=None) -> None:
        # An optional second clock, sampled as a state arrives and carried with
        # it. On the box it is the audio decoder's PTS: the broadcast's own
        # time, which is the only way to say that a subtitle waited a second and
        # a half *of television* rather than a second and a half of Python.
        self._stamp = stamp
        self.guard = EpochGuard()
        self._waiting: deque = deque()
        self._control: deque = deque(maxlen=control_queue_limit)
        self.upper_line = ""
        self.lower_line = ""
        self.session_id = ""
        # The sequence number of the state currently on the screen, so the
        # producer can be told what was painted rather than what was sent.
        self.pending_seq: int | None = None
        self.last_rendered_seq: int | None = None
        # When a state last arrived, so a panel whose producer has gone quiet
        # can be taken off the screen instead of standing there indefinitely.
        # Monotonic, because this is only ever read as an elapsed time and the
        # box corrects its wall clock after enigma2 has started.
        self.last_state_at = time.monotonic()
        # How long a state waits before it may be painted. Changed at runtime;
        # nothing else in the pipeline is told, because nothing else cares.
        self.publish_delay_ms = 0
        # Which message arrived first. A barrier may only discard a state that
        # was already waiting when it was raised: a state that arrived *after*
        # the clear belongs to what comes next, and dropping it would blank the
        # screen for the very words the producer sent to replace the old ones.
        self._arrivals = 0
        self.coalesced_while_waiting = 0
        self.published = 0
        self.invalidated_by_barrier = 0
        self.dropped_overflow = 0
        self.queue_depth_max = 0
        # Filled in by take_render for whoever is measuring; nothing in the
        # rendering path reads it.
        self.last_painted: dict | None = None

        self.messages_accepted = 0
        self.messages_invalid = 0
        self.states_received = 0
        self.states_superseded = 0
        self.states_rendered = 0
        self.controls_dropped = 0
        self.hello_seen = 0
        self.heartbeats_seen = 0
        self.last_error = ""

    # -- input -------------------------------------------------------------
    def handle_text(self, text: str) -> dict | None:
        """Take one wire message. Returns a render_ack to send back, or None."""
        try:
            message = validate(json.loads(text))
        except (ValueError, ProtocolError) as error:
            self.messages_invalid += 1
            self.last_error = f"{type(error).__name__}: {error}"
            return None

        if not self.guard.accept(message):
            return None
        self.messages_accepted += 1
        self._arrivals += 1

        kind = message["type"]
        if kind == "subtitle_state":
            self.states_received += 1
            if self._waiting:
                self.states_superseded += 1
            # The instant it arrived, not a deadline: the delay is applied when
            # the render tick looks, so changing it moves everything already
            # waiting without touching a single stored number. It is counted
            # from arrival rather than from the producer's own timestamp because
            # the two machines keep their own clocks, and the network between
            # them costs a handful of milliseconds against a delay measured in
            # hundreds.
            self._waiting.append((time.monotonic(), self._arrivals, message,
                                  self._stamp() if self._stamp is not None else None))
            while len(self._waiting) > WAITING_LIMIT:
                self._waiting.popleft()
                self.dropped_overflow += 1
            self.queue_depth_max = max(self.queue_depth_max, len(self._waiting))
            self.pending_seq = message.get("seq")
        elif kind in ("clear_subtitles", "source_changed"):
            if len(self._control) == self._control.maxlen:
                self.controls_dropped += 1
            message["_arrival"] = self._arrivals
            self._control.append(message)
        elif kind == "hello":
            self.hello_seen += 1
            # A hello opens a stream. The producer may be a fresh process, back
            # at epoch 0 and sequence 1, which the guard would otherwise read as
            # stale for the rest of the session.
            self.guard.reset()
            self.session_id = str(message.get("session_id", ""))
            self._waiting.clear()
            self.upper_line = ""
            self.lower_line = ""
        elif kind == "heartbeat":
            self.heartbeats_seen += 1
        return None

    def set_publish_delay_ms(self, delay_ms: int) -> None:
        """Change the delay now, including for everything already waiting.

        Each waiting state keeps the instant it arrived, so a change moves them
        all at once and none of them starts over: raising the delay does not
        make a state that has nearly waited its turn begin again, and lowering
        it can make several due at the same moment, of which only the newest is
        painted. That is what pressing the key expects to see.
        """
        self.publish_delay_ms = max(0, int(delay_ms))

    def drop_pending(self, reason: str = "barrier") -> None:
        """Forget everything that is waiting, now."""
        del reason
        if self._waiting:
            self.invalidated_by_barrier += len(self._waiting)
            self._waiting.clear()

    def _due_in_ms(self) -> float | None:
        if not self._waiting:
            return None
        due_at = self._waiting[0][0] + self.publish_delay_ms / 1000.0
        return round(max(0.0, due_at - time.monotonic()) * 1000, 1)

    def scheduler_state(self) -> dict:
        return {"publish_delay_ms": self.publish_delay_ms,
                "pending": bool(self._waiting),
                "queue_depth": len(self._waiting),
                "queue_depth_max": self.queue_depth_max,
                "due_in_ms": self._due_in_ms(),
                "published": self.published,
                "coalesced_while_waiting": self.coalesced_while_waiting,
                "dropped_overflow": self.dropped_overflow,
                "invalidated_by_barrier": self.invalidated_by_barrier}

    # -- output ------------------------------------------------------------
    def take_render(self) -> tuple[str, str] | None:
        """What to paint now, or None if the screen is already correct.

        Controls are applied before the pending state, so a clear followed by a
        new state within the same tick ends with the new state rather than a
        blank screen.
        """
        changed = False
        while self._control:
            control = self._control.popleft()
            if control["type"] not in ("clear_subtitles", "source_changed"):
                continue
            # A new source must not leave the previous source's words on the
            # picture for even one frame — nor let a state that was waiting out
            # its delay arrive afterwards and put them back. Only what was
            # already waiting is discarded; a state sent after the barrier is
            # the new source's own, and is left to serve its delay.
            raised_at = control.get("_arrival", 0)
            keep = deque((entry for entry in self._waiting if entry[1] > raised_at))
            self.invalidated_by_barrier += len(self._waiting) - len(keep)
            self._waiting = keep
            self.upper_line = ""
            self.lower_line = ""
            changed = True

        # Everything whose time has come. Normally that is one state; it is more
        # when the screen fell behind or the delay was just lowered, and then
        # only the newest is painted — the ones behind it were superseded before
        # anybody could have read them.
        now = time.monotonic()
        delay = self.publish_delay_ms / 1000.0
        state = None
        arrived_at = None
        coalesced_here = 0
        arrived_stamp = None
        while self._waiting and self._waiting[0][0] + delay <= now:
            arrived_at, _, state, arrived_stamp = self._waiting.popleft()
            self.published += 1
            if self._waiting and self._waiting[0][0] + delay <= now:
                self.coalesced_while_waiting += 1
                coalesced_here += 1
        if state is not None:
            self.last_state_at = time.monotonic()
            # What the diagnostic log needs and nothing else does: when this
            # state arrived, and how many older ones were due at the same
            # moment. Recorded rather than derived, because the difference
            # between arrival and render is the whole question.
            self.last_painted = {"seq": state.get("seq"),
                                 "arrived_monotonic": arrived_at,
                                 "due_monotonic": (arrived_at + delay
                                                   if arrived_at is not None else None),
                                 "coalesced_with_it": coalesced_here,
                                 "stamp_at_arrival": arrived_stamp,
                                 "delay_ms_in_force": self.publish_delay_ms,
                                 "generated_monotonic_ms":
                                     state.get("generated_monotonic_ms")}
            upper = state.get("upper_line", "")
            lower = state.get("lower_line", "")
            if (upper, lower) != (self.upper_line, self.lower_line):
                self.upper_line = upper
                self.lower_line = lower
                changed = True
            self.last_rendered_seq = state.get("seq")

        if not changed:
            return None
        self.states_rendered += 1
        return self.upper_line, self.lower_line

    def ack_for(self, seq: int, rendered_monotonic_ms: float | None = None) -> str:
        return json.dumps(render_ack(
            seq=seq, source_epoch=self.guard.current_epoch,
            rendered_monotonic_ms=rendered_monotonic_ms,
        ))

    def counters(self) -> dict:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "upper_line": self.upper_line,
            "lower_line": self.lower_line,
            "messages_accepted": self.messages_accepted,
            "messages_invalid": self.messages_invalid,
            "states_received": self.states_received,
            "states_superseded": self.states_superseded,
            "states_rendered": self.states_rendered,
            "controls_dropped": self.controls_dropped,
            "hello_seen": self.hello_seen,
            "session_id": self.session_id,
            "streams_started": self.guard.streams_started,
            "heartbeats_seen": self.heartbeats_seen,
            "rejected_stale_epoch": self.guard.rejected_stale_epoch,
            "rejected_stale_seq": self.guard.rejected_stale_seq,
            "current_epoch": self.guard.current_epoch,
            "latest_seq": self.guard.latest_seq,
            "last_rendered_seq": self.last_rendered_seq,
            "last_error": self.last_error,
        }
