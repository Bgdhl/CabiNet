#!/usr/bin/env python3
"""
Gate for the plain-words checks in front of the DHCP server
(G2S/dhcp_start_help.py, used by start-dhcp-enhanced.py).

Two things are pinned here.

  1. THE SERVICE ALWAYS STARTS. The checks exist to stop a second copy
     started by hand. If they ever stopped the copy systemd starts, no
     machine on the floor would get an address. So: with systemd's
     INVOCATION_ID in the environment, or when the service's main process
     is this one, or on ANY error asking systemd, the answer is "go ahead".

  2. The case from GitHub issue #15 (2026-11-16) reads sensibly: the script
     run by hand on an installed host, with the developer's "eno1" default.

Stdlib only, no root, no network card needed. Run from G2S/:
    python3 tools/test_dhcp_start_help.py
Exits 0 if all checks pass.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

G2S = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(G2S))
import dhcp_start_help as h  # noqa: E402

PASS = 0
FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {detail!r}")


class Out:
    def __init__(self, stdout="", returncode=0):
        self.stdout, self.returncode = stdout, returncode


def systemd(active=(), main_pid=4242, ip_lines="", calls=None):
    """A pretend `systemctl` / `ip`: which units are active, and their pid."""
    def run(cmd, timeout=3):
        if calls is not None:
            calls.append(cmd)
        if cmd[:2] == ["systemctl", "is-active"]:
            return Out("active\n" if cmd[2] in active else "inactive\n",
                       0 if cmd[2] in active else 3)
        if cmd[:2] == ["systemctl", "show"]:
            return Out(f"{main_pid}\n")
        if cmd[0] == "ip":
            return Out(ip_lines)
        raise AssertionError(cmd)
    return run


def raising(exc):
    def run(cmd, timeout=3):
        raise exc
    return run


IP_OUT = ("1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever\n"
          "2: eth0    inet 192.168.50.2/24 brd 192.168.50.255 scope global "
          "eth0\\       valid_lft forever\n"
          "3: wlan0    inet 192.168.1.40/24 brd 192.168.1.255 scope global "
          "dynamic wlan0\\       valid_lft 80000sec\n")
BY_HAND = {}                              # no INVOCATION_ID: a person's shell
BY_SYSTEMD = {"INVOCATION_ID": "0123456789abcdef0123456789abcdef"}
ME = {100, 99}                            # this process and its parent


def main():
    with tempfile.TemporaryDirectory(prefix="dhcp_help_") as tmp:
        net = os.path.join(tmp, "net")
        for name in ("lo", "eth0", "wlan0"):
            os.makedirs(os.path.join(net, name))

        print("— the service always starts")
        calls = []
        check("started by systemd: never asks, never stops",
              h.running_service(BY_SYSTEMD, systemd(active=h.SERVICE_UNITS,
                                                    calls=calls), ME) is None
              and calls == [], calls)
        check("...and the whole preflight lets it through",
              h.preflight("eth0", "192.168.50.2", BY_SYSTEMD,
                          systemd(active=h.SERVICE_UNITS), net, ME) == [])
        check("the service's own main process is never 'another copy'",
              h.running_service(BY_HAND, systemd(active=("cabinet-dhcp",),
                                                 main_pid=100), ME) is None)
        check("...nor is its child",
              h.running_service(BY_HAND, systemd(active=("cabinet-dhcp",),
                                                 main_pid=99), ME) is None)
        for what, run in (
                ("systemctl is not installed", raising(FileNotFoundError())),
                ("systemctl hangs", raising(
                    subprocess.TimeoutExpired("systemctl", 3))),
                ("systemctl answers nonsense for the pid",
                 systemd(active=("cabinet-dhcp",), main_pid="soon")),
                ("the unit has no main pid yet",
                 systemd(active=("cabinet-dhcp",), main_pid=0))):
            check(f"any doubt means go ahead: {what}",
                  h.running_service(BY_HAND, run, ME) is None)
        check("no service installed at all: go ahead",
              h.running_service(BY_HAND, systemd(), ME) is None)

        print("— a second copy by hand is stopped, in plain words")
        check("the service is found under its name",
              h.running_service(BY_HAND, systemd(active=("cabinet-dhcp",)),
                                ME) == "cabinet-dhcp")
        check("...and under the pre-rename name",
              h.running_service(BY_HAND, systemd(active=("casinonet-dhcp",)),
                                ME) == "casinonet-dhcp")
        said = h.preflight("eno1", "192.168.50.2", BY_HAND,
                           systemd(active=("cabinet-dhcp",), ip_lines=IP_OUT),
                           net, ME)
        check("issue #15: it says the service already runs it",
              said[:2] == ["The DHCP server is already running on this box, "
                           "as the cabinet-dhcp service.",
                           "There is nothing to start by hand."], said)
        check("...shows how to look at it and how to take over",
              any("journalctl -u cabinet-dhcp -f" in s for s in said)
              and said[-1] == "  sudo systemctl stop cabinet-dhcp", said)
        check("...and does not go on about the interface as well",
              not any("eno1" in s for s in said), said)

        print("— a wrong interface names the right one")
        check("interfaces are listed without loopback",
              h.interfaces(net) == ["eth0", "wlan0"], h.interfaces(net))
        check("the interface holding the host address is found",
              h.interface_holding("192.168.50.2",
                                  systemd(ip_lines=IP_OUT)) == "eth0")
        check("...and an address nobody holds finds none",
              h.interface_holding("10.9.9.9", systemd(ip_lines=IP_OUT))
              is None)
        said = h.preflight("eno1", "192.168.50.2", BY_HAND,
                           systemd(ip_lines=IP_OUT), net, ME)
        check("bench default on a Pi: says what is missing and what exists",
              said[:2] == ['This box has no network interface called "eno1".',
                           "It has: eth0, wlan0."], said)
        check("...and gives the command to run",
              said[-1] == "  sudo python3 start-dhcp-enhanced.py "
                          "--interface eth0", said)
        said = h.preflight("eno1", "192.168.50.2", BY_HAND,
                           raising(FileNotFoundError()), net, ME)
        check("no `ip` tool: still helpful, asks for the name",
              said[-1] == "  sudo python3 start-dhcp-enhanced.py "
                          "--interface <name>", said)
        check("the right interface goes ahead",
              h.preflight("eth0", "192.168.50.2", BY_HAND, systemd(), net,
                          ME) == [])
        check("when interfaces cannot be listed, nothing is refused",
              h.interfaces(os.path.join(tmp, "nope")) is None
              and h.preflight("eno1", "192.168.50.2", BY_HAND, systemd(),
                              os.path.join(tmp, "nope"), ME) == [])

        print("— a start that fails says the useful thing")
        check("not root: that is the reason, with the full command",
              h.failed_help("eth0", False)
              == ["Port 67 needs root:",
                  "  sudo python3 start-dhcp-enhanced.py --interface eth0"])
        check("root: no sudo advice to someone already using it",
              not any("sudo" in s for s in h.failed_help("eth0", True)))

        print("— the script uses it before it touches anything")
        script = G2S / "start-dhcp-enhanced.py"
        src = script.read_text("utf-8")
        check("the check runs before the config is written or a port opened",
              0 < src.find("dhcp_start_help.preflight(")
              < src.find("os.makedirs('python/web/data'")
              < src.find("server.start()"))
        check("the old 'make sure to run with sudo' catch-all is gone",
              "Make sure to run with sudo" not in src)
        if os.path.isdir(h.NET_DIR):
            cwd = os.path.join(tmp, "cwd")
            os.makedirs(cwd)
            p = subprocess.run(
                [sys.executable, str(script), "--interface", "nosuchif0"],
                cwd=cwd, capture_output=True, text=True, timeout=60,
                env=dict(os.environ, **BY_SYSTEMD))
            check("run for real with a made-up interface: stops, exit 2",
                  p.returncode == 2
                  and '[DHCP] This box has no network interface called '
                      '"nosuchif0".' in p.stdout, (p.returncode, p.stdout,
                                                    p.stderr[-300:]))
            check("...having written nothing", os.listdir(cwd) == [],
                  os.listdir(cwd))
        else:
            print("  (no /sys/class/net here — the live run is skipped)")

    print(f"\nRESULT: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
