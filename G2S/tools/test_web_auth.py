#!/usr/bin/env python3
"""Gate for the optional hub login (G2S/web_auth.py + the handler's gate).

Three layers:
  1. the pure parts — password hashing, the who-may-do-what tables, the
     signed-out display view;
  2. WebAuth over a real HubStore with a fake clock — sign-in, throttling,
     idle and hard time-outs, last-admin protection, the recovery tool;
  3. the real request handler over a real socket — with the lock OFF nothing
     changes for anyone; with it ON machines and displays still get through,
     signed-out people do not, a normal login runs the floor and an admin
     does everything.

Must end "RESULT: N passed, 0 failed".
"""

import http.client
import json
import os
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import web_auth as wa  # noqa: E402
from hub_store import HubStore  # noqa: E402
import g2s_host  # noqa: E402
import hub_login  # noqa: E402

_p = _f = 0


def check(name, ok, detail=""):
    global _p, _f
    if ok:
        _p += 1
        print(f"  ✅ {name}")
    else:
        _f += 1
        print(f"  ❌ {name} {detail}")


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def pure():
    print("\n== passwords ==")
    h = wa.hash_password("correct horse")
    check("hash is salted (two hashes of one password differ)",
          h != wa.hash_password("correct horse"))
    check("the password is not in the hash", "correct horse" not in h)
    check("right password verifies", wa.check_password("correct horse", h))
    check("wrong password is refused", not wa.check_password("wrong", h))
    check("pbkdf2 form verifies too", wa.check_password(
        "pw12345678", "pbkdf2$1000$00ff$" + __import__("hashlib").pbkdf2_hmac(
            "sha256", b"pw12345678", bytes.fromhex("00ff"), 1000,
            dklen=32).hex()))
    for junk in ("", "scrypt$x", "plain", "scrypt$14$8$1$zz$zz", None):
        check(f"garbage stored value {junk!r} is refused, not a crash",
              wa.check_password("x", junk) is False)
    check("short password refused", wa.password_error("1234567"))
    check("8-character password accepted",
          wa.password_error("12345678") is None)
    check("non-string password refused", wa.password_error(None))
    for bad in ("", "Admin", "a b", "-x", "x" * 33, None, "a/b"):
        check(f"username {bad!r} refused", wa.username_error(bad))
    for good in ("aj", "floor.staff", "night_shift-2", "7up"):
        check(f"username {good!r} accepted",
              wa.username_error(good) is None)

    print("\n== who may do what: GET ==")
    P, N, A = wa.PUBLIC, wa.NORMAL, wa.ADMIN
    for path, want in (
            ("/", N), ("/home", N), ("/ui", N), ("/webui/home.html", N),
            ("/test", A), ("/console", A), ("/webui/index.html", A),
            ("/webui/console.html", A),
            # the glass route serves any page by its last segment — it must
            # not be a side door to the operator pages
            ("/webui/glass/EGM/1/home.html", N),
            ("/webui/glass/EGM/1/index.html", A),
            ("/webui/glass.html", P), ("/webui/smib.html", P),
            ("/webui/glass/EGM/1/glass.html", P), ("/webui/login.html", P),
            ("/webui/board.html", P), ("/board", P), ("/login", P),
            ("/api/status", P), ("/api/accounts", P), ("/api/hubkey", P),
            ("/api/glass/state", P), ("/api/auth/state", P),
            ("/api/players", N), ("/api/fobs", N), ("/api/vouchers", N),
            ("/api/tito/tickets", N), ("/api/names", N),
            ("/api/gamenames", N), ("/api/fleet/check", N),
            ("/api/debug/log", A), ("/api/auth/logins", A),
            ("/api/something/new", N)):
        check(f"GET {path} -> {('public', 'normal', 'admin')[want]}",
              wa.get_level(path)[0] == want, wa.get_level(path))

    print("\n== who may do what: POST ==")
    j = json.dumps
    for path, body, want in (
            ("/G2S", "<soap/>", P), ("/anything", "", P),
            ("/api/sas/report", "{}", P), ("/api/companion/report", "{}", P),
            ("/api/glass/pinlogin", "{}", P), ("/api/glass/action", "{}", P),
            ("/api/auth/login", "{}", P), ("/api/auth/logout", "{}", P),
            ("/api/auth/touch", "{}", N), ("/api/auth/password", "{}", N),
            ("/api/auth/logins", "{}", A), ("/api/auth/config", "{}", A),
            ("/api/auth/loginsx", "{}", A), ("/api/auth/login/x", "{}", A),
            ("/api/auth/", "{}", A),
            ("/api/tito/mint", j({"smibId": "smib-1"}), P),
            ("/api/tito/issued", "{}", P), ("/api/tito/authorize", "{}", P),
            ("/api/tito/commit", "{}", P), ("/api/tito/sync", "{}", P),
            ("/api/tito/void", j({"smibId": "smib-1"}), P),
            ("/api/tito/void", j({"smibId": "webui"}), N),
            ("/api/tito/reissue", j({"smibId": "smib-1"}), A),
            ("/api/tito/reissue", j({"smibId": "webui"}), A),
            ("/api/tito/reusable", "{}", A),
            ("/api/tito/somethingnew", "{}", A),
            ("/api/command", j({"action": "clearHandpay"}), N),
            ("/api/command", j({"action": "voidValidationId"}), N),
            ("/api/command", j({"action": "setCabinetState"}), N),
            ("/api/command", j({"action": "floorLock"}), N),
            ("/api/command", j({"action": "addCredits"}), N),
            ("/api/command", j({"action": "getCabinetStatus"}), N),
            ("/api/command", j({"action": "setOption"}), A),
            ("/api/command", j({"action": "applyUpdate"}), A),
            ("/api/command", j({"action": "rebootEgmScript"}), A),
            ("/api/command", j({"action": "claimOwnership"}), A),
            ("/api/command", j({"action": "tournamentConfigure"}), A),
            ("/api/command", j({"action": "bonusAward"}), A),
            ("/api/command", "not json", A),
            ("/api/command", j(["clearHandpay"]), A),
            ("/api/command", j({"action": 7}), A),
            ("/api/sas/command", j({"type": "handpay_reset"}), N),
            ("/api/sas/command", j({"type": "sas_disable"}), N),
            ("/api/sas/command", j({"type": "aft_transfer"}), N),
            ("/api/sas/command", j({"type": "aft_register"}), A),
            ("/api/sas/command", j({"type": "set_validation_id"}), A),
            ("/api/accounts", j({"action": "adjust"}), N),
            ("/api/accounts", j({"action": "create"}), A),
            ("/api/accounts", j({"action": "delete"}), A),
            ("/api/settings", "{}", A), ("/api/players", "{}", A),
            ("/api/fobs", "{}", A), ("/api/names", "{}", A),
            ("/api/gamenames", "{}", A), ("/api/sas/forget", "{}", A),
            ("/api/g2s/forget", "{}", A), ("/api/fleet/fix", "{}", A),
            ("/api/fleet/check", "{}", A),
            ("/api/brandnew", "{}", A)):
        got = wa.post_level(path, body)
        check(f"POST {path} {body[:34]} -> "
              f"{('public', 'normal', 'admin')[want]}", got == want, got)
    check("nobody: public only",
          wa.allows(None, P) and not wa.allows(None, N))
    check("normal: normal but not admin",
          wa.allows({"role": "normal"}, N)
          and not wa.allows({"role": "normal"}, A))
    check("admin: everything", wa.allows({"role": "admin"}, A))
    check("an unknown role gets nothing",
          not wa.allows({"role": "root"}, N))

    print("\n== the signed-out display view ==")
    full = {
        "_engine": {"version": "9.9", "platform": "linux",
                    "hostBase": "http://10.0.0.2:8081"},
        "EGM_1": {"egmId": "EGM_1", "commsState": "onLine", "sasLink": "s/1",
                  "cabinet": {"identity": {"mfr": "X"}, "secret": 1},
                  "recentVouchers": [{"validationId": "123456789012345678"}],
                  "meters": {"a": 1}, "recentHandpays": [],
                  "pendingHandpays": []},
        "sas": {"s/1": {"online": True, "peer": "10.0.0.9", "sshUser": "pi",
                        "tickets": [{"vn": "1"}], "nickname": "Left",
                        "denomSuggest": {"state": "match"}}},
        "names": {"EGM_1": "Big Red"}, "tournament": {"phase": "idle"},
        "cardSessions": {}, "boardStamp": 5, "uiStamp": 6,
        "devices": {"abc": {"last_ip": "10.0.0.9"}},
        "glassSessions": {"byEgm": {"EGM_1": {"token": "t"}}},
        "tito": {"total": 3}, "companions": {"c": {}},
        "hostOptions": {"boardEnabled": True, "gameroomName": "Den",
                        "currencySymbol": "$", "pinAdminAllowed": True,
                        "updates": {"current": "abc"}},
        "activity": [{"label": "🎫 Ticket-out minted — vn 0012345678901234 "
                               "($5.00)", "icon": "🎫",
                      "extras": {"validation": "0012345678901234"}}]}
    pub = json.loads(wa.public_status(json.dumps(full)))
    txt = json.dumps(pub)
    check("says it is the display view", pub.get("loginRequired") is True)
    check("still looks like a CabiNet hub to a satellite probe",
          "_engine" in pub and pub["_engine"].get("version") == "9.9")
    check("board fields kept (machine state, names, tournament)",
          pub["EGM_1"]["commsState"] == "onLine"
          and pub["EGM_1"]["cabinet"] == {"identity": {"mfr": "X"}}
          and pub["names"] == {"EGM_1": "Big Red"}
          and pub["tournament"] == {"phase": "idle"}
          and pub["sas"]["s/1"]["online"] is True
          and pub["hostOptions"] == {"boardEnabled": True,
                                     "gameroomName": "Den",
                                     "currencySymbol": "$"})
    for secret in ("123456789012345678", "0012345678901234", "10.0.0.9",
                   "sshUser", "glassSessions", "devices", "meters",
                   "recentVouchers", "pinAdminAllowed", "updates", "extras",
                   "hostBase", "tickets", "companions", "uiStamp"):
        check(f"display view leaks no {secret}", secret not in txt)
    check("a ticket number on the tape keeps only its last four digits",
          "…1234" in pub["activity"][0]["label"])
    pa = wa.public_accounts({"accounts": [
        {"id": "a1", "name": "Sam", "kind": "player", "admin": True,
         "hasPin": True, "cashableMillicents": 5}], "ledger": [{"x": 1}]})
    check("display accounts: names and balances, no flags, no ledger",
          pa["accounts"] == [{"id": "a1", "name": "Sam", "kind": "player",
                              "cashableMillicents": 5}]
          and pa["ledger"] == [])

    print("\n== cookies ==")
    tok = "t" * 43
    check("token read back out of a Cookie header",
          wa.cookie_token(f"a=b; {wa.COOKIE}={tok}; c=d") == tok)
    check("no cookie -> ''", wa.cookie_token(None) == ""
          and wa.cookie_token("a=b") == "")
    check("an absurd cookie value is ignored",
          wa.cookie_token(f"{wa.COOKIE}=x") == "")
    sc = wa.set_cookie(tok)
    check("session cookie is HttpOnly and SameSite=Strict",
          "HttpOnly" in sc and "SameSite=Strict" in sc and "Path=/" in sc)


