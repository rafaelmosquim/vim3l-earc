#!/usr/bin/env python3
"""Always-on eARC capture -> network streamer for the Khadas VIM3L.

Captures whatever the TV sends over eARC (hw:0,5), follows format changes
(stereo/multichannel LPCM at any rate, AC-3, E-AC-3) and serves ONE fixed,
continuous stream to TCP clients:

    raw PCM, s16le, 48000 Hz, 6 channels, order FL FR FC LFE SL SR (5.1(side))

Stereo sources land on FL/FR with the other channels silent. While the input is
switching or absent, silence is sent in real time so the stream never stalls.

PC side example:
    nc khadas 4010 | pw-cat --playback --format s16 --rate 48000 --channels 6 -
"""
import argparse
import array
import math
import os
import select
import socket
import subprocess
import sys
import threading
import time

DEV = "hw:0,5"
OUT_RATE, OUT_CH = 48000, 6
FRAME = 2 * OUT_CH
HERE = os.path.dirname(os.path.realpath(__file__))
EXTRACT = os.path.join(HERE, "iec61937-extract.py")
AREC_BUF = ["-B", "60000", "-F", "10000"]  # 60 ms buffer, 10 ms periods
# A bitstream is captured at 192 kHz (768 kB/s), four times the byte rate of
# 48 kHz LPCM, and an overrun there corrupts a whole compressed frame rather
# than costing a few inaudible samples: that is what produced ffmpeg's
# "exponent -2 is out-of-range / error decoding the audio block" (2026-10-02).
# Give the bitstream path a much deeper buffer.
# The depth is what prevents the overrun, so it stays at 250 ms; the PERIOD was
# raised to 25 ms at the same time, which costs ~15 ms of latency on the Dolby
# path for nothing. Back to 10 ms periods 2026-10-03, with the deep buffer kept:
# the original overruns date from the era when two USB peripherals were plugged
# into the board and the eARC link was flapping, and since they were unplugged
# there have been zero decoder errors across every E-AC-3 session. If
# "error decoding the audio block" ever comes back, this is the first suspect.
AREC_BUF_BITSTREAM = ["-B", "250000", "-F", "10000"]  # 250 ms buffer, 10 ms periods
FF_LOWLAT = ["-hide_banner", "-loglevel", "error", "-nostdin",
             "-fflags", "nobuffer", "-flags", "low_delay",
             "-probesize", "32", "-analyzeduration", "0"]
FF_OUT = ["-ar", str(OUT_RATE), "-c:a", "pcm_s16le", "-f", "s16le",
          "-flush_packets", "1", "pipe:1"]

# eARC_RX Audio Coding Type values (see `amixer -c0 cget name='eARC_RX Audio Coding Type'`)
LPCM2 = {1, 2}
LPCM8 = {3}
AC3 = {7, 8}
EAC3 = {9, 17}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def ctl(name):
    out = subprocess.run(["amixer", "-c0", "cget", f"name={name}"],
                         capture_output=True, text=True).stdout
    for line in out.splitlines():
        if ": values=" in line:
            v = line.split("=", 1)[1].split(",")[0]
            return int(v, 0)
    return 0


def stream_format():
    return ctl("eARC_RX Audio Coding Type"), ctl("eARC_RX Audio Sample Frequency")


