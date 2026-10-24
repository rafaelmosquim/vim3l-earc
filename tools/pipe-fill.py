#!/usr/bin/env python3
"""Measure the standing fill of the capture pipeline's pipes (needs root).

Every stage of earc-streamer's pipeline is joined by a 64 kB kernel pipe. A
pipe's fill level is set by the worst transient it ever saw and never recovers:
once both sides run at the same average rate, whatever is sitting in it stays
there forever as pure latency, invisible to the Pacer -- which only controls
RATE, never absolute staleness. This prints what is actually in them.
"""
import array, fcntl, os, subprocess, sys, termios, time

F_GETPIPE_SZ = 1032
RATE_PCM_IN = 192000 * 2 * 2      # bitstream capture: 192 kHz stereo s16
RATE_PCM_IN_LPCM = 48000 * 2 * 2  # LPCM capture: 48 kHz stereo s16
RATE_OUT = 48000 * 6 * 2          # ffmpeg output: 48 kHz 5.1 s16


def procs():
    out = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True, text=True).stdout
    found = {}
    for line in out.splitlines():
        pid, _, args = line.strip().partition(" ")
        if "/proc/" in args or "pipe-fill" in args:
            continue
        for key in ("arecord", "iec61937-extract", "ffmpeg"):
            if key in args and "earc" in args or (key in args and key != "ffmpeg"):
                found.setdefault(key, (int(pid), args))
            elif key in args:
                found.setdefault(key, (int(pid), args))
    return found


def pipe_fds(pid):
    res = []
    try:
        fds = os.listdir(f"/proc/{pid}/fd")
    except OSError as e:
        return [("?", f"cannot read /proc/{pid}/fd: {e}")]
    for fd in sorted(fds, key=lambda x: int(x)):
        try:
            tgt = os.readlink(f"/proc/{pid}/fd/{fd}")
        except OSError:
            continue
        if tgt.startswith("pipe:"):
            res.append((fd, tgt))
    return res


def measure(pid, fd):
    """Bytes queued, and the pipe's capacity. FIONREAD does not consume."""
    try:
        f = os.open(f"/proc/{pid}/fd/{fd}", os.O_RDONLY | os.O_NONBLOCK)
    except OSError as e:
        return None, None, str(e)
    try:
        b = array.array("i", [0])
        fcntl.ioctl(f, termios.FIONREAD, b, True)
        try:
            cap = fcntl.fcntl(f, F_GETPIPE_SZ)
        except OSError:
            cap = None
        return b[0], cap, None
    except OSError as e:
        return None, None, str(e)
    finally:
        os.close(f)


def main():
    if os.geteuid() != 0:
        print("needs root: sudo python3 " + sys.argv[0])
    p = procs()
    if not p:
        print("no pipeline processes found -- is earc-streamer running?")
        return 1
    print("pipeline stages:")
    for k, (pid, args) in p.items():
        print(f"  {k:18} pid {pid}  {args[:90]}")
    bitstream = "iec61937-extract" in p
    cap_rate = RATE_PCM_IN if bitstream else RATE_PCM_IN_LPCM
    print(f"\npath: {'BITSTREAM (E-AC-3)' if bitstream else 'LPCM'}"
          f"   capture byte rate {cap_rate} B/s\n")
    print(f"{'sample':>6}  {'stage.fd':<28} {'bytes':>8} {'cap':>7}   as audio")
    for n in range(6):
        for k, (pid, _) in p.items():
            for fd, _tgt in pipe_fds(pid):
                if fd not in ("0", "1"):
                    continue
                q, cap, err = measure(pid, fd)
                if err:
                    print(f"{n:6}  {k + '.fd' + fd:<28} {err}")
                    continue
                # fd 0 of a stage is the pipe from the stage before it
                if k == "iec61937-extract" and fd == "0":
                    ms = f"{q / cap_rate * 1000:8.1f} ms"
                elif k == "ffmpeg" and fd == "0":
                    ms = ("  compressed: %.0f/%.0f/%.0f ms @ 192/384/640 kbps"
                          % (q / 24000 * 1000, q / 48000 * 1000, q / 80000 * 1000))
                elif k == "ffmpeg" and fd == "1":
                    ms = f"{q / RATE_OUT * 1000:8.1f} ms"
                else:
                    ms = ""
                print(f"{n:6}  {k + '.fd' + fd:<28} {q:8} {cap or '?':>7}   {ms}")
        print()
        time.sleep(0.5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
