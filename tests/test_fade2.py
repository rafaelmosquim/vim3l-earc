"""Per-boundary step checks. 997 Hz deliberately: nothing aligns to a block."""
import array, importlib.util, math, sys

spec = importlib.util.spec_from_file_location("st", (sys.argv[1] if len(sys.argv) > 1 else "/usr/local/lib/earc/earc-streamer.py"))
st = importlib.util.module_from_spec(spec); sys.argv = ["x"]
spec.loader.exec_module(st)
RATE, CH, FRAME, F = st.OUT_RATE, st.OUT_CH, st.FRAME, st.FADE_FRAMES

def sine(frames, hz=997.0, amp=32000, phase=0.0):
    s = array.array("h")
    for i in range(frames):
        v = int(amp * math.sin(2 * math.pi * hz * i / RATE + phase))
        for _ in range(CH):
            s.append(v)
    return s.tobytes()

def ch0(buf):
    s = array.array("h"); s.frombytes(buf); return list(s[0::CH])

def steps(*blocks):
    seq = []
    for b in blocks:
        seq += ch0(b)
    return max(abs(seq[i + 1] - seq[i]) for i in range(len(seq) - 1))

natural = steps(sine(RATE // 10))
bar = int(natural * 1.5)
print(f"natural max step (997 Hz, -0.2 dBFS): {natural}   pass if <= {bar}\n")
fails = []

def check(name, got):
    ok = got <= bar
    print(f"  {'PASS' if ok else 'FAIL'}  {name:<42} {got:6d}")
    if not ok:
        fails.append(name)

# --- 1. starve: real audio -> decay -> silence, worst phase (cut at the peak)
sig = sine(F, phase=math.pi / 2)
dec = st._decay(sig[-FRAME:], F)
check("audio -> decay -> silence", steps(sig, dec, bytes(F * FRAME)))
check("  old behaviour for comparison", steps(sig, bytes(F * FRAME)))

# --- 2. resume: silence -> faded-in audio
check("silence -> fade-in -> audio", steps(bytes(F * FRAME), st._scale(sig, st.FADE_IN)))
check("  old behaviour for comparison", steps(bytes(F * FRAME), sig))

# --- 3. splice: one continuous sine with a chunk cut out of the middle
whole = sine(F * 6)
A, skip = F * 2, F * 3
sent, before, head = whole[:A * FRAME], whole[A * FRAME:(A + skip) * FRAME], whole[(A + skip) * FRAME:]
check("continuous audio -> spliced cut", steps(sent, st._splice(before, head)))
check("  old behaviour for comparison", steps(sent, head))

# --- 4. degenerate inputs must not crash or step
check("decay at the minimum ramp length", steps(sig, st._decay(sig[-FRAME:], st.FADE_MIN_FRAMES), bytes(FRAME)))
assert st._splice(b"", head) == head, "empty before -> head unchanged"
assert st._splice(before, b"") == b"", "empty head -> empty"
assert len(st._splice(before, head)) == len(head), "splice must preserve length"
assert len(st._decay(sig[-FRAME:], F)) == F * FRAME, "decay length"
print(f"\n  PASS  degenerate inputs handled, lengths preserved")

print("\nFAILED:", fails if fails else "none")
sys.exit(1 if [f for f in fails if "old behaviour" not in f] else 0)