def sessions():
    print("\n== sign-in, sessions, time-outs ==")
    d = tempfile.mkdtemp()
    store = HubStore(os.path.join(d, "hub.db"))
    clk = Clock()
    a = wa.WebAuth(store, d, clock=clk)
    hdr = lambda t: {"Cookie": f"{wa.COOKIE}={t}"}  # noqa: E731

    check("off by default", a.required() is False)
    check("a local-tool secret was minted, 0600",
          len(a.local_token) >= 32 and
          (os.name == "nt" or
           os.stat(wa.token_path(d)).st_mode & 0o777 == 0o600))
    check("a second start reuses the same secret",
          wa.WebAuth(store, d).local_token == a.local_token)
    try:
        a.configure(required=True)
        check("lock cannot be turned on with no admin login", False)
    except ValueError:
        check("lock cannot be turned on with no admin login", True)
    for bad in (dict(username="Bad Name", role="admin", password="x" * 8),
                dict(username="ok", role="root", password="x" * 8),
                dict(username="ok", role="admin", password="short"),
                dict(username="ok", role="admin"),
                dict(username="ok", password="x" * 8)):
        try:
            a.set_login(**bad)
            check(f"bad login {bad} refused", False)
        except ValueError:
            check(f"bad login {sorted(bad)} refused", True)
    check("create admin", a.set_login("boss", "admin", "hunter2-hunter2")
          == "created")
    check("create normal", a.set_login("staff", "normal", "floor-pass-1")
          == "created")
    rec = store.login_get("boss")
    check("the password is stored hashed", "hunter2" not in rec["pw"]
          and rec["pw"].split("$")[0] in ("scrypt", "pbkdf2"))
    a.configure(required=True)
    check("lock on", a.required() is True)

    tok, res = a.login("boss", "nope", "10.0.0.5")
    check("wrong password refused", tok is None
          and res["error"] == "wrong username or password")
    tok, res2 = a.login("ghost", "nope", "10.0.0.5")
    check("unknown user gets the SAME answer", tok is None
          and res2["error"] == res["error"])
    tok, role = a.login(" Boss ", "hunter2-hunter2", "10.0.0.5")
    check("right password signs in (name is case/space tolerant)",
          bool(tok) and role == "admin")
    who = a.identify(hdr(tok))
    check("the cookie identifies the admin", who and who["user"] == "boss"
          and who["role"] == "admin" and who["local"] is False)
    check("the database holds a hash of the token, never the token",
          store.session_get(wa.token_hash(tok)) is not None
          and store.session_get(tok) is None)
    check("no cookie -> nobody", a.identify({}) is None)
    check("made-up cookie -> nobody", a.identify(hdr("z" * 43)) is None)
    check("the local-tool secret is an admin",
          a.identify({wa.TOKEN_HEADER: a.local_token})["role"] == "admin")
    check("a wrong secret is nobody",
          a.identify({wa.TOKEN_HEADER: "nope"}) is None)

    print("\n== idle time-out ==")
    clk.t += 29 * 60
    check("29 min of polling only: still signed in",
          a.identify(hdr(tok), active=False) is not None)
    clk.t += 2 * 60
    check("31 min with no real use: signed out, even though it polled",
          a.identify(hdr(tok), active=False) is None)
    check("...and the session is gone from the database",
          store.session_get(wa.token_hash(tok)) is None)
    tok, _ = a.login("boss", "hunter2-hunter2", "10.0.0.5")
    for _ in range(6):
        clk.t += 20 * 60
        ok = a.identify(hdr(tok), active=True)
    check("2 hours of real use every 20 min: still signed in", ok is not None)
    a.configure(idle_min=0)
    clk.t += 5 * 24 * 3600
    check("idle 'never': still signed in 5 days later",
          a.identify(hdr(tok)) is not None)
    clk.t += 26 * 24 * 3600
    check("...but no session outlives 30 days",
          a.identify(hdr(tok), active=True) is None)
    a.configure(idle_min=30)
    for bad in (-1, 1.5, "30", True, 10 ** 9):
        try:
            a.configure(idle_min=bad)
            check(f"idleMin {bad!r} refused", False)
        except ValueError:
            check(f"idleMin {bad!r} refused", True)

    print("\n== a restart keeps people signed in ==")
    tok, _ = a.login("staff", "floor-pass-1", "10.0.0.6")
    clk.t += 5 * 60
    a.identify(hdr(tok), active=True)       # activity written back to db
    b = wa.WebAuth(store, d, clock=clk)     # "the hub restarted"
    who = b.identify(hdr(tok))
    check("session survives a restart", who and who["user"] == "staff"
          and who["role"] == "normal")
    clk.t += 20 * 60
    check("...and the restart did not reset its idle clock backwards",
          b.identify(hdr(tok)) is not None)
    clk.t += 20 * 60
    check("...nor forwards", b.identify(hdr(tok)) is None)

    print("\n== changes apply to people already signed in ==")
    tok, _ = a.login("staff", "floor-pass-1", "10.0.0.6")
    a.set_login("staff", role="admin")
    check("promotion applies at once",
          a.identify(hdr(tok))["role"] == "admin")
    a.set_login("staff", role="normal")
    check("demotion applies at once",
          a.identify(hdr(tok))["role"] == "normal")
    a.set_login("staff", password="new-floor-pass")
    check("a new password signs the old session out",
          a.identify(hdr(tok)) is None)
    check("old password no longer works",
          a.login("staff", "floor-pass-1", "10.0.0.6")[0] is None)
    tok, _ = a.login("staff", "new-floor-pass", "10.0.0.6")
    a.logout(tok)
    check("sign out ends the session", a.identify(hdr(tok)) is None)
    tok, _ = a.login("staff", "new-floor-pass", "10.0.0.6")
    check("delete login", a.delete_login("staff") is True)
    check("...its session dies with it", a.identify(hdr(tok)) is None)
    check("deleting an unknown login is False",
          a.delete_login("staff") is False)

    print("\n== never lock everyone out ==")
    for what, fn in (("delete", lambda: a.delete_login("boss")),
                     ("demote", lambda: a.set_login("boss", role="normal"))):
        try:
            fn()
            check(f"cannot {what} the only admin while locked", False)
        except ValueError:
            check(f"cannot {what} the only admin while locked", True)
    a.set_login("boss2", "admin", "another-admin-1")
    check("with a second admin, the first can be demoted",
          a.set_login("boss", role="normal") == "updated")
    a.set_login("boss", role="admin")

    print("\n== wrong-password throttle ==")
    for i in range(wa.FAIL_LOCK_AFTER):
        t, r = a.login("boss", "bad", "10.0.0.7")
    check("5 wrong tries: refused", t is None)
    t, r = a.login("boss", "hunter2-hunter2", "10.0.0.7")
    check("...then even the right password must wait",
          t is None and r.get("retrySec", 0) > 0)
    check("...but only from that address",
          a.login("boss", "hunter2-hunter2", "10.0.0.8")[0] is not None)
    clk.t += wa.FAIL_LOCK_BASE_SEC + 1
    check("after the wait the right password works",
          a.login("boss", "hunter2-hunter2", "10.0.0.7")[0] is not None)

    print("\n== the recovery tool (hub_login.py) ==")
    db = os.path.join(d, "hub.db")
    tok, _ = a.login("boss", "hunter2-hunter2", "10.0.0.5")

    def tool(*argv):
        import contextlib
        import io
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                rc = hub_login.main(["--db", db, *argv])
        except SystemExit as e:
            return 1, str(e.code)
        return rc, buf.getvalue()

    rc, out = tool()
    check("status lists the lock and the logins",
          rc == 0 and "ON" in out and "boss" in out and "boss2" in out)
    rc, out = tool("passwd", "boss", "--password", "reset-by-tool-1")
    check("passwd resets a password", rc == 0, out)
    clk.t += wa.SESSION_RECHECK_SEC + 1
    check("...and the hub signs the old session out within half a minute",
          a.identify(hdr(tok)) is None)
    check("...new password works",
          a.login("boss", "reset-by-tool-1", "10.0.0.5")[0] is not None)
    rc, out = tool("passwd", "boss", "--password", "short")
    check("tool refuses a short password", rc == 1)
    rc, out = tool("add", "rescue", "--password", "rescue-pass-1")
    check("add makes an admin by default",
          rc == 0 and store.login_get("rescue")["role"] == "admin")
    rc, out = tool("add", "helper", "--normal", "--password", "helper-pw-1")
    check("add --normal makes a normal login",
          rc == 0 and store.login_get("helper")["role"] == "normal")
    rc, out = tool("add", "rescue", "--password", "rescue-pass-1")
    check("add refuses a duplicate", rc == 1)
    rc, out = tool("off")
    clk.t += wa.SETTING_TTL_SEC + 1
    check("off turns the lock off, and the hub notices",
          rc == 0 and a.required() is False)
    check("...logins are kept", store.login_get("boss") is not None)
    rc, out = tool("on")
    clk.t += wa.SETTING_TTL_SEC + 1
    check("on turns it back on", rc == 0 and a.required() is True)
    for n in ("boss2", "rescue"):
        tool("remove", n)
    rc, out = tool("remove", "boss")
    check("tool will not remove the only admin while locked",
          rc == 1 and store.login_get("boss") is not None)
    d2 = tempfile.mkdtemp()
    rc, out = tool("--db", os.path.join(d2, "none.db"))
    check("no database: a plain message, not a traceback",
          rc == 1 and "No hub database" in out)


