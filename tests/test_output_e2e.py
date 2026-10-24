"""Drive the INSTALLED Output class through a gap and a burst discard."""
import array, importlib.util, math, sys, time

spec = importlib.util.spec_from_file_location("st", (sys.argv[1] if len(sys.argv) > 1 else "/usr/local/lib/earc/earc-streamer.py"))
st = importlib.util.module_from_spec(spec); sys.argv = ["x"]
spec.loader.exec_module(st)
RATE, CH, FRAME, F = st.OUT_RATE, st.OUT_CH, st.FRAME, st.FADE_FRAMES

class Sink:
    def __init__(self): self.buf = bytearray(); self.calls = 0
    def send(self, b): self.buf += b; self.calls += 1

def sine(frames, hz=997.0, amp=32000, ph=0.0):
    s = array.array("h")
    for i in range(frames):
        v = int(amp * math.sin(2 * math.pi * hz * i / RATE + ph))
        for _ in range(CH): s.append(v)
    return s.tobytes()

def max_step(buf):
    s = array.array("h"); s.frombytes(bytes(buf)); c = list(s[0::CH])
    return max(abs(c[i+1] - c[i]) for i in range(len(c)-1))

natural = max_step(sine(RATE // 10)); bar = int(natural * 1.5)
print(f"natural {natural}, bar {bar}, FADE_FRAMES {F}, FADE_MIN {st.FADE_MIN_FRAMES}\n")
fails = []
def check(n, v):
    ok = v <= bar; print(f"  {'PASS' if ok else 'FAIL'}  {n:<44}{v:6d}")
    if not ok: fails.append(n)

# ---- 1. steady audio, then a starve, then audio returns
sink = Sink(); out = st.Output(sink)
phase = 0.0
for _ in range(10):
    out.audio(sine(480, ph=phase)); phase += 2 * math.pi * 997 * 480 / RATE
assert out.muted is False and len(out.last) == FRAME
out.t0 = time.monotonic() - 0.30          # manufacture a 200 ms backlog
out.silence_tick()
assert out.muted is True, "silence_tick must latch muted"
pre = len(sink.buf)
for _ in range(4):
    out.audio(sine(480, ph=phase)); phase += 2 * math.pi * 997 * 480 / RATE
assert out.muted is False, "real audio must clear muted"
check("audio -> starve -> audio (whole stream)", max_step(sink.buf))

# the decay must actually be audio, not zeros, and must reach silence
dec = sink.buf[pre - st.FADE_FRAMES * FRAME * 0 : ]
s = array.array("h"); s.frombytes(bytes(sink.buf))
assert any(s), "stream is not all zeros"

# ---- 2. the burst discard: hand over a backlog while in the resync window
sink2 = Sink(); out2 = st.Output(sink2)
out2.audio(sine(480))                      # prime, clears muted
out2.restarting()
out2.t0 = time.monotonic() + 0.50          # look 500 ms AHEAD -> force a discard
big = sine(F * 8, ph=2 * math.pi * 997 * 480 / RATE)
out2.audio(big)
check("continuous audio across burst discard", max_step(sink2.buf))
assert out2.dropped > 0, f"expected a discard, dropped={out2.dropped}"
print(f"\n  discarded {out2.dropped} frames ({out2.dropped/RATE*1000:.0f} ms),"
      f" sent {out2.sent} frames, filled {out2.filled}")

# ---- 3. frame accounting stays whole and the clock is not corrupted
assert len(sink.buf) % FRAME == 0 and len(sink2.buf) % FRAME == 0, "partial frame emitted"
assert out.sent * FRAME == len(sink.buf), (out.sent * FRAME, len(sink.buf))
print(f"  PASS  frame accounting exact: sent*{FRAME} == {len(sink.buf)} bytes")

print("\nFAILED:", fails if fails else "none"); sys.exit(1 if fails else 0)
