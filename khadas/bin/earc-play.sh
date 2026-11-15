#!/usr/bin/env bash
# Board-side playback: this machine's own eARC stream -> jitter buffer ->
# CamillaDSP -> UMC404HD. The ProDesk's half of the chain, moved onto the board.
#
# Replaces htpc/bin/earc-in.sh, and there is no pw-cat and no PipeWire in it:
# with the TV as the only source there is nothing to mix, so CamillaDSP's ALSA
# playback device is the end of the chain. PipeWire comes back only when the
# music streamer moves over -- and when it does, mind that the UMC404HD
# advertises its four outputs as channel map FL FR FC LFE, not FL FR RL RR.
#
# earc-recv.py stays in front of CamillaDSP. Its docstring measures ~160 ppm
# against the UMC404HD, ~10 ms a minute, and running both ends on one board does
# not change that: the clocks are still the TV's and the UMC's crystal. Its
# crossfaded drop/payback is what absorbs it, and CamillaDSP's stdin capture is
# paced by the playback device, which is exactly the backpressure it needs.
#
# IDLE GATING (2026-10-28). With the TV off this pipeline still ran at full
# rate on a stream of zeros: camilladsp 9.6% of a core, earc-recv 4.1%, and
# -- the expensive part -- 2004 USB interrupts a second feeding silence to the
# UMC, which is why cpu-sleep-0 was entered 25 times in 39 hours. So hold the
# pipeline down until the link is actually live.
#
# ONLY THE PLAYBACK HALF IS GATED. earc-streamer.service owns the eARC capture
# device and must keep running untouched: opening/closing that device toggles
# the eARC RX, and a couple of toggles make this TV fall back to ARC for good
# (see the BACKOFF table in earc-streamer.py). Nothing here goes near it --
# earc-recv is just a TCP client of the streamer's fan-out, and the streamer
# already handles clients coming and going.
#
# The gate is `eARC_RX attended type` (0 DISCONNECT / 1 ARC / 2 eARC), NOT the
# format registers: with the TV switched off those hold a stale 1/44100, so
# they cannot tell "off" from "playing 44.1k".
set -u

CDSP="${CDSP:-/usr/local/bin/camilladsp}"
CDSP_DIR="${CDSP_DIR:-$HOME/.config/camilladsp}"
MODE="$(cat "$CDSP_DIR/mode" 2>/dev/null || echo umc)"
CONFIG="${CDSP_CONFIG:-$CDSP_DIR/$MODE.yml}"
STATEFILE="${CDSP_STATEFILE:-$HOME/.local/state/camilladsp/statefile.yml}"
PORT="${CDSP_PORT:-1234}"
# Only used the very first time, before a statefile exists: volume_limit is
# 0 dB, so coming up without this would mean coming up at full scale. Passed as
# --gain=VALUE, because a bare "-g -20" makes clap read the -20 as a flag.
INITIAL_GAIN="${CDSP_INITIAL_GAIN:--20}"
RETRY_DELAY="${EARC_RETRY_DELAY:-3}"
RECV="${EARC_RECV:-/usr/local/bin/earc-recv.py}"
CARD="${EARC_CARD:-AMLAUGESOUND}"

# How long the link must read "not eARC" before the pipeline is torn down.
# Generous on purpose: a teardown costs a few seconds of catch-up on the way
# back, while thrashing it on a transient costs a dropout mid-film. The
# streamer's own NO_DATA_GRACE is 30 s, so sit well clear of it.
IDLE_GRACE="${EARC_IDLE_GRACE:-90}"
POLL="${EARC_IDLE_POLL:-2}"
# Reading the mixer does not touch the capture device, so polling it is free.

# cpufreq floor. rc.local chmod 777s these, so no privilege is needed. Held high
# while audio flows because camilladsp runs 256-frame chunks (5.3 ms) at
# Nice=-10 and NOT SCHED_FIFO, so a slow ramp is an xrun; dropped when idle,
# which is the whole point of the exercise. Empty value = leave cpufreq alone.
FREQ_PLAY="${EARC_FREQ_PLAY:-1400000}"
FREQ_IDLE="${EARC_FREQ_IDLE:-500000}"

log() { printf '%s earc-play: %s\n' "$(date '+%H:%M:%S')" "$*"; }

set_min_freq() {
    [ -n "$1" ] || return 0
    local p
    for p in /sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_min_freq; do
        [ -w "$p" ] && echo "$1" > "$p" 2>/dev/null
    done
}

attended() {
    amixer -c "$CARD" cget "name=eARC_RX attended type" 2>/dev/null \
        | sed -n 's/.*: values=\([0-9]*\).*/\1/p' | head -1
}

mkdir -p "$(dirname "$STATEFILE")"
[ -r "$CONFIG" ] || { log "no config at $CONFIG"; exit 1; }
[ -x "$CDSP" ]   || { log "no camilladsp at $CDSP"; exit 1; }
log "mode $MODE, config $CONFIG, idle grace ${IDLE_GRACE}s"

# The streamer is on this same board now. It is still a TCP server and
# earc-recv.py still reconnects on its own, so nothing has to be ordered.
export EARC_HOST="${EARC_HOST:-127.0.0.1}"

cleanup() { set_min_freq "$FREQ_IDLE"; }
trap cleanup EXIT

# Both halves are tracked explicitly through a fifo rather than as a shell
# pipeline: with `a | b &`, $! is only b, and earc-recv is b's SIBLING, not its
# child, so there is no way to reap it reliably from the one pid. Leaving it
# behind would hold the streamer's TCP client slot open and quietly defeat the
# whole gate.
start_pipeline() {
    local fifo
    fifo="$(mktemp -u "${TMPDIR:-/tmp}/earc-play.XXXXXX")"
    mkfifo -m 600 "$fifo" || return 1
    "$RECV" > "$fifo" &
    recv=$!
    "$CDSP" -p "$PORT" -s "$STATEFILE" --gain="$INITIAL_GAIN" -l warn "$CONFIG" < "$fifo" &
    cdsp=$!
    rm -f "$fifo"   # both ends are open; the name is no longer needed
}

stop_pipeline() {
    kill "$cdsp" "$recv" 2>/dev/null
    wait "$cdsp" 2>/dev/null
    wait "$recv" 2>/dev/null
}

while true; do
    # --- idle: wait for the link to come up ------------------------------
    set_min_freq "$FREQ_IDLE"
    if [ "$(attended)" != "2" ]; then
        log "link not eARC, holding pipeline down"
        while [ "$(attended)" != "2" ]; do sleep "$POLL"; done
        log "link up, starting pipeline"
    fi

    # --- playing ---------------------------------------------------------
    set_min_freq "$FREQ_PLAY"
    start_pipeline || { log "could not start pipeline"; sleep "$RETRY_DELAY"; continue; }

    idle_for=0
    gated=0
    while kill -0 "$cdsp" 2>/dev/null; do
        sleep "$POLL"
        if [ "$(attended)" = "2" ]; then
            idle_for=0
        else
            idle_for=$((idle_for + POLL))
            if [ "$idle_for" -ge "$IDLE_GRACE" ]; then
                log "link off ${idle_for}s, stopping pipeline"
                stop_pipeline
                gated=1
                break
            fi
        fi
    done

    # A deliberate teardown goes straight back to the idle wait; only a
    # pipeline that fell over on its own burns the retry delay.
    if [ "$gated" -eq 0 ]; then
        stop_pipeline
        log "pipeline exited, restarting in ${RETRY_DELAY}s"
        sleep "$RETRY_DELAY"
    fi
done