ATTENDED = {0: "DISCONNECT", 1: "ARC", 2: "eARC"}
# Waits between attempts after a failed open. Opening the capture device toggles
# the eARC RX, and a TV that sees the receiver flapping gives up on eARC and
# falls back to ARC -- on this TV that means it sends nothing at all and the link
# cannot recover without a TV-side toggle (observed 2026-10-02, when reopening
# every 2 s produced 66 cycles in 4 minutes). So back off instead, and read the
# link state from the mixer, which does not touch the device.
# The last tier was 15 s, which on a TV that is simply switched OFF (the link
# then reads ARC and the format registers hold a stale 1/44100, so every attempt
# opens a pipeline that never receives a byte) meant one open/close every 17 s
# for as long as the TV stayed off -- 630 of them in three hours on 2026-10-03.
# Each one toggles the eARC RX, which is the thing this table exists to avoid,
# so let it settle further. Nothing is lost on a live link: idle resets to -1 on
# any real audio and on any link-state change, so a TV coming back is picked up
# at once; the only cost is up to 30 s before noticing audio that starts while
# the link is still reading ARC.
BACKOFF = (1.0, 2.0, 5.0, 10.0, 15.0, 30.0)
# A format change must persist this long before we act on it. While an E-AC-3
# bitstream is flowing these registers flap between readings (observed jumping
# 9/192k <-> 1/44.1k <-> 16/192k every few seconds on 2026-10-02), and since
# every restart toggles the eARC RX, acting on each reading walked the TV back
# down to ARC within 90 s.
# 3.0 s until 2026-10-03, which is most of the remaining silence on a format
# change. The flapping it was sized for belongs to the USB-dropout era: with the
# link stable, the whole of a day's journal holds exactly two transient readings
# and they were withdrawn after 0.3 s and 0.8 s. 1.5 s absorbs both with room to
# spare and halves the gap. FORMAT_DISAGREE_MAX below is still the backstop, and
# the "withdrawn" log line is the measurement -- if those start appearing often,
# or appear with ages above ~1.2 s, put this back up.
FORMAT_DEBOUNCE = 1.5
# A reading that disagrees with the running pipeline now MUTES the output (see
# the main loop), so it must not be able to mute indefinitely: if the registers
# oscillate between two valid values, no single one ever holds for
# FORMAT_DEBOUNCE and the old code would have waited forever. Past this much
# continuous disagreement, act on the most recent reading instead.
FORMAT_DISAGREE_MAX = 8.0
# This TV stops transmitting during silent passages. Closing the capture device
# when that happens toggles the eARC RX, and a couple of those make the TV fall
# back to ARC (observed 2026-10-02: 64 s of clean E-AC-3, a quiet gap, two
# reopens, then ARC). So while the link is still attended and the format still
# reads the same, hold the device open this long and keep sending silence.
NO_DATA_GRACE = 30.0


def attended():
    return ctl("eARC_RX attended type")


class Clients:
    """Fan-out of the output stream to any number of TCP clients."""

    def __init__(self, port):
        self.lock = threading.Lock()
        self.socks = []
        srv = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        srv.bind(("::", port))
        srv.listen(4)
        threading.Thread(target=self._accept, args=(srv,), daemon=True).start()
        log(f"listening on tcp port {port}")

    def _accept(self, srv):
        while True:
            s, addr = srv.accept()
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 64 * 1024)
            s.setblocking(False)
            with self.lock:
                self.socks.append(s)
            log("client connected", addr[0])

    SEND_DEADLINE = 1.0  # s a client may stay blocked before we give up on it

    def send(self, data):
        with self.lock:
            for s in list(self.socks):
                try:
                    self._send_all(s, data)
                except (OSError, TimeoutError) as e:
                    log("dropping client:", e)
                    s.close()
                    self.socks.remove(s)

    @classmethod
    def _send_all(cls, s, data):
        """Write all of data, waiting for the socket to drain as needed.

        The sockets are non-blocking, so any burst larger than SO_SNDBUF (64 kB,
        ~110 ms of audio) fails part-way through with EAGAIN. That is ordinary
        back-pressure, not a dead client -- treating it as one dropped the PC
        every couple of minutes, which it heard as a pop (observed 2026-10-02).
        Wait for writability instead, and drop only a client that stays blocked.
        """
        view = memoryview(data)
        deadline = time.monotonic() + cls.SEND_DEADLINE
        while view:
            try:
                view = view[s.send(view):]
            except BlockingIOError:
                left = deadline - time.monotonic()
                if left <= 0 or not select.select([], [s], [], left)[1]:
                    raise TimeoutError("client too slow, send blocked for "
                                       f"{cls.SEND_DEADLINE:.0f}s")


