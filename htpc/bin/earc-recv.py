#!/usr/bin/env python3
"""Jitter-buffered receiver for the VIM3L eARC stream.

Reads raw interleaved PCM from a TCP socket and writes it to stdout, where
pw-cat is waiting. Why this exists instead of a plain `socat | pw-cat`:

  * The sender delivers in ~30 ms bursts, so a pipe fed directly from the
    socket is empty most of the time. pw-cat reads inside its realtime
    callback, so every empty read is an xrun -- a constant crackle
    (measured 219 xruns/minute on 2026-10-02).
  * The sender also stalls for ~300 ms and drops the connection every one to
    three minutes. Reconnecting the pipeline tears pw-cat down and silences
    audio for seconds; here pw-cat keeps running and only the socket
    reconnects, padded with silence.

A reader thread drains the socket as fast as it arrives (so the sender never
sees backpressure) into a byte buffer. The main thread writes whole frames to
stdout, which is paced by pw-cat, i.e. by the DAC clock. The buffer absorbs
jitter; if it runs dry we emit silence rather than stall, and if it grows past
the ceiling (sender running fast) we drop the oldest frames.

The sender's clock and the DAC's never match exactly, so drops are routine, not
just a recovery path: the buffer creeps up by the clock difference (~35 ppm
against the Fosi ZD3, ~160 ppm against the UMC404HD, i.e. ~10 ms a minute) and
the silence-debt payback below trims it back to the prefill level. A plain cut
is a waveform discontinuity, heard as a click, so every drop is crossfaded over
XFADE_MS from the audio before the cut into the audio after it, and the payback
waits until the buffer is DROP_HYST_MS over target so it fires every minute or
two (at UMC drift) rather than on every burst.
"""
import os
import socket
import sys
import threading
import time

try:
    import numpy as np
except ImportError:  # still works, but drops are plain cuts again
    np = None

HOST = os.environ.get("EARC_HOST", "192.168.15.100")
PORT = int(os.environ.get("EARC_PORT", "4010"))
RATE = int(os.environ.get("EARC_RATE", "48000"))
CHANNELS = int(os.environ.get("EARC_CHANNELS", "6"))
SAMPLE_BYTES = int(os.environ.get("EARC_SAMPLE_BYTES", "2"))
# Buffer before starting to play. Must exceed the sender's worst burst gap.
PREFILL_MS = float(os.environ.get("EARC_PREFILL_MS", "200"))
# Ceiling; above this the sender is ahead of the DAC and we drop down to prefill.
MAX_MS = float(os.environ.get("EARC_MAX_MS", "500"))
CONNECT_TIMEOUT = float(os.environ.get("EARC_CONNECT_TIMEOUT", "5"))
# No data for this long means the link is dead; drop it and reconnect.
STALL_TIMEOUT = float(os.environ.get("EARC_STALL_TIMEOUT", "5"))
RETRY_DELAY = float(os.environ.get("EARC_RETRY_DELAY", "0.1"))
# Back-off for a host that is not answering at all (board powered off), and how
# often to mention it. Retrying every 0.1 s there filled the journal with ~29k
# lines a day; a mid-stream reconnect still uses the fast RETRY_DELAY above.
DOWN_BACKOFF = (1.0, 2.0, 5.0, 10.0, 30.0)
DOWN_LOG_EVERY = 300.0
STATS_INTERVAL = float(os.environ.get("EARC_STATS_INTERVAL", "60"))
# Length of the crossfade across each drop, and how far over target the buffer
# may grow before the debt payback trims it (see the docstring).
XFADE_MS = float(os.environ.get("EARC_XFADE_MS", "20"))
DROP_HYST_MS = float(os.environ.get("EARC_DROP_HYST_MS", "15"))

FRAME = CHANNELS * SAMPLE_BYTES
BYTES_PER_SEC = RATE * FRAME
WRITE_FRAMES = 240  # 5 ms at 48 kHz
WRITE_BYTES = WRITE_FRAMES * FRAME
SILENCE = bytes(WRITE_BYTES)
XFADE_FRAMES = int(RATE * XFADE_MS / 1000.0)
XFADE_BYTES = XFADE_FRAMES * FRAME
if np is not None and SAMPLE_BYTES == 2 and XFADE_FRAMES > 0:
    # Raised-cosine pair summing to 1: right for the correlated audio either
    # side of a short cut, and click-free at both ends.
    _ramp = 0.5 - 0.5 * np.cos(np.pi * (np.arange(XFADE_FRAMES) + 0.5) / XFADE_FRAMES)
    FADE_IN = _ramp.astype(np.float32)[:, None]
    FADE_OUT = (1.0 - FADE_IN).astype(np.float32)
else:
    FADE_IN = FADE_OUT = None

