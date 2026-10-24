#!/usr/bin/env python3
"""CEC volume bridge: Khadas VIM3L -> CamillaDSP main fader.

The TV's volume keys reach the VIM3L as CEC <User Control Pressed> because the
board answers as the Audio System at logical address 5. The board forwards them
here as one-line commands and we move CamillaDSP's main volume fader, which is
applied after the pipeline -- after master_safety_comp -- so the compressor
thresholds and the loudness shelves keep referring to the content level rather
than to the listening level. It is also the only volume that covers both
sources, since Kodi and the TV feed both go through CamillaIn.

This process keeps no volume state of its own: every command is a round trip to
CamillaDSP, whose statefile is the one source of truth, so a CamillaDSP restart,
a mode switch (2.0/4.0) or a manual change can never desync it. CamillaDSP does
the clamping too (AdjustVolume takes a min/max), which is why holding the key
down cannot run past the limits.

Protocol, one command per line, replies "<volume 0-100> <mute 0|1>" or "ERR":

    up                raise by VOL_STEP dB
    down              lower by VOL_STEP dB
    mute              toggle mute
    mute 0 | mute 1   set mute
    set <0-100>       absolute, on the CEC scale
    get               current level, for <Give Audio Status>

The reply is what <Report Audio Status> needs: the TV draws its OSD bar from
mute << 7 | volume. Connections may be kept open; the board's daemon is expected
to hold one.
"""
import os
import socket
import socketserver
import sys
import threading
import time

import websocket

CAMILLA_URL = os.environ.get("EARC_VOL_CAMILLA", "ws://127.0.0.1:1234")
LISTEN_HOST = os.environ.get("EARC_VOL_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("EARC_VOL_PORT", "4011"))
# Only the board (and localhost, for testing) may change the volume.
ALLOWED = set(filter(None, os.environ.get(
    "EARC_VOL_ALLOWED", "192.168.15.100,127.0.0.1").split(",")))
# The fader range mapped onto CEC's 0-100. 0 dB is the reference the ZD3 and ZH3
# knobs are set for (~75 dB average dialogue), so it is also the ceiling.
DB_MIN = float(os.environ.get("EARC_VOL_DB_MIN", "-60"))
DB_MAX = float(os.environ.get("EARC_VOL_DB_MAX", "0"))
# Per keypress. 1.5 dB is ~40 presses across the whole range, which matches how
# fast the TV repeats a held key without being coarse.
VOL_STEP = float(os.environ.get("EARC_VOL_STEP", "1.5"))
WS_TIMEOUT = float(os.environ.get("EARC_VOL_WS_TIMEOUT", "2"))

_ws = None
_ws_lock = threading.Lock()


def log(msg):
    print("%s earc-volume: %s" % (time.strftime("%H:%M:%S"), msg),
          file=sys.stderr, flush=True)


def db_to_cec(db):
    span = DB_MAX - DB_MIN
    return max(0, min(100, int(round((db - DB_MIN) / span * 100.0))))


def cec_to_db(value):
    span = DB_MAX - DB_MIN
    return DB_MIN + max(0, min(100, value)) / 100.0 * span


def _command(payload, expect):
    """Send one websocket command, reconnecting once if the link went away."""
    global _ws
    import json

    for attempt in (1, 2):
        try:
            if _ws is None:
                _ws = websocket.create_connection(CAMILLA_URL, timeout=WS_TIMEOUT)
            _ws.send(payload)
            reply = json.loads(_ws.recv())[expect]
            if reply.get("result") != "Ok":
                raise RuntimeError("%s: %s" % (expect, reply))
            return reply.get("value")
        except Exception as exc:
            try:
                if _ws is not None:
                    _ws.close()
            except Exception:
                pass
            _ws = None
            if attempt == 2:
                raise
            log("websocket lost (%s), reconnecting" % exc)
    return None


def handle(line):
    """One command line in, one reply line out."""
    import json

    parts = line.split()
    if not parts:
        return None
    cmd, args = parts[0].lower(), parts[1:]

    with _ws_lock:
        if cmd in ("up", "down"):
            step = VOL_STEP if cmd == "up" else -VOL_STEP
            db = _command(json.dumps({"AdjustVolume": [step, DB_MIN, DB_MAX]}),
                          "AdjustVolume")
            mute = _command('"GetMute"', "GetMute")
        elif cmd == "mute":
            if args:
                want = args[0] not in ("0", "off", "false")
                _command(json.dumps({"SetMute": want}), "SetMute")
                mute = want
            else:
                mute = _command('"ToggleMute"', "ToggleMute")
            db = _command('"GetVolume"', "GetVolume")
        elif cmd == "set" and args:
            db = cec_to_db(int(float(args[0])))
            _command(json.dumps({"SetVolume": db}), "SetVolume")
            mute = _command('"GetMute"', "GetMute")
        elif cmd in ("get", "status"):
            db = _command('"GetVolume"', "GetVolume")
            mute = _command('"GetMute"', "GetMute")
        else:
            return "ERR"

    return "%d %d" % (db_to_cec(db), 1 if mute else 0)


class Handler(socketserver.StreamRequestHandler):
    timeout = 300

    def handle(self):
        peer = self.client_address[0]
        if ALLOWED and peer not in ALLOWED:
            log("refused %s" % peer)
            return
        log("connected %s" % peer)
        try:
            for raw in self.rfile:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    reply = handle(line)
                except Exception as exc:
                    log("%r failed: %s" % (line, exc))
                    reply = "ERR"
                if reply is None:
                    continue
                self.wfile.write((reply + "\n").encode())
                self.wfile.flush()
                log("%s -> %s" % (line, reply))
        except (socket.timeout, ConnectionResetError, BrokenPipeError):
            pass
        log("closed %s" % peer)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    log("listening on %s:%d, fader %.0f..%.0f dB in %.1f dB steps, allowed %s"
        % (LISTEN_HOST, LISTEN_PORT, DB_MIN, DB_MAX, VOL_STEP,
           ",".join(sorted(ALLOWED)) or "any"))
    Server((LISTEN_HOST, LISTEN_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