class Pipeline:
    """arecord [-> iec61937 extract] -> ffmpeg normalizing to 5.1/48k/s16."""

    def __init__(self, ctype, rate):
        self.procs = []
        if ctype in LPCM2 or ctype in LPCM8:
            ch = 2 if ctype in LPCM2 else 8
            rec = ["arecord", "-q", "-D", DEV, *AREC_BUF, "-f", "S16_LE", "-c", str(ch), "-r", str(rate), "-t", "raw"]
            if ch == 2:
                af = "pan=5.1(side)|FL=c0|FR=c1"
                inlay = "stereo"
            else:
                # Positional, because the input is 8 eARC slots carrying 5.1 (our CDS
                # advertises 5.1 speaker allocation), not a real 7.1 mix. The old
                # 7.1 fold `SL=0.7*SL+0.7*BL` lost one of its two terms to silence
                # and so handed the surrounds over 3.1 dB down -- measured, and
                # audible against the Dolby path, which is unity (2026-10-12).
                # The 0.707 on slots 7/8 is a no-op while they are silent and a
                # power-preserving fold if the TV ever does send 7.1.
                af = ("pan=5.1(side)|FL=c0|FR=c1|FC=c2|LFE=c3"
                      "|SL=c4+0.707*c6|SR=c5+0.707*c7")
                inlay = "7.1"
            ff = ["ffmpeg", *FF_LOWLAT, "-f", "s16le", "-ar", str(rate), "-ac", str(ch),
                  "-channel_layout", inlay, "-i", "pipe:0", "-af", af, *FF_OUT]
            self._chain([rec, ff])
        elif ctype in AC3 or ctype in EAC3:
            rec = ["arecord", "-q", "-D", DEV, *AREC_BUF_BITSTREAM, "-f", "S16_LE", "-c", "2",
                   "-r", str(max(rate, 192000) if ctype in EAC3 else rate), "-t", "raw"]
            # No `aresample=async=1` here: it pads the output to keep timestamps
            # when the decoder stumbles, and with a few corrupt frames per minute
            # that padding made the board emit ~18% MORE audio than real time,
            # which the PC then had to discard in audible lumps. A gap is better
            # than invented audio: the sender fills real gaps with silence itself.
            # probesize/analyzeduration are also raised from the LPCM path's
            # minimums, which are too small to parse a compressed stream reliably.
            ff = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                  "-fflags", "nobuffer+discardcorrupt", "-flags", "low_delay",
                  "-probesize", "65536", "-analyzeduration", "500000",
                  "-f", "eac3", "-i", "pipe:0",
                  "-af", "pan=5.1(side)|FL=FL|FR=FR|FC=FC|LFE=LFE|SL=SL|SR=SR",
                  *FF_OUT]
            self._chain([rec, ["python3", EXTRACT], ff])
        else:
            raise ValueError(f"unsupported coding type {ctype}")
        self.out = self.procs[-1].stdout
        os.set_blocking(self.out.fileno(), False)

    def _chain(self, cmds):
        prev = None
        for i, c in enumerate(cmds):
            p = subprocess.Popen(c, stdin=prev, stdout=subprocess.PIPE,
                                 stderr=None if i == len(cmds) - 1 else subprocess.DEVNULL)
            if prev is not None:
                prev.close()  # only the child holds it now
            prev = p.stdout
            self.procs.append(p)

    def read(self, timeout):
        r, _, _ = select.select([self.out], [], [], timeout)
        if not r:
            return b""
        try:
            return os.read(self.out.fileno(), 65536) or None  # None = EOF
        except BlockingIOError:
            return b""

    def stop(self):
        for p in self.procs:
            try:
                p.kill()  # arecord can hang in the driver; SIGKILL is the reliable stop
            except OSError:
                pass
        for p in self.procs:
            try:
                p.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass


