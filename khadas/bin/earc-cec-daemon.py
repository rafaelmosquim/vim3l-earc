#!/usr/bin/env python3
"""Minimal CEC Audio System responder for the Khadas VIM3L eARC/ARC input.

Registers as CEC device type 5 (Audio System) at logical address 5, publishes an
eARC capability data structure, and answers the TV's CEC queries so
it keeps routing audio to us. Incoming messages are read from the kernel log
(the Amlogic aocec driver logs "[RX] len: N, msg: .." when dbg_en=1); replies are
sent through /sys/class/cec/cmd. Must run as root.

Volume keys are forwarded to the HTPC, which owns the volume: this daemon keeps
no volume number of its own, it only relays <User Control Pressed> to
earc-volume.service and turns its "<volume> <mute>" reply into the
<Report Audio Status> byte the TV draws its OSD volume bar from.

It also claims a SECOND logical address as a Playback Device standing in for
Kodi on the HTPC, so the TV's remote navigation keys reach Kodi over the
network. See the KODI_LA block below for why that is the only way to get them.
"""
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time

CEC = "/sys/class/cec"
LA = 5
OSD_NAME = "Khadas eARC"
# Short Audio Descriptors (CTA-861)
SAD_LPCM = [0x0F, 0x7F, 0x07]   # LPCM, 8ch, 32-192 kHz, 16/20/24 bit
SAD_AC3 = [0x15, 0x07, 0x50]    # AC-3, 6ch, 32/44.1/48 kHz, max 640 kbps
SAD_EAC3 = [0x57, 0x07, 0x00]   # E-AC-3, 8ch, 32/44.1/48 kHz
SADS = {0x01: SAD_LPCM, 0x02: SAD_AC3, 0x0A: SAD_EAC3}
# eARC CDS: version 1, block id 1, payload len, Audio Data Block + Speaker Allocation (7.1)
_ADB = [0x20 | 9] + SAD_LPCM + SAD_AC3 + SAD_EAC3
_SPK = [0x83, 0x0F, 0x40, 0x00]
CDS = [0x01, 0x01, len(_ADB) + len(_SPK)] + _ADB + _SPK

RX_RE = re.compile(r"\[RX\] len: \d+, msg: ([0-9a-fA-F ]+)")
system_audio = True
# Legacy ARC is stereo only, so it is a fallback, never a choice: accept it only
# after eARC has been absent this long.
ARC_FALLBACK_AFTER = 30.0
# Starts "now", not 0: a fresh daemon must give eARC its grace period rather than
# treat its own startup as 30 s of eARC absence and pin the link to ARC on the
# first C3 (which is exactly what happened on 2026-10-02, twice).
last_earc = time.monotonic()
# Nothing on this board can talk the TV back up into eARC -- not the mixer's
# attended type, not the CDS, not the RX enable switch (tried 2026-10-02), and
# not CEC <Request ARC Termination> 0xC4 either: that was tried here for a day
# and the TV never once acted on it (6 attempts across two stuck-ARC episodes
# on 2026-10-03, no eARC retry after any of them). The nudge is gone. The link
# comes back on its own, or on a TV-side sound-output toggle.

# The HTPC owns the volume (step size and the 0-100 <-> dB mapping live there).
# Line protocol over a kept-open TCP connection: "up"/"down"/"mute"/"mute 0|1"/
# "set <0-100>"/"get" -> "<0-100> <0|1>", or "ERR".
HTPC_HOSTS = ["192.168.15.134", "rafael-server.local"]   # IP first, avahi as fallback
HTPC_PORT = 4011
HTPC_CONNECT_TIMEOUT = 0.4
HTPC_REPLY_TIMEOUT = 0.2     # drop the command rather than lag behind the keys
HTPC_RETRY_AFTER = 5.0       # don't retry a dead HTPC on every repeated key
VOL_QUEUE_MAX = 8            # bound the backlog if the HTPC goes slow
VOL_UNKNOWN = 0x7F           # <Report Audio Status>: volume unknown
# <User Control Pressed> params we act on; everything else is ignored.
KEY_CMDS = {0x41: "up", 0x42: "down", 0x43: "mute"}