# ---- the real handler over a real socket ----------------------------------

class _Any:
    """Stands in for the engine parts the gate never looks at."""

    def __getattr__(self, name):
        return lambda *a, **k: {}


class _Accounts:
    def snapshot(self):
        return {"accounts": [{"id": "a1", "name": "Sam", "kind": "player",
                              "admin": True, "cashableMillicents": 5}],
                "ledger": [{"secret": 1}]}


class _Engine:
    def __init__(self, store, auth):
        self.hub_store = store
        self.web_auth = auth
        self.account_store = _Accounts()
        self.voucher_store = _Any()
        self.host_log_file = None
        self.status_cache_lock = threading.Lock()
        self.status_state_seq = 0
        body = json.dumps({
            "_engine": {"version": "t"},
            "EGM_1": {"egmId": "EGM_1", "commsState": "onLine",
                      "meters": {"secretMeter": 1}},
            "sas": {"s/1": {"online": True, "peer": "10.9.9.9"}}})
        ent = {"body": body, "etag": '"e1"', "at": time.time(), "seq": 0}
        self.status_cache = {"slim": dict(ent), "full": dict(ent)}
        # "a rebuild is in flight" -> the handler serves the cached body
        self.status_cache_building = {"slim": True, "full": True}

    def hub_public_key(self):
        return "ssh-ed25519 AAAA test"


