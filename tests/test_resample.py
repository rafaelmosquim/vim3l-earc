"""Adaptive resampling in earc-recv.py ("fix 3", 2026-11-20).

Self-contained: loads htpc/bin/earc-recv.py from this checkout, needs numpy.
    python3 -m unittest tests/test_resample.py -v

What it checks:
  * the interpolator against an exactly resampled sine (quality per frequency);
  * the output does not depend on how the input was chunked (no seams);
  * the PI loop in a virtual-time model of the live chain -- a bursty sender
    with clock drift, a consumer paced like the pipe pacer -- holds the mean
    fill at target with no drops and no inserted silence, and learns the drift;
  * a sender stall + catch-up lump is cut once and the level recovers;
  * EARC_RESAMPLE=0 still runs the old drop logic.
"""
import importlib.util
import os
import random
import tempfile
import unittest

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "htpc", "bin", "earc-recv.py")
STATE_DIR = tempfile.mkdtemp(prefix="earc-rs-test-")


def load(**env):
    base = {"EARC_PREFILL_MS": "55", "EARC_DRC": "off",
            "EARC_DRC_FILE": os.path.join(STATE_DIR, "no-drc"),
            "EARC_RS_STATE": os.path.join(STATE_DIR, "rs-ppm-%d" % random.getrandbits(32))}
    base.update(env)
    old = {k: os.environ.get(k) for k in base}
    os.environ.update(base)
    try:
        spec = importlib.util.spec_from_file_location("earc_recv_%d" % random.getrandbits(32), SRC)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    mod.log = lambda msg: None
    return mod


rv = load()
RATE, CH, FRAME = rv.RATE, rv.CHANNELS, rv.FRAME


def tones(freqs, nframes, amp=16000.0, start=0):
    t = np.arange(start, start + nframes, dtype=np.float64)
    return np.stack([amp * np.sin(2 * np.pi * f * t / RATE) for f in freqs], axis=1)


def to_bytes(x):
    return np.clip(np.rint(x), -32768, 32767).astype("<i2").tobytes()


class Quality(unittest.TestCase):
    FREQS = (1000.0, 5000.0, 10000.0, 15000.0, 18000.0, 20000.0)
    # error relative to the tone, dB; -93 is the s16 quantisation floor here
    LIMITS = (-88.0, -88.0, -88.0, -88.0, -88.0, -80.0)

    def check(self, ppm):
        step = 1.0 + ppm * 1e-6
        n_in = RATE * 2
        r = rv.Resampler()
        data = bytearray(to_bytes(tones(self.FREQS, n_in)))
        out, used = r.run(data, step, n_in)
        k = np.arange(len(out))
        ideal = tones(self.FREQS, 1, start=0)  # shape check only
        pos = step * k  # absolute input position of output k (frac starts at 0)
        ideal = np.stack([16000.0 * np.sin(2 * np.pi * f * pos / RATE) for f in self.FREQS], axis=1)
        sl = slice(500, len(out) - 500)
        res = []
        for c, (f, lim) in enumerate(zip(self.FREQS, self.LIMITS)):
            err = out[sl, c] - ideal[sl, c]
            db = 20 * np.log10(np.sqrt(np.mean(err ** 2)) / (16000.0 / np.sqrt(2)))
            res.append((f, db))
            self.assertLess(db, lim, "%+.0f ppm, %.0f Hz: %.1f dB" % (ppm, f, db))
        return res

    def test_quality(self):
        for ppm in (160.0, -160.0, 1000.0, -1000.0):
            res = self.check(ppm)
            print("  %+5.0f ppm: " % ppm + "  ".join("%.0fk %.0f dB" % (f / 1000, d) for f, d in res))


