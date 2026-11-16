#!/usr/bin/env python3
"""
Regression test for the updater's "did the floor come back?" check (v0.3.4).

deploy/update.py used to call an update failed — and roll it back, restoring
the database — whenever a slot machine that had been on was not on again
210 s later. On 2026-11-15 a Bluebird 2 was restarted while an update ran
(it takes ten minutes to boot): the hub and its satellite Pi were both
running the new code and talking to each other, and the update was thrown
away. A machine being powered off does not power off its Pi.

Now the check judges what the update touched (the hub, the satellite Pis,
the reader Pis) and rolls back on evidence of breakage, not on absence:

  * the floor is re-read just before the restart, so a machine switched off
    while the gates ran is not waited for;
  * a satellite counts as back when the PI reports from its restarted
    service, whatever its machine is doing;
  * a SAS machine that answered up to the restart and never answers the new
    code still fails the update (that is what a broken SAS tree looks like);
  * a G2S machine that says nothing is "switched off"; one that talks and
    cannot rejoin fails the update.

In-process: imports deploy/update.py with the hub status, ssh and the clock
replaced by fakes. No network, no hub.

Run from G2S/:
    python3 tools/test_update_verify.py
Exits 0 if all checks pass.
"""

import copy
import importlib.util
import sys
from pathlib import Path

G2S = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "cabinet_update_verify", str(G2S.parent / "deploy" / "update.py"))
up = importlib.util.module_from_spec(spec)
spec.loader.exec_module(up)

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


class Clock:
    """Virtual time: sleep() just moves the hands."""

    def __init__(self):
        self.t = 1_000_000.0

    def time(self):
        return self.t

    def sleep(self, n):
        self.t += n


PI_A, PI_B, RDR = "192.168.50.102", "192.168.50.104", "192.168.50.110"
SAID = []


def sas(sid, peer, online=True, age=0.5, stale=False, heard_ago=0.2):
    """One satellite's entry in the hub status."""
    return {"smibId": sid, "peer": peer, "online": online,
            "reportAgeSec": age, "stale": stale,
            "lastSeenAgoSec": (0.2 if online else heard_ago)}


def floor(sats=(), egms=None, readers=()):
    st = {"sas": {"%s/1" % e["smibId"]: e for e in sats},
          "companions": {r["companionId"]: r for r in readers},
          "tournament": {"phase": "idle"}}
    for name, ent in (egms or {}).items():
        st[name] = ent
    return st


def egm(state="onLine", seen=0.0, ka=0.0):
    return {"commsState": state, "lastSeen": seen, "lastKeepAlive": ka}


def reader(cid=RDR, stale=False):
    return {"companionId": "companion-a", "peer": cid, "stale": stale}


class Rig:
    """Wires update.py to a scripted hub: after(t_since_restart) -> status."""

    def __init__(self, before, after, down=(), states=None, key="/k"):
        self.clock = Clock()
        self.after, self.down = after, set(down)
        self.states = states or {}
        self.key, self.probed = key, []
        self.sats = up.find_satellites(before)
        self.machines = up.online_machines(before)
        self.comps = up.find_companions(before)
        self.t0 = None
        up.time = self.clock
        up.say = lambda msg="": SAID.append(msg)
        up.hub_status = self.hub_status
        up.sat_ssh = self.sat_ssh
        del SAID[:]

    def hub_status(self, url, timeout=8):
        since = self.clock.t - (self.t0 if self.t0 is not None
                                else self.clock.t)
        st = self.after(since)
        return copy.deepcopy(st) if st is not None else {"__error__": "down"}

    def sat_ssh(self, peer, key, remote_cmd, check=True, timeout=120,
                user=None):
        self.probed.append(peer)
        if peer in self.down:
            return 255, "ssh: connect to host %s: No route to host" % peer
        st = self.states.get(peer, "active")
        return (0 if st == "active" else 3), st + "\n"

    def restart(self):
        """What restart_fleet does that verify() relies on."""
        self.t0 = self.clock.t
        for s in self.sats:
            s["restartedAt"] = self.clock.t

    def verify(self):
        self.restart()
        ok = up.verify("http://hub", self.sats, self.machines, self.comps,
                       dry=False, key=self.key)
        return ok, self.clock.t - self.t0, "\n".join(SAID)


def fresh(since):
    """A report from the restarted service: its age is less than the time
    since the restart (and it never looks older than half a second)."""
    return min(0.5, max(since - 0.1, 0.0))


