#!/usr/bin/env python3
"""
Regression test for the satellite catch-up (v0.3.3).

The updater used to refuse to run unless EVERY satellite Pi could be
reached, so one switched-off machine blocked the whole floor's update — and
a collector's machines are never all on. Now:

  * deploy/update.py skips a satellite it cannot reach (named, loudly),
    restarts only the ones it pushed, and rolls back only those;
  * deploy/update.py --catch-up <ip> brings ONE Pi in line with the hub's
    own code, restarting its service only if a file's content differs;
  * the hub (g2s_host.py) notes every Pi that starts reporting and runs
    that catch-up itself, so a machine that comes up after an update is
    never left on old code against a new hub.

In-process: imports deploy/update.py and g2s_host with ssh/rsync/the hub
status replaced by fakes. No network, no hub.db, no host process.

Run from G2S/:
    python3 tools/test_satellite_catchup.py
Exits 0 if all checks pass.
"""

import fcntl
import importlib.util
import json
import logging
import os
import sys
import tempfile
import threading
from pathlib import Path

G2S = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(G2S))
import g2s_host as gh  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "cabinet_update", str(G2S.parent / "deploy" / "update.py"))
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


ON, OFF = "192.168.50.104", "192.168.50.102"
STATUS = {
    "sas": {
        "smib-on/1": {"smibId": "smib-on", "peer": ON, "online": True,
                      "sshUser": "pi"},
        "smib-off/1": {"smibId": "smib-off", "peer": OFF, "online": False},
    },
    "companions": {
        "companion-off": {"companionId": "companion-off", "peer": OFF,
                          "stale": True},
    },
    "tournament": {"phase": "idle"},
}
# verbatim from a real hub -> satellite run (rsync 3.4.1 -> 3.2.7): a PUSH
# marks a sent file '<f', not '>f'
SENT = "<fcst...... transport/serial/sas_serial.py\n"
TIME_ONLY = ".f..t...... core/sas_meters.py\n.d..t...... core/\n"


class Fleet:
    """Stand-in for ssh + rsync. `down` = peers that do not answer."""

    def __init__(self, down=(), differs=(), rsync_rc=0, restart_rc=0):
        self.down, self.differs = set(down), set(differs)
        self.rsync_rc, self.restart_rc = rsync_rc, restart_rc
        self.calls = []         # (kind, peer, extra)

    def sat_ssh(self, peer, key, remote_cmd, check=True, timeout=120,
                user=None):
        if peer in self.down:
            return 255, "ssh: connect to host %s: No route to host" % peer
        if "WorkingDirectory" in remote_cmd:
            unit = "companion" if "companion" in remote_cmd else "sas"
            self.calls.append(("probe-" + unit, peer, user))
            sub = "Companion" if unit == "companion" else "SAS"
            return 0, "WorkingDirectory=/home/pi/CabiNet/%s\n" % sub
        if "systemctl restart" in remote_cmd:
            self.calls.append(("restart", peer, remote_cmd.split()[-1]))
            return self.restart_rc, ""
        return 0, ""

    def run(self, cmd, cwd=None, check=True, quiet=False, timeout=900,
            env=None):
        if cmd and cmd[0] == "rsync":
            peer = cmd[-1].split("@")[1].split(":")[0]
            dry = "--dry-run" in cmd
            self.calls.append(("rsync-dry" if dry else "rsync", peer,
                               cmd[-1]))
            if self.rsync_rc and not dry:
                if check:
                    raise up.Fail("command failed (rc=%d)" % self.rsync_rc)
                return self.rsync_rc, "rsync: connection unexpectedly closed"
            return 0, (SENT if peer in self.differs else TIME_ONLY)
        return 0, ""

    def kinds(self, peer=None):
        return [c[0] for c in self.calls if peer in (None, c[1])]


def install(fleet, status=None):
    up.sat_ssh, up.run = fleet.sat_ssh, fleet.run
    up.sat_clock = lambda peer, key, user=None: None
    up.hub_status = lambda url, timeout=8: dict(status or STATUS)
    up.say = lambda msg="": SAID.append(msg)
    del SAID[:]


SAID = []


