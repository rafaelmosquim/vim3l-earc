#!/bin/bash
# Print eARC format changes as the TV makes them. Opens its own capture because
# the coding-type registers read 0 unless a capture is running. Ctrl-C to stop.
# Run it only while earc-streamer is stopped (both want hw:0,5).
set -u
arecord -D hw:0,5 -f dat -t raw >/dev/null 2>/tmp/arecord-watch.err &
pid=$!
trap 'kill $pid 2>/dev/null' EXIT
sleep 1
kill -0 $pid 2>/dev/null || { echo "arecord failed:"; cat /tmp/arecord-watch.err; exit 1; }
last=
while kill -0 $pid 2>/dev/null; do
  ct=$(amixer -c0 cget "name=eARC_RX Audio Coding Type" | sed -n 's/^  : values=//p')
  sf=$(amixer -c0 cget "name=eARC_RX Audio Sample Frequency" | sed -n 's/^  : values=//p')
  at=$(amixer -c0 cget "name=eARC_RX attended type" | sed -n 's/^  : values=//p')
  now="coding type $ct, $sf Hz, attended $at"
  [ "$now" = "$last" ] || { echo "$(date +%H:%M:%S)  $now"; last="$now"; }
  sleep 0.3
done