def main():
    print("— everything comes back: passes at once, no warnings")
    before = floor([sas("smib-a", PI_A)], {"EGM_A": egm()}, [reader()])
    r = Rig(before, lambda t: floor([sas("smib-a", PI_A, age=fresh(t))]
                                    if t >= 2 else [],
                                    {"EGM_A": egm("onLine" if t >= 50
                                                  else "offline")},
                                    [reader()]))
    ok, took, text = r.verify()
    check("passes", ok is True, text)
    check("...as soon as the last machine is back, not after the full wait",
          50 <= took < 70, took)
    check("...with the usual one-line summary",
          "machines 1/1 back, satellites 1/1 online, readers 1/1 reporting"
          in text, text)
    check("...and no warning", "⚠️" not in text, text)
    check("...without probing any Pi", r.probed == [], r.probed)

    print("— 2026-11-15: the machine is restarting, the hub and Pi are fine")
    before = floor([sas("smib-a", PI_A)], {"EGM_A": egm()}, [reader()])
    r = Rig(before, lambda t: floor(
        [sas("smib-a", PI_A, online=False, age=fresh(t), heard_ago=None)],
        {"EGM_A": egm("offline")}, [reader()]))
    # the machine went dark BEFORE the restart: the last-moment look sees it
    r.clock.t += 240                       # the gates ran for four minutes
    gone = floor([sas("smib-a", PI_A, online=False, heard_ago=50.0)],
                 {"EGM_A": egm("onLine", ka=r.clock.t - 50)}, [reader()])
    gone["tournament"]["serverNow"] = r.clock.t
    up.hub_status = lambda url, timeout=8: copy.deepcopy(gone)
    r.machines = up.rebaseline("http://hub", r.sats, r.machines, r.comps)
    said_before = "\n".join(SAID)
    up.hub_status = r.hub_status
    check("the last look drops the SAS machine that stopped answering",
          r.sats[0]["online"] is False and r.sats[0]["reporting"] is True,
          r.sats)
    check("...and the G2S machine whose keepAlives stopped 50 s ago, though "
          "the hub still calls it onLine", r.machines == [], r.machines)
    check("...and says so", "not waiting for it" in said_before, said_before)
    ok, took, text = r.verify()
    check("the update is KEPT", ok is True, text)
    check("...without waiting out the clock for a machine that was already "
          "off", took < 30, took)

    print("— the last look only ever narrows")
    before = floor([sas("smib-a", PI_A, online=False, heard_ago=None)],
                   {"EGM_A": egm("offline")})
    r = Rig(before, lambda t: None)
    now = floor([sas("smib-a", PI_A)], {"EGM_A": egm()})
    up.hub_status = lambda url, timeout=8: copy.deepcopy(now)
    m = up.rebaseline("http://hub", r.sats, r.machines, r.comps)
    check("a machine that came on during the gates is not added",
          m == [] and r.sats[0]["online"] is False, (m, r.sats))
    before = floor([sas("smib-a", PI_A)], {"EGM_A": egm()})
    r = Rig(before, lambda t: None)
    up.hub_status = lambda url, timeout=8: {"__error__": "busy"}
    m = up.rebaseline("http://hub", r.sats, r.machines, r.comps)
    check("a hub that does not answer the last look changes nothing",
          m == ["EGM_A"] and r.sats[0]["online"] is True, (m, r.sats))
    r = Rig(before, lambda t: None)
    quiet = floor([sas("smib-a", PI_A)], {"EGM_A": egm("onLine")})
    up.hub_status = lambda url, timeout=8: copy.deepcopy(quiet)
    m = up.rebaseline("http://hub", r.sats, r.machines, r.comps)
    check("a joined machine the hub has no keepAlive stamp for is kept "
          "(some never send one)", m == ["EGM_A"], m)

    print("— a machine switched off DURING the wait")
    before = floor([sas("smib-a", PI_A)])
    r = Rig(before, lambda t: floor(
        [sas("smib-a", PI_A, online=False, age=fresh(t), heard_ago=30.0)]))
    ok, took, text = r.verify()
    check("SAS: it answered the new code and then stopped -> kept, warned",
          ok is True and "answered after the update and then stopped" in text,
          text)
    check("...the warning says it is not an update problem",
          "not an update problem" in text, text)
    before = floor([], {"EGM_A": egm()})
    r = Rig(before, lambda t: floor([], {"EGM_A": egm("offline")}))
    ok, took, text = r.verify()
    check("G2S: one machine that says nothing since the restart -> kept, "
          "warned", ok is True and "has said nothing to the hub" in text,
          text)

    print("— real breakage still rolls back")
    before = floor([sas("smib-a", PI_A)])
    r = Rig(before, lambda t: floor(
        [sas("smib-a", PI_A, online=False, age=fresh(t), heard_ago=None)]))
    ok, took, text = r.verify()
    check("SAS: answering up to the restart, never once after -> FAILS "
          "(the quiet stale-tree failure)",
          ok is False and "has not answered once since" in text, text)
    check("...after the full wait", took >= up.REJOIN_TIMEOUT, took)

    before = floor([sas("smib-a", PI_A)])
    r = Rig(before, lambda t: floor([sas("smib-a", PI_A, age=20 + t,
                                         stale=t > 1)]),
            states={PI_A: "failed"})
    ok, took, text = r.verify()
    check("a Pi that is on but whose service is not reporting -> FAILS",
          ok is False and "switched on but its SAS service is not reporting"
          in text and "failed" in text, text)
    check("...the Pi was asked", r.probed == [PI_A], r.probed)

    r = Rig(before, lambda t: floor([sas("smib-a", PI_A, age=5 + t)]))
    ok, took, text = r.verify()
    check("the OLD process's last report does not count as back",
          ok is False and "not reporting to the hub" in text, text)

    r = Rig(before, lambda t: None)
    ok, took, text = r.verify()
    check("a hub that never answers -> FAILS", ok is False
          and "hub silent" in text, text)

    before = floor([], {"EGM_A": egm()})
    r = Rig(before, lambda t: floor([], {"EGM_A": egm(
        "opening", seen=r.clock.t if t > 20 else 0.0)}))
    ok, took, text = r.verify()
    check("G2S: talking to the hub but never joined -> FAILS",
          ok is False and "talking to the hub but could not rejoin" in text,
          text)
    check("...only after the extra grace for a machine in mid-join",
          took >= up.REJOIN_TIMEOUT + up.REJOIN_GRACE - 6, took)

    late = up.REJOIN_TIMEOUT + 30
    r = Rig(before, lambda t: floor([], {"EGM_A": egm(
        "onLine" if t >= late else "opening", seen=r.clock.t)}))
    ok, took, text = r.verify()
    check("G2S: a machine that finishes joining inside the grace -> passes",
          ok is True and "⚠️" not in text and late <= took < late + 12,
          (took, text))

    before = floor([], {"EGM_A": egm(), "EGM_B": egm()})
    r = Rig(before, lambda t: floor([], {"EGM_A": egm("offline"),
                                         "EGM_B": egm("offline")}))
    ok, took, text = r.verify()
    check("G2S: two machines joined and BOTH silent -> FAILS",
          ok is False and "all 2 G2S machines" in text, text)
    r = Rig(before, lambda t: floor([], {"EGM_A": egm("onLine"),
                                         "EGM_B": egm("offline")}))
    ok, took, text = r.verify()
    check("G2S: one of two back, the other silent -> kept, warned",
          ok is True and "EGM_B has said nothing" in text, text)

    print("— a Pi that is switched off is not the update's doing")
    before = floor([sas("smib-a", PI_A), sas("smib-b", PI_B)])
    r = Rig(before, lambda t: floor(
        [sas("smib-a", PI_A, age=fresh(t)),
         sas("smib-b", PI_B, age=20 + t, stale=True)]), down=[PI_B])
    ok, took, text = r.verify()
    check("unreachable Pi -> kept, warned, named",
          ok is True and "smib-b" in text and "cannot be reached" in text,
          text)
    r = Rig(before, lambda t: floor(
        [sas("smib-a", PI_A, age=fresh(t)),
         sas("smib-b", PI_B, age=20 + t, stale=True)]), down=[PI_B],
        key=None)
    ok, took, text = r.verify()
    check("with no key to ask the Pi, stay strict -> FAILS", ok is False,
          text)

    print("— card readers")
    before = floor([], {}, [reader()])
    r = Rig(before, lambda t: floor([], {}, [reader(stale=True)]),
            down=[RDR])
    ok, took, text = r.verify()
    check("a reader that cannot be reached -> kept, warned",
          ok is True and "card reader" in text and "cannot be reached"
          in text, text)
    r = Rig(before, lambda t: floor([], {}, [reader(stale=True)]),
            states={RDR: "failed"})
    ok, took, text = r.verify()
    check("a reader that is on but not reporting -> FAILS",
          ok is False and "card reader" in text, text)

    print("— what counted before still counts")
    before = floor([sas("smib-a", PI_A, online=False, heard_ago=900.0),
                    sas("smib-b", PI_B, stale=True, age=4000)],
                   {"EGM_A": egm("offline")})
    r = Rig(before, lambda t: floor(
        [sas("smib-a", PI_A, online=False, age=fresh(t), heard_ago=None)],
        {"EGM_A": egm("offline")}))
    ok, took, text = r.verify()
    check("a machine that was already off, and a Pi that was already not "
          "reporting, are not waited for",
          ok is True and took < 30 and "⚠️" not in text, (took, text))
    st = floor([sas("smib-a", PI_A, stale=True)])
    check("find_satellites tells the Pi (reporting) from its machine "
          "(online)", up.find_satellites(st)[0]["reporting"] is False
          and up.find_satellites(st)[0]["online"] is True)
    r = Rig(floor([sas("smib-a", PI_A)]), lambda t: None)
    check("a dry run verifies nothing", up.verify(
        "http://hub", r.sats, r.machines, r.comps, dry=True) is True)

    print(f"\nRESULT: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
