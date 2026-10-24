# vim3l-earc — an HDMI eARC audio receiver on a $70 SBC

Almost nothing in the hobbyist price range will take an HDMI audio stream *in*.
Capture cards do video. AVRs do eARC but will not hand you the PCM.

The Amlogic S905D3 in the **Khadas VIM3L** has a real eARC/ARC receiver block in
silicon, and the vendor BSP kernel exposes it as an ordinary ALSA capture
device. That makes a DIY "TV → DSP → multichannel DAC" chain possible with no
AVR in the path.

This is the working code for one such chain, plus [what was actually
measured](docs/findings.md) getting there — including where it falls down.

```
 TV ──eARC──> VIM3L ──TCP 4010──> PC ──> CamillaDSP ──> USB DACs
                 │   5.1 s16le 48k      (PipeWire)
                 └──CEC──> volume keys ──TCP 4011──> CamillaDSP fader
                      └──> nav keys ────TCP 9090───> Kodi
```

The board captures whatever the TV sends over eARC, follows format changes, and
serves **one fixed, continuous stream**: raw PCM, s16le, 48 kHz, 6 channels,
FL FR FC LFE SL SR. Stereo sources land on FL/FR. While the input is switching
or absent it sends silence in real time, so the stream never stalls and the
receiving DSP never sees a gap.

## Status

In daily use since early October 2026, feeding a 4.0 CamillaDSP setup.

It works, and the timing on the PCM path is good enough that I stopped looking
for problems there. **The cost is software**: this is a vendor BSP kernel,
undocumented device numbering, and scripts you have to write — including your
own IEC 61937 unwrapper. Set your expectations by [the
limitations](docs/findings.md) and it earns its place.

The headline ones, before you buy a board for this:

* **No TrueHD and no Atmos.** Not advertised in the capability block, not
  received. On the TV tested, Atmos-badged app content arrives **re-encoded** to
  plain DD+ 5.1 with no object metadata at all, so this is a TV limitation
  upstream of any decoder. If lossless or height channels matter, a local player
  feeding your DSP is the better path.
* **Do not power the board from the TV's USB port.** eARC discovery runs exactly
  once, at kernel boot, and nothing in userspace can restart it — so a board
  that cold-boots with the TV loses the race and spends that power cycle in ARC.
* **Run it headless.** A USB keyboard plugged into the board killed the eARC
  heartbeat every 3 seconds.
* **One board, one TV (Samsung).** For eARC, interop is the whole game. Treat
  every TV-side observation here as n=1.

## Hardware

* Khadas VIM3L (Amlogic S905D3), Ubuntu 22.04.5, kernel 5.15.137 — the Khadas
  BSP kernel, **not** mainline. Mainline does not expose this.
* `ffmpeg` and `alsa-utils` on the board; `mediainfo` if you want to inspect
  bitstreams.
* Optional: an SSD1306 128×64 OLED on i2c-3 for a volume readout.
* A PC for the DSP end. The receiver there wants `numpy` (it degrades to plain
  cuts instead of fades without it) and PipeWire.

Note the capture device is **`hw:0,5`** on this image. Khadas' own documentation
says `hw:0,6`; on this image device 6 is a loopback. That detail costs people an
afternoon.

## Install

Board side:

```sh
git clone https://github.com/<you>/vim3l-earc
cd vim3l-earc
sudo bash install.sh              # or --no-oled
```

PC side, **without** sudo (they are user services, they need the session's
PipeWire):

```sh
cd htpc && ./install.sh
```

Then check it:

```sh
systemctl status earc-cec earc-streamer
journalctl -u earc-streamer -f
```

### Things you must change for your own setup

The board-side scripts have my LAN addresses compiled in. These are the only
three places:

