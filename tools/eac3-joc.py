#!/usr/bin/env python3
"""Report what an E-AC-3 elementary stream contains, and whether it carries
Dolby Atmos (JOC).

Atmos in E-AC-3 is signalled per ETSI TS 103 420 Annex C: the DEPENDENT
substream (strmtyp == 1) carries a one-byte addbsi payload whose value is the
JOC complexity index -- the object count, commonly 11 to 16. So:

  independent substream only ................ plain DD+ 5.1, no Atmos
  + dependent substream, no addbsi byte ..... DD+ 7.1 extension, no Atmos
  + dependent substream with addbsi byte .... DD+ with Joint Object Coding

The bsi walk follows ETSI TS 102 366 Annex E.1.2.2; we only need to reach
addbsie, but every field before it has to be stepped over exactly.
"""
import sys
from collections import Counter

ACMOD = {0: "1+1", 1: "1/0 mono", 2: "2/0 stereo", 3: "3/0", 4: "2/1",
         5: "3/1", 6: "2/2 quad", 7: "3/2"}
ACMOD_CH = {0: 2, 1: 1, 2: 2, 3: 3, 4: 3, 5: 4, 6: 4, 7: 5}
FSCOD = {0: 48000, 1: 44100, 2: 32000}
NUMBLKS = {0: 1, 1: 2, 2: 3, 3: 6}
STRMTYP = {0: "independent", 1: "dependent", 2: "AC-3 converted", 3: "reserved"}


class Bits:
    def __init__(self, data, byte_off=0):
        self.d, self.p = data, byte_off * 8

    def u(self, n):
        v = 0
        for _ in range(n):
            v = (v << 1) | ((self.d[self.p >> 3] >> (7 - (self.p & 7))) & 1)
            self.p += 1
        return v


def parse(data, off):
    """Parse one frame's bsi. Returns a dict, or None if it does not look sane."""
    b = Bits(data, off)
    if b.u(16) != 0x0B77:
        return None
    f = {}
    f["strmtyp"] = b.u(2)
    f["substreamid"] = b.u(3)
    frmsiz = b.u(11)
    f["bytes"] = (frmsiz + 1) * 2
    fscod = b.u(2)
    if fscod == 3:
        b.u(2)                      # fscod2
        f["numblks"] = 6
        f["rate"] = None            # half-rate, not seen in practice here
    else:
        f["numblks"] = NUMBLKS[b.u(2)]
        f["rate"] = FSCOD[fscod]
    acmod = f["acmod"] = b.u(3)
    lfeon = f["lfeon"] = b.u(1)
    f["bsid"] = b.u(5)
    b.u(5)                          # dialnorm
    if b.u(1):
        b.u(8)                      # compr
    if acmod == 0:
        b.u(5)
        if b.u(1):
            b.u(8)
    f["chanmap"] = None
    if f["strmtyp"] == 1:
        if b.u(1):                  # chanmape
            f["chanmap"] = b.u(16)
    if b.u(1):                      # mixmdate
        if acmod > 2:
            b.u(2)                  # dmixmod
        if (acmod & 1) and acmod > 2:
            b.u(3); b.u(3)          # ltrtcmixlev, lorocmixlev
        if acmod & 4:
            b.u(3); b.u(3)          # ltrtsurmixlev, lorosurmixlev
        if lfeon:
            if b.u(1):              # lfemixlevcode
                b.u(5)
        if f["strmtyp"] == 0:
            if b.u(1):
                b.u(6)              # pgmscl
            if acmod == 0:
                if b.u(1):
                    b.u(6)
            if b.u(1):
                b.u(6)              # extpgmscl
            mixdef = b.u(2)
            if mixdef == 1:
                b.u(1); b.u(1); b.u(3)
            elif mixdef == 2:
                b.u(12)
            elif mixdef == 3:
                b.u((b.u(5) + 2) * 8)
            if acmod < 3:
                if b.u(1):
                    b.u(8); b.u(6)
                if acmod == 0:
                    if b.u(1):
                        b.u(8); b.u(6)
            if b.u(1):              # frmmixcfginfoe
                if f["numblks"] == 1:
                    b.u(5)
                else:
                    for _ in range(f["numblks"]):
                        if b.u(1):
                            b.u(5)
    if b.u(1):                      # infomdate
        f["bsmod"] = b.u(3)
        b.u(1); b.u(1)              # copyrightb, origbs
        if acmod == 2:
            b.u(2); b.u(2)          # dsurmod, dheadphonmod
        if acmod >= 6:
            b.u(2)                  # dsurexmod
        if b.u(1):                  # audprodie
            b.u(5); b.u(2); b.u(1)
        if acmod == 0:
            if b.u(1):
                b.u(5); b.u(2); b.u(1)
        if fscod < 3:
            b.u(1)                  # sourcefscod
    if f["strmtyp"] == 0 and f["numblks"] != 6:
        b.u(1)                      # convsync
    if f["strmtyp"] == 2:
        if f["numblks"] != 6 or b.u(1):
            b.u(6)                  # frmsizecod
    f["addbsi"] = b""
    if b.u(1):                      # addbsie
        n = b.u(6) + 1
        f["addbsi"] = bytes(b.u(8) for _ in range(n))
    return f