class Seamless(unittest.TestCase):
    def test_chunking_does_not_matter(self):
        m = load(EARC_RS_KP="0", EARC_RS_KI="0")
        n_in = RATE * 3
        src = to_bytes(tones((997.0, 3001.0, 7919.0, 60.0, 12007.0, 17011.0), n_in, amp=20000.0))
        step = 1.0 + 160e-6
        ref, _ = m.Resampler().run(bytearray(src), step, n_in)
        ref = np.clip(np.rint(ref), -32768, 32767).astype(np.int16)

        buf = m.Buffer()
        buf.rs_int = buf.rs_ppm = 160.0
        rng = random.Random(1)
        pos, got = 0, []
        while pos < len(src):
            n = rng.randrange(1, 9000)         # arbitrary byte counts, like recv()
            buf.put(src[pos:pos + n])
            pos += n
            while len(buf.data) > 70 * RATE * FRAME // 1000:  # stay clear of COARSE
                c = buf.take()
                if not buf.priming:
                    got.append(np.frombuffer(c, dtype="<i2").reshape(-1, CH))
        got = np.concatenate(got[1:])           # skip the faded-in first write
        off = 240                               # ...which was 240 frames
        n = min(len(got), len(ref) - off) - 100
        diff = np.abs(got[:n].astype(np.int32) - ref[off:off + n].astype(np.int32)).max()
        self.assertLessEqual(diff, 1, "seam/chunking error %d LSB" % diff)
        self.assertEqual(buf.dropped_bytes, 0)


def simulate(m, minutes, drift_ppm, seed=0, stall_at=None, stall_ms=300.0,
             jitter_ms=4.0, late_every_s=20.0, late_ms=20.0, saved_ppm=None):
    """Virtual-time model of the live chain, 1 ms ticks.

    Sender: the TV clock runs at RATE*(1+drift); the decoder emits one E-AC-3
    frame (1536 frames, 32 ms) at a time, and each reaches the receiver after a
    half-normal delay (sigma jitter_ms), plus late_ms on roughly one block every
    late_every_s, in order (TCP). Consumer: drains the pipe at exactly RATE and
    calls take() whenever it holds <= 8 ms (EARC_PIPE_FILL_MS=8). A stall holds
    every delivery back for stall_ms, then they all land at once.
    """
    rng = random.Random(seed)
    buf = m.Buffer()
    if saved_ppm is not None:
        buf.rs_int = buf.rs_ppm = saved_ppm
    BLOCK = 1536
    block_ms = BLOCK / (RATE * (1.0 + drift_ppm * 1e-6)) * 1000.0
    p_late = block_ms / (late_every_s * 1000.0)
    blocks, nb, last_due = [], 0, 0.0
    stall0 = None if stall_at is None else stall_at * 60000.0
    pipe = 0.0
    t = 0.0
    trace = []
    end = minutes * 60000.0
    while t < end:
        t += 1.0
        while (nb + 1) * block_ms <= t:      # decoder finished another block
            nb += 1
            due = nb * block_ms + abs(rng.gauss(0.0, jitter_ms))
            if rng.random() < p_late:
                due += late_ms
            if stall0 is not None and stall0 <= due < stall0 + stall_ms:
                due = stall0 + stall_ms
            last_due = max(last_due, due)
            blocks.append(last_due)
        n = 0
        while blocks and blocks[0] <= t:
            blocks.pop(0)
            n += 1
        if n:
            buf.put(bytes(n * BLOCK * FRAME))
        pipe = max(0.0, pipe - RATE / 1000.0)
        while pipe <= 8 * RATE / 1000.0:
            pipe += len(buf.take()) / FRAME
        if int(t) % 1000 == 0:
            with buf.lock:
                trace.append((t / 1000.0, len(buf.data) / FRAME / RATE * 1000.0, buf.rs_ppm,
                              buf.underrun_bytes, buf.dropped_bytes, buf.rs_in, buf.rs_out,
                              buf.low))
                buf.low = None
    return buf, trace


def window(trace, t0, t1):
    rows = [r for r in trace if t0 <= r[0] < t1]
    lv = np.array([r[1] for r in rows])
    pp = np.array([r[2] for r in rows])
    a, b = rows[0], rows[-1]
    realised = ((b[5] - a[5]) / float(b[6] - a[6]) - 1.0) * 1e6
    lows = [r[7] for r in rows if r[7] is not None]
    low = min(lows) / FRAME / RATE * 1000.0 if lows else float("nan")
    return lv, pp, b[3] - a[3], b[4] - a[4], realised, low