def updater_checks(root):
    print("— updater: an unreachable satellite is skipped, not a hard stop")
    sats = up.find_satellites(STATUS)
    f = Fleet(down=[OFF])
    install(f)
    touched = {"pushed": False}
    done = up.push_satellites(root, sats, "/k", dry=False, touched=touched)
    check("the run carries on and pushes the reachable satellite",
          [s["smibId"] for s in done] == ["smib-on"], done)
    check("nothing is copied to the unreachable one",
          "rsync" not in f.kinds(OFF), f.kinds(OFF))
    said = "\n".join(SAID)
    check("the skipped satellite is NAMED with the card's NOT-updated token",
          'Satellite "smib-off" NOT updated' in said, said)
    check("and the operator is told the hub will catch it up",
          "brings it up to date by itself" in said)
    check("touched carries exactly the pushed set for rollback",
          touched["sats"] is done and touched["pushed"] is True)
    check("the satellite's own reported login is used",
          ("probe-sas", ON, "pi") in f.calls, f.calls)

    f = Fleet(down=[ON, OFF])
    install(f)
    touched = {"pushed": False}
    done = up.push_satellites(root, sats, "/k", dry=False, touched=touched)
    check("every satellite off: still no stop, nothing pushed, not 'pushed'",
          done == [] and touched["pushed"] is False)

    f = Fleet(down=[OFF])
    install(f)
    check("a dry run skips it the same way (this is what stopped the "
          "2026-11-15 dry run)",
          [s["smibId"] for s in up.push_satellites(root, sats, "/k",
                                                   dry=True)] == ["smib-on"]
          and f.kinds(ON) == ["probe-sas", "rsync-dry"], f.calls)

    f = Fleet(rsync_rc=12)
    install(f)
    touched = {"pushed": False}
    try:
        up.push_satellites(root, sats, "/k", dry=False, touched=touched)
        stopped = False
    except up.Fail:
        stopped = True
    check("a REACHABLE satellite whose copy fails still stops the run "
          "(half-new tree → rollback)",
          stopped and touched["pushed"] is True
          and [s["smibId"] for s in touched["sats"]] == ["smib-on"])

    print("— updater: what counts as 'differs'")
    check("a file to be sent differs (push direction, '<f')",
          up.tree_differs(SENT))
    check("a brand-new file differs",
          up.tree_differs("<f+++++++++ core/new_module.py\n"))
    check("the pull-direction marker counts too",
          up.tree_differs(">f.st...... core/sas_meters.py\n"))
    check("a new directory differs", up.tree_differs("cd+++++++++ core/\n"))
    check("timestamps alone do not (no restart for nothing)",
          not up.tree_differs(TIME_ONLY))
    check("empty output does not", not up.tree_differs(""))
    cmd = up.rsync_cmd("/k", "/src/", "pi@h:/d/", dry=True, checksum=True)
    check("the catch-up compares content and never deletes",
          "--checksum" in cmd and "--dry-run" in cmd
          and not any(a.startswith("--delete") for a in cmd))
    check("data/ is excluded from every copy",
          all(x in cmd for x in ("data/", "logs/")))

    print("— updater: --catch-up for one Pi")
    f = Fleet()
    install(f)
    code, res = up.catch_up(root, "http://hub", __file__, ON)
    check("already current: exit 0, state current, NOTHING restarted",
          code == up.CATCHUP_OK and res["sas"] == "current"
          and "restart" not in f.kinds() and "rsync" not in f.kinds(),
          (code, res, f.calls))
    check("the last line is the machine-readable result",
          SAID[-1].startswith(up.CATCHUP_TAG)
          and json.loads(SAID[-1][len(up.CATCHUP_TAG):])["sas"] == "current")

    f = Fleet(differs=[ON])
    install(f)
    code, res = up.catch_up(root, "http://hub", __file__, ON)
    check("out of date: files copied, cabinet-sas restarted, state updated",
          code == up.CATCHUP_OK and res["sas"] == "updated"
          and f.kinds(ON) == ["probe-sas", "rsync-dry", "rsync", "restart"]
          and ("restart", ON, "cabinet-sas") in f.calls, (code, res, f.calls))

    f = Fleet(differs=[OFF])
    install(f)
    code, res = up.catch_up(root, "http://hub", __file__, OFF)
    check("a Pi that is satellite AND card reader gets both trees",
          code == up.CATCHUP_OK and res["sas"] == "updated"
          and res["companion"] == "updated"
          and ("restart", OFF, "cabinet-companion") in f.calls,
          (code, res, f.calls))

    f = Fleet(down=[OFF])
    install(f)
    code, res = up.catch_up(root, "http://hub", __file__, OFF)
    check("unreachable: exit 4, nothing touched",
          code == up.CATCHUP_UNREACHABLE and res["sas"] == "unreachable"
          and f.calls == [], (code, res, f.calls))

    f = Fleet(differs=[ON], restart_rc=1)
    install(f)
    code, res = up.catch_up(root, "http://hub", __file__, ON)
    check("copied but the service would not restart: failed, said plainly",
          code == up.CATCHUP_FAILED and res["sas"].startswith("failed")
          and "did not restart" in res["sas"], (code, res))

    f = Fleet(differs=[ON])
    install(f, dict(STATUS, tournament={"phase": "running"}))
    code, res = up.catch_up(root, "http://hub", __file__, ON)
    check("a live tournament defers it — no restart under a round",
          code == up.CATCHUP_DEFERRED and f.calls == [], (code, f.calls))

    f = Fleet(differs=[ON])
    install(f)
    held = open(os.path.join(root, ".cabinet-update.lock"), "a+")
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    out = {}
    t = threading.Thread(target=lambda: out.update(
        r=up.catch_up(root, "http://hub", __file__, ON)))
    t.start()
    t.join(20)
    fcntl.flock(held, fcntl.LOCK_UN)
    held.close()
    check("an update in progress defers it (same lock as the updater)",
          out.get("r", (None,))[0] == up.CATCHUP_DEFERRED and f.calls == [],
          (out, f.calls))

    f = Fleet()
    install(f)
    code, res = up.catch_up(root, "http://hub", __file__, "192.168.50.77")
    check("an address the hub does not know: nothing done, exit 0",
          code == up.CATCHUP_OK and f.calls == [])