def _make_handler(engine):
    class H(g2s_host.G2SRequestHandler):
        host_engine = engine

    # Past the gate, the operator endpoints do real work on a real engine.
    # Here they only need to say they were reached.
    def reached(name):
        def f(self, *a, **k):
            return self._json(200, {"reached": name})
        return f
    for n in ("_handle_command", "_handle_tito", "_handle_sas_report",
              "_handle_companion_report", "_handle_sas_command",
              "_handle_settings", "_handle_accounts", "_handle_players",
              "_handle_fobs", "_handle_set_name", "_handle_game_name",
              "_handle_sas_forget", "_handle_g2s_forget",
              "_handle_fleet_check", "_handle_fleet_fix",
              "_handle_glass_pin_login", "_handle_glass_action"):
        setattr(H, n, reached(n))
    return H


class Client:
    def __init__(self, port):
        self.port = port
        self.cookie = None

    def req(self, method, path, body=None, headers=None, keep=True):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = dict(headers or {})
        if self.cookie and keep:
            h["Cookie"] = self.cookie
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else (
                body if isinstance(body, str) else json.dumps(body)
            ).encode()
            h.setdefault("Content-Type", "application/json")
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        raw = r.read().decode("utf-8", "replace")
        sc = r.getheader("Set-Cookie")
        if sc and keep:
            v = sc.split(";", 1)[0]
            self.cookie = None if v.endswith("=") else v
        try:
            js = json.loads(raw)
        except ValueError:
            js = None
        c.close()
        return r.status, js, raw, r

    def get(self, path, **k):
        return self.req("GET", path, **k)

    def post(self, path, body=None, **k):
        return self.req("POST", path, body if body is not None else {}, **k)