| File | Line | What |
|------|------|------|
| `khadas/bin/earc-cec-daemon.py` | 61 | `HTPC_HOSTS` — where to forward CEC volume keys |
| `khadas/bin/earc-cec-daemon.py` | 95 | `KODI_HOST` — where to forward CEC nav keys |
| `khadas/lib/earc-oled.py` | 20 | `HTPC_HOSTS` — where the OLED reads the volume from |

Also in `earc-cec-daemon.py`: `KODI_PA` is the physical address of the HDMI port
your media player is on (`0x40,0x00` = HDMI 4). The PC side already reads its
settings from the environment — `EARC_HOST` is the board's address, see
`htpc/systemd/earc-in.service.d/latency.conf` for how the units override things.

## What is here

```
khadas/bin/earc-cec-daemon.py   CEC Audio System (LA 5) + Playback Device (LA 4);
                                sets the CDS at boot, relays volume and nav keys
khadas/lib/earc-streamer.py     the main service: capture, format following,
                                decode, 5.1 fan-out on TCP 4010
khadas/lib/iec61937-extract.py  IEC 61937 burst unwrapper (AC-3 / E-AC-3) -> ES
khadas/lib/earc-oled.py         volume on an SSD1306, lit only while it moves
khadas/lib/ssd1306.py           minimal I2C panel driver, no dependencies
khadas/systemd/                 the three units

htpc/bin/earc-recv.py           jitter-buffered receiver; a plain socat|pw-cat
                                crackles, and the docstring says why
htpc/bin/earc-in.sh             receiver -> pw-cat -> CamillaDSP sink
htpc/bin/earc-volume.py         CEC volume -> CamillaDSP main fader, TCP 4011
htpc/bin/earc-fix               my own deploy helper; needs an interactive sudo
htpc/systemd/                   user units and the latency drop-in

tools/earc-cds.sh               read/write the capability block (presets)
tools/earc-capture.sh           one-shot capture to WAV, format auto-detected
tools/earc-format-watch.sh      print format changes as the TV makes them
tools/arc-audio-system.sh       bring ARC up by hand via CEC, then test-capture
tools/eac3-joc.py               what an E-AC-3 stream contains, and whether it
                                carries Atmos (JOC) per ETSI TS 103 420
tools/earc-chanmap-probe.py     catch the next 8ch LPCM session and log the
                                channel order
tools/pcmdelay.py               measure an ALSA playback device's real latency
tools/pipe-fill.py              standing fill of the pipeline's kernel pipes

tests/                          click/pop regression tests; see tests/README
docs/findings.md                everything that was measured, and the traps
docs/cds.md                     how to decode and write the eARC CDS by hand
```

`tools/earc-format-watch.sh` and `tools/earc-capture.sh` both want `hw:0,5`
exclusively, so stop `earc-streamer` before using them.

## Quick things worth knowing

The format registers read **zero unless a capture is open**, so a dead-looking
link may just be an idle one:

```sh
amixer -c0 cget "name=eARC_RX attended type"     # 2 = eARC, 1 = fell back to ARC
```

What the TV has actually been sending, from the streamer's own log:

```sh
journalctl -u earc-streamer --no-pager \
  | grep -o "stream: coding type .*" | sort | uniq -c | sort -rn
```

Filter that on `link eARC` — the big `link ARC` count is the idle retry loop,
not content. And a coding type 9 at "192000 Hz" is the IEC 61937 carrier rate
(4 × 48k), not a real sample rate.

The capability block the TV reads is a writable mixer control, decoded byte by
byte in [docs/cds.md](docs/cds.md):

```sh
tools/earc-cds.sh show
```

## Tests

```sh
python3 tests/test_output_e2e.py       # defaults to the INSTALLED streamer
python3 tests/test_output_e2e.py khadas/lib/earc-streamer.py
```

Rows marked `FAIL   old behaviour for comparison` are expected — they assert the
bug is still reproducible, and the suite's exit status ignores them. See
`tests/README`, including why the tones are 997 Hz and not 1 kHz.

## License

MIT. See [LICENSE](LICENSE).