def main():
    data = open(sys.argv[1], "rb").read()
    print(f"{len(data)} bytes of elementary stream")
    i, frames, bad = 0, [], 0
    while True:
        j = data.find(b"\x0b\x77", i)
        if j < 0 or j + 8 > len(data):
            break
        try:
            f = parse(data, j)
        except (IndexError, KeyError):
            f = None
        if f is None or f["bytes"] < 16 or j + f["bytes"] > len(data) + 4096:
            bad += 1
            i = j + 2
            continue
        frames.append(f)
        i = j + f["bytes"]

    if not frames:
        print("no parseable E-AC-3 frames")
        return 1
    print(f"{len(frames)} frames parsed, {bad} failed resyncs\n")

    groups = Counter((f["strmtyp"], f["substreamid"]) for f in frames)
    print(f"{'substream':<34} {'frames':>7} {'bytes/frm':>10} {'kbps':>7}")
    for (st, sid), n in sorted(groups.items()):
        sub = [f for f in frames if (f["strmtyp"], f["substreamid"]) == (st, sid)]
        sz = Counter(f["bytes"] for f in sub).most_common(1)[0][0]
        blks = sub[0]["numblks"]
        secs = blks * 256 / (sub[0]["rate"] or 48000)
        label = f"{STRMTYP[st]} #{sid}"
        print(f"{label:<34} {n:>7} {sz:>10} {sz * 8 / secs / 1000:>7.0f}")
        a, lfe = sub[0]["acmod"], sub[0]["lfeon"]
        print(f"    acmod {a} ({ACMOD[a]}){'+LFE' if lfe else ''}"
              f"  = {ACMOD_CH[a] + lfe} ch   bsid {sub[0]['bsid']}"
              f"  {blks} blocks  {sub[0]['rate']} Hz")
        cm = [f["chanmap"] for f in sub if f["chanmap"] is not None]
        if cm:
            print(f"    chanmap 0x{cm[0]:04x}")
        add = Counter(f["addbsi"] for f in sub)
        for payload, cnt in add.most_common(3):
            if payload:
                print(f"    addbsi {payload.hex(' ')}  ({cnt} frames)")

    joc = [f for f in frames if f["strmtyp"] == 1 and len(f["addbsi"]) == 1]
    print()
    if joc:
        objs = Counter(f["addbsi"][0] for f in joc)
        print("*** DOLBY ATMOS (JOC) PRESENT ***")
        for v, cnt in objs.most_common():
            print(f"    complexity index {v} -> {v} objects   ({cnt} frames)")
    elif any(f["strmtyp"] == 1 for f in frames):
        print("dependent substream present but no 1-byte addbsi:"
              " DD+ channel extension (7.1), NOT Atmos")
    else:
        print("independent substream only: plain DD+, no Atmos")
    return 0


if __name__ == "__main__":
    sys.exit(main())
