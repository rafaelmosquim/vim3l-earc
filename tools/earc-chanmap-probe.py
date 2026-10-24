#!/usr/bin/env python3
"""Wait for the TV to send multichannel LPCM (coding type 3) and log what is
actually in each channel slot.

Why: the streamer's 8-channel LPCM pipeline declares the input as ffmpeg's 7.1
layout (FL FR FC LFE BL BR SL SR), but HDMI/eARC puts 5.1 in CTA-861 channel
allocation order, which is FL FR LFE FC RL RR -- FC and LFE swapped. The eARC
link only ever carries 8ch LPCM for a few minutes at a time (8 sessions since
2026-10-03), so this probe sits on the format register and grabs a sample the
next time it happens instead of waiting for someone to be at the keyboard.

Reads the streamer's own fan-out port (4010), i.e. POST-pan 5.1, which is
enough: the pan is a permutation plus gains, so if the output's FC slot is the
one full of sub-100 Hz energy and the LFE slot holds the broadband signal, the
input order is CTA and the mapping needs swapping. Per-channel zero-crossing
rate separates them unambiguously -- an LFE channel is lowpassed at 120 Hz.
"""
import array, math, socket, subprocess, sys, time

PORT, HOST = 4010, "127.0.0.1"
CH, RATE = 6, 48000
NAMES = ["FL", "FR", "FC", "LFE", "SL", "SR"]
SECS = 8
LOG = "/home/khadas/claude/chanmap-probe.log"
DEADLINE = time.time() + 24 * 3600


def log(msg):
    line = f"{time.strftime('%m-%d %H:%M:%S')} {msg}"
    with open(LOG, "a") as f:
        f.write(line + "\n")
    print(line, flush=True)


def mixer(name):
    out = subprocess.run(["amixer", "-c0", "cget", f"name={name}"],
                         capture_output=True, text=True).stdout
    for ln in out.splitlines():
        if ": values=" in ln:
            return ln.split("=", 1)[1].strip()
    return "?"


def grab(secs):
    want = secs * RATE * CH * 2
    buf = bytearray()
    s = socket.create_connection((HOST, PORT), 5)
    s.settimeout(5)
    try:
        while len(buf) < want:
            b = s.recv(65536)
            if not b:
                break
            buf += b
    finally:
        s.close()
    return bytes(buf)


def analyse(raw):
    a = array.array("h")
    a.frombytes(raw[:len(raw) // (2 * CH) * 2 * CH])
    n = len(a) // CH
    rows = []
    for c in range(CH):
        sl = a[c::CH]
        sq = 0
        zc = 0
        prev = 0
        for v in sl:
            sq += v * v
            if (v >= 0) != (prev >= 0):
                zc += 1
            prev = v
        rms = math.sqrt(sq / max(1, len(sl)))
        db = 20 * math.log10(rms / 32768) if rms > 0 else -999
        rows.append((NAMES[c], db, zc / (n / RATE)))
    return n, rows


def report(tag):
    ctype = mixer("eARC_RX Audio Coding Type")
    ca = mixer("eARC_RX Channel Allocation")
    freq = mixer("eARC_RX Audio Sample Frequency")
    log(f"--- {tag}: coding type {ctype}, CA {ca}, freq {freq}")
    try:
        raw = grab(SECS)
    except Exception as e:
        log(f"    capture failed: {e}")
        return
    n, rows = analyse(raw)
    log(f"    {n / RATE:.1f}s captured")
    for name, db, zcr in rows:
        log(f"    {name:3s} {db:7.2f} dBFS   zcr {zcr:7.0f}/s")
    fl = dict((r[0], r[1]) for r in rows)
    if fl["FL"] > -90 and fl["SL"] > -90:
        log(f"    surround vs front: SL {fl['SL'] - fl['FL']:+.2f} dB, "
            f"SR {fl['SR'] - fl['FR']:+.2f} dB")
    zc = dict((r[0], r[2]) for r in rows)
    if zc["FC"] and zc["LFE"] and fl["LFE"] > -90:
        if zc["FC"] < zc["LFE"] / 3:
            log("    VERDICT: FC slot is the lowpassed one -> input is in CTA "
                "order (FL FR LFE FC ...), the pipeline has FC/LFE SWAPPED")
        elif zc["LFE"] < zc["FC"] / 3:
            log("    VERDICT: LFE slot is the lowpassed one -> input order "
                "matches the pipeline, no swap needed")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "now":
        report("manual sample")
        return
    log("probe started, waiting for coding type 3")
    seen = False
    while time.time() < DEADLINE:
        ct = mixer("eARC_RX Audio Coding Type")
        if ct == "3" and not seen:
            seen = True
            report("multichannel LPCM detected")
        elif ct != "3":
            seen = False
        time.sleep(3)
    log("probe deadline reached, exiting")


main()