# A TV routes navigation keys to the device at the ACTIVE SOURCE's physical
# address, and never to the audio system: across three days of journal this one
# sent LA 5 nothing but 0x41/0x42/0x43. Kodi runs on the HTPC, on the TV's
# HDMI 4 (4.0.0.0), whose GPU has no CEC at all, so nothing answers there and
# the keys fall on the floor. So claim a SECOND logical address, Playback
# Device 1, and report HDMI 4 as its physical address: the TV then has a CEC
# device on the port it is already showing, and sends that device the keys. The
# audio system persona (LA 5 at 3.0.0.0, our real port) is left alone and keeps
# eARC and volume -- the two share only the bus.
#
# Holding two logical addresses at once is a hardware feature of this CEC
# controller: CECB's DWC_CEC_ADDR_L is a bitmask of accepted addresses, not a
# value. The only userspace path to it is the driver's debug interface, where
# `addbaddr N` ORs one bit in (cec_logicaddr_add). Do NOT use the log_addr
# sysfs file, which REPLACES the whole bitmask (cec_logicaddr_set), and do NOT
# use `addaddr`, which does the right thing to the bitmask but also moves
# cec_info.log_addr -- the source address the kernel's own replies are built
# from, i.e. it would drag the audio system off LA 5.
KODI_LA = 4                   # Playback Device 1
KODI_PA = (0x40, 0x00)        # 4.0.0.0: the TV's HDMI 4, where the HTPC is
KODI_DEV_TYPE = 0x04          # <Report Physical Address> device type: playback
KODI_OSD_NAME = "Kodi"
# Kodi's JSON-RPC over raw TCP: no HTTP framing and no auth, on a kept-open
# connection, so a keypress costs one small write.
KODI_HOST = "192.168.15.134"
KODI_PORT = 9090
KODI_CONNECT_TIMEOUT = 0.4
KODI_RETRY_AFTER = 5.0        # don't redial a dead Kodi on every repeated key
KEY_QUEUE_MAX = 16
# Keys go through Input.ButtonEvent, NOT Input.Up/Select/Back. The Input.*
# methods inject a raw GUI move action, and a window is free to ignore it:
# in Fullscreen video they are accepted, return "OK", and do absolutely
# nothing -- measured 2026-10-03, the player clock did not move by a single
# millisecond. ButtonEvent instead feeds the button through Kodi's keymap, so
# every key means per window what it means on a real remote: arrows navigate a
# menu but seek during playback, select opens the OSD, back closes it.
KODI_KEYMAP = "R1"            # Kodi's remote keymap (KB would be the keyboard)
# <User Control Pressed> params -> Kodi remote button (CEC 1.4 table 27).
# An unknown button name is rejected with -32602, so these are validated names.
KODI_KEYS = {
    0x00: "select",
    0x01: "up",
    0x02: "down",
    0x03: "left",
    0x04: "right",
    0x0D: "back",             # Samsung's RETURN key
}
# cmd is a single sysfs file and three threads write it (CEC loop, link watcher,
# volume worker), so serialise it.
tx_lock = threading.Lock()
vol_q = queue.Queue(VOL_QUEUE_MAX)
key_q = queue.Queue(KEY_QUEUE_MAX)


def attended():
    """eARC RX link state: 0 DISCONNECT, 1 ARC, 2 eARC."""
    out = subprocess.run(["amixer", "-c0", "cget", "name=eARC_RX attended type"],
                         capture_output=True, text=True).stdout
    for line in out.splitlines():
        if ": values=" in line:
            try:
                return int(line.split("=", 1)[1].split(",")[0], 0)
            except ValueError:
                break
    return 0


def log(*a):
    print(*a, flush=True)


def wr(path, val):
    with open(f"{CEC}/{path}", "w") as f:
        f.write(val)


def send(*b):
    msg = " ".join(f"{x:02X}" for x in b)
    with tx_lock:
        log("TX", msg)
        try:
            wr("cmd", msg)
        except OSError as e:
            log("TX failed:", e)
        time.sleep(0.05)


