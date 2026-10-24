#!/usr/bin/env python3
"""Measure the standing output latency of an ALSA playback device.

snd_pcm_delay() reports how many frames sit between the application pointer
and the DAC, i.e. exactly the latency a sample incurs after CamillaDSP hands
it to ALSA.  Sweep the requested latency to find the floor this driver can
actually hold without underrunning.
"""
import ctypes, sys, time

a = ctypes.CDLL("libasound.so.2")
PLAYBACK, S16_LE, RW_INTERLEAVED = 0, 2, 3

a.snd_strerror.restype = ctypes.c_char_p


def err(rc):
    return a.snd_strerror(rc).decode()


def probe(dev, rate, ch, want_us):
    pcm = ctypes.c_void_p()
    rc = a.snd_pcm_open(ctypes.byref(pcm), dev.encode(), PLAYBACK, 0)
    if rc < 0:
        return None, f"open: {err(rc)}"
    try:
        rc = a.snd_pcm_set_params(pcm, S16_LE, RW_INTERLEAVED, ch, rate, 1, want_us)
        if rc < 0:
            return None, f"set_params: {err(rc)}"
        buf, per = ctypes.c_ulong(), ctypes.c_ulong()
        a.snd_pcm_get_params(pcm, ctypes.byref(buf), ctypes.byref(per))

        # Fill the ring, then let it run long enough to prove it is stable.
        chunk = (ctypes.c_char * (per.value * ch * 2))()
        for _ in range(max(1, buf.value // per.value) + 1):
            if a.snd_pcm_writei(pcm, chunk, per.value) < 0:
                return None, "underrun while priming"
        delay = ctypes.c_long()
        samples = []
        t_end = time.monotonic() + 1.0
        while time.monotonic() < t_end:
            n = a.snd_pcm_writei(pcm, chunk, per.value)
            if n < 0:
                return None, f"underrun: {err(n)}"
            if a.snd_pcm_delay(pcm, ctypes.byref(delay)) == 0:
                samples.append(delay.value)
        return (buf.value, per.value, samples), None
    finally:
        a.snd_pcm_close(pcm)


dev = sys.argv[1] if len(sys.argv) > 1 else "hw:0,0"
rate = int(sys.argv[2]) if len(sys.argv) > 2 else 48000
ch = int(sys.argv[3]) if len(sys.argv) > 3 else 2
print(f"{dev}  {rate} Hz  {ch}ch  S16_LE\n")
print(f"{'asked':>8} {'buffer':>14} {'period':>14} {'measured delay':>22}")
for want_us in (5000, 10000, 20000, 40000, 80000, 160000):
    got, why = probe(dev, rate, ch, want_us)
    if why:
        print(f"{want_us/1000:7.0f}m {why:>52}")
        continue
    buf, per, s = got
    lo, hi = min(s) / rate * 1000, max(s) / rate * 1000
    print(f"{want_us/1000:7.0f}m {buf:7} fr {buf/rate*1000:5.1f}m"
          f" {per:7} fr {per/rate*1000:5.1f}m"
          f"   {lo:6.1f}-{hi:6.1f} ms  (n={len(s)})")
