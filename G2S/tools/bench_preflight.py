#!/usr/bin/env python3
"""
Bench pre-flight — GO/NO-GO check for the AVP join session.

Run from G2S/ right before powering the machine:
    python3 tools/bench_preflight.py
    python3 tools/bench_preflight.py --interface eth0     # name the slot NIC

Checks the environment hazards that have actually bitten this project:
wrong/missing slot-VLAN IP, port squatters, the webserver.py UDP-67 trap,
firewall state, and stale config values. Exits 0 on GO.

Works both on a bare bench box (nothing running yet) and on a hub built
from deploy/DEPLOY.md, where the CabiNet services already hold TCP 8081
and UDP 67 — our own servers on those ports are a pass, not a squatter.

The slot NIC is found, in order: --interface, the NIC that already holds
the server IP, the NIC the installed cabinet-dhcp unit is pinned to, then
the first wired NIC present that has no other address (a Pi names it eth0
or end0, a PC eno1/enpXsY).
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

SERVER_IP = "192.168.50.2"
AVP_IP = "192.168.50.100"
G2S_DIR = Path(__file__).resolve().parent.parent
DHCP_UNIT = "/etc/systemd/system/cabinet-dhcp.service"
WIRED_NIC = re.compile(r"^(eth|end|eno|enp|ens|enx)")

GO = True
WARN = 0


def res(ok, label, detail="", fix="", warn=False):
    global GO, WARN
    if ok:
        print(f"  ✅ {label}" + (f" — {detail}" if detail else ""))
    elif warn:
        WARN += 1
        print(f"  ⚠️  {label}" + (f" — {detail}" if detail else ""))
        if fix:
            print(f"      fix: {fix}")
    else:
        GO = False
        print(f"  ❌ {label}" + (f" — {detail}" if detail else ""))
        if fix:
            print(f"      fix: {fix}")


def sh(cmd):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True,
                              text=True, timeout=10).stdout
    except Exception:
        return ""


def nic_holding_ip(addr_out, ip=SERVER_IP):
    """NIC name from `ip -o -4 addr show` output that carries ip, or None."""
    for ln in addr_out.splitlines():
        m = re.match(r"\d+:\s+(\S+)\s+inet\s+(\S+)/", ln)
        if m and m.group(2) == ip:
            return m.group(1).split("@")[0]
    return None


def nic_from_unit(unit_text):
    """The --interface the cabinet-dhcp unit's ExecStart is pinned to."""
    for ln in unit_text.splitlines():
        if ln.strip().startswith("ExecStart="):
            m = re.search(r"--interface[= ](\S+)", ln)
            if m:
                return m.group(1)
    return None


def wired_nics(link_out):
    """Wired NIC names from `ip -o link show` output, in listing order."""
    out = []
    for ln in link_out.splitlines():
        m = re.match(r"\d+:\s+([^:@\s]+)", ln)
        if m and WIRED_NIC.match(m.group(1)):
            out.append(m.group(1))
    return out


def pick_nic(explicit, addr_out, unit_text, link_out):
    """(nic, how-it-was-chosen). nic is None when nothing wired exists."""
    if explicit:
        return explicit, "from --interface"
    nic = nic_holding_ip(addr_out)
    if nic:
        return nic, f"holds {SERVER_IP}"
    nic = nic_from_unit(unit_text)
    if nic:
        return nic, "from the cabinet-dhcp unit"
    nics = wired_nics(link_out)
    if nics:
        # A wired NIC that already has some other IPv4 address is the
        # uplink (LAN / internet), so the ones without an address go first.
        addressed = {m.group(1).split("@")[0] for m in re.finditer(
            r"^\d+:\s+(\S+)\s+inet\s", addr_out, re.M)}
        nics.sort(key=lambda n: n in addressed)
        if len(nics) == 1:
            return nics[0], "the only wired NIC"
        return nics[0], ("a guess: first wired NIC without another address"
                         f"; others: {', '.join(nics[1:])}")
    return None, "no wired NIC found"


def holder_pids(ss_lines):
    """pids named in ss -p output lines (empty when ss could not see them)."""
    return [int(p) for ln in ss_lines for p in re.findall(r"pid=(\d+)", ln)]


def cmdline(pid):
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\0", b" ").decode(errors="replace")