def phys_addr():
    pa = open(f"{CEC}/physical_addr").read().strip().zfill(4)
    return int(pa[:2], 16), int(pa[2:], 16)


def bcast(*b):
    send((LA << 4) | 0xF, *b)


def from_kodi(dst, *b):
    """Send as the playback persona, whose source nibble is KODI_LA, not LA."""
    send((KODI_LA << 4) | dst, *b)


def setup():
    wr("dbg_en", "1")
    wr("device_type", "5")
    wr("log_addr", str(LA))
    # Order matters: log_addr replaces the controller's address bitmask, so the
    # second address has to be added after it, never before.
    wr("dbg", f"addbaddr {KODI_LA}")
    cds = ",".join(f"0x{x:02x}" for x in CDS + [0] * (256 - len(CDS)))
    subprocess.run(["amixer", "-q", "-c0", "cset", "name=eARC_RX CDS", cds], check=False)
    subprocess.run(["amixer", "-q", "-c0", "cset", "name=ARC eARC RX enable", "on"], check=False)
    subprocess.run(["amixer", "-q", "-c0", "cset", "name=eARC RX ARC Switch", "on"], check=False)
    hi, lo = phys_addr()
    bcast(0x84, hi, lo, 0x05)              # Report Physical Address
    bcast(0x72, 0x01 if system_audio else 0x00)  # Set System Audio Mode
    # Put the playback persona on the bus so the TV lists it on HDMI 4. This is
    # a Report Physical Address and nothing more: broadcasting <Active Source>
    # here would make the TV switch inputs on every daemon restart.
    from_kodi(0xF, 0x84, *KODI_PA, KODI_DEV_TYPE)


class Volume:
    """Line-protocol client for earc-volume.service on the HTPC.

    The connection is kept open between commands, and dropped on any timeout:
    a reply that arrives late must never be read as the answer to the next
    command. Nothing here is cached -- every answer comes from the HTPC.
    """

    def __init__(self):
        self._sock = None
        self._rf = None
        self._fail_until = 0.0

    def _close(self):
        for obj in (self._rf, self._sock):
            try:
                if obj is not None:
                    obj.close()
            except OSError:
                pass
        self._sock = self._rf = None

    def _connect(self):
        err = None
        for host in HTPC_HOSTS:
            try:
                sock = socket.create_connection((host, HTPC_PORT),
                                                HTPC_CONNECT_TIMEOUT)
            except OSError as e:
                err = e
                continue
            sock.settimeout(HTPC_REPLY_TIMEOUT)
            self._sock, self._rf = sock, sock.makefile("rb")
            log(f"volume: connected to {host}:{HTPC_PORT}")
            return
        raise err

    def _once(self, cmd):
        self._sock.sendall((cmd + "\n").encode())
        line = self._rf.readline()
        if not line:
            raise OSError("connection closed by the HTPC")
        return line.decode(errors="replace").strip()

    def _talk(self, cmd):
        retry_ok = self._sock is not None   # a kept-open socket may be stale
        while True:
            if self._sock is None:
                if time.monotonic() < self._fail_until:
                    return None
                try:
                    self._connect()
                except OSError as e:
                    self._fail_until = time.monotonic() + HTPC_RETRY_AFTER
                    log(f"volume: cannot reach the HTPC: {e}")
                    return None
            try:
                return self._once(cmd)
            except socket.timeout:
                log(f"volume: no reply to {cmd!r} in"
                    f" {HTPC_REPLY_TIMEOUT * 1000:.0f} ms, dropping it")
                self._close()
                return None
            except OSError as e:
                self._close()
                if not retry_ok:
                    log(f"volume: {cmd!r} failed: {e}")
                    return None
                retry_ok = False            # reconnect and send it once more

    def command(self, cmd):
        """Run one command; return (volume, mute) or None if there is no answer."""
        reply = self._talk(cmd)
        if reply is None:
            return None
        parts = reply.split()
        if len(parts) != 2 or not parts[0].isdigit() or parts[1] not in ("0", "1"):
            log(f"volume: {cmd!r} -> {reply!r}")
            return None
        return min(100, int(parts[0])), int(parts[1])