def bare_engine():
    """The catch-up bookkeeping needs no hub.db — build just that much."""
    e = gh.G2SHost.__new__(gh.G2SHost)
    e.sat_sync_lock = threading.Lock()
    e._sat_sync = {}
    e.update_lock = threading.Lock()
    e._update = {"state": "idle"}
    e.listen_port = 8081
    return e


def hub_checks():
    print("— hub: which reports mean 'check this Pi'")
    e = bare_engine()
    T = 1_000_000.0
    real_time = gh.time.time
    clock = [T]
    gh.time.time = lambda: clock[0]
    try:
        e.sat_sync_note(ON, "sas", "boot-1")
        r = e._sat_sync[ON]
        check("first report from a Pi: pending, after a settle delay",
              r["state"] == "pending"
              and r["nextAt"] == T + e.SAT_SYNC_SETTLE_SEC)
        check("not due before the settle delay", e._sat_sync_due() is None)
        clock[0] = T + 16
        e.sat_sync_note(ON, "sas", "boot-1")
        check("due after it, and marked checking so it runs once",
              e._sat_sync_due() == ON and r["state"] == "checking"
              and e._sat_sync_due() is None)

        e._sat_sync_settle(ON, 0, {"sas": "current", "detail": "ok"})
        check("already current: settled, no further checks",
              r["state"] == "current" and e._sat_sync_due() is None)
        for i in range(1, 30):
            clock[0] += 1
            e.sat_sync_note(ON, "sas", "boot-1")
        check("steady reporting never re-arms the check",
              r["state"] == "current")

        clock[0] += e.SAT_SYNC_STALE["sas"] + 5
        e.sat_sync_note(ON, "sas", "boot-1")
        check("a report after a silence (machine came back): pending again",
              r["state"] == "pending")
        clock[0] += 16
        e.sat_sync_note(ON, "sas", "boot-1")
        e._sat_sync_due()
        e.sat_sync_note(ON, "sas", "boot-2")
        check("a daemon restart WHILE we check it is our own restart — "
              "not re-armed", r["state"] == "checking")
        e._sat_sync_settle(ON, 0, {"sas": "updated", "detail": "was old"})
        check("out of date and fixed: state updated", r["state"] == "updated")
        clock[0] += 3
        e.sat_sync_note(ON, "sas", "boot-3")
        check("a daemon restart at any other time re-arms the check",
              r["state"] == "pending")

        e.sat_sync_note(ON, "companion", "c-1")
        check("the card reader on the same Pi shares the one record",
              list(e._sat_sync) == [ON] and "companion" in r["last"])

        print("— hub: outcomes and retry schedule")
        e = bare_engine()
        e.sat_sync_note(OFF, "sas", "b")
        r = e._sat_sync[OFF]
        waits = []
        for _ in range(6):
            clock[0] = r["nextAt"] + 1
            r["last"]["sas"] = clock[0] - 1     # it kept reporting meanwhile
            e.sat_sync_note(OFF, "sas", "b")
            assert e._sat_sync_due() == OFF
            e._sat_sync_settle(OFF, 4, {"sas": "unreachable", "detail": "x"})
            waits.append(round(r["nextAt"] - clock[0]))
        check("unreachable: retried on a slowing schedule that bottoms out",
              waits == [60, 300, 900, 1800, 1800, 1800]
              and r["state"] == "unreachable", waits)

        clock[0] = r["nextAt"] + 1
        check("a Pi that stopped reporting is not chased",
              e._sat_sync_due() is None)
        e.sat_sync_note(OFF, "sas", "b")
        check("…and its return re-arms the check from the top",
              r["state"] == "pending" and r["tries"] == 0)

        clock[0] = r["nextAt"] + 1
        e.sat_sync_note(OFF, "sas", "b")
        e._sat_sync_due()
        e._sat_sync_settle(OFF, 3, {"detail": "an update is running"})
        check("deferred (update running / tournament): short retry, no "
              "strike against it",
              r["state"] == "deferred" and r["tries"] == 0
              and round(r["nextAt"] - clock[0]) == e.SAT_SYNC_DEFER_SEC)

        e = bare_engine()
        e.sat_sync_note(ON, "sas", "b0")
        r = e._sat_sync[ON]
        for i in range(3):
            clock[0] += 20
            e.sat_sync_note(ON, "sas", "b%d" % i)
            r["state"] = "checking"
            e._sat_sync_settle(ON, 0, {"sas": "updated", "detail": "fixed"})
        check("three 'fixed' in a row means the copy is not sticking: stop "
              "restarting the machine's link",
              r["state"] == "failed" and "keep coming back" in r["detail"]
              and round(r["nextAt"] - clock[0]) == 1800,
              (r["state"], r["detail"]))

        print("— hub: what /api/status shows")
        e = bare_engine()
        check("a Pi this process has not seen has no codeSync",
              e.sat_sync_view(ON) is None)
        e.sat_sync_note(ON, "sas", "b")
        v = e.sat_sync_view(ON)
        check("codeSync is the small public shape",
              sorted(v) == ["at", "detail", "state"]
              and v["state"] == "pending")
        e.sat_sync_note("", "sas", "b")
        e.sat_sync_note(None, "sas", "b")
        check("a report with no source address is ignored",
              list(e._sat_sync) == [ON])

        print("— hub: running the updater's catch-up")
        e = bare_engine()
        e.sat_sync_note(ON, "sas", "b")
        clock[0] += 20
        e.sat_sync_note(ON, "sas", "b")
        e._sat_sync_due()
        seen = {}

        class P:
            returncode = 0
            stderr = ""
            stdout = ("CabiNet catch-up\n" + up.CATCHUP_TAG + json.dumps(
                {"peer": ON, "sas": "updated", "companion": None,
                 "detail": "was out of date"}) + "\n")

        real_run = gh.subprocess.run
        gh.subprocess.run = lambda cmd, **kw: (seen.update(cmd=cmd), P())[1]
        try:
            e._sat_sync_run(ON)
        finally:
            gh.subprocess.run = real_run
        check("the hub shells out to deploy/update.py --catch-up <ip>",
              seen["cmd"][1].endswith(os.path.join("deploy", "update.py"))
              and seen["cmd"][2:4] == ["--catch-up", ON], seen)
        check("and folds its result line into the record",
              e._sat_sync[ON]["state"] == "updated"
              and e._sat_sync[ON]["detail"] == "was out of date")

        def boom(cmd, **kw):
            raise gh.subprocess.TimeoutExpired(cmd, 1)

        e._sat_sync[ON]["state"] = "checking"
        gh.subprocess.run = boom
        try:
            e._sat_sync_run(ON)
        finally:
            gh.subprocess.run = real_run
        check("a hung catch-up never raises — it is a failed try",
              e._sat_sync[ON]["state"] == "failed"
              and e._sat_sync[ON]["tries"] == 1)
    finally:
        gh.time.time = real_time


def main():
    logging.getLogger("g2s_host").setLevel(logging.CRITICAL)
    with tempfile.TemporaryDirectory() as root:
        os.makedirs(os.path.join(root, "SAS"))
        os.makedirs(os.path.join(root, "Companion"))
        updater_checks(root)
    hub_checks()
    print("=" * 50)
    print(f"RESULT: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
