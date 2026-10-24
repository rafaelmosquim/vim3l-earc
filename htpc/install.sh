#!/bin/bash
# Install the PC-side half: the jitter-buffered receiver and the CEC volume
# bridge. These are USER services (they need the session's PipeWire), so this
# runs WITHOUT sudo.
#
# Set EARC_HOST to the board's address if it is not the default in earc-recv.py.
set -e
cd "$(dirname "$(readlink -f "$0")")"
[ "$(id -u)" != 0 ] || { echo "run this WITHOUT sudo - they are user services" >&2; exit 1; }

install -d "$HOME/.local/bin" "$HOME/.config/systemd/user/earc-in.service.d"
install -m 755 bin/earc-recv.py bin/earc-in.sh bin/earc-volume.py "$HOME/.local/bin/"
install -m 644 systemd/earc-in.service systemd/earc-volume.service "$HOME/.config/systemd/user/"
install -m 644 systemd/earc-in.service.d/latency.conf \
    "$HOME/.config/systemd/user/earc-in.service.d/"

systemctl --user daemon-reload
systemctl --user enable --now earc-in.service earc-volume.service
sleep 3
for u in earc-in.service earc-volume.service; do
    printf '%-22s %s\n' "$u" "$(systemctl --user is-active "$u")"
done
