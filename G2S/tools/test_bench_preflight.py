#!/usr/bin/env python3
"""
Regression test for bench_preflight.py on a hub built from deploy/DEPLOY.md
(GitHub issue #7: a Raspberry Pi 5 on Bookworm got NO-GO on three checks
that were all false alarms).

  * The slot NIC was hardcoded to eno1; a Pi names it eth0 or end0. It is
    now found: --interface, else the NIC holding the server IP, else the
    NIC the cabinet-dhcp unit is pinned to, else the first wired NIC.
  * TCP 8081 held by our own g2s_host.py showed as a squatter, because ss
    names the process "python3".
  * UDP 67 held by our own DHCP server showed as a conflict; run without
    sudo, ss shows no owner at all for that root-owned socket.

In-process (imports bench_preflight; importing runs no checks). No network.

Run from G2S/:
    python3 tools/test_bench_preflight.py
Exits 0 if all checks pass.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bench_preflight as bp  # noqa: E402

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


ADDR_PI = """\
1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever preferred_lft forever
2: eth0    inet 192.168.50.2/24 brd 192.168.50.255 scope global eth0\\       valid_lft forever
3: wlan0    inet 192.168.1.44/24 brd 192.168.1.255 scope global dynamic wlan0\\       valid_lft 1s
"""
ADDR_NO_SLOT_IP = """\
1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever preferred_lft forever
3: wlan0    inet 192.168.1.44/24 brd 192.168.1.255 scope global dynamic wlan0\\       valid_lft 1s
"""
ADDR_NEAR_MISS = """\
2: end0    inet 192.168.50.20/24 brd 192.168.50.255 scope global end0\\       valid_lft forever
"""
LINK_PI = """\
1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536 qdisc noqueue state UNKNOWN mode DEFAULT
2: end0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc mq state UP mode DEFAULT
3: wlan0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc fq_codel state UP mode DORMANT
"""
LINK_VLAN = """\
1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536
2: enp3s0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500
4: enp3s0.50@enp3s0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500
"""
ADDR_UPLINK = """\
2: enp4s0    inet 192.168.1.10/24 brd 192.168.1.255 scope global dynamic enp4s0\\       valid_lft 1s
"""
LINK_TWO_WIRED = """\
1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536
2: enp4s0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500
3: eno1: <NO-CARRIER,BROADCAST,MULTICAST,UP> mtu 1500
"""
LINK_WIFI_ONLY = """\
1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536
3: wlan0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500
"""
UNIT = """\
[Service]
WorkingDirectory=/home/pi/CabiNet/G2S
#ExecStart=/usr/bin/python3 -u start-dhcp-enhanced.py --interface eno1
ExecStart=/usr/bin/python3 -u start-dhcp-enhanced.py --interface end0
"""
# The two lines from the issue-7 report (run without sudo on the Pi).
SS_TCP_OURS = ('LISTEN 0      5            0.0.0.0:8081       0.0.0.0:*    '
               'users:(("python3",pid=%d,fd=3))')
SS_UDP_NO_OWNER = "UNCONN 0      0            0.0.0.0:67         0.0.0.0:*"


def main():
    print("— slot NIC selection")
    check("--interface wins over everything",
          bp.pick_nic("enp9s0", ADDR_PI, UNIT, LINK_PI)[0] == "enp9s0")
    check("the NIC holding the server IP is chosen (Pi: eth0)",
          bp.pick_nic(None, ADDR_PI, UNIT, LINK_PI)[0] == "eth0")
    check("192.168.50.20 is not mistaken for 192.168.50.2",
          bp.nic_holding_ip(ADDR_NEAR_MISS) is None)
    check("no slot IP yet: the cabinet-dhcp unit's NIC (commented line ignored)",
          bp.pick_nic(None, ADDR_NO_SLOT_IP, UNIT, LINK_PI)[0] == "end0")
    check("no slot IP, no unit: first wired NIC (Pi 5 Bookworm: end0)",
          bp.pick_nic(None, ADDR_NO_SLOT_IP, "", LINK_PI)[0] == "end0")
    check("wired NIC listing skips lo/wlan and strips the @parent suffix",
          bp.wired_nics(LINK_VLAN) == ["enp3s0", "enp3s0.50"],
          bp.wired_nics(LINK_VLAN))
    check("two wired NICs: the one with a LAN address is the uplink, the "
          "other is the slot NIC",
          bp.pick_nic(None, ADDR_UPLINK, "", LINK_TWO_WIRED)[0] == "eno1",
          bp.pick_nic(None, ADDR_UPLINK, "", LINK_TWO_WIRED))
    nic, how = bp.pick_nic(None, ADDR_NO_SLOT_IP, "", LINK_WIFI_ONLY)
    check("nothing wired: None, never a made-up name", nic is None, (nic, how))
    check("eno1 is not assumed when it does not exist",
          bp.pick_nic(None, "", "", "")[0] is None)

    print("— port ownership")
    me = os.getpid()                     # cmdline: ... test_bench_preflight.py
    line = SS_TCP_OURS % me
    check("owner shown and it is our script: ours",
          bp.ours([line], "test_bench_preflight.py", False) is True)
    check("owner shown and it is something else: not ours, even if our "
          "script runs elsewhere",
          bp.ours([line], "g2s_host.py", True) is False)
    check("issue #7 UDP 67 line: no owner shown, our DHCP running: ours",
          bp.ours([SS_UDP_NO_OWNER], "start-dhcp-enhanced.py", True) is True)
    check("no owner shown and our DHCP not running: unknown, so a conflict",
          not bp.ours([SS_UDP_NO_OWNER], "start-dhcp-enhanced.py", False))
    dhcp = "start-dhcp-enhanced.py"
    check("the systemd unit's command line counts as running the script",
          bp.runs_script("/usr/bin/python3 -u start-dhcp-enhanced.py "
                         "--interface eth0", dhcp))
    check("so does a sudo'd bench start with a path",
          bp.runs_script("sudo python3 /home/pi/CabiNet/G2S/"
                         "start-dhcp-enhanced.py", dhcp))
    check("a shell or editor that only mentions the name does not",
          not bp.runs_script("bash -c python3 start-dhcp-enhanced.py", dhcp)
          and not bp.runs_script("nano start-dhcp-enhanced.py", dhcp)
          and not bp.runs_script("python3 other.py start-dhcp-enhanced.py",
                                 dhcp))
    check("pids parsed from every owner on a line",
          bp.holder_pids(['users:(("a",pid=11,fd=3),("b",pid=22,fd=4))'])
          == [11, 22])

    print("=" * 50)
    print(f"RESULT: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