# --- click-free boundaries -------------------------------------------------
# A format change makes several waveform discontinuities in a row: the mute
# when the registers disagree, the discard of the restart burst, and the resume
# afterwards.  Each hard step from a non-zero sample to zero (or back) is a
# click, and that run of steps is what was audible as "clicks and pops for a
# second or two" after every codec switch (2026-10-15).  Silence was never the
# problem; the edges either side of it were.
#
# The two cases need different treatment, and conflating them is a trap:
#   * A SPLICE has audio on both sides -- the burst discard throws away the
#     middle, and the audio that would have played is still in hand, so the
#     join can be crossfaded properly.
#   * A STARVE has nothing on the far side.  There is no audio left to fade
#     into, so anything emitted is invented: hold the last sample's level and
#     decay it.  That starts exactly where the waveform stopped, so there is no
#     step at all, and 10 ms of sub-100 Hz decay is inaudible next to the
#     full-scale edge it replaces.
FADE_MS = 10.0
FADE_FRAMES = max(1, int(OUT_RATE * FADE_MS / 1000.0))
# Shortest ramp that still kills a step rather than halving it: a one-frame
# "ramp" is just a smaller step. Unreachable as things stand -- silence_tick
# only runs once lag exceeds UNDERRUN, so it always has >= 5000 frames of
# gap to work with -- but the decay below must not be trusted to a stub.
FADE_MIN_FRAMES = max(1, OUT_RATE // 1000)  # 1 ms
# A decoder started mid-stream has no history to decode against. E-AC-3
# reuses exponent and mantissa state across frames, so the first frame or
# two after a cold start decode to garbage. That is audible as clicks when
# the TV switches INTO the bitstream path and only then: on 2026-10-15 a
# YouTube channel change from type 1 to type 9 clicked while type 9 to
# type 1 was clean, because raw LPCM has no decoder state to prime.
# ffmpeg's discardcorrupt does not help -- those frames are not corrupt,
# they are merely the first. Throw the settling window away instead. The
# gap on a format change is already seconds long, so this costs nothing.
SETTLE_MS = 96.0  # three 32 ms E-AC-3 frames
# The eARC format registers LAG THE WIRE. Measured 2026-10-15 on a YouTube
# channel change: the TV stopped sending for 2.0 s, resumed with an E-AC-3
# bitstream, and the registers went on reporting the old type 1 for a
# further 650 ms. Nothing yet knew the format had changed, so the gate
# stayed open and the LPCM pipeline forwarded the bitstream as raw PCM --
# full-scale noise, proven by Pa/Pb sync words 24576 bytes apart (the
# 6144-frame E-AC-3 repetition period) in the captured output, at a
# zero-crossing rate of 0.53 against 0.08 for real audio.
#
# FORMAT_DEBOUNCE cannot help: there is no disagreement to debounce yet.
# Re-reading the registers on resume cannot help either -- they are the
# thing that is late. What IS reliable is that the junk only ever follows a
# gap in the data, so a gap is treated as invalidating the format: hold
# silence long enough for the registers to catch up, and let the normal
# debounce take over if they do report a change.
#
# A content detector was tried first and rejected on measurement: the junk
# arrives in 13 ms bursts, shorter than any usable analysis window, so
# zero-crossing and level tests caught 4 of 34 chunks and fired 200 ms
# late. See tests/ and the 2026-10-15 capture.
GAP_RESYNC = 0.2     # s of no data that invalidates the format reading
GAP_HOLD_MS = 1000.0  # how long to stay silent afterwards (650 ms measured)
# The gap above is NOT reliable on its own: measured 01:00:43 on 2026-10-15,
# the wire switched to a bitstream with the data still flowing and no gap at
# all. What held on both occasions is the RATE. An IEC 61937 carrier runs at
# 192 kHz, four times the byte rate of the 48 kHz LPCM the pipeline expects,
# so the input races ahead of real time. A genuine live PCM source cannot:
# the most it can ever hand over at once is what sits in arecord's buffer,
# and AREC_BUF above is 60 ms deep. So a sustained lead past this limit is
# proof the wire is no longer PCM, available at once rather than 650 ms late.
# Sized against the worst legitimate burst: the pacer deliberately runs KEEP
# (40 ms) behind, and the largest lump arecord can ever hand over at once is
# its whole 60 ms buffer, which lands at only +20 ms of lead. 50 ms leaves
# 30 ms of margin on that and trips as early as the arithmetic allows: at
# four times real time the lead closes 0.09 s in 30 ms of wall clock, so
# roughly 120 ms of junk is forwarded before the latch -- against 650 ms
# when waiting for the registers. Lowering KEEP would shorten it further and
# is not worth the jitter margin.
PCM_AHEAD_LIMIT = 0.05  # s of lead on a PCM path that cannot be legitimate


def _ramp(n, up):
    """Raised-cosine gain ramp over n frames: 0 -> 1 if `up`, else 1 -> 0.

    Zero slope at both ends, so a join is smooth in the first derivative and
    not merely continuous; the rising and falling pair sum to 1, which is what
    makes them usable as a crossfade over correlated audio.
    """
    step = math.pi / n
    if up:
        return [0.5 - 0.5 * math.cos(step * (i + 0.5)) for i in range(n)]
    return [0.5 + 0.5 * math.cos(step * (i + 0.5)) for i in range(n)]


FADE_IN = _ramp(FADE_FRAMES, True)


def _scale(buf, gains):
    """s16le `buf` with its first len(gains) frames multiplied by `gains`."""
    n = min(len(gains), len(buf) // FRAME)
    if n <= 0:
        return buf
    s = array.array("h")
    s.frombytes(buf[:n * FRAME])
    for i in range(n):
        g, o = gains[i], i * OUT_CH
        for c in range(o, o + OUT_CH):
            s[c] = int(s[c] * g)
    return s.tobytes() + buf[n * FRAME:]


def _decay(frame, n):
    """n frames ramping the level of `frame`'s last frame down to silence."""
    s = array.array("h")
    s.frombytes(frame[-FRAME:])
    out = array.array("h", bytes(n * FRAME))
    for i, g in enumerate(_ramp(n, False)):
        o = i * OUT_CH
        for c in range(OUT_CH):
            out[o + c] = int(s[c] * g)
    return out.tobytes()


def _splice(before, head):
    """`head` with `before`'s opening frames crossfaded into its own.

    `before` is the audio that would have played next had nothing been cut, so
    its first sample continues what was last sent; `head` is the audio being
    jumped to.  Returns the same length as `head`.
    """
    n = min(FADE_FRAMES, len(before) // FRAME, len(head) // FRAME)
    if n <= 0:
        return head
    a = array.array("h")
    a.frombytes(before[:n * FRAME])
    b = array.array("h")
    b.frombytes(head[:n * FRAME])
    down, up = _ramp(n, False), _ramp(n, True)
    for i in range(n):
        o = i * OUT_CH
        for c in range(o, o + OUT_CH):
            b[c] = int(a[c] * down[i] + b[c] * up[i])
    return b.tobytes() + head[n * FRAME:]


class Output:
    """Keeps the outgoing stream continuous and at real-time rate.

    Real audio is forwarded as it arrives. A frame clock tracks how much audio
    real time says we should have sent; only when we fall more than UNDERRUN
    behind (input switching, TV silent) is silence inserted to catch up.

    A pipeline start is the one moment the clock cannot be trusted, so it gets
    its own short window -- see restarting().
    """
    AHEAD_LIMIT = 0.2  # s ahead of real time before we drop instead of forwarding
    UNDERRUN = 0.15  # s behind real time before filling with silence
    KEEP = 0.04      # s of lag left after filling, so real audio isn't pushed late
    MAX_LAG = 0.3    # beyond this, skip the backlog instead of sending it
    CHUNK = 0.032    # s of silence per write, to keep bursts small
    RESYNC = 1.5     # s after a pipeline start where excess is shed in one go

    def __init__(self, clients):
        self.clients = clients
        self.pending = b""
        self.t0 = time.monotonic()
        self.sent = 0  # frames
        self.filled = 0  # frames of inserted silence
        self.dropped = 0  # frames discarded for arriving faster than real time
        self.last_drop_log = 0.0
        self.resync_until = 0.0  # while in the future, shed without the cap
        self.muted = True   # True when the last thing sent was silence
        self.discard_until = 0.0  # while in the future, decoded audio is junk
        self.pcm_guard = False    # this pipeline expects PCM, so police the rate
        self.pcm_broken = False   # the rate proved the wire is not PCM
        self.last = b""     # final frame of real audio, to decay out of

    def _lag(self):
        return (time.monotonic() - self.t0) - self.sent / OUT_RATE

    def hold(self, seconds):
        """Discard and stay muted for `seconds`: the format is not trustworthy.

        Never shortens a hold already in progress. The discarded audio leaves
        the frame clock behind and silence_tick() fills the gap, so what goes
        out is silence rather than whatever could not be decoded.
        """
        self.discard_until = max(self.discard_until, time.monotonic() + seconds)
        self.muted = True

    def restarting(self, settle=0.0, pcm_guard=False):
        """Call when a new capture pipeline is about to start producing.

        A fresh pipeline hands over several seconds of already-captured audio
        in one burst -- measured at ~7.2 s on each of two format changes on
        2026-10-03. That excess genuinely has to go: forwarding it would plant
        those seconds as latency on every client. The problem was never the
        dropping, it was the RATE of dropping. audio()'s steady-state quarter
        cap can only shed ~25% of each buffer, so a 7 s burst bled out over
        ~16 s of part-real, part-skipped output, and that is what was audible
        as a glitch on every format change.

        So open a short window where the excess can go at once, and re-anchor
        the clock: the gap we just sat through was filled with silence that was
        already sent, not audio we still owe. The window is bounded, so a wrong
        clock still cannot run away with the stream the way it did on
        2026-10-02 -- the worst it can cost is RESYNC seconds.
        """
        now = time.monotonic()
        self.t0 = now - self.sent / OUT_RATE
        self.resync_until = now + self.RESYNC
        # `settle` discards the decoder's first output outright -- see SETTLE_MS.
        self.discard_until = now + settle
        self.pcm_guard = pcm_guard
        self.pcm_broken = False
        if settle:
            # Stay muted across it, so the first audio that does survive gets
            # the fade-in in audio() rather than stepping in from zero.
            self.muted = True

    def audio(self, data):
        if self.pcm_broken:
            # The wire stopped being PCM and this pipeline cannot decode it.
            # It will not become PCM again without a format change, and that
            # forces a restart, so stay silent until then rather than letting
            # another burst through each time a timed hold lapses.
            self.dropped += len(data) // FRAME
            self.pending = b""
            return
        if time.monotonic() < self.discard_until:
            # Decoder still priming: drop this outright. Not forwarding leaves
            # the frame clock behind, and silence_tick() then fills the gap with
            # silence -- which is exactly what should go out in place of junk.
            self.dropped += len(data) // FRAME
            self.pending = b""
            return
        data = self.pending + data
        cut = len(data) - len(data) % FRAME  # never split a frame
        self.pending = data[cut:]
        if cut:
            # A live source cannot legitimately produce faster than real time, so
            # anything beyond AHEAD_LIMIT is excess we must shed here, where the
            # clock is, rather than let it pile up as latency and get dumped in
            # audible lumps at the far end. In the steady state this should
            # barely ever fire: after the 2026-10-03 format change the link sat
            # on E-AC-3 for minutes with nothing dropped at all. (An earlier
            # comment here claimed the E-AC-3 path runs ~18% fast and cost the
            # PC ~11 s a minute. That predates the iec61937-extract rework and
            # no longer holds -- don't tune against it.)
            ahead = -self._lag()
            if (self.pcm_guard and ahead > PCM_AHEAD_LIMIT
                    and time.monotonic() >= self.resync_until):
                # See PCM_AHEAD_LIMIT: this much lead cannot come from a live
                # PCM source, so the wire has changed under us and what is
                # arriving is a bitstream. Forwarding it as PCM is full-scale
                # noise. Latch silent and wait for the format registers to
                # catch up and trigger the restart.
                #
                # This must be tested BEFORE the AHEAD_LIMIT block below, not
                # inside it: nesting it there meant it could not fire until the
                # lead had already passed 0.2 s, which let 440 ms of junk out
                # instead of 120 ms (measured, tests/test_rateguard.py).
                self.pcm_broken = True
                self.muted = True
                self.dropped += cut // FRAME
                self.pending = b""
                log(f"input {ahead:.2f}s ahead of real time on a PCM path:"
                    " the wire is not PCM any more, muting until restart")
                return
            if ahead > self.AHEAD_LIMIT:
                want = int((ahead - self.KEEP) * OUT_RATE) * FRAME
                if time.monotonic() < self.resync_until:
                    # Just after a pipeline start: take the whole startup burst
                    # at once. See restarting() for why the cap below is wrong
                    # here -- shedding a quarter at a time is what made this
                    # audible for ~16 s instead of being over inside a buffer.
                    skip = min(want, cut)
                else:
                    # Never shed more than a quarter of what arrives. A wrong
                    # clock may then degrade the sound, but it can no longer
                    # silence the stream -- which is exactly what happened when
                    # discards were also counted as sent below (2026-10-02):
                    # every drop made the stream look further ahead and the
                    # sender ate all of it.
                    skip = min(want, cut // 4 // FRAME * FRAME)
                if skip:
                    # The audio about to be thrown away opens where the audio
                    # already sent left off, so it is the correct "before" side
                    # for the splice. Grab it before dropping it.
                    before = data[:skip]
                    data = data[skip:]
                    cut -= skip
                    if cut:
                        data = _splice(before, data[:cut]) + data[cut:]
                    # Deliberately NOT added to self.sent: that counter is the
                    # audio we actually forwarded, and it is what _lag() weighs
                    # against elapsed time. Discards shrink the backlog only by
                    # letting real time catch up.
                    self.dropped += skip // FRAME
                    now = time.monotonic()
                    if now - self.last_drop_log > 10.0:
                        self.last_drop_log = now
                        log(f"source ahead of real time: dropped {self.dropped / OUT_RATE:.1f}s"
                            " in total (upstream is producing too fast)")
        if cut:
            out = data[:cut]
            if self.muted:
                # First real audio after a gap: ramp up from zero instead of
                # stepping straight to wherever the waveform happened to be.
                out = _scale(out, FADE_IN)
                self.muted = False
            self.clients.send(out)
            self.sent += cut // FRAME
            self.last = out[-FRAME:]

    def silence_tick(self):
        lag = self._lag()
        if lag <= self.UNDERRUN:
            return
        self.pending = b""
        if lag > self.MAX_LAG:
            # A long gap (input switching, TV silent, pipeline restart). Sending
            # the whole backlog at once overflows every client's socket buffer
            # and lands seconds of latency downstream, so skip it: that audio
            # never existed. Re-anchor the clock and carry on.
            self.t0 = time.monotonic() - self.sent / OUT_RATE - self.KEEP
            lag = self._lag()
        frames = int((lag - self.KEEP) * OUT_RATE)
        if not self.muted:
            self.muted = True
            # Decay into the head of the gap rather than stepping to zero. It
            # replaces silence that was going to be sent anyway, so it costs no
            # latency and does not move the clock.
            n = min(FADE_FRAMES, frames)
            if n >= FADE_MIN_FRAMES and len(self.last) >= FRAME:
                self.clients.send(_decay(self.last, n))
                self.sent += n
                self.filled += n
                frames -= n
        step = int(self.CHUNK * OUT_RATE)
        while frames > 0:
            n = min(frames, step)
            self.clients.send(bytes(n * FRAME))
            self.sent += n
            self.filled += n
            frames -= n


def probe():
    """Open the input briefly so the receiver reports the stream format."""
    p = subprocess.Popen(["arecord", "-q", "-D", DEV, "-f", "S16_LE", "-c", "2", "-r", "48000",
                          "-t", "raw", "/dev/null"], stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 1.5
        fmt = (0, 0)
        while time.monotonic() < deadline:
            time.sleep(0.15)
            fmt = stream_format()
            if fmt[0]:
                break
        return fmt
    finally:
        p.kill()
        p.wait()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=4010)
    args = ap.parse_args()

    out = Output(Clients(args.port))
    idle = -1   # consecutive failed attempts, for the back-off above
    att = None

    def wait_quietly(seconds):
        """Keep the outgoing stream alive without touching the capture device."""
        t_end = time.monotonic() + seconds
        while time.monotonic() < t_end:
            out.silence_tick()
            time.sleep(0.01)

    while True:
        out.silence_tick()
        now_att = attended()
        if now_att != att:
            log(f"link: {ATTENDED.get(now_att, now_att)}"
                + ("  (ARC carries stereo at best; re-toggle eARC on the TV for 5.1)"
                   if now_att == 1 else ""))
            att = now_att
            idle = -1  # a mode change deserves a prompt retry
        ctype, rate = probe() if att else (0, 0)
        if not ctype or not rate or not (ctype in LPCM2 | LPCM8 | AC3 | EAC3):
            idle = min(idle + 1, len(BACKOFF) - 1)
            wait_quietly(BACKOFF[idle])
            continue

        log(f"stream: coding type {ctype}, {rate} Hz, link {ATTENDED.get(att, att)}")
        pipe = Pipeline(ctype, rate)
        # Bitstream paths also discard the decoder's settling window; LPCM is a
        # passthrough with no decoder state, and measurably does not need it.
        out.restarting(SETTLE_MS / 1000.0 if ctype in AC3 | EAC3 else 0.0,
                       pcm_guard=ctype in LPCM2 | LPCM8)
        last_data = time.monotonic()
        last_check = 0.0
        pending = None
        pending_since = 0.0
        disagree_since = 0.0   # 0 = the registers agree with this pipeline
        last_hold_check = 0.0
        last_hold_log = 0.0
        try:
            while True:
                data = pipe.read(0.02)
                now = time.monotonic()
                if data is None:
                    log("pipeline ended")
                    break
                if data:
                    if (ctype in LPCM2 | LPCM8
                            and now - last_data > GAP_RESYNC):
                        # See GAP_HOLD_MS. Only the LPCM paths need this: the
                        # bitstream paths run through iec61937-extract, which
                        # emits recognised burst payloads only and so cannot
                        # forward a format it was not expecting.
                        out.hold(GAP_HOLD_MS / 1000.0)
                        log(f"data resumed after {now - last_data:.1f}s gap,"
                            f" holding {GAP_HOLD_MS:.0f}ms for the format to settle")
                    last_data = now
                    idle = -1  # real audio: the next failure starts fresh
                if data and pending is None:
                    out.audio(data)
                else:
                    # Either nothing arrived, or a different valid format is
                    # already standing at the receiver, which means what is
                    # arriving no longer matches the pipeline decoding it. On
                    # the LPCM path that is an IEC 61937 bitstream being
                    # forwarded as raw PCM -- loud static, not audio, and the
                    # whole of the audible glitch on a format change once the
                    # startup burst was dealt with. Hold silence until the
                    # debounce resolves rather than send that to the PC.
                    out.silence_tick()
                if now - last_data > 2.0 and now - last_hold_check > 1.0:
                    last_hold_check = now
                    quiet = now - last_data
                    # Only give up if the link or the format actually moved; a
                    # silent TV is not a reason to touch the device (see above).
                    if (quiet < NO_DATA_GRACE and attended() == 2
                            and stream_format() == (ctype, rate)):
                        if now - last_hold_log > 10.0:
                            last_hold_log = now
                            log(f"input quiet {quiet:.0f}s, link and format unchanged,"
                                " holding the device open")
                    else:
                        log(f"no data for {quiet:.0f}s, closing the input")
                        idle = min(idle + 1, len(BACKOFF) - 1)
                        break
                if now - last_check > 0.25:
                    last_check = now
                    fmt = stream_format()
                    if fmt == (ctype, rate) or fmt[0] not in LPCM2 | LPCM8 | AC3 | EAC3:
                        # Unchanged, or a transient/unsupported reading. While
                        # audio is arriving, what the decoder makes of the stream
                        # is the better authority than these registers.
                        if pending is not None:
                            # This is the number that decides whether
                            # FORMAT_DEBOUNCE can be shortened: a pending that
                            # never commits is exactly the transient the
                            # debounce exists to absorb, and it has just cost a
                            # muted gap. If these are rare, the debounce can
                            # come down; if they are common, it cannot, and the
                            # switch has to be detected from the data instead.
                            log(f"format reading {pending[0]}/{pending[1]} Hz withdrawn"
                                f" after {now - pending_since:.1f}s (output was muted)")
                        pending = None
                        disagree_since = 0.0
                    else:
                        if not disagree_since:
                            disagree_since = now
                        if fmt != pending:
                            pending, pending_since = fmt, now
                        if (now - pending_since >= FORMAT_DEBOUNCE
                                or now - disagree_since >= FORMAT_DISAGREE_MAX):
                            log(f"format changed to coding type {fmt[0]}, {fmt[1]} Hz"
                                f" (stable {now - pending_since:.1f}s,"
                                f" disagreeing {now - disagree_since:.1f}s)")
                            break
        finally:
            pipe.stop()
            if idle >= 0:
                wait_quietly(BACKOFF[idle])


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