class Loop(unittest.TestCase):
    def run_case(self, drift, saved=None, minutes=12, seed=0):
        m = load()
        buf, tr = simulate(m, minutes, drift, seed=seed, saved_ppm=saved)
        lv, pp, under, dropped, realised, low = window(tr, 300, minutes * 60)
        print("  drift %+5.0f ppm start %s: mean fill %.1f ms (lowest %.1f), ppm %.0f..%.0f, "
              "realised %+.1f, silence %.0f ms, dropped %.0f ms"
              % (drift, "-" if saved is None else "%+.0f" % saved, lv.mean(), low,
                 pp.min(), pp.max(), realised, under / FRAME / RATE * 1000,
                 dropped / FRAME / RATE * 1000))
        self.assertEqual(under, 0)
        self.assertEqual(dropped, 0)
        self.assertEqual(buf.dropped_bytes, 0, "a drop happened during convergence")
        self.assertLess(abs(lv.mean() - m.RS_TARGET_MS), 3.0)
        self.assertLess(abs(realised - drift), 5.0)
        # 1 cent of pitch is ~578 ppm; a 120 ppm wander is 0.2 cents, inaudible.
        self.assertLess(pp.max() - pp.min(), 120.0)

    def test_drift_plus_160_cold(self):
        self.run_case(160.0)

    def test_drift_plus_160_warm(self):
        self.run_case(160.0, saved=158.0)

    def test_drift_minus_300(self):
        self.run_case(-300.0, seed=3)

    def test_stall_and_lump(self):
        m = load()
        buf, tr = simulate(m, 10, 160.0, stall_at=4, stall_ms=300.0, saved_ppm=160.0)
        before = [r for r in tr if r[0] < 240][-1]
        after = tr[-1]
        lv, pp, under, dropped, realised, low = window(tr, 360, 600)
        print("  stall 300 ms @4 min: silence %.0f ms, dropped %.0f ms; 2-6 min later mean %.1f ms"
              % ((after[3] - before[3]) / FRAME / RATE * 1000,
                 (after[4] - before[4]) / FRAME / RATE * 1000, lv.mean()))
        self.assertGreater(after[4] - before[4], 0, "the catch-up lump was not cut")
        self.assertEqual(under, 0)
        self.assertEqual(dropped, 0)
        self.assertLess(abs(lv.mean() - m.RS_TARGET_MS), 3.0)


class Starve(unittest.TestCase):
    """test_recv_fade2.py's starve/recover checks, for the resampling path: it
    keeps HALF frames back as lookahead, so writes are not all WRITE_FRAMES and
    that file's fixed-size setup does not apply."""

    def test_starve_and_recover_are_click_free(self):
        m = load()
        hz, amp = 997.0, 32000.0
        sig = tones((hz,) * CH, RATE, amp=amp)          # 1 s, all channels alike
        natural = np.abs(np.diff(sig[:, 0])).max()
        buf = m.Buffer()
        buf.rs_int = buf.rs_ppm = 160.0
        out = []
        buf.put(to_bytes(sig[:RATE // 10]))             # 100 ms, then starve
        while True:
            c = np.frombuffer(buf.take(), dtype="<i2").reshape(-1, CH)
            out.append(c)
            if buf.priming and not c.any():
                break
        for _ in range(3):
            out.append(np.frombuffer(buf.take(), dtype="<i2").reshape(-1, CH))
        buf.put(to_bytes(sig[RATE // 10:RATE // 2]))    # data returns
        for _ in range(40):
            out.append(np.frombuffer(buf.take(), dtype="<i2").reshape(-1, CH))
        y = np.concatenate(out)[:, 0].astype(np.int32)
        worst = np.abs(np.diff(y)).max()
        print("  starve/recover: worst step %d, natural %d" % (worst, natural))
        self.assertLessEqual(worst, natural * 1.5)
        self.assertGreater(buf.underrun_bytes, 0)


class Fallback(unittest.TestCase):
    def test_resample_off_is_the_old_drop_logic(self):
        m = load(EARC_RESAMPLE="0")
        buf, tr = simulate(m, 4, 160.0)
        self.assertIsNone(buf.rs)
        self.assertGreater(buf.dropped_bytes, 0)


if __name__ == "__main__":
    unittest.main()
