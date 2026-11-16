#!/usr/bin/env bash
# kiosk_pointer.sh — hide the mouse arrow on the hub's touchscreen.
#
# Run ON the hub, as root, once:
#   sudo ./deploy/kiosk_pointer.sh            # hide it
#   sudo ./deploy/kiosk_pointer.sh --undo     # put it back
#
# WHY A SCRIPT, AND WHY ROOT. The arrow is not the web page's and not a
# mouse: the Pi's HDMI ports count as pointing devices, so the kiosk's
# compositor draws a cursor (see 99-cabinet-kiosk-pointer.rules). The page
# cannot remove it — the browser never receives a pointer event, so it never
# gets to apply `cursor: none` — and an update cannot either: the updater may
# restart the hub service and nothing else. It takes one udev rule, which
# only root can install. deploy/kiosk_setup.sh installs the same rule on a
# new kiosk; this script is for a kiosk that is already running.
#
# It restarts the kiosk (the compositor only looks at its input devices when
# it starts), so the touchscreen goes blank for a few seconds and, if the hub
# login is on, asks you to sign in again.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "kiosk_pointer: run me with sudo (udev rules need root)" >&2
    exit 1
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RULE="99-cabinet-kiosk-pointer.rules"
DST="/etc/udev/rules.d/$RULE"

if [[ "${1:-}" == "--undo" ]]; then
    rm -f "$DST"
    echo "==> removed $DST — the mouse arrow comes back"
else
    if [[ ! -f "$HERE/$RULE" ]]; then
        echo "kiosk_pointer: $HERE/$RULE not found — update CabiNet first." >&2
        exit 1
    fi
    install -m 644 "$HERE/$RULE" "$DST"
    echo "==> installed $DST"
fi

udevadm control --reload
udevadm trigger --subsystem-match=input --action=change
udevadm settle --timeout=10 || true

# cabinet-kiosk is the unit kiosk_setup.sh installs; casinonet-kiosk is its
# name on a hub set up before the project was renamed.
restarted=0
for unit in cabinet-kiosk.service casinonet-kiosk.service; do
    if systemctl is-active --quiet "$unit"; then
        echo "==> restarting $unit"
        systemctl restart "$unit"
        restarted=1
    fi
done
if [[ $restarted -eq 0 ]]; then
    echo "    no kiosk is running here — the rule applies when one starts"
fi
echo "kiosk_pointer: done."