def volume_worker():
    """Talk to the HTPC off the CEC read loop, and report what it answers.

    The TV repeats <User Control Pressed> while a key is held; each one is
    forwarded as it arrives (no local accumulation) and each reply produces an
    unsolicited <Report Audio Status>, which is what Samsung animates its volume
    bar from.
    """
    vol = Volume()
    while True:
        cmd, reply_to = vol_q.get()
        try:
            state = vol.command(cmd)
            if state is None:
                if reply_to is not None:   # answer a query, don't guess a number
                    send((LA << 4) | reply_to, 0x7A, VOL_UNKNOWN)
                continue
            level, mute = state
            log(f"volume: {cmd} -> {level}{' muted' if mute else ''}")
            dst = 0x0 if reply_to is None else reply_to
            send((LA << 4) | dst, 0x7A, (mute << 7) | level)
        except Exception as e:                # keep the worker alive
            log("volume_worker:", e)


def volume_cmd(cmd, reply_to=None):
    """Queue a command for the worker. Never blocks the CEC read loop."""
    try:
        vol_q.put_nowait((cmd, reply_to))
    except queue.Full:
        log(f"volume: backlog full, dropping {cmd!r}")


class Kodi:
    """Fire-and-forget JSON-RPC client for Kodi on the HTPC.

    Nothing here needs Kodi's answer: unlike the volume path, which has to turn
    a reply into <Report Audio Status>, a navigation key has nothing to report
    back to the TV. So a key is written and forgotten.

    Reading still matters, though. Kodi pushes its event notifications to every
    connected JSON-RPC client, so this socket receives a steady trickle of
    Player.OnSeek and friends whether we asked for anything or not. Left
    unread, that eventually fills the receive buffer and Kodi blocks writing to
    us, so the worker drains it on idle as well as on every key. The bytes are
    discarded unparsed, but scanned for "error" -- the one thing worth knowing
    about is a key Kodi refused, which would otherwise fail silently.
    """

    def __init__(self):
        self._sock = None
        self._fail_until = 0.0
        self._id = 0

    def _close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None

    def _connect(self):
        sock = socket.create_connection((KODI_HOST, KODI_PORT),
                                        KODI_CONNECT_TIMEOUT)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setblocking(False)
        self._sock = sock
        log(f"kodi: connected to {KODI_HOST}:{KODI_PORT}")

    def _drain(self):
        """Discard whatever Kodi has sent; b"" means it hung up."""
        while True:
            try:
                chunk = self._sock.recv(65536)
                if chunk == b"":
                    raise OSError("kodi closed the connection")
                if b'"error"' in chunk:
                    log("kodi: rejected a request:",
                        chunk.decode(errors="replace")[:200])
            except BlockingIOError:
                return

    def idle(self):
        """Keep the connection drained between keys. Never reconnects: with no
        key to deliver there is nothing to reconnect for."""
        if self._sock is None:
            return
        try:
            self._drain()
        except OSError as e:
            log(f"kodi: connection dropped: {e}")
            self._close()

    def press(self, button):
        now = time.monotonic()
        if self._sock is None and now < self._fail_until:
            return
        retry_ok = True
        while True:
            try:
                if self._sock is None:
                    self._connect()
                self._drain()
                self._id += 1
                req = {"jsonrpc": "2.0", "id": self._id,
                       "method": "Input.ButtonEvent",
                       "params": {"button": button, "keymap": KODI_KEYMAP}}
                self._sock.sendall(json.dumps(req).encode())
                return
            except OSError as e:
                self._close()
                if not retry_ok:
                    self._fail_until = time.monotonic() + KODI_RETRY_AFTER
                    log(f"kodi: {button} failed: {e}")
                    return
                retry_ok = False        # reconnect and send it once more


def key_worker():
    """Relay keys off the CEC read loop, so a slow HTPC never stalls CEC."""
    kodi = Kodi()
    while True:
        try:
            button = key_q.get(timeout=0.2)
        except queue.Empty:
            kodi.idle()                 # so Kodi never blocks writing to us
            continue
        try:
            log("kodi:", button)
            kodi.press(button)
        except Exception as e:          # keep the worker alive
            log("key_worker:", e)