def ours(ss_lines, script, running):
    """Is the port in ss_lines held by our own `script`?  True / False, or
    None when ss shows no owner (a root-owned socket seen without sudo) and
    the script is not running either. With an owner shown, every owner must
    be the script; with none shown, the script running is taken as the
    answer."""
    pids = holder_pids(ss_lines)
    if pids:
        return all(runs_script(cmdline(p), script) for p in pids)
    return True if running else None


def runs_script(cmd, script):
    """Is cmd (a process command line) a Python interpreter running
    `script`? A shell, editor or log viewer that merely mentions the name
    does not count."""
    return re.match(r"(sudo\s+(-\S+\s+)*)?\S*python[\d.]*\s+(-\S+\s+)*"
                    r"(\S*/)?" + re.escape(script) + r"(\s|$)", cmd) is not None


def running(script):
    out = sh(f"pgrep -af '{script}' 2>/dev/null")
    return any(runs_script(ln.partition(" ")[2], script)
               for ln in out.splitlines())


def main():
    global GO
    ap = argparse.ArgumentParser(description="CabiNet bench pre-flight")
    ap.add_argument("--interface", default=None,
                    help="slot NIC (default: found automatically)")
    args = ap.parse_args()
    try:
        unit_text = Path(DHCP_UNIT).read_text()
    except OSError:
        unit_text = ""
    NIC, how = pick_nic(args.interface, sh("ip -o -4 addr show 2>/dev/null"),
                        unit_text, sh("ip -o link show 2>/dev/null"))

    print("CabiNet bench pre-flight\n" + "=" * 40)
    check_nic(NIC, how)
    g2s_up, dhcp_up = check_ports()
    check_rest()

    print("\n" + "=" * 40)
    if GO:
        extra = f" ({WARN} warning{'s' * (WARN != 1)})" if WARN else ""
        if g2s_up and dhcp_up:
            print(f"GO{extra} — the CabiNet servers are already running.")
            print(f"  capture: sudo tcpdump -i {NIC} -w logs/bench_$(date +%Y%m%d_%H%M%S).pcap host {AVP_IP}")
            print("  then power the machine.")
        else:
            print(f"GO{extra} — start order:")
            if not dhcp_up:
                print(f"  T1: sudo python3 start-dhcp-enhanced.py --interface {NIC}   (from G2S/)")
            if not g2s_up:
                print("  T2: python3 g2s_host.py --harvest              (NO sudo)")
            print(f"  T3: sudo tcpdump -i {NIC} -w logs/bench_$(date +%Y%m%d_%H%M%S).pcap host {AVP_IP}")
            print("  then power the AVP.")
    else:
        print("NO-GO — fix the ❌ items above first.")
    return 0 if GO else 1


def check_nic(NIC, how):
    print(f"\n[1] Slot VLAN NIC ({NIC or 'not found'} — {how})")
    if not NIC:
        res(False, "a wired NIC exists", "none of eth*/end*/eno*/enp* found",
            "list them: ip link — then rerun with --interface <name>")
        return
    ip_out = sh(f"ip addr show {NIC} 2>/dev/null")
    if not ip_out:
        res(False, f"{NIC} exists", "interface not found",
            "list them: ip link — then rerun with --interface <name>")
        return
    has_ip = f"inet {SERVER_IP}/" in ip_out
    res(has_ip, f"{SERVER_IP}/24 assigned",
        "" if has_ip else "no slot-VLAN IP",
        f"sudo ip addr add {SERVER_IP}/24 dev {NIC} && sudo ip link set {NIC} up")
    carrier = "NO-CARRIER" not in ip_out and "state UP" in ip_out
    res(carrier, "link is UP with carrier",
        "" if carrier else "NO-CARRIER / link down",
        "plug the cable to the slot switch / machine, then: "
        f"sudo ip link set {NIC} up", warn=True)


