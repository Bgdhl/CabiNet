"""
Plain-words help for someone starting the DHCP server by hand
(start-dhcp-enhanced.py).

Why this exists (2026-11-16, GitHub issue #15): on an installed host the
cabinet-dhcp service already runs the DHCP server. A tester ran the script
by hand as a bench guide said to, with no --interface, and got

    [DHCP] Successfully bound to port 67
    [DHCP] FATAL: cannot pin socket to eno1: [Errno 19] No such device
    [DHCP] Make sure to run with sudo: ...

None of which says what was actually the matter: "eno1" is the developer's
PC network card, the Pi has no such thing, he WAS using sudo — and nothing
needed starting. Worse, port 67 is opened with SO_REUSEADDR, so had he
passed the right interface a SECOND DHCP server would have started next to
the service's.

Everything here is a pure function over things passed in, so it can be
tested without a network card, root or systemd (tools/test_dhcp_start_help.py).

THE ONE RULE: the service must never talk itself out of starting. Whenever
there is any doubt about who started this process, running_service() says
"no service" and the script carries on exactly as it always did.
"""

import os
import subprocess

SERVICE_UNITS = ("cabinet-dhcp", "casinonet-dhcp")   # new and pre-rename name
NET_DIR = "/sys/class/net"
SCRIPT = "start-dhcp-enhanced.py"


def _run(cmd, timeout=3):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def interfaces(net_dir=NET_DIR):
    """This box's network interfaces without loopback, or None when that
    cannot be told (then nothing is checked)."""
    try:
        return sorted(n for n in os.listdir(net_dir) if n != "lo")
    except OSError:
        return None


def interface_holding(ip, run=_run):
    """The interface that has this IPv4 address, or None if unknown."""
    try:
        out = run(["ip", "-o", "-4", "addr", "show"]).stdout
    except Exception:                                    # noqa: BLE001
        return None
    for line in out.splitlines():
        parts = line.split()
        # "2: eth0    inet 192.168.50.2/24 brd ... scope global eth0"
        if len(parts) >= 4 and parts[2] == "inet" \
                and parts[3].split("/")[0] == ip:
            return parts[1]
    return None


def running_service(environ=None, run=_run, pids=None):
    """The DHCP service already serving on this box, or None.

    None, always, when systemd started THIS process — it sets INVOCATION_ID
    for everything a unit runs — or when the service's main process is this
    one or its parent. None on any error, timeout or unexpected answer."""
    environ = os.environ if environ is None else environ
    if environ.get("INVOCATION_ID"):
        return None
    mine = pids if pids is not None else {os.getpid(), os.getppid()}
    for unit in SERVICE_UNITS:
        try:
            if run(["systemctl", "is-active", unit]).stdout.strip() != "active":
                continue
            main_pid = int(run(["systemctl", "show", "-p", "MainPID",
                                "--value", unit]).stdout.strip() or 0)
        except Exception:                                # noqa: BLE001
            return None
        if main_pid <= 0 or main_pid in mine:
            return None
        return unit
    return None


def already_running_help(unit):
    return [
        f"The DHCP server is already running on this box, as the {unit} "
        "service.",
        "There is nothing to start by hand.",
        f"  systemctl status {unit}        is it up",
        f"  journalctl -u {unit} -f        watch machines ask for an address",
        "To run it by hand instead, stop the service first:",
        f"  sudo systemctl stop {unit}",
    ]


def missing_interface_help(interface, names, holder=None, server_ip=None):
    lines = [f'This box has no network interface called "{interface}".']
    if names:
        lines.append("It has: " + ", ".join(names) + ".")
    if holder and holder != interface:
        lines += [f"{server_ip} is on {holder}, so the slot network is most "
                  "likely plugged in there:",
                  f"  sudo python3 {SCRIPT} --interface {holder}"]
    else:
        lines += ["Name the one the slot network is plugged into:",
                  f"  sudo python3 {SCRIPT} --interface <name>"]
    return lines


def failed_help(interface, is_root):
    if not is_root:
        return ["Port 67 needs root:",
                f"  sudo python3 {SCRIPT} --interface {interface}"]
    return ["The lines above say why."]


def preflight(interface, server_ip, environ=None, run=_run, net_dir=NET_DIR,
              pids=None):
    """What stops a start before anything is opened or written: a list of
    lines to show, or [] to go ahead."""
    unit = running_service(environ=environ, run=run, pids=pids)
    if unit:
        return already_running_help(unit)
    names = interfaces(net_dir)
    if names is not None and interface not in names:
        return missing_interface_help(interface, names,
                                      interface_holding(server_ip, run=run),
                                      server_ip)
    return []
