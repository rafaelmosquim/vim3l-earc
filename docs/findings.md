# Findings

Everything here was measured on one board and one TV. For eARC, interop is the
whole game, so treat every TV-side observation as n=1 — especially the CDS one.

Test system: Khadas VIM3L (Amlogic S905D3), Ubuntu 22.04.5, kernel 5.15.137
(Khadas BSP, not mainline). One Samsung TV.

## The capture device

```
hw:0,5   EARC/ARC-dummy-alsaPORT-earc          eARC/ARC capture
hw:0,6   LOOPBACK-A-dummy-alsaPORT-loopback    loopback, NOT eARC
```

Khadas' own documentation says eARC capture is on `hw:0,6`. On this image
device 6 is a loopback and eARC is on `hw:0,5`. That detail costs people an
afternoon.

Hardware params on the capture device: channels 1–32, rates 8000–192000,
formats S16/S24/S32_LE.

The driver exports 11 mixer controls — attended type (2 = eARC linked, 1 = fell
back to ARC), audio coding type, sample frequency, word length, channel
allocation, and the read/write CDS block. **They all read zero unless a capture
is actually open**, which is worth knowing before you conclude the link is dead.
`tools/earc-format-watch.sh` opens its own capture for exactly that reason.

## Formats actually received

Counted from the streamer's journal, which logs a line every time it opens the
capture and reads the format registers:

```
journalctl -u earc-streamer --no-pager \
  | grep -o "stream: coding type .*" | sort | uniq -c | sort -rn
```

```
Coding type  Format                       Rate             Link   Events
1            LPCM                         44.1 kHz         eARC    1482
1            LPCM                         48 kHz           eARC      73
9            E-AC-3 (Dolby Digital Plus)  192 kHz carrier  eARC      92
3            AC-3 (Dolby Digital)         48 kHz           eARC       4
```

Two traps in reading that:

* The 192 kHz on the E-AC-3 line is the **IEC 61937 carrier rate** (4 × 48k),
  not a real sample rate.
* Filter on `link eARC`. The much larger `44100 Hz, link ARC` count is not
  content at all — it is what the registers read while the link is down or has
  fallen back to ARC, once per retry of the streamer's open/fail/close loop. It
  is the resting state with the TV off.

Rates above 48 kHz are untested: the CDS advertises up to 192 kHz LPCM and this
TV has only ever sent 44.1 and 48.

## Link behaviour

```
TV on:   IDLE2 -> IDLE1 -> EARC                 about 1 s
TV off:  EARC -> LOSTHB -> TIMEOUT -> IDLE2     under 1 s
```

Negotiation is fast, fallback is clean, and once linked it holds — sessions of
1 m 20 s, 18 m 10 s and 1 h 33 m with no mid-session heartbeat loss; 27
successful negotiations on record.

`attended type` = 1 (ARC) with the TV off is the **expected** resting state, not
a fault. A single LOSTHB about a second after entering EARC, immediately
followed by EARC again, is a benign establishment transient that appears on
every link-up. Only LOSTHB → TIMEOUT means the TV really went away.

## The cold-boot race, and why it is not fixable from the board

CMDC discovery runs **exactly once**, at driver probe during kernel boot
(~+7.3 s here). If the TV is not answering yet you get 11 DISC1/DISC2 cycles,
then `EARCRX_CMDC_TIMEOUT`, then IDLE2 — and nothing on the board restarts it.

Signatures: success is ~4 DISC cycles then `EARCRX_CMDC_EARC` inside a second;
failure is ~11 cycles then TIMEOUT.

Tried, and none of it produces any CMDC activity at all:

* `fake_plug` 0→1 on `/sys/class/amhdmitx/amhdmitx0` — does deliver a plug
  notification and makes CEC re-read the physical address, but zero CMDC.
* `phy` 0→1 — no plug notification at all, only powers the TMDS lines.
* Driver unbind/rebind via `/sys/bus/platform/drivers/EARC` — probe logs
  "HDMITX cable is plugin" and then goes **straight** to IDLE2, never attempting
  DISC.
* CEC `<Request ARC Termination>` 0xC4 — the TV Feature Aborts it every time.
* CEC `<Terminate ARC>` + `<Report ARC Terminated>` unprompted — acked, then
  Feature Aborted, and it kills the ARC audio.

Delaying the boot does not help either: a late probe never even attempts DISC.

