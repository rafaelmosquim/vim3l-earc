"""The gap hold, driven by the real bitstream-as-PCM junk captured 2026-10-15.

tests/data/junk-eac3-as-pcm.raw is 670 ms of the actual output the LPCM
pipeline produced while the TV had already switched the wire to E-AC-3:
IEC 61937 Pa/Pb sync words 24576 bytes apart, zero-crossing rate 0.499,
mean level 15716 of 32767. This is what must never reach the speakers.
"""
import array, importlib.util, math, os, sys, time

HERE = os.path.dirname(os.path.realpath(__file__))
path = sys.argv[1] if len(sys.argv) > 1 else "/usr/local/lib/earc/earc-streamer.py"
spec = importlib.util.spec_from_file_location("st", path)
st = importlib.util.module_from_spec(spec); sys.argv = ["x"]
spec.loader.exec_module(st)
RATE, CH, FRAME = st.OUT_RATE, st.OUT_CH, st.FRAME

class Sink:
    def __init__(self): self.buf = bytearray()
    def send(self, b): self.buf += b

junk = open(os.path.join(HERE, "data", "junk-eac3-as-pcm.raw"), "rb").read()
step = int(RATE * 0.020) * FRAME
chunks = [junk[i:i + step] for i in range(0, len(junk), step)]
print(f"junk sample: {len(junk)} B = {len(junk)/576000.0:.2f} s in {len(chunks)} chunks")
print(f"GAP_RESYNC {st.GAP_RESYNC}s  GAP_HOLD_MS {st.GAP_HOLD_MS}\n")

fails = []
def check(n, cond, note=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {n:<48}{note}")
    if not cond: fails.append(n)

def sine(frames, hz=997.0, ph=0.0):
    s = array.array("h")
    for i in range(frames):
        v = int(32000 * math.sin(2*math.pi*hz*i/RATE + ph))
        for _ in range(CH): s.append(v)
    return s.tobytes()

# --- without the hold, this junk goes straight to the wire (the bug)
sink = Sink(); out = st.Output(sink)
for c in chunks: out.audio(c)
check("baseline: junk reaches the wire unguarded", len(sink.buf) > 0,
      f"{len(sink.buf)} B leaked")

# --- with the hold, none of it does
sink2 = Sink(); out2 = st.Output(sink2)
out2.audio(sine(480))                      # normal audio first
before = len(sink2.buf)
out2.hold(st.GAP_HOLD_MS / 1000.0)
check("hold() latches muted", out2.muted is True)
for c in chunks: out2.audio(c)
check("not one byte of junk is forwarded", len(sink2.buf) == before,
      f"{len(sink2.buf) - before} B leaked")

# --- a shorter hold must not cut a longer one short
d = out2.discard_until
out2.hold(0.001)
check("hold() never shortens an active hold", out2.discard_until == d)

# --- and real audio still resumes cleanly afterwards
out2.discard_until = time.monotonic()      # expire it
out2.audio(sine(480, ph=math.pi/2))        # worst phase
check("audio resumes after the hold", len(sink2.buf) > before,
      f"{len(sink2.buf) - before} B")
check("muted cleared on resume", out2.muted is False)
tail = bytes(sink2.buf[before:])
s = array.array("h"); s.frombytes(bytes(FRAME) + tail); c0 = list(s[0::CH])
step_max = max(abs(c0[i+1] - c0[i]) for i in range(len(c0) - 1))
check("resume fades in from silence", step_max <= 6258, f"step {step_max}")

# --- the bitstream paths must NOT take the hold (extractor already guards them)
check("hold is LPCM-only in the main loop",
      "ctype in LPCM2 | LPCM8" in open(path).read())

print("\nFAILED:", fails if fails else "none"); sys.exit(1 if fails else 0)