def http_layer():
    d = tempfile.mkdtemp()
    store = HubStore(os.path.join(d, "hub.db"))
    auth = wa.WebAuth(store, d)
    eng = _Engine(store, auth)
    srv = g2s_host.ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(eng))
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    anon, staff, boss = Client(port), Client(port), Client(port)

    OPERATOR_POSTS = (
        ("/api/command", {"action": "clearHandpay"}),
        ("/api/command", {"action": "setOption"}),
        ("/api/sas/command", {"type": "handpay_reset"}),
        ("/api/sas/command", {"type": "aft_register"}),
        ("/api/accounts", {"action": "adjust"}),
        ("/api/accounts", {"action": "create"}),
        ("/api/tito/void", {"smibId": "webui"}),
        ("/api/tito/reissue", {"smibId": "webui"}),
        ("/api/tito/reusable", {"smibId": "webui"}),
        ("/api/settings", {}), ("/api/players", {}), ("/api/fobs", {}),
        ("/api/names", {}), ("/api/gamenames", {}), ("/api/sas/forget", {}),
        ("/api/g2s/forget", {}), ("/api/fleet/fix", {}))
    MACHINE_POSTS = (
        ("/api/sas/report", {}), ("/api/companion/report", {}),
        ("/api/tito/mint", {"smibId": "smib-1"}),
        ("/api/tito/issued", {"smibId": "smib-1"}),
        ("/api/tito/authorize", {"smibId": "smib-1"}),
        ("/api/tito/commit", {"smibId": "smib-1"}),
        ("/api/tito/sync", {"smibId": "smib-1"}),
        ("/api/tito/void", {"smibId": "smib-1"}),
        ("/api/glass/pinlogin", {}), ("/api/glass/action", {}))

    print("\n== lock OFF: nothing changes for anyone ==")
    for path, body in OPERATOR_POSTS + MACHINE_POSTS:
        st, js, _, _ = anon.post(path, body)
        check(f"open: POST {path} {json.dumps(body)[:30]} reaches its "
              "handler", st == 200 and js and "reached" in js, (st, js))
    st, js, raw, _ = anon.get("/api/status")
    check("open: /api/status is the full body",
          st == 200 and "secretMeter" in raw and "loginRequired" not in raw)
    st, js, raw, _ = anon.get("/api/accounts")
    check("open: /api/accounts is the full snapshot",
          st == 200 and js["ledger"] and js["accounts"][0].get("admin"))
    st, js, _, _ = anon.get("/api/auth/state")
    check("open: state says not required, everyone is the operator",
          js == {"required": False, "signedIn": False, "user": None,
                 "role": "admin", "idleMin": 30}, js)
    st, _, _, r = anon.get("/login")
    check("open: /login just sends you to the hub page",
          st == 302 and r.getheader("Location") == "/")
    for page in ("/", "/home", "/test", "/console", "/board",
                 "/webui/glass.html"):
        st, _, raw, _ = anon.get(page)
        check(f"open: page {page} is served", st == 200 and "<" in raw, st)

    print("\n== setting it up ==")
    st, js, _, _ = anon.post("/api/auth/config", {"required": True})
    check("cannot lock with no logins", js["ok"] is False, js)
    st, js, _, _ = anon.post("/api/auth/logins", {
        "action": "set", "username": "boss", "role": "admin",
        "password": "hunter2-hunter2"})
    check("add admin login", js == {"ok": True, "result": "created"}, js)
    st, js, _, _ = anon.post("/api/auth/logins", {
        "action": "set", "username": "staff", "role": "normal",
        "password": "floor-pass-1"})
    check("add normal login", js.get("ok") is True, js)
    st, js, _, _ = anon.post("/api/auth/logins", {
        "action": "set", "username": "x", "role": "admin",
        "password": "short"})
    check("short password refused with a reason",
          js["ok"] is False and "8" in js["error"], js)
    st, js, _, _ = anon.post("/api/auth/config", {"required": True})
    check("turning the lock on needs proof an admin login works",
          js["ok"] is False and not auth.required(), js)
    st, js, _, _ = anon.post("/api/auth/config", {
        "required": True, "username": "staff", "password": "floor-pass-1"})
    check("...a normal login is not enough",
          js["ok"] is False and not auth.required(), js)
    st, js, _, _ = anon.post("/api/auth/config", {
        "required": True, "username": "boss", "password": "wrong"},
        keep=False)
    check("...nor a wrong password", js["ok"] is False
          and not auth.required(), js)
    st, js, _, _ = boss.post("/api/auth/config", {
        "required": True, "username": "boss",
        "password": "hunter2-hunter2"})
    check("admin turns the lock on and is left signed in",
          js.get("ok") is True and js["required"] is True
          and boss.cookie is not None, js)

    print("\n== lock ON: signed-out people ==")
    anon.cookie = None
    for path, body in OPERATOR_POSTS:
        st, js, _, _ = anon.post(path, body)
        check(f"signed out: POST {path} {json.dumps(body)[:30]} -> 401",
              st == 401 and js.get("login") is True, (st, js))
    for path in ("/api/players", "/api/fobs", "/api/vouchers",
                 "/api/tito/tickets", "/api/names", "/api/gamenames",
                 "/api/debug/log", "/api/fleet/check", "/api/auth/logins"):
        st, js, _, _ = anon.get(path)
        check(f"signed out: GET {path} -> 401", st == 401, st)
    for page in ("/", "/home", "/test", "/console", "/webui/home.html",
                 "/webui/index.html", "/webui/glass/E/1/home.html"):
        st, _, _, r = anon.get(page)
        loc = r.getheader("Location") or ""
        check(f"signed out: page {page} -> the sign-in page",
              st == 302 and loc.startswith("/login?next="), (st, loc))
    st, _, raw, _ = anon.get("/login")
    check("signed out: /login is the sign-in form",
          st == 200 and "/api/auth/login" in raw)
    st, js, _, _ = anon.post("/api/auth/logins", {
        "action": "set", "username": "evil", "role": "admin",
        "password": "evil-evil-evil"})
    check("signed out: cannot add a login",
          st == 401 and store.login_get("evil") is None)
    st, js, _, _ = anon.post("/api/auth/config", {"required": False})
    check("signed out: cannot turn the lock off",
          st == 401 and auth.required())

    print("\n== lock ON: machines and displays carry on ==")
    for path, body in MACHINE_POSTS:
        st, js, _, _ = anon.post(path, body)
        check(f"machine: POST {path} still reaches its handler",
              st == 200 and js and "reached" in js, (st, js))
    st, _, raw, _ = anon.req("POST", "/G2S", "<not-soap", headers={
        "Content-Type": "text/xml"})
    check("machine: a G2S POST is not asked to sign in (it gets the SOAP "
          "path's own answer)", st not in (401, 403, 302), st)
    for page in ("/board", "/webui/board.html", "/webui/glass.html",
                 "/webui/smib.html", "/webui/hello.html",
                 "/webui/glass/EGM_1/1/glass.html"):
        st, _, raw, _ = anon.get(page)
        check(f"display: {page} is served", st == 200 and "<" in raw, st)
    st, js, raw, _ = anon.get("/api/status?slim=1")
    check("display: /api/status answers, as the display view",
          st == 200 and js.get("loginRequired") is True
          and "_engine" in js and js["EGM_1"]["commsState"] == "onLine")
    check("display: ...with nothing it should not carry",
          "secretMeter" not in raw and "10.9.9.9" not in raw)
    st, js, raw, _ = anon.get("/api/status")
    check("display: asking for the full status still gets the display view",
          js.get("loginRequired") is True and "secretMeter" not in raw)
    st, js, _, _ = anon.get("/api/accounts")
    check("display: /api/accounts has names and balances only",
          st == 200 and js["ledger"] == []
          and "admin" not in js["accounts"][0])
    st, js, _, _ = anon.get("/api/hubkey")
    check("setup: a new satellite can still fetch the hub's public key",
          st == 200 and js.get("publicKey"))
    st, js, _, _ = anon.get("/api/auth/state")
    check("state for a signed-out browser gives nothing away",
          js == {"required": True, "signedIn": False, "user": None,
                 "role": None}, js)

    print("\n== lock ON: signing in ==")
    st, js, _, _ = staff.post("/api/auth/login", {
        "username": "staff", "password": "nope"})
    check("wrong password -> 401, no cookie",
          st == 401 and staff.cookie is None)
    st, js, _, r = staff.post("/api/auth/login", {
        "username": "staff", "password": "floor-pass-1"})
    check("right password -> a session cookie",
          st == 200 and js == {"ok": True, "role": "normal"}
          and "HttpOnly" in (r.getheader("Set-Cookie") or ""))

    print("\n== lock ON: a normal login runs the floor ==")
    NORMAL_OK = {("/api/command", "clearHandpay"),
                 ("/api/sas/command", "handpay_reset"),
                 ("/api/accounts", "adjust"), ("/api/tito/void", "webui")}
    for path, body in OPERATOR_POSTS:
        key = (path, body.get("action") or body.get("type")
               or body.get("smibId"))
        st, js, _, _ = staff.post(path, body)
        if key in NORMAL_OK:
            check(f"normal: POST {path} {key[1]} allowed",
                  st == 200 and "reached" in js, (st, js))
        else:
            check(f"normal: POST {path} {key[1] or ''} -> 403 admin only",
                  st == 403 and js.get("admin") is True, (st, js))
    st, js, raw, _ = staff.get("/api/status")
    check("normal: sees the full status",
          st == 200 and "secretMeter" in raw and "loginRequired" not in raw)
    st, js, _, _ = staff.get("/api/accounts")
    check("normal: sees the full accounts", js["ledger"])
    for path in ("/api/debug/log", "/api/auth/logins"):
        st, js, _, _ = staff.get(path)
        check(f"normal: GET {path} -> 403", st == 403, st)
    st, _, raw, _ = staff.get("/")
    check("normal: the hub page is served", st == 200 and "<" in raw)
    for page in ("/test", "/console"):
        st, _, raw, _ = staff.get(page)
        check(f"normal: {page} -> an 'Admin only' page, not a redirect",
              st == 403 and "Admin only" in raw, st)
    st, js, _, _ = staff.get("/api/auth/state")
    check("normal: state names them",
          js.get("user") == "staff" and js.get("role") == "normal")
    st, js, _, _ = staff.post("/api/auth/logins", {
        "action": "set", "username": "staff", "role": "admin"})
    check("normal: cannot promote themselves",
          st == 403 and store.login_get("staff")["role"] == "normal")
    st, js, _, _ = staff.post("/api/auth/password", {
        "current": "wrong", "new": "brand-new-pass"})
    check("normal: changing password needs the current one",
          js["ok"] is False)
    old_cookie = staff.cookie
    st, js, _, _ = staff.post("/api/auth/password", {
        "current": "floor-pass-1", "new": "brand-new-pass"})
    check("normal: can change their own password, and stays signed in",
          js.get("ok") is True and staff.cookie != old_cookie
          and staff.get("/api/players")[0] != 401, js)
    st, _, _, _ = Client(port).req("GET", "/api/players", headers={
        "Cookie": old_cookie})
    check("...the old session is dead", st == 401)

    print("\n== lock ON: an admin does everything ==")
    for path, body in OPERATOR_POSTS:
        st, js, _, _ = boss.post(path, body)
        check(f"admin: POST {path} {json.dumps(body)[:30]} allowed",
              st == 200 and "reached" in js, (st, js))
    st, js, _, _ = boss.get("/api/auth/logins")
    check("admin: lists logins (no password material)",
          st == 200 and {x["username"] for x in js["logins"]}
          == {"boss", "staff"} and "pw" not in json.dumps(js)
          and js["me"] == "boss", js)
    for page in ("/test", "/console"):
        st, _, raw, _ = boss.get(page)
        check(f"admin: {page} is served", st == 200, st)
    st, js, _, _ = boss.post("/api/auth/logins", {
        "action": "delete", "username": "boss"})
    check("admin: cannot delete the only admin while locked",
          js["ok"] is False and store.login_get("boss"), js)
    st, js, _, _ = boss.post("/api/auth/config", {"idleMin": 60})
    check("admin: sets the idle time", js.get("idleMin") == 60, js)

    print("\n== lock ON: the hub's own tools ==")
    tool = {wa.TOKEN_HEADER: auth.local_token}
    st, js, raw, _ = anon.get("/api/status", headers=tool)
    check("local tool: full status with the secret",
          st == 200 and "10.9.9.9" in raw and "loginRequired" not in raw)
    st, js, _, _ = anon.get("/api/auth/logins", headers=tool)
    check("local tool: admin endpoints too",
          st == 200 and js["me"] is None, st)
    st, js, _, _ = anon.get("/api/players", headers={
        wa.TOKEN_HEADER: "guess"})
    check("a wrong secret is just signed out", st == 401)

    print("\n== signing out, and turning it off again ==")
    st, js, _, _ = staff.post("/api/auth/logout")
    check("sign out clears the cookie", js == {"ok": True}
          and staff.cookie is None)
    st, js, _, _ = Client(port).req("GET", "/api/players", headers={
        "Cookie": old_cookie})
    check("...and the session is gone", st == 401)
    st, js, _, _ = boss.post("/api/auth/config", {"required": False})
    check("admin turns the lock off", js.get("required") is False, js)
    time.sleep(wa.SETTING_TTL_SEC + 0.3)
    st, js, _, _ = anon.post("/api/settings", {})
    check("...and the pages are open again", st == 200 and "reached" in js)
    srv.shutdown()


