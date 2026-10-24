#!/bin/bash
# Make the VIM3L announce itself to the TV as a CEC Audio System and request ARC,
# then test-capture from the eARC/ARC RX PCM (hw:0,5).
# Run as root: sudo bash arc-audio-system.sh
set -u
CEC=/sys/class/cec
PA=$(cat $CEC/physical_addr)          # e.g. 3000 -> bytes 30 00
PA_HI=${PA:0:2}; PA_LO=${PA:2:2}

echo 1 > /sys/class/cec/dbg_en 2>/dev/null

# Device type 5 = Audio System, logical address 5
echo 5 > $CEC/device_type
echo 5 > $CEC/log_addr
sleep 0.5
echo "device_type=$(cat $CEC/device_type) log_addr=$(cat $CEC/log_addr) phys=$PA"

# <Report Physical Address> (broadcast): 5F 84 PA_HI PA_LO 05
echo "5F 84 $PA_HI $PA_LO 05" > $CEC/cmd
sleep 0.3
# <Set System Audio Mode> On (broadcast): 5F 72 01
echo "5F 72 01" > $CEC/cmd
sleep 0.3
# <Request ARC Initiation> to TV: 50 C3
echo "50 C3" > $CEC/cmd
sleep 1
# <Report ARC Initiated> to TV: 50 C1 (answer to the TV's <Initiate ARC>)
echo "50 C1" > $CEC/cmd
sleep 1

# Make sure the ARC/eARC receiver is enabled
amixer -c0 cset name='ARC eARC RX enable' on >/dev/null
amixer -c0 cset name='eARC RX ARC Switch' on >/dev/null
echo "attended type: $(amixer -c0 cget name='eARC_RX attended type' | grep ': values')"

echo "--- recent kernel log"
dmesg | grep -iE 'cec|earc' | tail -25

echo "--- 3 s test capture from hw:0,5 (play something on the TV now)"
OUT=/tmp/arc_test.wav
rm -f $OUT
timeout 6 arecord -D hw:0,5 -f S16_LE -c2 -r48000 -d3 $OUT
python3 - "$OUT" <<'EOF'
import sys, wave, array, os
p = sys.argv[1]
if not os.path.exists(p) or os.path.getsize(p) <= 44:
    print("RESULT: no samples received (TV is not sending ARC audio)")
else:
    w = wave.open(p); a = array.array('h', w.readframes(w.getnframes()))
    print(f"RESULT: {len(a)//2} frames, peak {max(map(abs, a)) if a else 0}")
EOF
