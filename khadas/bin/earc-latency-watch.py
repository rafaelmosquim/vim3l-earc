#!/usr/bin/env python3
"""Hold eARC_RX Latency at a per-format value.

Two facts make this necessary rather than nice to have:

  * The driver writes EARCRX_DEFAULT_LATENCY = 100 into ERX_LATENCY_REG on
    EVERY link-up and nowhere else, so any tuned value is lost the next time
    the TV is power-cycled.
  * A HIGHER register value means MORE audio lag (established by listening
    test 2026-10-24: 0xC8 was worse than 0x64, and 0x00 better again), and the
    LPCM and bitstream paths differ by ~64 ms of inherent Dolby decode cost --
    32 ms of IEC 61937 burst assembly plus one 32 ms decoder frame. One static
    value therefore cannot serve both formats.

Writing the register does not disturb the link (tested while eARC was up: no
LOSTHB, attended type stayed 2), and mixer polling was ruled out long ago as a
cause of the flapping in docs/findings.md #4.

Values are in ms, 0-255, and 0 is the floor -- about 100 ms of audio-lag
correction is all this lever has. Override per format with the environment:

    EARC_LAT_BITSTREAM=0     AC-3 / E-AC-3   (the long path)
    EARC_LAT_LPCM=0          LPCM            (~64 ms shorter through the same
                                              chain, so if the bitstream path
                                              is slightly late this one is
                                              near sync; raise it if LPCM
                                              turns out to be EARLY)
"""
import os
import subprocess
import sys
import time

CARD = os.environ.get("EARC_CARD", "AMLAUGESOUND")  # card id, not index: see earc-streamer.py
LAT = "eARC_RX Latency"
ATT = "eARC_RX attended type"
CODING = "eARC_RX Audio Coding Type"
POLL = float(os.environ.get("EARC_LAT_POLL", "1.0"))

# Coding types, as earc-streamer.py groups them.
LPCM = {1, 2, 3}
BITSTREAM = {7, 8, 9, 17}
WANT = {
    "lpcm": int(os.environ.get("EARC_LAT_LPCM", "0")),
    "bitstream": int(os.environ.get("EARC_LAT_BITSTREAM", "0")),
}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def cget(name):
    """First value of a mixer control, or None if it cannot be read."""
    out = subprocess.run(["amixer", "-c", CARD, "cget", f"name={name}"],
                         capture_output=True, text=True).stdout
    for line in out.splitlines():
        if ": values=" in line:
            v = line.split("=", 1)[1].split(",")[0].strip()
            try:
                return int(v, 0)
            except ValueError:
                return None
    return None


def cset(name, value):
    return subprocess.run(["amixer", "-q", "-c", CARD, "cset",
                           f"name={name}", f"0x{value:02x}"]).returncode == 0


def want_for(coding):
    if coding in BITSTREAM:
        return "bitstream", WANT["bitstream"]
    if coding in LPCM:
        return "lpcm", WANT["lpcm"]
    return None, None


def main():
    log(f"holding {LAT}: lpcm={WANT['lpcm']}ms bitstream={WANT['bitstream']}ms")
    last = None   # (label, value) we last applied, for quiet logging
    while True:
        time.sleep(POLL)
        # 0xFF is not a setting, it is what the register reads with the link
        # down; and the format registers read zero unless a capture is open.
        if cget(ATT) != 2:
            last = None
            continue
        coding = cget(CODING)
        label, target = want_for(coding)
        if target is None:
            continue
        now = cget(LAT)
        if now == target:
            if last != (label, target):
                log(f"{label} (coding type {coding}): latency already {target}ms")
                last = (label, target)
            continue
        if cset(LAT, target):
            log(f"{label} (coding type {coding}): latency {now} -> {target}ms")
            last = (label, target)
        else:
            log(f"failed to set latency to {target}ms")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
