#!/usr/bin/env python3
"""Unwrap an IEC 61937 (S/PDIF / HDMI bitstream) capture into a raw elementary stream.

Reads 16-bit little-endian stereo PCM frames from stdin (as captured by arecord
-f S16_LE -c 2), finds the Pa/Pb burst preambles and writes the payload of each
burst to stdout with each 16-bit word byte-swapped back to stream order.
Works for AC-3 (type 1) and E-AC-3 (type 21); pipe the result into
`ffmpeg -f eac3 -i -` (E-AC-3 demuxer also accepts plain AC-3).
"""
import os
import struct
import sys

DUMP = os.environ.get("IEC_DUMP") or "/home/khadas/claude/earc-es-dump.eac3"
DUMP_LIMIT = 1_500_000  # ~19 s at 640 kbps; self-limiting, then it stops

SYNC = b"\x72\xf8\x1f\x4e"  # Pa=0xF872, Pb=0x4E1F as little-endian words
TYPES_LEN_IN_BITS = {1}     # AC-3 burst length (Pd) is in bits; E-AC-3 is in bytes


def main():
    inp, out = sys.stdin.buffer, sys.stdout.buffer
    try:
        dump, dumped = open(DUMP, 'wb'), 0
    except OSError:
        dump, dumped = None, 0
    buf = bytearray()
    warned = set()
    while True:
        chunk = inp.read1(65536)  # return as soon as anything arrives
        if not chunk:
            break
        buf += chunk
        while True:
            i = buf.find(SYNC)
            if i < 0:
                del buf[: max(0, len(buf) - 3)]
                break
            if len(buf) < i + 8:
                del buf[:i]
                break
            pc, pd = struct.unpack_from("<HH", buf, i + 4)
            dtype = pc & 0x1F
            length = pd // 8 if dtype in TYPES_LEN_IN_BITS else pd
            end = i + 8 + length + (length & 1)
            if len(buf) < end:
                del buf[:i]
                break
            if dtype in (1, 21):
                payload = bytearray(buf[i + 8:end])
                payload[0::2], payload[1::2] = payload[1::2], payload[0::2]
                out.write(payload[:length])
                if dump is not None and dumped < DUMP_LIMIT:
                    try:
                        dump.write(payload[:length])
                        dump.flush()
                        dumped += length
                    except OSError:
                        dump = None
            elif dtype not in warned and dtype not in (0, 3):  # 0 = null, 3 = pause
                warned.add(dtype)
                print(f"iec61937: skipping unsupported data type {dtype}", file=sys.stderr)
            del buf[:end]
        out.flush()


if __name__ == "__main__":
    try:
        main()
    except (BrokenPipeError, KeyboardInterrupt):
        # Reader went away (e.g. ffmpeg hit its -t limit); avoid a noisy flush error at exit
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