# The crossfade above covers a SPLICE: a cut between two pieces of real audio,
# both in hand, so the join can be blended. A STARVE is the other kind of
# discontinuity -- real audio meeting silence, and silence meeting real audio
# when data returns -- and there the far side does not exist yet. Those were
# plain steps, and a codec change makes a run of them as the sender mutes,
# restarts its pipeline and re-anchors its clock: heard as clicks and pops for
# a second or two (2026-10-15). Silence was never the problem; its edges were.
#
# Going quiet, hold the last sample's level and decay it -- that starts exactly
# where the waveform stopped, so there is no step, and 5 ms of sub-100 Hz decay
# is inaudible next to the full-scale edge it replaces. Coming back, ramp up
# from zero. Replaying the previous 5 ms scaled down does NOT work: it restarts
# the waveform 5 ms earlier and steps to it (measured at 31725 on a -0.2 dBFS
# tone, i.e. no better than the hard cut it was meant to fix).
MUTE_FADE = WRITE_FRAMES  # 5 ms, exactly one write: no state machine needed
if np is not None and SAMPLE_BYTES == 2:
    _mramp = 0.5 - 0.5 * np.cos(np.pi * (np.arange(MUTE_FADE) + 0.5) / MUTE_FADE)
    MUTE_IN = _mramp.astype(np.float32)[:, None]
    MUTE_OUT = (1.0 - MUTE_IN).astype(np.float32)
else:
    MUTE_IN = MUTE_OUT = None


