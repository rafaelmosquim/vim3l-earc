#!/bin/bash
# Install the board-side (Khadas VIM3L) half of the eARC receiver.
#   sudo bash install.sh            everything
#   sudo bash install.sh --no-oled  skip the front-panel OLED service
#
# The OLED service is optional and needs an SSD1306 on i2c-3; without one it
# just fails to start, so it is installed only when asked for.
set -e
cd "$(dirname "$(readlink -f "$0")")"
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }

OLED=yes
[ "${1-}" = --no-oled ] && OLED=no

install -m 755 khadas/bin/earc-cec-daemon.py /usr/local/bin/earc-cec-daemon.py
install -d /usr/local/lib/earc
install -m 755 khadas/lib/earc-streamer.py khadas/lib/iec61937-extract.py /usr/local/lib/earc/
install -m 644 khadas/systemd/earc-cec.service khadas/systemd/earc-streamer.service \
    /etc/systemd/system/
UNITS="earc-cec.service earc-streamer.service"

if [ "$OLED" = yes ]; then
    install -m 755 khadas/lib/earc-oled.py khadas/lib/ssd1306.py /usr/local/lib/earc/
    install -m 644 khadas/systemd/earc-oled.service /etc/systemd/system/
    UNITS="$UNITS earc-oled.service"
fi

# The streamer runs as khadas:audio and the OLED needs i2c as well.
usermod -aG audio khadas 2>/dev/null || true
[ "$OLED" = yes ] && { usermod -aG i2c khadas 2>/dev/null || true; }

systemctl daemon-reload
systemctl enable $UNITS
# The CEC daemon must claim its logical addresses before the streamer opens the
# capture, and the streamer holds hw:0,5 exclusively.
systemctl restart earc-cec.service
sleep 5
systemctl restart earc-streamer.service
sleep 4
[ "$OLED" = yes ] && systemctl restart earc-oled.service || true

for u in $UNITS; do printf '%-24s %s\n' "$u" "$(systemctl is-active "$u")"; done
echo
journalctl -u earc-streamer --no-pager -n 10 -o cat
