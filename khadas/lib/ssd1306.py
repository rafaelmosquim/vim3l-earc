#!/usr/bin/env python3
"""Minimal SSD1306 driver over /dev/i2c-N. Standard library only.

The board has no pip, and a status display does not need a rendering stack.
This talks to the panel through i2c-dev directly: ioctl(I2C_SLAVE) to pick the
address, then writes prefixed with a control byte -- 0x00 for commands, 0x40
for frame data.
"""
import fcntl, os

I2C_SLAVE = 0x0703
WIDTH, HEIGHT = 128, 64
PAGES = HEIGHT // 8

INIT = [
    0xAE,                    # display off
    0xD5, 0x80,              # clock divide / oscillator
    0xA8, HEIGHT - 1,        # multiplex ratio
    0xD3, 0x00,              # display offset
    0x40,                    # start line 0
    0x8D, 0x14,              # charge pump on (0x10 if externally supplied)
    0x20, 0x00,              # horizontal addressing mode
    0xA1,                    # segment remap (column 127 -> SEG0)
    0xC8,                    # COM scan direction, flipped
    0xDA, 0x12,              # COM pin configuration, alternative
    0x81, 0x7F,              # contrast
    0xD9, 0xF1,              # pre-charge period
    0xDB, 0x40,              # VCOMH deselect level
    0xA4,                    # resume from RAM
    0xA6,                    # normal (not inverted)
    0xAF,                    # display on
]


class Display:
    def __init__(self, bus=3, addr=0x3C):
        self.fd = os.open(f"/dev/i2c-{bus}", os.O_RDWR)
        fcntl.ioctl(self.fd, I2C_SLAVE, addr)
        self.buf = bytearray(WIDTH * PAGES)
        for c in INIT:
            self.cmd(c)
        self.show()

    def cmd(self, *vals):
        os.write(self.fd, bytes([0x00]) + bytes(vals))

    def show(self):
        self.cmd(0x21, 0, WIDTH - 1)      # column range
        self.cmd(0x22, 0, PAGES - 1)      # page range
        # i2c-dev transfers are bounded; a page at a time is well inside it
        for i in range(0, len(self.buf), WIDTH):
            os.write(self.fd, bytes([0x40]) + bytes(self.buf[i:i + WIDTH]))

    def fill(self, on=True):
        for i in range(len(self.buf)):
            self.buf[i] = 0xFF if on else 0x00

    def pixel(self, x, y, on=True):
        if 0 <= x < WIDTH and 0 <= y < HEIGHT:
            i = (y // 8) * WIDTH + x
            if on:
                self.buf[i] |= 1 << (y & 7)
            else:
                self.buf[i] &= ~(1 << (y & 7)) & 0xFF

    def on(self):
        """Panel on. RAM survives an off, so this re-shows the last frame."""
        self.cmd(0xAF)

    def off(self):
        """Panel off: the controller idles and nothing is lit, which both saves
        power and keeps a static number from burning into the pixels."""
        self.cmd(0xAE)

    def close(self):
        os.close(self.fd)


if __name__ == "__main__":
    import math, time
    d = Display()
    print("init ok")
    d.fill(True); d.show(); print("all pixels on");  time.sleep(1.5)
    d.fill(False); d.show(); print("cleared");       time.sleep(0.5)
    for x in range(WIDTH):                            # a sine, to prove addressing
        d.pixel(x, int(32 + 28 * math.sin(x / 10.0)))
    for x in range(0, WIDTH, 2):
        d.pixel(x, 0); d.pixel(x, HEIGHT - 1)
    d.show(); print("pattern drawn -- look at the screen")


# --- text ------------------------------------------------------------------
# No pip on this board, so rather than ship a font table we parse one of the
# console fonts already installed. PSF is a trivial format: a short header,
# then one fixed-size bitmap per glyph, rows padded to whole bytes.

import gzip, struct


class Font:
    def __init__(self, path):
        op = gzip.open if path.endswith(".gz") else open
        with op(path, "rb") as f:
            d = f.read()
        if d[:2] == b"\x36\x04":                      # PSF1
            self.h = d[3]
            self.w = 8
            self.n = 512 if d[2] & 1 else 256
            off = 4
        elif d[:4] == b"\x72\xb5\x4a\x86":            # PSF2
            _, off, _, self.n, charsize, self.h, self.w = struct.unpack_from("<7I", d, 4)
        else:
            raise ValueError(f"{path}: not a PSF font")
        self.stride = (self.w + 7) // 8
        self.data = d[off:]

    def glyph(self, ch):
        i = ord(ch)
        if i >= self.n:
            i = ord("?")
        base = i * self.stride * self.h
        return [self.data[base + r * self.stride:base + (r + 1) * self.stride]
                for r in range(self.h)]


def _text(disp, x, y, s, font, on=True, scale=1):
    """Draw s at (x, y). scale>1 expands each glyph pixel into a square block,
    which is how a 16x32 console font becomes legible across the whole panel."""
    for ch in s:
        rows = font.glyph(ch)
        for ry, row in enumerate(rows):
            for rx in range(font.w):
                if row[rx >> 3] & (0x80 >> (rx & 7)):
                    for dy in range(scale):
                        for dx in range(scale):
                            disp.pixel(x + rx * scale + dx, y + ry * scale + dy, on)
        x += font.w * scale
    return x


Display.text = _text


def _render(disp):
    """ASCII-art dump of the frame buffer, for checking output without eyes on."""
    out = []
    for y in range(HEIGHT):
        out.append("".join(
            "#" if disp.buf[(y // 8) * WIDTH + x] & (1 << (y & 7)) else "."
            for x in range(WIDTH)))
    return "\n".join(out)


Display.render = _render