def key_cmd(button):
    """Queue a key for the worker. Never blocks the CEC read loop."""
    try:
        key_q.put_nowait(button)
    except queue.Full:
        log(f"kodi: backlog full, dropping {button}")


def handle_kodi(src, op, args):
    """The playback persona on LA 4: answer discovery, relay navigation keys.

    It deliberately says nothing about audio, ARC or standby. Those belong to
    the audio system on LA 5, and a second opinion on them from a second
    logical address is exactly how this would break eARC.
    """
    if op == 0x83:                                   # Give Physical Address
        from_kodi(0xF, 0x84, *KODI_PA, KODI_DEV_TYPE)
    elif op == 0x46:                                 # Give OSD Name
        from_kodi(src, 0x47, *KODI_OSD_NAME.encode()[:14])
    elif op == 0x8C:                                 # Give Device Vendor ID
        from_kodi(0xF, 0x87, 0x00, 0x00, 0x00)
    elif op == 0x9F:                                 # Get CEC Version
        from_kodi(src, 0x9E, 0x05)
    elif op == 0x8F:                                 # Give Device Power Status
        from_kodi(src, 0x90, 0x00)                   # on: Kodi is always up
    elif op == 0x8D:                                 # Menu Request
        # Reporting the menu ACTIVE is what tells a TV that its remote should
        # be driving this device. args[0] 0x02 only queries, 0x00/0x01 ask us
        # to switch it on or off; we are a remote-controlled menu either way.
        from_kodi(src, 0x8E, 0x00)                   # Menu Status: activated
    elif op == 0x44:                                 # User Control Pressed
        button = KODI_KEYS.get(args[0]) if args else None
        if button:
            key_cmd(button)
    elif op == 0x45:                                 # User Control Released
        pass                                         # Kodi does its own repeat
    elif op in (0x00, 0x90, 0x9E, 0x47, 0x84, 0x87, 0x8E):
        pass                                         # reports: nothing to do
    else:
        from_kodi(src, 0x00, op, 0x00)               # Feature Abort


def watch_link():
    """Keep last_earc honest, independently of when the TV happens to send C3.

    Updating it only inside the C3 handler meant a transient ARC reading during
    renegotiation looked like a sustained absence of eARC, so the fallback fired
    while the link was still settling and dragged it down to stereo.
    """
    global last_earc
    while True:
        try:
            if attended() == 2:
                last_earc = time.monotonic()
        except Exception as e:
            log("watch_link:", e)
        time.sleep(2.0)


