"""Drive the real Buffer through starve and recovery. 997 Hz: nothing aligns."""
import array, importlib.util, math, sys

spec = importlib.util.spec_from_file_location("rv", "/home/rafael/.local/bin/earc-recv.py")
rv = importlib.util.module_from_spec(spec); spec.loader.exec_module(rv)
RATE, CH, FRAME, W = rv.RATE, rv.CHANNELS, rv.FRAME, rv.WRITE_FRAMES

def sine(frames, hz=997.0, amp=32000, phase=0.0):
    s = array.array("h")
    for i in range(frames):
        v = int(amp * math.sin(2 * math.pi * hz * i / RATE + phase))
        for _ in range(CH):
            s.append(v)
    return s.tobytes()

def steps(*blocks):
    seq = []
    for b in blocks:
        s = array.array("h"); s.frombytes(b); seq += list(s[0::CH])
    return max(abs(seq[i + 1] - seq[i]) for i in range(len(seq) - 1))

natural = steps(sine(RATE // 10)); bar = int(natural * 1.5)
print(f"numpy: {rv.np is not None}   MUTE_FADE={rv.MUTE_FADE} fr "
      f"({rv.MUTE_FADE / RATE * 1000:.1f} ms)   WRITE_FRAMES={W}")
print(f"natural max step: {natural}   pass if <= {bar}\n")
fails = []
def check(name, got, baseline=False):
    ok = got <= bar
    print(f"  {'PASS' if ok else ('(bug)' if baseline else 'FAIL')}  {name:<40} {got:6d}")
    if not ok and not baseline:
        fails.append(name)

buf = rv.Buffer(); buf.priming = False
buf.data = bytearray(sine(W * 2, phase=math.pi / 2))
t1 = buf.take()            # fades in from zero (muted at construction)
t2 = buf.take()            # clean continuation
m1 = buf.take()            # buffer empty -> decay out
m2 = buf.take()            # still empty -> plain silence
for nm, b in (("t1", t1), ("t2", t2), ("m1", m1), ("m2", m2)):
    assert len(b) == rv.WRITE_BYTES, (nm, len(b))
assert m2 == rv.SILENCE, "second call in one gap must be plain silence"
check("real audio -> decay -> silence", steps(t1, t2, m1, m2))

buf.priming = False
buf.data = bytearray(sine(W, phase=math.pi / 2))
t3 = buf.take()            # data back -> ramp up from zero
check("silence -> fade-in -> audio", steps(m2, t3))

# What the same two boundaries did before the patch.
raw = sine(W, phase=math.pi / 2)
check("old: audio -> hard silence", steps(raw, rv.SILENCE), baseline=True)
check("old: silence -> hard audio", steps(rv.SILENCE, raw), baseline=True)

# drop()'s existing splice crossfade must still work (it was already correct).
buf2 = rv.Buffer(); buf2.priming = False
buf2.data = bytearray(sine(W * 8))
before = bytes(buf2.data[:rv.XFADE_BYTES])
buf2.drop(rv.XFADE_BYTES * 2)
check("drop() splice still click-free", steps(before, bytes(buf2.data[:rv.XFADE_BYTES * 2])))

print("\nFAILED:", fails if fails else "none")
sys.exit(1 if fails else 0)