def _fade(buf, gains):
    """s16le `buf` with its first len(gains) frames scaled by `gains`."""
    if gains is None:
        return buf
    n = min(len(gains), len(buf) // FRAME)
    if n <= 0:
        return buf
    head = np.frombuffer(buf[:n * FRAME], dtype="<i2").reshape(-1, CHANNELS)
    return (head * gains[:n]).astype("<i2").tobytes() + buf[n * FRAME:]


def _decay(frame):
    """One write of audio ramping the level of `frame` down to silence."""
    last = np.frombuffer(frame[-FRAME:], dtype="<i2").reshape(1, CHANNELS)
    return (last * MUTE_OUT).astype("<i2").tobytes()


def ms(nbytes):
    return nbytes / BYTES_PER_SEC * 1000.0


def log(msg):
    print("%s earc-recv: %s" % (time.strftime("%H:%M:%S"), msg), file=sys.stderr, flush=True)


class Buffer:
    def __init__(self):
        self.lock = threading.Condition()
        self.data = bytearray()
        self.connected = False
        self.priming = True
        self.reconnects = 0
        self.underrun_bytes = 0
        self.dropped_bytes = 0
        self.debt = 0  # silence we inserted and owe back, see put()
        self.peak = 0
        self.muted = True  # True when the last thing handed out was silence
        self.last = b""    # final frame of real audio, to decay out of

    def drop(self, nbytes):
        """Remove nbytes (whole frames) from the head of the buffer, which is the
        audio due to play next, crossfading across the cut. Caller holds lock."""
        if nbytes <= 0:
            return
        self.dropped_bytes += nbytes
        if FADE_IN is None or len(self.data) < nbytes + XFADE_BYTES:
            del self.data[:nbytes]
            return
        # Slicing a bytearray copies it, so no buffer export pins self.data.
        before = np.frombuffer(self.data[:XFADE_BYTES], dtype="<i2").reshape(-1, CHANNELS)
        after = np.frombuffer(self.data[nbytes:nbytes + XFADE_BYTES], dtype="<i2").reshape(-1, CHANNELS)
        mixed = before * FADE_OUT + after * FADE_IN
        self.data[nbytes:nbytes + XFADE_BYTES] = (
            np.clip(np.rint(mixed), -32768, 32767).astype("<i2").tobytes())
        del self.data[:nbytes]

    def put(self, chunk):
        with self.lock:
            self.data += chunk
            if len(self.data) > self.peak:
                self.peak = len(self.data)
            target = int(PREFILL_MS / 1000.0 * BYTES_PER_SEC)
            # Pay back inserted silence. Every chunk of silence we hand pw-cat
            # while starved is audio time we spent without consuming the buffer,
            # so when data returns the buffer holds that much extra -- pure added
            # latency that nothing else removes (seen settling at ~480 ms after a
            # few link bounces). Give it back now, while the glitch that caused it
            # is still the dominant artefact, instead of carrying it for hours.
            hyst = int(DROP_HYST_MS / 1000.0 * BYTES_PER_SEC)
            if self.debt and len(self.data) > target + hyst:
                give = min(self.debt, len(self.data) - target) // FRAME * FRAME
                if give:
                    self.drop(give)
                    self.debt -= give
            ceiling = MAX_MS / 1000.0 * BYTES_PER_SEC
            if len(self.data) > ceiling:
                excess = (len(self.data) - target) // FRAME * FRAME
                self.drop(excess)
                self.debt = 0
            if self.priming and len(self.data) >= PREFILL_MS / 1000.0 * BYTES_PER_SEC:
                self.priming = False
            self.lock.notify_all()

    def take(self):
        """Return up to WRITE_BYTES of audio, or silence if we have nothing."""
        with self.lock:
            if self.priming:
                # Hand pw-cat silence immediately rather than waiting for the
                # buffer to fill: a write that blocks here starves the pipe and
                # pw-cat xruns, which is the very thing the buffer is for. The
                # pipe itself paces us, so this costs no CPU.
                self.underrun_bytes += WRITE_BYTES
                self.debt += WRITE_BYTES
                return self._mute()
            n = min(len(self.data) // FRAME * FRAME, WRITE_BYTES)
            if n == 0:
                self.underrun_bytes += WRITE_BYTES
                self.debt += WRITE_BYTES
                self.priming = True
                return self._mute()
            chunk = bytes(self.data[:n])
            del self.data[:n]
            if self.muted:
                # Data is back: ramp up from zero instead of stepping to it.
                chunk = _fade(chunk, MUTE_IN)
                self.muted = False
            self.last = chunk[-FRAME:]
            return chunk

    def _mute(self):
        """Silence, decayed out of the last real audio on the first call.

        Caller holds the lock. Later calls in the same gap are plain silence:
        the level has already reached zero, so there is nothing to decay.
        """
        if self.muted or MUTE_OUT is None or len(self.last) < FRAME:
            self.muted = True
            return SILENCE
        self.muted = True
        return _decay(self.last)


def reader(buf):
    down = -1          # consecutive failed connects, for DOWN_BACKOFF
    down_since = 0.0
    last_down_log = 0.0
    while True:
        try:
            sock = socket.create_connection((HOST, PORT), timeout=CONNECT_TIMEOUT)
        except OSError as exc:
            now = time.monotonic()
            if down < 0:
                down_since = now
                last_down_log = now
                log("connect to %s:%d failed (%s), retrying" % (HOST, PORT, exc))
            elif now - last_down_log >= DOWN_LOG_EVERY:
                last_down_log = now
                log("%s:%d still unreachable after %.0f min (%s)"
                    % (HOST, PORT, (now - down_since) / 60.0, exc))
            down = min(down + 1, len(DOWN_BACKOFF) - 1)
            time.sleep(DOWN_BACKOFF[down])
            continue
        if down >= 0:
            log("back up after %.0f s unreachable" % (time.monotonic() - down_since))
            down = -1
        sock.settimeout(STALL_TIMEOUT)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except OSError:
            pass
        log("connected to %s:%d" % (HOST, PORT))
        with buf.lock:
            buf.connected = True
        started = time.monotonic()
        received = 0
        try:
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    log("sender closed the connection after %.1fs (%.1fs of audio)"
                        % (time.monotonic() - started, received / BYTES_PER_SEC))
                    break
                received += len(chunk)
                buf.put(chunk)
        except socket.timeout:
            log("no data for %.1fs after %.1fs, reconnecting" % (STALL_TIMEOUT, time.monotonic() - started))
        except OSError as exc:
            log("read error after %.1fs: %s" % (time.monotonic() - started, exc))
        finally:
            sock.close()
            with buf.lock:
                buf.connected = False
                buf.reconnects += 1
                # Throw the buffer away and re-prime on reconnect. The
                # sender flushes ~120 ms of stale audio into every new
                # connection; keeping it adds latency on each reconnect and
                # eventually forces a lumpy, audible drop. It also guarantees
                # frame alignment, so channels can never rotate on resume.
                buf.data.clear()
                buf.priming = True
        time.sleep(RETRY_DELAY)


def main():
    buf = Buffer()
    threading.Thread(target=reader, args=(buf,), daemon=True).start()
    out = sys.stdout.buffer
    last_stats = time.monotonic()
    base = (0, 0, 0)
    while True:
        out.write(buf.take())
        out.flush()
        now = time.monotonic()
        if now - last_stats >= STATS_INTERVAL:
            with buf.lock:
                level, under, dropped, reconnects, peak = (
                    len(buf.data), buf.underrun_bytes, buf.dropped_bytes, buf.reconnects, buf.peak)
                buf.peak = level
            du, dd, dr = under - base[0], dropped - base[1], reconnects - base[2]
            base = (under, dropped, reconnects)
            log("buffer %.0fms (peak %.0fms) | last %.0fs: silence inserted %.0fms, "
                "dropped %.0fms, reconnects %d" % (ms(level), ms(peak), now - last_stats, ms(du), ms(dd), dr))
            last_stats = now


if __name__ == "__main__":
    try:
        main()
    except (BrokenPipeError, KeyboardInterrupt):
        pass
