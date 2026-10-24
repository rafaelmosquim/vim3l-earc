#!/usr/bin/env python3
"""Show the system volume on the 128x64 OLED (i2c-3, 0x3C) while it is moving.

The panel lights only when the volume actually changes and powers down HOLD
seconds later, so a number cannot sit burning into the pixels overnight.

The HTPC owns the volume -- step size and the 0-100 <-> dB mapping live in
earc-volume.service -- so this asks it rather than keeping a number of its own.
An unreachable HTPC is not a change: the panel stays dark rather than lighting
up to report that it has lost contact.
"""
import os, socket, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ssd1306

FONT = "/usr/share/consolefonts/Lat15-Terminus32x16.psf.gz"   # 16x32
MAX_SCALE = 3     # beyond this the blocks get coarse enough to look ragged

HTPC_HOSTS = ["192.168.15.134", "rafael-server.local"]
HTPC_PORT = 4011
CONNECT_TIMEOUT = 0.4
REPLY_TIMEOUT = 0.3
RETRY_AFTER = 5.0
PERIOD = 0.25          # follows the remote closely; one tiny request per tick
HOLD = 5.0             # how long the number stays lit after it stops moving


class Volume:
    """'get' -> '<0-100> <0|1>'. Kept-open socket, dropped on any timeout: a
    late reply must never be read as the answer to the next request."""

    def __init__(self):
        self._sock = self._rf = None
        self._fail_until = 0.0

    def _close(self):
        for obj in (self._rf, self._sock):
            try:
                if obj is not None:
                    obj.close()
            except OSError:
                pass
        self._sock = self._rf = None

    def get(self):
        if self._sock is None:
            if time.monotonic() < self._fail_until:
                return None
            for host in HTPC_HOSTS:
                try:
                    s = socket.create_connection((host, HTPC_PORT), CONNECT_TIMEOUT)
                except OSError:
                    continue
                s.settimeout(REPLY_TIMEOUT)
                self._sock, self._rf = s, s.makefile("rb")
                break
            else:
                self._fail_until = time.monotonic() + RETRY_AFTER
                return None
        try:
            self._sock.sendall(b"get\n")
            line = self._rf.readline()
            if not line:
                raise OSError("closed")
        except OSError:
            self._close()
            return None
        parts = line.decode(errors="replace").split()
        if len(parts) != 2 or not parts[0].isdigit() or parts[1] not in ("0", "1"):
            return None
        return min(100, int(parts[0])), int(parts[1])


def label(vol):
    return "MUTE" if vol[1] else str(vol[0])


def ink_rows(font, text):
    """First and last glyph row that has any ink. A console font reserves rows
    for ascenders and descenders that digits never use, so centring on the
    glyph box wastes a third of a 64-pixel panel."""
    top, bot = font.h, -1
    for ch in text:
        for r, row in enumerate(font.glyph(ch)):
            if any(row):
                top = min(top, r)
                bot = max(bot, r)
    return (0, font.h - 1) if bot < 0 else (top, bot)


def draw(d, font, text):
    """Largest scale that still fits, so two digits fill the panel and three
    step down one notch rather than being clipped."""
    d.fill(False)
    top, bot = ink_rows(font, text)
    ink_h = bot - top + 1
    scale = max(1, min(MAX_SCALE,
                       ssd1306.WIDTH // (len(text) * font.w),
                       ssd1306.HEIGHT // ink_h))
    w, h = len(text) * font.w * scale, ink_h * scale
    x = max(0, (ssd1306.WIDTH - w) // 2)
    y = (ssd1306.HEIGHT - h) // 2 - top * scale
    d.text(x, y, text, font, scale=scale)


def main():
    d = ssd1306.Display(bus=3, addr=0x3C)
    d.off()                       # dark until there is something to say
    font = ssd1306.Font(FONT)
    src = Volume()
    current = None                # last volume we have actually seen
    lit_until = 0.0
    awake = False

    while True:
        vol = src.get()
        now = time.monotonic()

        if vol is not None and vol != current:
            # First reading after start counts: it proves the panel works.
            draw(d, font, label(vol))
            d.show()
            if not awake:
                d.on()
                awake = True
            current = vol
            lit_until = now + HOLD

        if awake and now >= lit_until:
            d.off()
            awake = False

        time.sleep(PERIOD)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