def check_ports():
    """Section [2]. Returns (g2s_host already up, our DHCP already up)."""
    print("\n[2] Ports")
    g2s_up = dhcp_up = False
    tcp = sh("ss -tlnp 2>/dev/null")
    tcp8081 = [ln for ln in tcp.splitlines() if re.search(r":8081\s", ln)]
    if not tcp8081:
        res(True, "TCP 8081 free", "start g2s_host.py when ready")
    else:
        m = re.search(r"users:\(\((\"[^\"]+\")", tcp8081[0])
        who = m.group(1) if m else "a process ss cannot name without sudo"
        mine = ours(tcp8081, "g2s_host.py", running("g2s_host.py"))
        if mine:
            g2s_up = True
            res(True, "TCP 8081", "g2s_host.py is already running")
        else:
            res(False, "TCP 8081 free", f"held by {who}",
                "kill the old server (check for webserver.py / diagnostic "
                "servers)")

    udp = sh("ss -ulnp 2>/dev/null")
    udp67 = [ln for ln in udp.splitlines() if re.search(r":67\s", ln)]
    foreign = [ln for ln in udp67 if "virbr0" not in ln]
    if not udp67:
        res(True, "UDP 67 free",
            "start start-dhcp-enhanced.py (sudo) when ready")
    elif not foreign:
        res(True, "UDP 67", "only libvirt dnsmasq on virbr0 (coexists via "
            "SO_REUSEADDR + SO_BINDTODEVICE)")
    else:
        mine = ours(foreign, "start-dhcp-enhanced.py",
                    running("start-dhcp-enhanced.py"))
        if mine:
            dhcp_up = True
            res(True, "UDP 67", "start-dhcp-enhanced.py is already running"
                + ("" if holder_pids(foreign) else
                   " (owner not shown without sudo — rerun with sudo to "
                   "rule out a second DHCP server)"))
        else:
            res(False, "UDP 67 conflict", foreign[0].strip(),
                "stop the conflicting DHCP server (dnsmasq? old "
                "webserver.py?)")
    return g2s_up, dhcp_up


def check_rest():
    print("\n[3] Process traps")
    ps = sh("pgrep -af 'webserver.py|g2s_diagnostic|simple_server' 2>/dev/null")
    ps = "\n".join(ln for ln in ps.splitlines()
                   if "pgrep" not in ln and "/bin/sh" not in ln)
    res(not ps.strip(), "no legacy servers running",
        ps.strip()[:80] if ps.strip() else "",
        "kill them — webserver.py's integrated DHCP fights on UDP 67")

    print("\n[4] Firewall")
    ufw = sh("sudo -n ufw status 2>/dev/null") or sh("ufw status 2>/dev/null")
    if "inactive" in ufw:
        res(True, "ufw inactive", "nothing blocks 8081/67/69")
    elif "active" in ufw:
        ok = "8081" in ufw
        res(ok, "ufw active with 8081 rule",
            "" if ok else "no 8081 allow rule",
            f"sudo ufw allow from 192.168.50.0/24 to any port 8081 proto tcp",
            warn=not ok)
    else:
        res(True, "ufw status unknown (needs sudo)",
            "run: sudo ufw status — expect inactive", warn=True)

    print("\n[5] Config sanity")
    g2s_xml = (G2S_DIR / "tftp-root/igt/g2s.xml")
    t = g2s_xml.read_text() if g2s_xml.exists() else ""
    res("<Port>8081</Port>" in t and SERVER_IP in t,
        "tftp-root/igt/g2s.xml -> 192.168.50.2:8081",
        "" if t else "file missing", "restore from git")
    cfg = G2S_DIR / "config/g2s_config.json"
    try:
        c = json.loads(cfg.read_text())
        ok = c.get("http_port") == 8081 and not c.get("ssl_enabled", False)
        res(ok, "config/g2s_config.json http:8081 cert-less",
            f"http_port={c.get('http_port')} ssl={c.get('ssl_enabled')}",
            "set http_port/https_port 8081, ssl_enabled false")
    except Exception as e:
        res(False, "config/g2s_config.json parses", str(e), "fix the JSON")

    print("\n[6] AVP reachability (informational)")
    ping = sh(f"ping -c1 -W1 {AVP_IP} 2>/dev/null")
    if "1 received" in ping or "1 packets received" in ping:
        res(True, f"AVP answers at {AVP_IP}",
            "it already has its lease — skip waiting for DHCP")
    else:
        res(True, f"AVP not pingable at {AVP_IP} yet",
            "normal if it's powered off / hasn't leased — DHCP will handle it")


if __name__ == "__main__":
    sys.exit(main())
