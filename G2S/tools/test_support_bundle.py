#!/usr/bin/env python3
"""
Gate for what a support bundle must never carry (deploy/support_bundle.py).

A tester sends a bundle to someone else. Up to script v1.1 it took every
file in G2S/data, which since the hub login (v0.3.4) means:

  * hub_api_token      — lets a local tool skip the hub login;
  * hub.db             — the login password hashes and signed-in sessions;
  * account_state.json — the key player PINs are hashed with, and the
                         hashes. A PIN is a few digits, so with both it is
                         found in a few thousand tries.

Found 2026-11-16 in a real bundle. This builds a pretend hub data folder
holding all three, bundles it, and searches EVERY byte of the result for
them. It also checks that the hub's own files are left exactly as they were,
and that what is useful for a diagnosis (usernames, roles, players,
balances, the other tables) is still there.

Stdlib only. Run from G2S/:
    python3 tools/test_support_bundle.py
Exits 0 if all checks pass.
"""

import hashlib
import io
import json
import os
import sqlite3
import sys
import tarfile
import tempfile
from pathlib import Path

G2S = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(G2S.parent / "deploy"))
import support_bundle as sb  # noqa: E402

PASS = 0
FAIL = 0

TOKEN = "tok-" + "a1b2c3d4" * 5
PW_HASH = "pbkdf2$200000$5a17$" + "9f" * 32
SESSION = hashlib.sha256(b"cookie").hexdigest()
PIN_SECRET = "c0ffee" * 11
PIN_HASH = hashlib.sha256(b"1234").hexdigest()
SECRETS = {"the API token": TOKEN, "a login password hash": PW_HASH,
           "a session": SESSION, "the PIN key": PIN_SECRET,
           "a PIN hash": PIN_HASH}


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {detail!r}")


def make_hub_data(data):
    """A hub's G2S/data with every secret in it, WAL mode like the real one."""
    os.makedirs(data)
    with open(os.path.join(data, "hub_api_token"), "w") as f:
        f.write(TOKEN + "\n")
    state = {"accountSeq": 2, "pinSecret": PIN_SECRET, "ledger": [],
             "accounts": {
                 "house": {"id": "house", "name": "The Bank",
                           "cashableMillicents": 99000000},
                 "p1": {"id": "p1", "name": "Sam", "pinHash": PIN_HASH,
                        "cashableMillicents": 1234500}}}
    for name in ("account_state.json", "account_state.json.corrupt-1"):
        with open(os.path.join(data, name), "w") as f:
            json.dump(state, f, indent=1)
    with open(os.path.join(data, "dhcp_leases.json"), "w") as f:
        json.dump({"00:11:22:33:44:55": "192.168.50.100"}, f)
    db = sqlite3.connect(os.path.join(data, "hub.db"))
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE web_logins (username TEXT PRIMARY KEY, role TEXT "
               "NOT NULL, pw TEXT NOT NULL, created_at TEXT NOT NULL, "
               "updated_at TEXT NOT NULL, last_login_at TEXT)")
    db.execute("CREATE TABLE web_sessions (token_hash TEXT PRIMARY KEY, "
               "username TEXT NOT NULL, created_at REAL NOT NULL, "
               "last_active REAL NOT NULL)")
    db.execute("CREATE TABLE machines (machine_key TEXT PRIMARY KEY, "
               "nickname TEXT)")
    db.execute("INSERT INTO web_logins VALUES ('owner','admin',?,'t','t',NULL)",
               (PW_HASH,))
    db.execute("INSERT INTO web_logins VALUES ('floor','normal',?,'t','t',NULL)",
               (PW_HASH,))
    db.execute("INSERT INTO web_sessions VALUES (?,'owner',1.0,2.0)",
               (SESSION,))
    db.execute("INSERT INTO machines VALUES ('smib-1/1','A560')")
    db.commit()
    db.close()


def snapshot(folder):
    """{name: bytes} of a folder, to prove nothing in it was touched. SQLite's
    own -wal/-shm working files are not compared: reading a WAL database
    creates them when the hub is not running, as it always has."""
    return {n: open(os.path.join(folder, n), "rb").read()
            for n in sorted(os.listdir(folder))
            if not n.endswith(("-wal", "-shm"))}


def bundle_of(data, out):
    b = sb.Bundle(out)
    try:
        sb.collect_host_data(b, data)
    finally:
        b.close()
    members = {}
    with tarfile.open(out, "r:gz") as tar:
        for m in tar.getmembers():
            members[m.name] = tar.extractfile(m).read()
    return members


