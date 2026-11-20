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
the trim in put() takes it back to the prefill level. A plain cut is a waveform
discontinuity, heard as a click, so every drop is crossfaded over XFADE_MS from
the audio before the cut into the audio after it, and the trim waits until the
buffer is DROP_HYST_MS over target so it fires every minute or two (at UMC
drift) rather than on every burst.

ADAPTIVE RESAMPLING (2026-11-20, "fix 3"; EARC_RESAMPLE=1, the default). The
drop above still skips 15-20 ms of real audio every couple of minutes, and on a
held note or a film score that can be heard. Instead, take() now reads the
buffer through a variable-ratio windowed-sinc interpolator and plays it
continuously ~160 ppm faster than it arrives (0.016 %, far below hearing), with
the ratio steered by a slow PI loop on the buffer's MEAN level. Nothing is cut:
the steady drift never reaches the drop at all, and the level sits flat at
EARC_RS_TARGET_MS instead of saw-toothing up by DROP_HYST_MS between trims. A
crossfaded drop stays only as the catch-up for a sender lump after a stall
(EARC_RS_COARSE_MS over target), and MAX_MS stays as the last resort.
EARC_RESAMPLE=0 (or no numpy) restores the drop-only behaviour exactly.
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
# may grow before the trim in put() takes it back (see the docstring).
XFADE_MS = float(os.environ.get("EARC_XFADE_MS", "20"))
DROP_HYST_MS = float(os.environ.get("EARC_DROP_HYST_MS", "15"))
# Capacity of the pipe on stdout. take() never blocks -- it hands out audio or
# silence at once -- so whatever pipe sits between us and the consumer is kept
# permanently FULL, and its whole capacity is added latency. Linux defaults to
# 64 KiB, which at 6 ch s16 48 kHz is ~114 ms on top of PREFILL_MS (found
# 2026-11-15; nothing had measured it). 8192 B is ~14 ms; the kernel rounds up
# to a power-of-two number of pages and never goes below one page (4096 B,
# ~7 ms). 0 leaves the pipe alone.
PIPE_BYTES = int(os.environ.get("EARC_PIPE_BYTES", "8192"))
# Pace writes by what is actually IN the pipe instead of by its capacity. The
# capacity is counted in whole pages, so on a kernel with 16 KiB pages (the Pi 5)
# the smallest pipe is one 16 KiB slot: it fills with five 2880 B writes, and a
# write cannot go into a second page until the reader has drained ALL of the
# first. The writer then sat blocked for ~25 ms and took five chunks at once,
# emptying the jitter buffer in one gulp: 90-120 ms of inserted silence a minute
# at PREFILL 55 (measured 2026-11-15, network jitter only 5.8 ms). With this set,
# the loop waits until FIONREAD shows at most this much queued, so the pipe holds
# PIPE_FILL_MS + one write and never blocks. Give it more than one page of
# capacity (EARC_PIPE_BYTES >= 2 pages) so a write always has a slot.
# 0 = off: rely on the pipe's backpressure (fine with 4 KiB pages).
PIPE_FILL_MS = float(os.environ.get("EARC_PIPE_FILL_MS", "0"))

