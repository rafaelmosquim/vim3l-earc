"""The decoder settling window: junk discarded, resume still click-free."""
import array, importlib.util, math, sys, time

spec = importlib.util.spec_from_file_location("st", (sys.argv[1] if len(sys.argv) > 1 else "/usr/local/lib/earc/earc-streamer.py"))
st = importlib.util.module_from_spec(spec); sys.argv = ["x"]
spec.loader.exec_module(st)
RATE, CH, FRAME = st.OUT_RATE, st.OUT_CH, st.FRAME

class Sink:
    def __init__(self): self.buf = bytearray()
    def send(self, b): self.buf += b

def sine(frames, hz=997.0, amp=32000, ph=0.0):
    s = array.array("h")
    for i in range(frames):
        v = int(amp * math.sin(2*math.pi*hz*i/RATE + ph))
        for _ in range(CH): s.append(v)
    return s.tobytes()

def max_step(b):
    s = array.array("h"); s.frombytes(bytes(b)); c = list(s[0::CH])
    return max(abs(c[i+1]-c[i]) for i in range(len(c)-1))

natural = max_step(sine(RATE//10)); bar = int(natural*1.5)
print(f"SETTLE_MS = {st.SETTLE_MS}  (= {st.SETTLE_MS/1000*RATE:.0f} frames)\n")
fails = []
def check(n, cond, note=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {n:<46}{note}")
    if not cond: fails.append(n)

sink = Sink(); out = st.Output(sink)
out.audio(sine(480))                       # prime: clears muted
check("audio flows before a restart", len(sink.buf) > 0, f"{len(sink.buf)} B")

# --- bitstream restart: a settling window
out.restarting(0.05)
check("restart(settle) latches muted", out.muted is True)
before, dropped0 = len(sink.buf), out.dropped
junk = sine(480, hz=7000.0)                # stand-in for decoder garbage
out.audio(junk); out.audio(junk)
check("junk inside the window is discarded",
      len(sink.buf) == before, f"{len(sink.buf)-before} B emitted")
check("discards are counted", out.dropped > dropped0, f"+{out.dropped-dropped0} frames")

# --- window expires, real audio resumes
while time.monotonic() < out.discard_until:
    time.sleep(0.005)
out.audio(sine(480, ph=math.pi/2))         # worst phase: starts at the peak
check("audio resumes after the window", len(sink.buf) > before,
      f"{len(sink.buf)-before} B")
check("muted cleared on resume", out.muted is False)
tail = bytes(sink.buf[before:])
step = max_step(bytes(FRAME) + tail)
check("resume fades in from silence", step <= bar, f"step {step} (bar {bar})")
assert junk not in bytes(sink.buf), "junk must never reach the wire"

# --- LPCM restart: no window at all
sink2 = Sink(); out2 = st.Output(sink2)
out2.audio(sine(480)); out2.restarting(0.0)
out2.audio(sine(480, ph=math.pi/2))
check("restart(0) passes audio immediately", len(sink2.buf) > 480*FRAME,
      f"{len(sink2.buf)} B")

print("\nFAILED:", fails if fails else "none"); sys.exit(1 if fails else 0)
