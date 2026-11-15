#!/usr/bin/env python3
"""hub_login.py — manage the hub's optional login from the hub's own shell.

This is the way back in when you are locked out: it works on the hub's
database directly, so it needs no sign-in — only a shell on the hub as the
account the hub runs as. Safe to run while the hub is running; the hub picks
changes up within about half a minute.

    python3 G2S/tools/hub_login.py                 show the lock and the logins
    python3 G2S/tools/hub_login.py add NAME        add an admin login (asks for a password)
    python3 G2S/tools/hub_login.py add NAME --normal
    python3 G2S/tools/hub_login.py passwd NAME     set a new password
    python3 G2S/tools/hub_login.py remove NAME
    python3 G2S/tools/hub_login.py off             turn the lock off
    python3 G2S/tools/hub_login.py on              turn it on (needs an admin login)
"""

import argparse
import getpass
import os
import sqlite3
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import web_auth  # noqa: E402  (G2S/web_auth.py)

DEFAULT_DB = os.path.join(os.path.dirname(HERE), "data", "hub.db")


def _now():
    return datetime.now(timezone.utc).astimezone().isoformat(
        timespec="seconds")


def _open(path):
    if not os.path.isfile(path):
        sys.exit("No hub database at %s — has the hub run on this machine?"
                 % path)
    c = sqlite3.connect(path, timeout=10)
    c.row_factory = sqlite3.Row
    have = {r["name"] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "web_logins" not in have:
        sys.exit("This hub's database has no logins yet. Update the hub and "
                 "start it once, then run this again.")
    return c


def _setting(c, key, default):
    r = c.execute("SELECT v FROM host_settings WHERE k=?", (key,)).fetchone()
    return r["v"] if r else default


def _set_setting(c, key, value):
    c.execute("INSERT INTO host_settings(k, v) VALUES(?,?) "
              "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, value))


def _admins(c):
    return c.execute("SELECT COUNT(*) AS n FROM web_logins WHERE "
                     "role='admin'").fetchone()["n"]


def _ask_password(pw=None):
    if pw is None:
        pw = getpass.getpass("New password: ")
        if pw != getpass.getpass("Again: "):
            sys.exit("The two passwords did not match — nothing changed.")
    err = web_auth.password_error(pw)
    if err:
        sys.exit(err + " — nothing changed.")
    return pw


def cmd_status(c, a):
    on = _setting(c, "web_login_required", "0") == "1"
    print("Hub login lock: %s" % ("ON — the operator pages need a sign-in"
                                  if on else "off — the pages are open"))
    rows = c.execute("SELECT username, role, last_login_at FROM web_logins "
                     "ORDER BY username").fetchall()
    if not rows:
        print("No logins yet.  Add one:  hub_login.py add NAME")
    for r in rows:
        print("  %-20s %-7s last signed in: %s"
              % (r["username"], r["role"], r["last_login_at"] or "never"))
    return 0


def cmd_add(c, a):
    name = a.name.strip().lower()
    err = web_auth.username_error(name)
    if err:
        sys.exit(err)
    if c.execute("SELECT 1 FROM web_logins WHERE username=?",
                 (name,)).fetchone():
        sys.exit("%r already exists — use 'passwd %s' to change its "
                 "password." % (name, name))
    pw = _ask_password(a.password)
    role = web_auth.ROLE_NORMAL if a.normal else web_auth.ROLE_ADMIN
    now = _now()
    c.execute("INSERT INTO web_logins(username, role, pw, created_at, "
              "updated_at) VALUES(?,?,?,?,?)",
              (name, role, web_auth.hash_password(pw), now, now))
    c.commit()
    print("Added %s login %r." % (role, name))
    return 0


def cmd_passwd(c, a):
    name = a.name.strip().lower()
    if not c.execute("SELECT 1 FROM web_logins WHERE username=?",
                     (name,)).fetchone():
        sys.exit("No login named %r." % name)
    pw = _ask_password(a.password)
    c.execute("UPDATE web_logins SET pw=?, updated_at=? WHERE username=?",
              (web_auth.hash_password(pw), _now(), name))
    c.execute("DELETE FROM web_sessions WHERE username=?", (name,))
    c.commit()
    print("Password changed for %r. Browsers signed in with it are signed "
          "out within half a minute." % name)
    return 0


def cmd_remove(c, a):
    name = a.name.strip().lower()
    r = c.execute("SELECT role FROM web_logins WHERE username=?",
                  (name,)).fetchone()
    if not r:
        sys.exit("No login named %r." % name)
    if r["role"] == "admin" and _admins(c) <= 1 and \
            _setting(c, "web_login_required", "0") == "1":
        sys.exit("%r is the only admin login and the lock is on — add "
                 "another admin, or run 'off' first." % name)
    c.execute("DELETE FROM web_logins WHERE username=?", (name,))
    c.execute("DELETE FROM web_sessions WHERE username=?", (name,))
    c.commit()
    print("Removed %r." % name)
    return 0


def cmd_off(c, a):
    _set_setting(c, "web_login_required", "0")
    c.commit()
    print("The lock is off — the hub pages are open to anyone on the "
          "network. Logins were kept.")
    return 0


def cmd_on(c, a):
    if not _admins(c):
        sys.exit("There is no admin login yet — add one first:  "
                 "hub_login.py add NAME")
    _set_setting(c, "web_login_required", "1")
    c.commit()
    print("The lock is on — the hub pages now need a sign-in.")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Manage the hub's optional login (works without "
                    "signing in — run it on the hub).")
    ap.add_argument("--db", default=DEFAULT_DB, help=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("status", help="show the lock and the logins (default)")
    p = sub.add_parser("add", help="add a login (admin unless --normal)")
    p.add_argument("name")
    p.add_argument("--normal", action="store_true",
                   help="a normal login instead of an admin")
    p.add_argument("--password", help=argparse.SUPPRESS)  # tests only
    p = sub.add_parser("passwd", help="set a new password")
    p.add_argument("name")
    p.add_argument("--password", help=argparse.SUPPRESS)  # tests only
    p = sub.add_parser("remove", help="delete a login")
    p.add_argument("name")
    sub.add_parser("off", help="turn the lock off")
    sub.add_parser("on", help="turn the lock on")
    a = ap.parse_args(argv)
    c = _open(a.db)
    try:
        return {"add": cmd_add, "passwd": cmd_passwd, "remove": cmd_remove,
                "off": cmd_off, "on": cmd_on}.get(a.cmd, cmd_status)(c, a)
    finally:
        c.close()


if __name__ == "__main__":
    sys.exit(main())