def main():
    with tempfile.TemporaryDirectory(prefix="bundle_gate_") as tmp:
        # the script's own temp files go to a folder of ours, so "nothing is
        # left behind" is judged on this run alone, whatever /tmp holds
        scratch = os.path.join(tmp, "scratch")
        os.makedirs(scratch)
        tempfile.tempdir = scratch
        data = os.path.join(tmp, "G2S", "data")
        make_hub_data(data)
        before = snapshot(data)
        members = bundle_of(data, os.path.join(tmp, "out.tar.gz"))
        after = snapshot(data)
        raw = open(os.path.join(tmp, "out.tar.gz"), "rb").read()
        manifest = members.get("MANIFEST.txt", b"").decode("utf-8", "replace")

        print("— none of the secrets is anywhere in the bundle")
        everything = b"\n".join(members.values())
        for what, secret in SECRETS.items():
            check(f"{what} is not in any file of the bundle",
                  secret.encode() not in everything)
        check("...nor in the archive's own bytes",
              not any(s.encode() in raw for s in SECRETS.values()))
        check("the token file is not a member at all",
              "host/data/hub_api_token" not in members, sorted(members))
        check("...and the manifest says it was left out",
              "SKIP  host/data/hub_api_token" in manifest, manifest)

        print("— the database copy opens, cleaned")
        dbfile = os.path.join(tmp, "copy.db")
        with open(dbfile, "wb") as f:
            f.write(members.get("host/data/hub.db", b""))
        try:
            db = sqlite3.connect(dbfile)
            logins = db.execute("SELECT username, role, pw FROM web_logins "
                                "ORDER BY username").fetchall()
            sessions = db.execute("SELECT COUNT(*) FROM web_sessions"
                                  ).fetchone()[0]
            machines = db.execute("SELECT * FROM machines").fetchall()
            intact = db.execute("PRAGMA integrity_check").fetchone()[0]
            db.close()
        except sqlite3.Error as e:
            logins, sessions, machines, intact = [], -1, [], str(e)
        check("usernames and roles are kept, the hashes are blank",
              logins == [("floor", "normal", ""), ("owner", "admin", "")],
              logins)
        check("signed-in sessions are gone", sessions == 0, sessions)
        check("other tables are untouched", machines == [("smib-1/1", "A560")],
              machines)
        check("the copy is a sound database", intact == "ok", intact)
        check("no stray journal files ride along",
              not [n for n in members if n.endswith(("-wal", "-shm", ".raw"))],
              sorted(members))

        print("— the state file keeps what a diagnosis needs")
        for name in ("host/data/account_state.json",
                     "host/data/account_state.json.corrupt-1"):
            try:
                state = json.loads(members.get(name, b"{}"))
            except ValueError as e:
                state = {"error": str(e)}
            p1 = state.get("accounts", {}).get("p1", {})
            check(f"{name.split('/')[-1]}: still valid, player and balance "
                  "kept", p1.get("name") == "Sam"
                  and p1.get("cashableMillicents") == 1234500, state)
            check("...and it still shows that the player HAS a PIN",
                  p1.get("pinHash") == sb.REMOVED
                  and state.get("pinSecret") == sb.REMOVED, p1)
        check("a file with no secrets goes in byte for byte",
              members.get("host/data/dhcp_leases.json")
              == before["dhcp_leases.json"])

        print("— the hub's own files are left exactly as they were")
        check("no file in the data folder changed, appeared or vanished",
              before == after,
              [n for n in set(before) | set(after)
               if before.get(n) != after.get(n)])
        live = sqlite3.connect(os.path.join(data, "hub.db"))
        check("the live database still has its logins and session",
              live.execute("SELECT COUNT(*) FROM web_logins WHERE pw=?",
                           (PW_HASH,)).fetchone()[0] == 2
              and live.execute("SELECT COUNT(*) FROM web_sessions"
                               ).fetchone()[0] == 1)
        live.close()

        print("— a database that cannot be cleaned is left out, not sent raw")
        broken = os.path.join(tmp, "broken", "data")
        os.makedirs(broken)
        with open(os.path.join(broken, "hub.db"), "wb") as f:
            f.write(b"not a database " + PW_HASH.encode() + b"\x00" * 4096)
        members2 = bundle_of(broken, os.path.join(tmp, "out2.tar.gz"))
        check("nothing of it is in the bundle",
              PW_HASH.encode() not in b"\n".join(members2.values())
              and not [n for n in members2 if "hub.db" in n], sorted(members2))
        check("...and the manifest says why",
              "SKIP  host/data/hub.db" in members2.get(
                  "MANIFEST.txt", b"").decode("utf-8", "replace"))
        check("no snapshot is left behind in the temp folder",
              os.listdir(scratch) == [], os.listdir(scratch))
        tempfile.tempdir = None

        print("— the note inside the bundle tells the tester")
        src = (G2S.parent / "deploy" / "support_bundle.py").read_text("utf-8")
        check("the README text names what is left out",
              "Left out on purpose" in src)
        check("every listed secret has a rule",
              sb.SECRET_FILES == ("hub_api_token",)
              and set(sb.SECRET_JSON_KEYS) == {"pinSecret", "pinHash"}
              and {t for t, _ in sb.DB_SCRUB} == {"web_logins",
                                                  "web_sessions"})

    print(f"\nRESULT: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