# Adaptive resampling, see the docstring. The target is the MEAN fill, not the
# post-burst peak the drop logic trims to: with drops the mean sat ~15 ms under
# PREFILL_MS (55 -> ~40 measured 2026-11-20) and the lows came from the valleys
# just after each trim. With a flat mean the low point is mean minus half the
# sender's burst swing.
RESAMPLE = os.environ.get("EARC_RESAMPLE", "1").strip().lower() not in ("0", "off", "no", "false")
RS_TARGET_MS = float(os.environ.get("EARC_RS_TARGET_MS", "45"))
# PI loop on the mean level. The plant is an integrator (1 ppm of ratio moves
# the level 0.001 ms per second), so KP sets the loop's crossover (~0.05 rad/s,
# a ~20 s time constant) and KI the integral zero a quarter of that below it.
# The level is low-passed first (RS_LEVEL_TAU) so the sender's 30-70 ms burst
# saw-tooth leaves only a few ppm of ripple on the ratio.
RS_KP = float(os.environ.get("EARC_RS_KP", "50"))        # ppm per ms of error
RS_KI = float(os.environ.get("EARC_RS_KI", "0.6"))       # ppm per ms per second
RS_LEVEL_TAU = float(os.environ.get("EARC_RS_LEVEL_TAU", "2"))
RS_MAX_PPM = float(os.environ.get("EARC_RS_MAX_PPM", "1000"))  # 1000 ppm = 1.7 cents
# A sender lump after a stall (hundreds of ms at once) would take minutes to
# play off at RS_MAX_PPM, so above target + COARSE the excess down to
# target + COARSE/2 is dropped, crossfaded, like before.
RS_COARSE_MS = float(os.environ.get("EARC_RS_COARSE_MS", "60"))
# After a re-seed, hold the integrator until the mean is within RS_SETTLE_MS of
# target or RS_SETTLE_S has passed (see _steer).
RS_SETTLE_MS = 5.0
RS_SETTLE_S = 20.0
# The integrator's value is saved here once per stats line and seeds the next
# start, so a restart begins at the learnt ~160 ppm instead of re-learning it.
RS_STATE_FILE = os.path.expanduser(os.environ.get("EARC_RS_STATE", "~/.local/state/earc/rs-ppm"))
# Interpolator: 2*RS_HALF taps, Kaiser-windowed sinc, coefficients tabulated at
# RS_PHASES fractional positions and linearly interpolated between them.
RS_HALF = int(os.environ.get("EARC_RS_HALF", "32"))
RS_PHASES = 256
RS_CUTOFF = 0.92   # of Nyquist: flat to ~20 kHz
RS_BETA = 8.6      # ~86 dB stopband

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



# ---------------------------------------------------------------------------
# Night mode: a Dolby-style dynamic range control (2026-11-15).
#
# What Dolby DRC does (DP569/DP570 manuals): the ENCODER measures the whole mix,
# weighted to ignore deep bass, and writes one gain per block that the decoder
# applies to ALL channels alike, so the front/surround balance never moves. The
# gain follows a fixed curve anchored at the dialogue level (dialnorm): quiet
# passages are boosted toward it, a null band around it is left alone, louder
# material is cut, gently at first and then at 20:1. Line mode keeps dialogue at
# its level; RF ("heavy") mode compresses harder.
#
# The TV re-encodes app audio to DD+ with dialnorm -31 ("no information") and
# near-zero gain words, so the stream's own DRC does nothing here (measured
# 2026-11-15: drc_scale 0 and 1 decode identically). This re-creates it after
# the decoder instead. What it keeps from Dolby: the profile curves, one gain for
# all six channels from a loudness measure of the whole mix (BS.1770 weights:
# L/R/C 1.0, surrounds 1.41, LFE out; K-weighting applied in the FFT domain), and
# lookahead -- the audio queued in this buffer is already here, so a cut can
# start before the peak plays, at no extra latency. Curves are relative to
# EARC_DRC_REF, the dialogue loudness this stream actually has (no real dialnorm
# to read); the stats line logs the measured loudness to set it by.
#
# Profiles (dB relative to REF; Dolby's tables are quoted for dialnorm -31):
#   film_standard  boost up to +6 (2:1 from -12), null 0..+5, 2:1 to +15, then 20:1
#   film_light     boost up to +6 (2:1 from -22 to -10), null -10..+10, 2:1 to +20, 20:1
#   music_light    boost up to +12 (2:1 below -10), null -10..+10, 2:1 above
#   night          NOT a Dolby table: harder, for late night. +9 boost (2:1 below
#                  0), null 0..+3, 3:1 to +10, then 20:1
# Select with EARC_DRC, or live by writing the profile name into EARC_DRC_FILE
# (checked once a second; a missing file means EARC_DRC). "off" bypasses it.
DRC_PROFILES = {
    #               boost_max boost_ratio null_lo null_hi early_hi early_ratio cut_ratio
    "film_standard": (6.0, 2.0, 0.0, 5.0, 15.0, 2.0, 20.0),
    "film_light":    (6.0, 2.0, -10.0, 10.0, 20.0, 2.0, 20.0),
    "music_light":   (12.0, 2.0, -10.0, 10.0, 10.0, 2.0, 2.0),
    "night":         (9.0, 2.0, 0.0, 3.0, 10.0, 3.0, 20.0),
}
DRC_DEFAULT = os.environ.get("EARC_DRC", "off").strip().lower()
DRC_FILE = os.path.expanduser(os.environ.get("EARC_DRC_FILE", "~/.config/earc/drc"))
DRC_REF = float(os.environ.get("EARC_DRC_REF", "-27"))
DRC_LOOKAHEAD_MS = float(os.environ.get("EARC_DRC_LOOKAHEAD_MS", "20"))
DRC_ATTACK_MS = float(os.environ.get("EARC_DRC_ATTACK_MS", "10"))
DRC_RELEASE_MS = float(os.environ.get("EARC_DRC_RELEASE_MS", "1500"))
DRC_SLOW_MS = float(os.environ.get("EARC_DRC_SLOW_MS", "400"))
# BS.1770 channel weights in this stream's order, FL FR FC LFE SL SR.
DRC_CH_WEIGHTS = (1.0, 1.0, 1.0, 0.0, 1.41, 1.41)


