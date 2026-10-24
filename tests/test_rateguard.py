"""The rate guard, fed the real junk at the real arrival rate.

An IEC 61937 carrier at 192 kHz delivers 4x the byte rate of the 48 kHz LPCM
the pipeline expects. This replays tests/data/junk-eac3-as-pcm.raw at that rate
and measures how much reaches the wire before the guard latches.
"""
import array, importlib.util, math, os, sys, time

HERE = os.path.dirname(os.path.realpath(__file__))
path = sys.argv[1] if len(sys.argv) > 1 else "/usr/local/lib/earc/earc-streamer.py"
spec = importlib.util.spec_from_file_location("st", path)
st = importlib.util.module_from_spec(spec); sys.argv = ["x"]
spec.loader.exec_module(st)
RATE, CH, FRAME = st.OUT_RATE, st.OUT_CH, st.FRAME
RATIO = 4.0            # 192 kHz carrier vs 48 kHz expected

class Sink:
    def __init__(self): self.buf = bytearray()
    def send(self, b): self.buf += b

def sine(frames, hz=997.0, ph=0.0):
    s = array.array("h")
    for i in range(frames):
        v = int(32000 * math.sin(2*math.pi*hz*i/RATE + ph))
        for _ in range(CH): s.append(v)
    return s.tobytes()

junk = open(os.path.join(HERE, "data", "junk-eac3-as-pcm.raw"), "rb").read()
CHUNK = int(RATE * 0.020)
print(f"PCM_AHEAD_LIMIT {st.PCM_AHEAD_LIMIT}s  KEEP {st.Output.KEEP}s  ratio {RATIO}x\n")
fails = []
def check(n, cond, note=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {n:<46}{note}")
    if not cond: fails.append(n)

# --- steady PCM, then the wire turns into a bitstream
sink = Sink(); out = st.Output(sink)
out.restarting(0.0, pcm_guard=True)
out.resync_until = 0.0                      # past the startup window
ph = 0.0
for _ in range(25):                         # 500 ms of honest audio, paced
    out.t0 -= 0.020
    out.audio(sine(CHUNK, ph=ph)); ph += 2*math.pi*997*CHUNK/RATE
honest = len(sink.buf)
check("honest paced PCM is not flagged", out.pcm_broken is False,
      f"{honest} B through")

# a full-buffer burst (60 ms at once) must also not trip it
out.audio(sine(int(RATE*0.060), ph=ph))
check("a 60 ms arecord burst does not trip it", out.pcm_broken is False)
honest = len(sink.buf)

# Anchor the clock at the pacer's documented steady state before measuring.
# Letting the harness's own Python execution time set the starting lag inflated
# this to 240 ms; the running system holds exactly KEEP of lag (silence_tick
# re-anchors to it), and that is what the leak should be measured against.
out.t0 = time.monotonic() - out.sent / RATE - out.KEEP
print(f"    anchored at ahead = {-((time.monotonic()-out.t0)-out.sent/RATE):+.3f}s"
      f" (steady state is -KEEP = {-out.KEEP:+.3f}s)")

# now the junk, arriving RATIO times faster than real time
for i in range(0, len(junk), CHUNK * FRAME):
    out.t0 -= 0.020 / RATIO                 # wall clock advances 4x slower
    out.audio(junk[i:i + CHUNK * FRAME])
leak = len(sink.buf) - honest
check("guard latched on the rate", out.pcm_broken is True)
check("leak under 160 ms", leak / 576000.0 < 0.160,
      f"{leak} B = {leak/576000.0*1000:.0f} ms of junk"
      f"  (vs 650 ms waiting for the registers)")

# --- and nothing more gets through, however much arrives
after = len(sink.buf)
for _ in range(50):
    out.t0 -= 0.020
    out.audio(junk[:CHUNK * FRAME])
check("latch holds for the rest of the pipeline", len(sink.buf) == after,
      f"{len(sink.buf) - after} B after latch")

# --- a restart clears it, and bitstream pipelines are never guarded
out.restarting(0.0, pcm_guard=True)
check("restart clears the latch", out.pcm_broken is False)
out.restarting(0.0, pcm_guard=False)
check("bitstream pipelines are not rate-guarded", out.pcm_guard is False)
out.resync_until = 0.0
out.discard_until = 0.0
for i in range(0, 20 * CHUNK * FRAME, CHUNK * FRAME):
    out.t0 -= 0.020 / RATIO
    out.audio(junk[i:i + CHUNK * FRAME])
check("...so a fast bitstream path still passes", out.pcm_broken is False)

print("\nFAILED:", fails if fails else "none"); sys.exit(1 if fails else 0)