def handle(msg):
    global system_audio, last_earc
    if len(msg) < 2:
        return
    src, dst = msg[0] >> 4, msg[0] & 0xF
    if dst not in (LA, KODI_LA, 0xF) or src in (LA, KODI_LA):
        return
    if dst == KODI_LA:
        handle_kodi(src, msg[1], msg[2:])
        return
    op, args = msg[1], msg[2:]
    directed = dst == LA
    hi, lo = phys_addr()

    if op == 0x83:                                   # Give Physical Address
        bcast(0x84, hi, lo, 0x05)
    elif op == 0x46 and directed:                    # Give OSD Name
        send((LA << 4) | src, 0x47, *OSD_NAME.encode()[:14])
    elif op == 0x8C and directed:                    # Give Device Vendor ID
        bcast(0x87, 0x00, 0x00, 0x00)
    elif op == 0x9F and directed:                    # Get CEC Version
        send((LA << 4) | src, 0x9E, 0x05)
    elif op == 0x8F and directed:                    # Give Device Power Status
        send((LA << 4) | src, 0x90, 0x00)
    elif op == 0x70 and directed:                    # System Audio Mode Request
        system_audio = True
        bcast(0x72, 0x01)
    elif op == 0x7D and directed:                    # Give System Audio Mode Status
        send((LA << 4) | src, 0x7E, 0x01 if system_audio else 0x00)
    elif op == 0x71 and directed:                    # Give Audio Status
        volume_cmd("get", reply_to=src)              # worker answers 0x7A
    elif op == 0x44 and directed:                    # User Control Pressed
        cmd = KEY_CMDS.get(args[0]) if args else None
        if cmd:
            volume_cmd(cmd)                          # worker answers 0x7A
    elif op == 0xA4 and directed:                    # Request Short Audio Descriptor
        sads = [b for a in args if (a & 0x3F) in SADS for b in SADS[a & 0x3F]]
        if sads:
            send((LA << 4) | src, 0xA3, *sads)
        else:
            send((LA << 4) | src, 0x00, 0xA4, 0x03)  # Feature Abort: invalid operand
    elif op == 0xC3 and directed:                    # Request ARC Initiation
        # This TV asks every 5 s whether to start LEGACY ARC, which carries
        # stereo at best. Answering it while eARC is up makes the TV switch down:
        # on 2026-10-02 a working eARC link died 3 s after we replied C0/C1 here.
        # But never answering is just as bad -- the TV then starts nothing at all
        # and the input stays silent forever. So prefer eARC, and take ARC only
        # once eARC has been gone a while.
        att = attended()
        if att == 2:
            last_earc = time.monotonic()
        if att == 2 or time.monotonic() - last_earc < ARC_FALLBACK_AFTER:
            log(f"C3: not answering, link is {att} (holding out for eARC)")
        else:
            log("C3: no eARC for %.0fs, accepting legacy ARC (stereo)"
                % ARC_FALLBACK_AFTER)
            send((LA << 4) | src, 0xC0)              # Initiate ARC
            send((LA << 4) | src, 0xC1)              # Report ARC Initiated
    elif op == 0xC4 and directed:                    # Request ARC Termination
        # The TV sends this when it is moving the link UP to eARC. Feature
        # Aborting it (the old default path) left the TV stuck on ARC, so agree.
        if attended() == 2:
            last_earc = time.monotonic()
        send((LA << 4) | src, 0xC5)                  # Terminate ARC
        send((LA << 4) | src, 0xC2)                  # Report ARC Terminated
    elif op == 0xC0 and directed:                    # Initiate ARC
        send((LA << 4) | src, 0xC1)                  # Report ARC Initiated
    elif op == 0xC5 and directed:                    # Terminate ARC
        send((LA << 4) | src, 0xC2)                  # Report ARC Terminated
    elif op in (0x86, 0x80) and not directed:        # Set Stream Path / Routing
        # Broadcast, so it lands here rather than in handle_kodi. The target is
        # the last two operands either way (0x80 carries the old address too).
        if tuple(args[-2:]) == KODI_PA:
            from_kodi(0xF, 0x82, *KODI_PA)           # Active Source: that's us
    elif op == 0x36:                                 # Standby
        pass
    elif op in (0x45, 0x00, 0x72, 0x7E, 0x82, 0x84, 0x87, 0x90, 0x9E, 0x47, 0xA3, 0xC1, 0xC2, 0xA7, 0xA8):
        pass                                         # key release / reports: nothing to do
    elif directed:
        send((LA << 4) | src, 0x00, op, 0x00)        # Feature Abort: unrecognized opcode


def main():
    setup()
    threading.Thread(target=watch_link, daemon=True).start()
    threading.Thread(target=volume_worker, daemon=True).start()
    threading.Thread(target=key_worker, daemon=True).start()
    # Follow new kernel log records only (SEEK_END skips the existing backlog)
    fd = os.open("/dev/kmsg", os.O_RDONLY)
    os.lseek(fd, 0, os.SEEK_END)
    while True:
        try:
            line = os.read(fd, 8192).decode(errors="replace")
        except BrokenPipeError:  # records overwritten while we were slow
            continue
        m = RX_RE.search(line)
        if not m:
            continue
        msg = [int(x, 16) for x in m.group(1).split()]
        log("RX", " ".join(f"{x:02X}" for x in msg))
        try:
            handle(msg)
        except Exception as e:  # keep running on bad input
            log("handler error:", e)


if __name__ == "__main__":
    sys.exit(main())