def drc_curve(level, prof):
    """Gain in dB the profile asks for at `level` dB relative to the reference."""
    boost_max, boost_ratio, null_lo, null_hi, early_hi, early_ratio, cut_ratio = prof
    if level < null_lo:
        return min(boost_max, (null_lo - level) * (1.0 - 1.0 / boost_ratio))
    if level <= null_hi:
        return 0.0
    if level <= early_hi:
        return -(level - null_hi) * (1.0 - 1.0 / early_ratio)
    return (-(early_hi - null_hi) * (1.0 - 1.0 / early_ratio)
            - (level - early_hi) * (1.0 - 1.0 / cut_ratio))


def _k_weight_power(n):
    """|H(f)|^2 of the BS.1770 K-weighting (48 kHz biquads) at the rfft bins of an
    n-sample block, with the Parseval factors folded in."""
    w = np.pi * np.arange(n // 2 + 1) / (n / 2.0)
    z1, z2 = np.exp(-1j * w), np.exp(-2j * w)
    shelf = ((1.53512485958697 - 2.69169618940638 * z1 + 1.19839281085285 * z2)
             / (1.0 - 1.69065929318241 * z1 + 0.73248077421585 * z2))
    hpf = (1.0 - 2.0 * z1 + z2) / (1.0 - 1.99004745483398 * z1 + 0.99007225036621 * z2)
    p = np.abs(shelf * hpf) ** 2
    p[1:(n + 1) // 2] *= 2.0  # one-sided spectrum: count the mirrored bins
    return (p / float(n * n)).astype(np.float32)


class Drc:
    BLOCK = 240  # = WRITE_FRAMES, 5 ms

    def __init__(self):
        self.ok = np is not None and SAMPLE_BYTES == 2 and CHANNELS == 6
        self.profile = None
        self.name = "off"
        self.gain_db = 0.0
        self.slow_p = None
        self.reset_stats()
        if not self.ok:
            return
        self.kw = _k_weight_power(self.BLOCK)[None, :, None]
        self.chw = np.array(DRC_CH_WEIGHTS, dtype=np.float32)
        blk_s = self.BLOCK / float(RATE)
        self.a_att = 1.0 - np.exp(-blk_s / (DRC_ATTACK_MS / 1000.0))
        self.a_rel = 1.0 - np.exp(-blk_s / (DRC_RELEASE_MS / 1000.0))
        self.a_slow = 1.0 - np.exp(-blk_s / (DRC_SLOW_MS / 1000.0))
        self.look_bytes = int(DRC_LOOKAHEAD_MS / 1000.0 * RATE) // self.BLOCK * self.BLOCK * FRAME
        self.select(DRC_DEFAULT)

    def select(self, name):
        name = (name or "off").strip().lower()
        if name == self.name:
            return
        if name != "off" and name not in DRC_PROFILES:
            log("drc: unknown profile %r, keeping %s" % (name, self.name))
            return
        if name != "off" and not self.ok:
            log("drc: needs numpy and 6ch s16; staying off")
            return
        self.name = name
        self.profile = DRC_PROFILES.get(name)
        log("drc: %s (ref %.1f LKFS)" % (name, DRC_REF))

    def reset_stats(self):
        self.st_n = 0
        self.st_gsum = 0.0
        self.st_gmin = None
        self.st_gmax = None
        self.st_psum = 0.0

    def _powers(self, frames):
        """K-weighted, channel-weighted mean-square power per 240-frame block."""
        nb = len(frames) // self.BLOCK
        if nb == 0:
            return np.zeros(0, dtype=np.float32)
        x = frames[:nb * self.BLOCK].reshape(nb, self.BLOCK, CHANNELS).astype(np.float32) / 32768.0
        spec = np.fft.rfft(x, axis=1)
        p = (spec.real ** 2 + spec.imag ** 2) * self.kw   # (nb, bins, ch)
        return p.sum(axis=1) @ self.chw                     # (nb,)

    def process(self, chunk, ahead):
        """Apply the gain to `chunk` (s16le bytes); `ahead` is the audio queued
        after it (lookahead). Returns new bytes."""
        if self.profile is None:
            if self.gain_db == 0.0:
                return chunk
            target = 0.0  # switched off: glide back to unity, then bypass
            cur_p = None
        else:
            cur = np.frombuffer(chunk, dtype="<i2").reshape(-1, CHANNELS)
            # The queue can end mid-frame (the socket delivers arbitrary byte
            # counts), so only whole frames go into the lookahead.
            nlook = min(self.look_bytes, len(ahead) // FRAME * FRAME)
            look = np.frombuffer(bytes(ahead[:nlook]), dtype="<i2").reshape(-1, CHANNELS)
            pc = self._powers(cur)
            pl = self._powers(look)
            if len(pc):
                cur_p = float(pc.mean())
            else:  # short chunk: unweighted fallback
                cur_p = float(((cur.astype(np.float32) / 32768.0) ** 2 @ self.chw).mean())
            self.slow_p = cur_p if self.slow_p is None else (
                self.slow_p + self.a_slow * (cur_p - self.slow_p))
            allp = np.concatenate([[cur_p], pl]) if len(pl) else np.array([cur_p])
            if len(allp) > 1:  # ~10 ms detector: mean of neighbouring blocks
                allp = 0.5 * (allp[1:] + allp[:-1])
            fast_db = -0.691 + 10.0 * np.log10(max(float(allp.max()), 1e-12)) - DRC_REF
            slow_db = -0.691 + 10.0 * np.log10(max(self.slow_p, 1e-12)) - DRC_REF
            # Boost follows the slow level, so single words in a quiet scene do
            # not pump it. The fast (lookahead) level only caps it: where the
            # curve cuts, never exceed that cut; where it boosts, never push the
            # fast level past the top of the null band.
            g_fast = drc_curve(fast_db, self.profile)
            cap = g_fast if g_fast <= 0.0 else max(g_fast, self.profile[3] - fast_db)
            target = min(drc_curve(slow_db, self.profile), cap)
        prev = self.gain_db
        a = self.a_att if target < prev else self.a_rel
        new = prev + a * (target - prev)
        if self.profile is None and abs(new) < 0.05:
            new = 0.0
        self.gain_db = new
        if cur_p is not None:
            self.st_n += 1
            self.st_gsum += new
            self.st_psum += cur_p
            self.st_gmin = new if self.st_gmin is None else min(self.st_gmin, new)
            self.st_gmax = new if self.st_gmax is None else max(self.st_gmax, new)
        if prev == 0.0 and new == 0.0:
            return chunk
        cur = np.frombuffer(chunk, dtype="<i2").reshape(-1, CHANNELS)
        g0, g1 = 10.0 ** (prev / 20.0), 10.0 ** (new / 20.0)
        ramp = np.linspace(g0, g1, len(cur), endpoint=False, dtype=np.float32)[:, None]
        out = np.clip(np.rint(cur * ramp), -32768, 32767)
        return out.astype("<i2").tobytes()

    def stats(self):
        """One-line summary since the last call, or None while off and idle."""
        if self.st_n == 0:
            s = None if self.name == "off" else "drc %s idle" % self.name
        else:
            loud = -0.691 + 10.0 * np.log10(max(self.st_psum / self.st_n, 1e-12))
            s = ("drc %s: gain %+.1f/%+.1f/%+.1f dB (min/avg/max), loudness %.1f LKFS (ref %.1f)"
                 % (self.name, self.st_gmin, self.st_gsum / self.st_n, self.st_gmax, loud, DRC_REF))
        self.reset_stats()
        return s

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


class Resampler:
    """Variable-ratio windowed-sinc interpolation off the head of Buffer.data.

    Output frame k is the input band-limited-interpolated at position
    p_k = frac + k * step, counted in frames from the head of the buffer, using
    input frames floor(p_k) - HALF + 1 .. floor(p_k) + HALF. The HALF - 1 frames
    before the head that this needs are kept in `hist`, so successive calls
    join seamlessly: the output is the same however the input was chunked. The
    cost is HALF frames (0.7 ms) of lookahead that must be in the buffer.
    """

    def __init__(self, half=RS_HALF, phases=RS_PHASES, cutoff=RS_CUTOFF, beta=RS_BETA):
        self.half = half
        self.taps = 2 * half
        self.phases = phases
        frac = np.arange(phases + 1, dtype=np.float64) / phases
        off = np.arange(self.taps, dtype=np.float64) - (half - 1)
        x = off[None, :] - frac[:, None]            # tap distance from p, in frames
        win = np.i0(beta * np.sqrt(np.clip(1.0 - (x / half) ** 2, 0.0, None))) / np.i0(beta)
        h = cutoff * np.sinc(cutoff * x) * win
        h /= h.sum(axis=1, keepdims=True)           # exact unity gain at DC
        self.table = h.astype(np.float32)           # (phases + 1, taps)
        self.reset()

    def reset(self):
        """Forget the past (after a gap, which fades back in from zero anyway)."""
        self.hist = np.zeros((self.half - 1, CHANNELS), dtype=np.float32)
        self.frac = 0.0

    def run(self, data, step, nmax):
        """Interpolate up to nmax frames from `data` (s16le bytearray) at `step`
        input frames per output frame. Returns (float32 (n, CHANNELS), frames of
        `data` consumed), or (None, 0) when the buffer cannot cover one frame."""
        half = self.half
        avail = len(data) // FRAME
        room = avail - 1 - half - self.frac   # last p whose taps are all present
        if room < 0:
            return None, 0
        n = min(int(room / step) + 1, nmax)
        p = self.frac + step * np.arange(n + 1, dtype=np.float64)
        need = int(p[n - 1]) + half + 1
        # bytes() copies, so no numpy view pins the bytearray (it is resized).
        new = np.frombuffer(bytes(data[:need * FRAME]), dtype="<i2").reshape(need, CHANNELS)
        X = np.concatenate([self.hist, new.astype(np.float32)])   # X[j] = input j-(half-1)
        i = p[:n].astype(np.int64)
        ph = (p[:n] - i) * self.phases
        j0 = np.minimum(ph.astype(np.int64), self.phases - 1)
        w = (ph - j0).astype(np.float32)[:, None]
        coef = self.table[j0] * (1.0 - w) + self.table[j0 + 1] * w              # (n, taps)
        # Output k reads X[i_k .. i_k + taps - 1]. With the step this close to
        # 1, i_k - k is constant except where the position slips a whole frame,
        # at most once per call, so the windows of each run are one zero-copy
        # strided view over X. (A gather of all n windows plus a batched matmul
        # was 3x slower on the Pi 5: 720 vs 206 us per 5 ms.)
        d0 = int(i[0])
        if int(i[n - 1]) - (n - 1) == d0:       # no slip: the usual case
            runs = ((0, n, d0),)
        else:   # a 5 ms write has at most one; a long call (tests) many
            cuts = [0] + list(np.flatnonzero(np.diff(i - np.arange(n))) + 1) + [n]
            runs = [(a, b, int(i[a])) for a, b in zip(cuts[:-1], cuts[1:])]
        out = np.empty((n, CHANNELS), dtype=np.float32)
        s0, s1 = X.strides
        for s, e, base in runs:
            # win[k, c, t] = X[base + k + t, c], built directly: the numpy
            # helper for this costs ~60 us of Python per call on the Pi 5.
            win = np.ndarray((e - s, CHANNELS, self.taps), dtype=np.float32, buffer=X,
                             offset=base * s0, strides=(s0, s1, s0))
            out[s:e] = np.einsum("nct,nt->nc", win, coef[s:e])                 # (run, ch)
        head = int(p[n])
        self.frac = float(p[n] - head)
        self.hist = X[head:head + half - 1].copy()   # input head-half+1 .. head-1
        return out, head


def load_ppm():
    try:
        with open(RS_STATE_FILE) as f:
            v = float(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0
    return max(-RS_MAX_PPM, min(RS_MAX_PPM, v))


def save_ppm(v):
    try:
        os.makedirs(os.path.dirname(RS_STATE_FILE), exist_ok=True)
        tmp = RS_STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            f.write("%.2f\n" % v)
        os.replace(tmp, RS_STATE_FILE)
    except OSError:
        pass


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
        # Lowest level seen right after a take() since the last stats line: the
        # real safety margin, i.e. how much of PREFILL_MS is never touched.
        # None until the first take() of real audio in the interval.
        self.low = None
        self.muted = True  # True when the last thing handed out was silence
        self.last = b""    # final frame of real audio, to decay out of
        self.drc = Drc()
        # Adaptive resampling state (None = drop-only mode).
        self.rs = Resampler() if (RESAMPLE and np is not None and SAMPLE_BYTES == 2) else None
        self.rs_int = load_ppm() if self.rs is not None else 0.0  # integrator, ppm
        self.rs_ppm = self.rs_int                                 # ratio applied, ppm
        self.level_f = None    # low-passed fill in ms; None = re-seed on next take
        self.rs_hold = 0.0     # seconds left with the integrator held, see _steer
        self.rs_in = 0         # frames consumed / produced since the last stats
        self.rs_out = 0        #   line, for the ratio actually realised
        self.rs_lo = None
        self.rs_hi = None
        self.lvl_sum = 0.0     # for the mean fill in the stats line
        self.lvl_n = 0

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
            # Trim the buffer back to target whenever it is hyst over it. This
            # is the one rule that keeps latency fixed, and it answers two
            # things that both push the buffer up and that nothing else takes
            # back down:
            #
            #   * Inserted silence. Every chunk of silence we hand the sink
            #     while starved is audio time we spent without consuming the
            #     buffer, so when data returns the buffer holds that much extra
            #     (seen settling at ~480 ms after a few link bounces). Giving it
            #     back now means the glitch that caused it is still the dominant
            #     artefact, instead of carrying the latency for hours.
            #   * Clock drift, which is the steady one: ~160 ppm against the
            #     UMC404HD, ~10 ms a minute, forever. This used to be gated on
            #     self.debt, so the trim only ran while there was inserted
            #     silence left to repay -- a finite pot. On 2026-10-24 the pot
            #     ran out 2h50m in and the buffer then climbed 73 -> 401 ms in
            #     35 minutes, pure added lip sync lag, until the MAX_MS ceiling
            #     below would have cut it all back in one audible jump.
            #
            # debt is still tracked so a drop that happens to coincide with a
            # repayment is not counted twice.
            hyst = int(DROP_HYST_MS / 1000.0 * BYTES_PER_SEC)
            if self.rs is not None:
                # Drift is the resampler's job now; only a lump far above the
                # mean (the sender catching up after a stall) is cut.
                hi = int((RS_TARGET_MS + RS_COARSE_MS) / 1000.0 * BYTES_PER_SEC)
                if len(self.data) > hi:
                    keep = int((RS_TARGET_MS + RS_COARSE_MS / 2.0) / 1000.0 * BYTES_PER_SEC)
                    give = (len(self.data) - keep) // FRAME * FRAME
                    if give:
                        self.drop(give)
                        self.debt = max(0, self.debt - give)
                        self.level_f = None
            elif len(self.data) > target + hyst:
                give = (len(self.data) - target) // FRAME * FRAME
                if give:
                    self.drop(give)
                    self.debt = max(0, self.debt - give)
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
            if self.rs is not None:
                out, used = self.rs.run(self.data, 1.0 + self.rs_ppm * 1e-6, WRITE_FRAMES)
                n = 0 if out is None else len(out) * FRAME
            else:
                n = min(len(self.data) // FRAME * FRAME, WRITE_BYTES)
            if n == 0:
                self.underrun_bytes += WRITE_BYTES
                self.debt += WRITE_BYTES
                self.priming = True
                self.low = 0
                if self.rs is not None:
                    self.rs.reset()
                    self.level_f = None
                return self._mute()
            if self.rs is not None:
                del self.data[:used * FRAME]
                self.rs_in += used
                self.rs_out += len(out)
                chunk = np.clip(np.rint(out), -32768, 32767).astype("<i2").tobytes()
                self._steer(len(out))
            else:
                chunk = bytes(self.data[:n])
                del self.data[:n]
            self.lvl_sum += len(self.data)
            self.lvl_n += 1
            if self.low is None or len(self.data) < self.low:
                self.low = len(self.data)
            try:
                chunk = self.drc.process(chunk, self.data)
            except Exception as exc:  # never let night mode take the audio down
                log("drc: disabled after error: %r" % (exc,))
                self.drc.profile, self.drc.name, self.drc.gain_db = None, "error", 0.0
            if self.muted:
                # Data is back: ramp up from zero instead of stepping to it.
                chunk = _fade(chunk, MUTE_IN)
                self.muted = False
            self.last = chunk[-FRAME:]
            return chunk

    def _steer(self, frames):
        """One step of the PI loop that sets the resampling ratio. Caller holds
        the lock; `frames` is how much audio the last take() played."""
        level = len(self.data) / BYTES_PER_SEC * 1000.0
        dt = frames / float(RATE)
        if self.level_f is None:
            # Re-seeded after a prime, a reconnect or a cut. The level then
            # starts well off target for reasons that are not drift (a fresh
            # PREFILL_MS, or the rest of a lump), so the integrator -- which is
            # the learnt drift -- is held until the proportional term has
            # brought the level back near target, or RS_SETTLE_S has passed.
            self.level_f = level
            self.rs_hold = RS_SETTLE_S
        else:
            self.level_f += (level - self.level_f) * min(1.0, dt / RS_LEVEL_TAU)
        err = self.level_f - RS_TARGET_MS
        if self.rs_hold > 0.0:
            self.rs_hold = 0.0 if abs(err) < RS_SETTLE_MS else self.rs_hold - dt
        else:
            self.rs_int = max(-RS_MAX_PPM, min(RS_MAX_PPM, self.rs_int + RS_KI * err * dt))
        self.rs_ppm = max(-RS_MAX_PPM, min(RS_MAX_PPM, self.rs_int + RS_KP * err))
        if self.rs_lo is None or self.rs_ppm < self.rs_lo:
            self.rs_lo = self.rs_ppm
        if self.rs_hi is None or self.rs_ppm > self.rs_hi:
            self.rs_hi = self.rs_ppm

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
                if buf.rs is not None:
                    buf.rs.reset()
                    buf.level_f = None
        time.sleep(RETRY_DELAY)


def shrink_stdout_pipe():
    """Cap the stdout pipe at PIPE_BYTES; see the comment where it is set."""
    if PIPE_BYTES <= 0:
        return
    try:
        import fcntl
        setsz = getattr(fcntl, "F_SETPIPE_SZ", 1031)
        getsz = getattr(fcntl, "F_GETPIPE_SZ", 1032)
        fd = sys.stdout.fileno()
        old = fcntl.fcntl(fd, getsz)
        new = fcntl.fcntl(fd, setsz, PIPE_BYTES)
        log("stdout pipe %d -> %d bytes (%.1f ms -> %.1f ms)"
            % (old, new, ms(old), ms(new)))
    except OSError as exc:  # not a pipe (tty, file), or the kernel refused
        log("stdout pipe left as is: %s" % exc)


def pipe_pacer():
    """Return a wait() that holds the writer until the stdout pipe is down to
    PIPE_FILL_MS, or None when pacing is off or stdout is not a pipe."""
    if PIPE_FILL_MS <= 0:
        return None
    import array
    import fcntl
    import termios
    fd = sys.stdout.fileno()
    limit = int(PIPE_FILL_MS / 1000.0 * BYTES_PER_SEC)
    queued = array.array("i", [0])
    try:
        fcntl.ioctl(fd, termios.FIONREAD, queued, True)
    except OSError as exc:
        log("pipe pacing off: %s" % exc)
        return None
    log("pipe pacing at %.1f ms (%d bytes)" % (PIPE_FILL_MS, limit))

    def wait():
        while True:
            fcntl.ioctl(fd, termios.FIONREAD, queued, True)
            excess = queued[0] - limit
            if excess <= 0:
                return
            # Sleep about as long as the consumer needs to eat the excess.
            time.sleep(max(0.0005, excess / BYTES_PER_SEC))
    return wait


def main():
    shrink_stdout_pipe()
    pace = pipe_pacer()
    buf = Buffer()
    if buf.rs is not None:
        log("drift: adaptive resampling, mean target %.0f ms, %d taps, start %+.0f ppm"
            % (RS_TARGET_MS, buf.rs.taps, buf.rs_int))
    else:
        log("drift: crossfaded drops (target %.0f ms + %.0f ms hysteresis)"
            % (PREFILL_MS, DROP_HYST_MS))
    threading.Thread(target=reader, args=(buf,), daemon=True).start()
    out = sys.stdout.buffer
    last_stats = time.monotonic()
    last_drc_check = 0.0
    base = (0, 0, 0)
    while True:
        if pace is not None:
            pace()
        out.write(buf.take())
        out.flush()
        now = time.monotonic()
        if now - last_drc_check >= 1.0:
            last_drc_check = now
            try:
                with open(DRC_FILE) as f:
                    want = (f.read().split() or ["off"])[0]
            except OSError:
                want = DRC_DEFAULT
            with buf.lock:
                buf.drc.select(want)
        if now - last_stats >= STATS_INTERVAL:
            with buf.lock:
                level, under, dropped, reconnects, peak, low = (
                    len(buf.data), buf.underrun_bytes, buf.dropped_bytes, buf.reconnects, buf.peak,
                    buf.low)
                buf.peak = level
                buf.low = None
                mean = buf.lvl_sum / buf.lvl_n if buf.lvl_n else None
                buf.lvl_sum, buf.lvl_n = 0.0, 0
                rs_line = None
                if buf.rs is not None:
                    if buf.rs_out:
                        realised = (buf.rs_in / float(buf.rs_out) - 1.0) * 1e6
                        rs_line = "resample %+.0f ppm (%+.0f..%+.0f)" % (
                            realised, buf.rs_lo, buf.rs_hi)
                        save_ppm(buf.rs_int)
                    else:
                        rs_line = "resample idle"
                    buf.rs_in = buf.rs_out = 0
                    buf.rs_lo = buf.rs_hi = None
            du, dd, dr = under - base[0], dropped - base[1], reconnects - base[2]
            base = (under, dropped, reconnects)
            low_s = "-" if low is None else "%.0fms" % ms(low)
            mean_s = "" if mean is None else ", mean %.0fms" % ms(mean)
            log("buffer %.0fms (low %s, peak %.0fms%s) | last %.0fs: silence inserted %.0fms, "
                "dropped %.0fms, reconnects %d%s"
                % (ms(level), low_s, ms(peak), mean_s, now - last_stats, ms(du), ms(dd), dr,
                   "" if rs_line is None else " | " + rs_line))
            with buf.lock:
                drc_line = buf.drc.stats()
            if drc_line:
                log(drc_line)
            last_stats = now


if __name__ == "__main__":
    try:
        main()
    except (BrokenPipeError, KeyboardInterrupt):
        pass
