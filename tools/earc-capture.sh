#!/bin/bash
# Capture audio from the eARC input (hw:0,5), auto-detecting the stream format.
#   LPCM          -> recorded as-is (2 or 8 channels)
#   AC-3 / E-AC-3 -> IEC 61937 bitstream, decoded to multichannel PCM by ffmpeg
# Usage: earc-capture.sh [output.wav|-] [seconds]
#   Output defaults to earc.wav; "-" writes WAV to stdout. Seconds omitted = until Ctrl+C.
set -u
DEV=hw:0,5
OUT=${1:-earc.wav}
SECS=${2:-}

ctl() { amixer -c0 cget name="$1" | sed -n 's/.*: values=//p'; }

# The receiver only reports the stream format while the PCM is open, so probe briefly.
arecord -q -D $DEV -f S16_LE -c2 -r48000 -t raw /dev/null 2>/dev/null &
PROBE=$!
sleep 1.5
TYPE=$(ctl 'eARC_RX Audio Coding Type')
RATE=$(ctl 'eARC_RX Audio Sample Frequency')
ALLOC=$(ctl 'eARC_RX Channel Allocation')
kill -9 $PROBE 2>/dev/null; wait $PROBE 2>/dev/null
sleep 0.3

[ "$RATE" = 0 ] && RATE=48000
DUR=(); [ -n "$SECS" ] && DUR=(-d "$SECS")
FFDUR=(); [ -n "$SECS" ] && FFDUR=(-t "$SECS")

case "$TYPE" in
  1|2) KIND=pcm; CH=2 ;;                       # stereo LPCM
  3)   KIND=pcm; CH=8 ;;                       # multichannel LPCM
  7|8) KIND=spdif; CH=2 ;;                     # AC-3
  9|17) KIND=spdif; CH=2; [ "$RATE" -lt 192000 ] && RATE=192000 ;;  # E-AC-3 runs at 4x rate
  *)   echo "Unsupported or no stream (coding type $TYPE). Is the TV playing?" >&2; exit 1 ;;
esac
echo "eARC stream: coding type $TYPE, rate $RATE, channel allocation $ALLOC -> $KIND" >&2

if [ "$KIND" = pcm ]; then
  if [ "$OUT" = - ]; then
    exec arecord -q -D $DEV -f S16_LE -c $CH -r "$RATE" "${DUR[@]}" -t wav
  fi
  exec arecord -q -D $DEV -f S16_LE -c $CH -r "$RATE" "${DUR[@]}" "$OUT"
fi

command -v ffmpeg >/dev/null || { echo "ffmpeg is required for Dolby streams: sudo apt install ffmpeg" >&2; exit 1; }
# Unwrap the IEC 61937 bursts ourselves, then decode. ffmpeg's own -f spdif can do
# E-AC-3 from ffmpeg 6.0 onwards, but not reading live from an ALSA device, and
# Ubuntu 22.04's 4.4.2 cannot do it at all. See docs/findings.md.
HERE=$(dirname "$(readlink -f "$0")")
# The unwrapper ships in khadas/lib/ in the repo and in /usr/local/lib/earc/
# once installed; accept either, and a sibling copy for a flat checkout.
for d in "$HERE/../khadas/lib" /usr/local/lib/earc "$HERE"; do
  [ -r "$d/iec61937-extract.py" ] && { EXTRACT="$d/iec61937-extract.py"; break; }
done
[ -n "${EXTRACT-}" ] || { echo "iec61937-extract.py not found" >&2; exit 1; }
arecord -q -D $DEV -f S16_LE -c 2 -r "$RATE" -t raw |
  python3 "$EXTRACT" |
  ffmpeg -hide_banner -loglevel warning -f eac3 -i pipe:0 "${FFDUR[@]}" -c:a pcm_s16le -f wav -y "$OUT"
