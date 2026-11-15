#!/bin/bash
# Install the board-side (Khadas VIM3L) half of the eARC receiver.
#   sudo bash install.sh              everything
#   sudo bash install.sh --no-oled    skip the front-panel OLED service
#   sudo bash install.sh --no-play    capture and stream only, no DSP on the board
#
# Two topologies, and --no-play is the switch between them:
#
#   default     TV -> eARC -> streamer -> 127.0.0.1:4010 -> earc-recv ->
#               CamillaDSP -> UMC404HD. Everything on this one board.
#   --no-play   TV -> eARC -> streamer -> TCP 4010 -> a PC that ran
#               htpc/install.sh. The original split setup.
#
# The two local-dsp drop-ins belong to the playback half: they point the CEC
# daemon and the OLED at 127.0.0.1 for volume, which is only true when the DSP
# runs here. With --no-play they are REMOVED, so the address compiled into
# earc-cec-daemon.py and earc-oled.py takes effect again.
#
# The OLED service is optional and needs an SSD1306 on i2c-3; without one it
# just fails to start, so it is installed only when asked for.
set -e
cd "$(dirname "$(readlink -f "$0")")"

# Flags before the root check, so a typo costs nothing to find.
OLED=yes
PLAY=yes
for a in "$@"; do
    case "$a" in
        --no-oled) OLED=no ;;
        --no-play) PLAY=no ;;
        *) echo "unknown option: $a" >&2; exit 2 ;;
    esac
done

[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }

# ---- capture, CEC, and the lip-sync latency hold ----------------------------
install -m 755 khadas/bin/earc-cec-daemon.py khadas/bin/earc-latency-watch.py \
    /usr/local/bin/
install -d /usr/local/lib/earc
install -m 755 khadas/lib/earc-streamer.py khadas/lib/iec61937-extract.py \
    /usr/local/lib/earc/
install -m 644 khadas/systemd/earc-cec.service \
    khadas/systemd/earc-streamer.service \
    khadas/systemd/earc-latency-watch.service /etc/systemd/system/
UNITS="earc-cec.service earc-streamer.service earc-latency-watch.service"

if [ "$OLED" = yes ]; then
    install -m 755 khadas/lib/earc-oled.py khadas/lib/ssd1306.py /usr/local/lib/earc/
    install -m 644 khadas/systemd/earc-oled.service /etc/systemd/system/
    UNITS="$UNITS earc-oled.service"
fi

# ---- board-side playback: jitter buffer -> CamillaDSP -> USB interface -------
if [ "$PLAY" = yes ]; then
    # earc-recv.py and earc-volume.py are still under htpc/ because that is
    # where they were written and they are byte-identical for both ends -- only
    # the machine they get installed on moved. Shared, not forked.
    install -m 755 khadas/bin/earc-play.sh htpc/bin/earc-recv.py \
        htpc/bin/earc-volume.py /usr/local/bin/
    install -m 644 khadas/systemd/earc-play.service \
        khadas/systemd/earc-volume.service /etc/systemd/system/
    install -d /etc/systemd/system/earc-play.service.d
    install -m 644 khadas/systemd/earc-play.service.d/latency.conf \
        /etc/systemd/system/earc-play.service.d/
    install -d /etc/systemd/system/earc-cec.service.d
    install -m 644 khadas/systemd/earc-cec.service.d/local-dsp.conf \
        /etc/systemd/system/earc-cec.service.d/
    if [ "$OLED" = yes ]; then
        install -d /etc/systemd/system/earc-oled.service.d
        install -m 644 khadas/systemd/earc-oled.service.d/local-dsp.conf \
            /etc/systemd/system/earc-oled.service.d/
    fi
    UNITS="$UNITS earc-play.service earc-volume.service"
else
    # Rolling back to the split topology. The files can stay; what matters is
    # that this board stops competing with the PC for the stream and for the
    # volume, so the units go away and the local-dsp drop-ins come off.
    systemctl disable --now earc-play.service earc-volume.service 2>/dev/null || true
    rm -f /etc/systemd/system/earc-cec.service.d/local-dsp.conf \
        /etc/systemd/system/earc-oled.service.d/local-dsp.conf
fi

# The streamer runs as khadas:audio and the OLED needs i2c as well.
usermod -aG audio khadas 2>/dev/null || true
[ "$OLED" = yes ] && { usermod -aG i2c khadas 2>/dev/null || true; }

# ---- two things this installer cannot supply --------------------------------
# Say so here rather than leaving earc-play to crash-loop with "no camilladsp"
# or "no config" every 3 seconds in the journal.
if [ "$PLAY" = yes ]; then
    HOME_DIR="$(getent passwd khadas | cut -d: -f6)"
    CDSP_DIR="$HOME_DIR/.config/camilladsp"
    MODE="$(cat "$CDSP_DIR/mode" 2>/dev/null || echo umc)"
    [ -x /usr/local/bin/camilladsp ] || cat <<'MSG'

WARNING: /usr/local/bin/camilladsp is missing. earc-play needs it and will
         restart-loop until it is there. Fetch the official aarch64 release
         (camilladsp-linux-aarch64) and install it with mode 755. Note the
         released binary is built with the websocket feature only: its capture
         backends are RawFile/WavFile/Stdin/SignalGenerator/Alsa, which is all
         the Stdin -> Alsa chain here needs.
MSG
    [ -r "$CDSP_DIR/$MODE.yml" ] || cat <<MSG

WARNING: no CamillaDSP config at $CDSP_DIR/$MODE.yml
         ("$CDSP_DIR/mode" selects it; default "umc"). earc-play exits
         immediately without one. The configs are NOT in this repository --
         they hold a room-specific calibration -- so this is yours to provide.
MSG
fi

systemctl daemon-reload
systemctl enable $UNITS

# Order matters. The CEC daemon must claim its logical addresses and load the
# CDS before the streamer opens the capture, and the streamer holds the eARC
# capture device exclusively. latency-watch has to come after it as well: the
# format registers read zero unless a capture is open, so with no streamer
# there is no coding type for it to decide on.
systemctl restart earc-cec.service
sleep 5
systemctl restart earc-streamer.service
sleep 4
systemctl restart earc-latency-watch.service
[ "$OLED" = yes ] && systemctl restart earc-oled.service || true
if [ "$PLAY" = yes ]; then
    systemctl restart earc-play.service
    systemctl restart earc-volume.service
    sleep 2
fi

for u in $UNITS; do printf '%-28s %s\n' "$u" "$(systemctl is-active "$u")"; done
echo
journalctl -u earc-streamer --no-pager -n 10 -o cat