def migration():
    print("\n== database: v15 -> v16 ==")
    import sqlite3
    d = tempfile.mkdtemp()
    p = os.path.join(d, "hub.db")
    s = HubStore(p)
    s.set_host_setting("gameroom_name", "Den")
    with s.lock:
        s._conn.execute("DROP TABLE web_logins")
        s._conn.execute("DROP TABLE web_sessions")
        s._conn.execute("UPDATE schema_meta SET value='15' "
                        "WHERE key='version'")
        s._conn.commit()
        s._conn.close()
    s2 = HubStore(p)
    c = sqlite3.connect(p)
    ver = c.execute("SELECT value FROM schema_meta WHERE key='version'"
                    ).fetchone()[0]
    tabs = {r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    c.close()
    check("a v15 database is brought to v16", ver == "16", ver)
    check("...with the login tables", {"web_logins", "web_sessions"} <= tabs)
    check("...and what was there is untouched",
          s2.host_setting("gameroom_name") == "Den")
    check("...and the lock is off after an update",
          wa.WebAuth(s2, d).required() is False)
    try:
        s2.set_host_setting("web_login_required", "1")
        ok = True
    except ValueError:
        ok = False
    check("the lock settings are known settings", ok)


def main():
    pure()
    sessions()
    http_layer()
    migration()
    print(f"\nRESULT: {_p} passed, {_f} failed")
    return 1 if _f else 0


if __name__ == "__main__":
    sys.exit(main())
