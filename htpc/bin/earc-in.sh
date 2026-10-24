#!/usr/bin/env bash
# Receives the 5.1 LPCM stream that the Khadas VIM3L captures from the TV's
# eARC output (raw interleaved S16_LE, 48 kHz, 6 ch on TCP 4010) and feeds it
# into CamillaIn, the same 6-channel PipeWire sink Kodi uses, so the existing
# CamillaDSP instance (2.0.yml) does the 5.1 -> 2.0 processing.
#
# earc-recv.py owns the socket and holds the jitter buffer; see its docstring
# for why a plain socat|pw-cat crackles. pw-cat stays up across sender
# reconnects, so a dropped connection costs a few ms of silence, not seconds.
#
# Channel order on the wire is eARC 5.1: FL FR FC LFE SL SR. It is mapped onto
# the sink's FL FR FC LFE RL RR -- those are the names the CamillaIn null sink
# uses, and with SL/SR PipeWire finds no match and drops the surrounds.
set -u

# pw-cat only has to cover its own scheduling now; earc-recv.py absorbs the
# network and sender jitter. Keep both as low as the pops allow, for lip sync.
LATENCY="${EARC_LATENCY:-30ms}"
SINK="${EARC_SINK:-CamillaIn}"
RATE="${EARC_RATE:-48000}"
CHANNELS="${EARC_CHANNELS:-6}"
CHANNEL_MAP="${EARC_CHANNEL_MAP:-FL,FR,FC,LFE,RL,RR}"
FORMAT="${EARC_FORMAT:-s16}"
RETRY_DELAY="${EARC_RETRY_DELAY:-3}"

log() { printf '%s earc-in: %s\n' "$(date '+%H:%M:%S')" "$*"; }

wait_for_sink() {
    for _ in $(seq 1 60); do
        if pactl list short sinks 2>/dev/null | grep -qw "$SINK"; then
            return 0
        fi
        sleep 1
    done
    log "sink $SINK never appeared"
    return 1
}

wait_for_sink || exit 1

while true; do
    "$HOME/.local/bin/earc-recv.py" |
    pw-cat --playback --raw \
        --target "$SINK" \
        --format "$FORMAT" \
        --rate "$RATE" \
        --channels "$CHANNELS" \
        --channel-map "$CHANNEL_MAP" \
        --latency "$LATENCY" \
        --media-role Movie \
        -P 'node.name=earc-in' \
        -P 'node.description=TV eARC 5.1' \
        -
    log "pipeline exited, restarting in ${RETRY_DELAY}s"
    sleep "$RETRY_DELAY"
done