**So don't power the board from the TV's USB port.** It cold-boots when the TV
wakes, loses the race, and spends that whole power cycle in ARC. On always-on
power the problem disappears, because while the receiver sits in
`EARCRX_CMDC_EARC` an interrupted link re-discovers by itself, and the TV
re-offers discovery at its next power-on. Six days up across many TV power
cycles after rewiring.

If you test any of this, **stop `earc-streamer` first** and check it is really
inactive. It has `Restart=always`, and on a dead link it loops open → "no data
for 2 s" → close, which toggles the receiver and silently pollutes results.

## You cannot steer the TV by advertising formats

A Dolby-only CDS was loaded (no LPCM advertised). The link did a full
DISC → EARC re-discovery, which is exactly when the TX reads the capability
block, and the TV carried on sending LPCM a minute later. The real lever is the
TV's own "Digital Output Audio Format" menu, not anything the receiver claims.

See [cds.md](cds.md) for the byte format and how to write it.

## No TrueHD, no Atmos — and the reason is upstream of the board

Neither is advertised in the CDS nor received.

The TV does not pass Atmos from its own apps. A 12 s capture off `hw:0,5`
during a TV-app movie, unwrapped and run through `mediainfo`, reports
"Commercial name: **Dolby Digital Plus**" — not "with Dolby Atmos" — L R C LFE
Ls Rs, 640 kb/s CBR, 1536 samples per frame. A syncframe parse
(`tools/eac3-joc.py`) found 375 frames in **one** independent substream and
**zero** dependent substreams.

640 kbps CBR DD+ 5.1 with no JOC is the signature of TV-side **re-encoding**;
streaming Atmos is normally 768 kbps DD+ with JOC. The TV's "Digital Output
Audio Format" was already on Auto, so Auto does not mean passthrough for
internal apps on this set.

This was confirmed twice, the second time with a subscription upgraded so the
app definitely showed an Atmos badge: byte-wise different capture, identical
character. So there is no object metadata arriving for any renderer to render —
the limitation is the TV, not the decoder.

Two pieces of evidence that the **board** is not the limit: the capture device's
hw params cover 8 ch at 192 kHz comfortably, and a CDS advertising MAT was
accepted with the link staying at attended type 2. Whether the receiver can
actually decode MAT is unproven either way; the coding-type register is a plain
integer with no text table.

`mediainfo` is the authoritative JOC detector here. **ffprobe does not report
JOC.**

## ffmpeg and IEC 61937 E-AC-3

Check your ffmpeg version before writing an unwrapper.

* Ubuntu 22.04's ffmpeg **4.4.2** refuses the capture outright:
  `Data type 0x0015 in IEC 61937 is not implemented`.
* E-AC-3 support landed in ffmpeg's spdif demuxer in **6.0** — verified absent
  in 5.1, present in 6.0 and later. On a current build you can demux a captured
  **file** directly and skip custom code entirely.
* Reading **live** from the ALSA device is a separate problem. I could not get
  ffmpeg to do that at all even on a new build, and nor could another member
  testing on an rk3588, so the streaming path still wants its own unwrapper.

That is what `khadas/lib/iec61937-extract.py` is for: find the `72 f8 1f 4e`
sync word, unwrap the bursts, byte-swap each 16-bit word back to stream order,
and pipe the result into `ffmpeg -f eac3`. Note the length field differs between
formats — AC-3's Pd is in **bits**, E-AC-3's is in **bytes**.

## Latency

Method: film the TV screen while recording the room sound in the same clip, so
the clip's own A/V track carries the offset. Standard A/V sync test pattern.

**The LPCM path is already in sync.** Audio onset landed at 0.5422 s (14 beeps,
spread ±3 ms); the visual flash began at roughly 0.543 s. Offset about zero
within a measurement precision of ±15 ms. That is end-to-end for the whole chain
— eARC capture, network hop, jitter buffer, PipeWire, CamillaDSP, USB DACs — so
if you build this, **don't go hunting for latency to remove on the PCM path**;
you will push it early.

The bitstream path costs about 80 ms more, most of it inherent to decoding a
compressed format:

```
Capture period penalty      15 ms
IEC 61937 burst assembly    32 ms   (6144-frame repetition period at the 192 kHz carrier)
One decoder frame           32 ms   (1536 samples at 48 kHz)
-------------------------------------
Added vs LPCM               79 ms
```

Only the first row is recoverable. Do not cut the receiver's prefill to chase
it: that applies to both paths and would push the already-correct LPCM path
early.

CamillaDSP is not a suspect if you use it the way this chain does: chunksize
1024 (21.3 ms) and every filter is Biquad/BiquadCombo, so there is no FIR group
delay to trade away.

## A USB keyboard killed the link

With a keyboard and mouse plugged into the board, the receiver lost its
heartbeat every ~3 s and the TV fell back to ARC continuously. Unplugging them
fixed it outright. Network traffic and mixer polling had already been ruled out.
Run it headless.

## Format changes are not seamless

Switching LPCM → E-AC-3 originally produced 16–26 seconds of loud static. The
cause is worth knowing because it is one-directional and the asymmetry explains
it: the PCM path is a raw passthrough, so it will happily forward an IEC 61937
bitstream onward **as though it were PCM**. The reverse is always clean, because
the bitstream path contains the unwrapper — which finds no sync word in PCM and
emits nothing, a free mute.

Two fixes, both in `earc-streamer.py`:

1. **The startup burst.** A fresh pipeline hands over ~7.2 s of already-captured
   audio at once, and the steady-state quarter cap on shedding bled it out over
   ~16 s of part-real, part-skipped output. `Output.restarting()` now re-anchors
   the clock and opens a 1.5 s window where the excess goes in one go. The
   quarter cap must stay outside that window — unbounded shedding silenced the
   stream when it was tried.
2. **The static.** The main loop no longer forwards audio once the registers
   report a valid format that differs from the running pipeline; it holds silence
   instead. Guarded by a timeout, because muting on disagreement could otherwise
   mute forever if the registers oscillated.

Down to about 3 s of silence, which is the format debounce.

## Bonus: two CEC personas at once

Since the board is on the HDMI bus it answers CEC as an Audio System (logical
address 5), so the TV remote's volume keys reach it.

A TV routes **navigation** keys only to the device at the active source's
physical address, never to the audio system. So the daemon claims a **second**
logical address — Playback Device 1 — and reports the media player's HDMI port
as that persona's physical address; the TV then sends it the nav keys, which get
relayed on to Kodi.

Holding two logical addresses at once is a hardware feature of this controller:
CECB's `DWC_CEC_ADDR_L` is a **bitmask** of accepted addresses, not a value. The
only userspace path to it is the driver's debug interface:

```
echo "addbaddr 4" > /sys/class/cec/dbg          # OR one bit in
grep CEC_ADDR_L /sys/class/cec/dump_reg          # 0x30 = LA 4 and 5 both live
```

Two traps: the `log_addr` sysfs file **replaces** the whole bitmask, so the
`addbaddr` must come after it; and `addaddr` (no b) fixes the bitmask but also
moves the source address the kernel's own replies are built from, dragging the
audio system off LA 5.

A broadcast `<Report Physical Address>` was enough — the TV asked the new
persona no discovery questions and needed no Anynet+ rescan. Adding LA 4 did not
disturb eARC and produced zero kernel log noise.

One wrinkle: this TV shows **no on-screen volume bar**, because it never polls
`<Give Audio Status>` and expects the device to display its own volume. That is
why `earc-oled.py` exists. Looks like a TV-side limitation.

Kodi keys must go through `Input.ButtonEvent`, never `Input.Up`/`Input.Select`.
The `Input.*` methods inject a raw GUI action that a window may silently ignore:
in the fullscreen video window they return "OK" and do nothing at all.

## What has not been measured

* **Bit-exactness of the capture.** The PCM path is a raw `arecord` passthrough
  and there is no reason to think it alters samples, but no bit-compare against
  a known source has been run. Treat it as unverified.
* **No electrical measurements.** No jitter, no THD+N at the capture point.
  Everything here is functional and timing behaviour.
* **One TV.**

## Source for the CEC driver

The CEC driver is Amlogic's own `aocec`, not the Linux CEC framework — hence
`/dev/cec` and `/sys/class/cec/*` rather than `/dev/cec0` and `cec-ctl`. The
installed kernel headers keep only Kconfig and Makefile, so read it at:

```
https://github.com/khadas/common_drivers/tree/khadas-vims-5.15.y/drivers/media/cec/
```

`hdmi_ao_cec.c` has the sysfs attributes including the `dbg` command table (the
only place that command list exists — `/sys/class/cec/dbg` reads back empty),
and `hdmi_aocec_api.c` has `cec_logicaddr_set/add/remove`. Note that
`khadas/linux` does **not** contain `common_drivers/`; it is its own repo.
