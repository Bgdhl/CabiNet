"""Optional hub login — who may use the operator pages and API.

OFF by default: with `web_login_required` unset the hub behaves exactly as it
always has (everything on the port is open to the slot LAN). Turned ON, three
kinds of caller are told apart:

  * MACHINES AND DISPLAYS never sign in and never could — EGMs posting SOAP,
    SAS satellites and companion readers reporting, the pages a cabinet shows
    on its own glass, the wall-TV board. Their endpoints stay open (PUBLIC).
    The board and the satellites' "is this a CabiNet hub?" probe read
    /api/status, so a signed-out caller gets a cut-down DISPLAY VIEW of it
    (public_status) instead of a refusal.
  * PEOPLE sign in with a username and password (web_logins in hub.db) and
    carry a session cookie. A login is 'normal' (see everything, run the
    floor day to day) or 'admin' (everything).
  * LOCAL TOOLS on the hub itself (deploy/update.py, support_bundle.py, …)
    send the secret in data/hub_api_token, readable only by the account the
    hub runs as. Being able to read that file already means owning the hub.

What this does NOT do, on purpose and said out loud: it does not authenticate
machines. Anything on the slot LAN can still post as a satellite, as it always
could — that network is trusted, and a Pi Zero in a cabinet has no way to hold
a secret a person with a screwdriver cannot read. And the hub speaks plain
HTTP, so a password crosses the LAN unencrypted. The lock keeps people out of
the operator pages; it is not a substitute for keeping strangers off the slot
network.

stdlib only (the hub runs on system python)."""

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time

log = logging.getLogger("cabinet.webauth")

ROLE_ADMIN = "admin"
ROLE_NORMAL = "normal"
ROLES = (ROLE_ADMIN, ROLE_NORMAL)

PUBLIC, NORMAL, ADMIN = 0, 1, 2
_ROLE_LEVEL = {ROLE_NORMAL: NORMAL, ROLE_ADMIN: ADMIN}

COOKIE = "cabinet_session"
TOKEN_HEADER = "X-CabiNet-Token"
TOKEN_FILE = "hub_api_token"

USERNAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,31}")
PASSWORD_MIN = 8
PASSWORD_MAX = 128

IDLE_MIN_DEFAULT = 30
IDLE_MIN_MAX = 7 * 24 * 60
SESSION_MAX_AGE_SEC = 30 * 24 * 3600   # hard cap, whatever the idle setting
SESSION_RECHECK_SEC = 30               # re-read a cached session from the db
SESSION_SAVE_SEC = 60                  # how often activity is written back
SETTING_TTL_SEC = 2.0

FAIL_LOCK_AFTER = 5
FAIL_LOCK_BASE_SEC = 60
FAIL_LOCK_MAX_SEC = 3600


# ---- passwords -------------------------------------------------------------

def username_error(username):
    if not isinstance(username, str) or \
            not USERNAME_RE.fullmatch(username):
        return ("username must be 1-32 characters: lowercase letters, "
                "digits, dot, dash or underscore, starting with a letter "
                "or digit")
    return None


def password_error(password):
    if not isinstance(password, str):
        return "password required"
    if len(password) < PASSWORD_MIN:
        return f"password must be at least {PASSWORD_MIN} characters"
    if len(password) > PASSWORD_MAX:
        return f"password must be at most {PASSWORD_MAX} characters"
    return None


def hash_password(password):
    """-> 'scrypt$<log2 n>$<r>$<p>$<salt hex>$<hash hex>' (or the pbkdf2
    form on a Python built without scrypt). The parameters travel with the
    hash, so they can be raised later without invalidating old logins."""
    salt = secrets.token_bytes(16)
    pw = password.encode("utf-8")
    if hasattr(hashlib, "scrypt"):
        try:
            h = hashlib.scrypt(pw, salt=salt, n=1 << 14, r=8, p=1, dklen=32)
            return "scrypt$14$8$1$%s$%s" % (salt.hex(), h.hex())
        except (ValueError, MemoryError):
            pass                           # OpenSSL refused — fall through
    iters = 200_000
    h = hashlib.pbkdf2_hmac("sha256", pw, salt, iters, dklen=32)
    return "pbkdf2$%d$%s$%s" % (iters, salt.hex(), h.hex())


def check_password(password, stored):
    """Constant-time verify. Any malformed stored value is simply False."""
    try:
        parts = stored.split("$")
        pw = password.encode("utf-8")
        if parts[0] == "scrypt" and len(parts) == 6:
            h = hashlib.scrypt(pw, salt=bytes.fromhex(parts[4]),
                               n=1 << int(parts[1]), r=int(parts[2]),
                               p=int(parts[3]), dklen=32)
            return hmac.compare_digest(h.hex(), parts[5])
        if parts[0] == "pbkdf2" and len(parts) == 4:
            h = hashlib.pbkdf2_hmac("sha256", pw, bytes.fromhex(parts[2]),
                                    int(parts[1]), dklen=32)
            return hmac.compare_digest(h.hex(), parts[3])
    except (ValueError, TypeError, AttributeError, MemoryError,
            UnicodeError):
        pass
    return False


# A real hash to burn time against when the username does not exist, so a
# wrong username and a wrong password take the same time to refuse.
_DUMMY_HASH = hash_password(secrets.token_hex(8))


def token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---- who may do what -------------------------------------------------------
# The split the owner chose: a NORMAL login sees everything and runs the
# floor day to day (clear handpays, void tickets, lock/unlock machines, load
# player credits). Setup, money-creating ticket controls, logins, updates and
# the bench pages are ADMIN.

# /api/command actions a normal login may send. Every action whose name
# starts with "get" is a read and is allowed too.
NORMAL_COMMANDS = frozenset((
    "keepAlive", "refreshGameStatus", "gameSweep", "checkUpdates",
    "clearHandpay", "resetHandpay", "voidValidationId",
    "setCabinetState", "floorLock", "floorUnlock",
    "addCredits", "printTicket", "watCancel", "cardOut"))

# /api/sas/command types a normal login may send.
NORMAL_SAS_COMMANDS = frozenset((
    "handpay_reset", "sas_disable", "sas_enable",
    "legacy_bonus", "aft_transfer",
    "read_machine_info", "sas_read_player_denoms", "sas_read_game_info"))

# /api/accounts actions a normal login may send (the rest create, rename or
# delete players).
NORMAL_ACCOUNT_ACTIONS = frozenset(("adjust",))

# Pages (by file name, wherever under /webui/ they are asked for).
_PAGE_LEVEL = {"home.html": NORMAL, "index.html": ADMIN,
               "console.html": ADMIN}
_ROUTE_LEVEL = {"/": NORMAL, "/ui": NORMAL, "/home": NORMAL,
                "/home.html": NORMAL,
                "/test": ADMIN, "/testpanel": ADMIN, "/index.html": ADMIN,
                "/console": ADMIN, "/console.html": ADMIN}

# The satellite's own ticket verbs. `void` is on the list because a satellite
# sends it too; the web page's void is told apart by its smibId (below).
_TITO_SATELLITE = frozenset(("mint", "issued", "authorize", "commit", "sync",
                             "void"))
WEBUI_SMIB = "webui"


def get_level(path):
    """-> (level, is_page) for a GET. `path` is already unquoted and has no
    query string. is_page: a refusal should send a browser to /login rather
    than answer JSON."""
    if path in _ROUTE_LEVEL:
        return _ROUTE_LEVEL[path], True
    if path.startswith("/webui/"):
        return _PAGE_LEVEL.get(path.rsplit("/", 1)[-1], PUBLIC), True
    if not path.startswith("/api/"):
        return PUBLIC, False               # /login, /board, 404s
    if path.rstrip("/") == "/api/auth/state" or \
            path.startswith(("/api/status", "/api/accounts", "/api/hubkey",
                             "/api/glass/")):
        # status and accounts answer a signed-out caller with the display
        # view — the handler does the cutting.
        return PUBLIC, False
    if path.startswith(("/api/debug/", "/api/auth/")):
        return ADMIN, False
    return NORMAL, False


def _body_field(raw, key):
    try:
        d = json.loads(raw) if raw and raw.strip() else {}
    except (ValueError, TypeError):
        return None
    v = d.get(key) if isinstance(d, dict) else None
    return v if isinstance(v, str) else None


def post_level(path, raw):
    """Level a POST needs. Anything that is not an /api/ path is a machine
    talking G2S. An unknown /api/ path is ADMIN — a new endpoint is closed
    until someone decides otherwise."""
    path = path.split("?", 1)[0]
    if not path.startswith("/api/"):
        return PUBLIC
    if path.startswith("/api/auth/"):
        # EXACT verbs — "/api/auth/logins" (manage logins, admin) begins
        # with "/api/auth/login" (sign in, public); a prefix match here
        # once let a signed-out caller add a login.
        verb = path[len("/api/auth/"):].strip("/")
        if verb in ("login", "logout"):
            return PUBLIC
        if verb in ("touch", "password"):
            return NORMAL
        return ADMIN
    if path.startswith(("/api/sas/report", "/api/companion/report",
                        "/api/glass/")):
        return PUBLIC
    if path.startswith("/api/tito/"):
        sub = path[len("/api/tito/"):].strip("/")
        if sub not in _TITO_SATELLITE:
            return ADMIN                   # reissue, reusable, anything new
        if sub == "void" and \
                (_body_field(raw, "smibId") or "").strip() == WEBUI_SMIB:
            return NORMAL
        return PUBLIC
    if path.startswith("/api/command"):
        a = _body_field(raw, "action") or ""
        return NORMAL if a in NORMAL_COMMANDS or a.startswith("get") \
            else ADMIN
    if path.startswith("/api/sas/command"):
        return NORMAL if _body_field(raw, "type") in NORMAL_SAS_COMMANDS \
            else ADMIN
    if path.startswith("/api/accounts"):
        return NORMAL if _body_field(raw, "action") in \
            NORMAL_ACCOUNT_ACTIONS else ADMIN
    return ADMIN


def allows(who, level):
    """who: identify()'s answer (or None)."""
    if level == PUBLIC:
        return True
    return bool(who) and _ROLE_LEVEL.get(who.get("role"), PUBLIC) >= level


# ---- the display view ------------------------------------------------------
# What a signed-out caller may read while the lock is on: enough for the
# wall-TV board to draw and for a satellite to recognize a CabiNet hub.

_PUB_MACHINE = ("egmId", "commsState", "sasLink", "recentHandpays",
                "pendingHandpays")
_PUB_SAS = ("online", "stale", "nickname", "pendingHandpay", "denomSuggest")
_PUB_TOP = ("names", "tournament", "cardSessions", "boardStamp")
_PUB_OPTIONS = ("boardEnabled", "gameroomName", "currencySymbol")
_PUB_ACCOUNT = ("id", "name", "kind", "cashableMillicents",
                "promoMillicents", "nonCashMillicents")
_LONG_DIGITS = re.compile(r"\d{9,}")


def _mask_digits(text):
    """A ticket's validation number is the ticket. The activity tape names
    them; the display view keeps only the last four digits."""
    return _LONG_DIGITS.sub(lambda m: "…" + m.group(0)[-4:], text)


def public_status(body):
    """Cut a slim /api/status body (JSON text) down to the display view."""
    src = json.loads(body)
    out = {"slim": True, "loginRequired": True}
    eng = src.get("_engine")
    eng = eng if isinstance(eng, dict) else {}
    out["_engine"] = {k: eng[k] for k in ("version", "platform") if k in eng}
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(v.get("commsState"), str):
            m = {f: v[f] for f in _PUB_MACHINE if f in v}
            ident = (v.get("cabinet") or {}).get("identity") \
                if isinstance(v.get("cabinet"), dict) else None
            if ident is not None:
                m["cabinet"] = {"identity": ident}
            out[k] = m
    for k in _PUB_TOP:
        if k in src:
            out[k] = src[k]
    sas = src.get("sas")
    if isinstance(sas, dict):
        out["sas"] = {k: {f: v[f] for f in _PUB_SAS if f in v}
                      for k, v in sas.items() if isinstance(v, dict)}
    ho = src.get("hostOptions")
    if isinstance(ho, dict):
        out["hostOptions"] = {k: ho[k] for k in _PUB_OPTIONS if k in ho}
    act = src.get("activity")
    if isinstance(act, list):
        tape = []
        for e in act:
            if not isinstance(e, dict):
                continue
            e = {k: v for k, v in e.items() if k != "extras"}
            for f in ("label", "sub"):
                if isinstance(e.get(f), str):
                    e[f] = _mask_digits(e[f])
            tape.append(e)
        out["activity"] = tape
    return json.dumps(out, separators=(",", ":"))


def public_accounts(snapshot):
    """Names and balances for the board's standings; no ledger, no flags."""
    rows = snapshot.get("accounts") if isinstance(snapshot, dict) else None
    return {"accounts": [{k: a[k] for k in _PUB_ACCOUNT if k in a}
                         for a in (rows or []) if isinstance(a, dict)],
            "ledger": [], "loginRequired": True}


# ---- the local-tool secret -------------------------------------------------

def token_path(data_dir):
    return os.path.join(data_dir, TOKEN_FILE)


def read_local_token(data_dir):
    """What a tool on the hub sends as X-CabiNet-Token; '' when there is
    none (a hub that has never run this version, or a file we may not
    read). Never raises."""
    try:
        with open(token_path(data_dir), "r", encoding="ascii") as f:
            return f.read().strip()
    except (OSError, UnicodeError):
        return ""


def ensure_local_token(data_dir):
    """Read the secret, minting it (0600) on first run. '' if the data dir
    is unwritable — local tools then simply have no shortcut."""
    tok = read_local_token(data_dir)
    if len(tok) >= 32:
        return tok
    tok = secrets.token_urlsafe(32)
    path = token_path(data_dir)
    try:
        os.makedirs(data_dir, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write(tok + "\n")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return tok
    except OSError as e:
        log.warning("could not write %s (%s) — local tools will need a "
                    "login while the hub lock is on", path, e)
        return ""


# ---- sessions --------------------------------------------------------------

def cookie_token(header):
    """The session token out of a Cookie header, or ''."""
    for part in (header or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == COOKIE:
            v = v.strip()
            return v if 20 <= len(v) <= 128 else ""
    return ""


def set_cookie(token, max_age=SESSION_MAX_AGE_SEC):
    return ("%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Strict"
            % (COOKIE, token, max_age))


def clear_cookie():
    return "%s=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict" % COOKIE


class WebAuth:
    """Sessions, sign-in throttling and the lock setting, over a HubStore."""

    def __init__(self, store, data_dir=None, clock=time.time):
        self.store = store
        self.clock = clock
        self.lock = threading.Lock()
        self._sessions = {}     # token_hash -> entry (see _load)
        self._fails = {}        # (peer, username) -> {n, strikes, until}
        self._setting = {}      # key -> (value, read_at)
        self.local_token = ensure_local_token(data_dir) if data_dir else ""

    # -- settings ------------------------------------------------------
    def _read_setting(self, key, default):
        now = self.clock()
        hit = self._setting.get(key)
        if hit and now - hit[1] <= SETTING_TTL_SEC:
            return hit[0]
        v = self.store.host_setting(key, default)
        self._setting[key] = (v, now)
        return v

    def required(self):
        return self._read_setting("web_login_required", "0") == "1"

    def idle_min(self):
        try:
            v = int(self._read_setting("web_login_idle_min",
                                       str(IDLE_MIN_DEFAULT)))
        except (TypeError, ValueError):
            v = IDLE_MIN_DEFAULT
        return v if 0 <= v <= IDLE_MIN_MAX else IDLE_MIN_DEFAULT

    def configure(self, required=None, idle_min=None):
        """Raises ValueError on a refusal."""
        if idle_min is not None:
            if isinstance(idle_min, bool) or not isinstance(idle_min, int) \
                    or not 0 <= idle_min <= IDLE_MIN_MAX:
                raise ValueError("idleMin must be a whole number of minutes, "
                                 "0 (never) to %d" % IDLE_MIN_MAX)
            self.store.set_host_setting("web_login_idle_min", str(idle_min))
        if required is not None:
            if not isinstance(required, bool):
                raise ValueError("required must be true or false")
            if required and not self.admin_count():
                raise ValueError("create an admin login first — otherwise "
                                 "nobody could sign in")
            self.store.set_host_setting("web_login_required",
                                        "1" if required else "0")
        self._setting.clear()

    # -- logins --------------------------------------------------------
    def admin_count(self):
        return sum(1 for r in self.store.login_list()
                   if r["role"] == ROLE_ADMIN)

    def set_login(self, username, role=None, password=None):
        """Create or change a login. Raises ValueError on a refusal."""
        err = username_error(username)
        if err:
            raise ValueError(err)
        if role is not None and role not in ROLES:
            raise ValueError("role must be 'admin' or 'normal'")
        if password is not None:
            err = password_error(password)
            if err:
                raise ValueError(err)
        cur = self.store.login_get(username)
        if cur is None and (role is None or password is None):
            raise ValueError("a new login needs a role and a password")
        if cur and role == ROLE_NORMAL and cur["role"] == ROLE_ADMIN \
                and self.required() and self.admin_count() <= 1:
            raise ValueError("that is the only admin login — make another "
                             "admin first, or turn the lock off")
        what = self.store.login_put(
            username, role,
            hash_password(password) if password is not None else None)
        if cur and password is not None:
            self.drop_sessions(username)    # a new password signs it out
        with self.lock:                     # a role change applies at once
            for ent in self._sessions.values():
                if ent["user"] == username:
                    ent["checked"] = 0
        return what

    def delete_login(self, username):
        cur = self.store.login_get(username)
        if cur is None:
            return False
        if cur["role"] == ROLE_ADMIN and self.required() \
                and self.admin_count() <= 1:
            raise ValueError("that is the only admin login — make another "
                             "admin first, or turn the lock off")
        self.store.login_delete(username)
        self.drop_sessions(username)
        return True

    def drop_sessions(self, username):
        self.store.session_delete(username=username)
        with self.lock:
            for h in [h for h, e in self._sessions.items()
                      if e["user"] == username]:
                del self._sessions[h]

    # -- signing in ----------------------------------------------------
    def _locked_for(self, key, now):
        f = self._fails.get(key)
        return max(0, int(f["until"] - now)) if f else 0

    def login(self, username, password, peer="?"):
        """-> (token, role) on success, else (None, {"error", "retrySec"?}).
        Wrong name and wrong password answer alike."""
        username = username.strip().lower() if isinstance(username, str) \
            else ""
        password = password if isinstance(password, str) else ""
        key = (peer, username[:64])
        now = self.clock()
        with self.lock:
            wait = self._locked_for(key, now)
        if wait:
            return None, {"error": "too many wrong tries — wait %d s" % wait,
                          "retrySec": wait}
        rec = self.store.login_get(username) \
            if USERNAME_RE.fullmatch(username) else None
        good = check_password(password[:PASSWORD_MAX],
                              rec["pw"] if rec else _DUMMY_HASH)
        if not rec or not good:
            with self.lock:
                if len(self._fails) > 2000:        # bound a spray
                    self._fails.clear()
                f = self._fails.setdefault(
                    key, {"n": 0, "strikes": 0, "until": 0})
                f["n"] += 1
                if f["n"] >= FAIL_LOCK_AFTER:
                    f["n"] = 0
                    f["strikes"] += 1
                    f["until"] = now + min(
                        FAIL_LOCK_MAX_SEC,
                        FAIL_LOCK_BASE_SEC * 2 ** (f["strikes"] - 1))
                    log.warning("🔐 hub login: %d wrong tries for %r from "
                                "%s — locked for %d s", FAIL_LOCK_AFTER,
                                username, peer, int(f["until"] - now))
            log.info("🔐 hub login REFUSED for %r from %s", username, peer)
            return None, {"error": "wrong username or password"}
        with self.lock:
            self._fails.pop(key, None)
        token = secrets.token_urlsafe(32)
        h = token_hash(token)
        self.store.session_put(h, username, now)
        self.store.login_mark(username)
        self.store.session_delete(before=now - SESSION_MAX_AGE_SEC)
        with self.lock:
            self._sessions[h] = {"user": username, "role": rec["role"],
                                 "created": now, "at": now, "saved": now,
                                 "checked": now}
        log.info("🔐 hub login: %s (%s) signed in from %s", username,
                 rec["role"], peer)
        return token, rec["role"]

    def logout(self, token):
        if not token:
            return
        h = token_hash(token)
        with self.lock:
            ent = self._sessions.pop(h, None)
        self.store.session_delete(token_hash=h)
        if ent:
            log.info("🔐 hub login: %s signed out", ent["user"])

    # -- who is asking -------------------------------------------------
    def identify(self, headers, active=False):
        """-> {"user", "role", "local"} or None. `active`: this request is
        a person doing something (a page load, an action) — it restarts the
        idle clock. Background polls must pass False, or an open dashboard
        would never time out."""
        tok = headers.get(TOKEN_HEADER)
        if tok and self.local_token and \
                hmac.compare_digest(tok.strip(), self.local_token):
            return {"user": "local tool", "role": ROLE_ADMIN, "local": True}
        token = cookie_token(headers.get("Cookie"))
        if not token:
            return None
        h = token_hash(token)
        now = self.clock()
        with self.lock:
            ent = self._sessions.get(h)
        if ent is None or now - ent["checked"] > SESSION_RECHECK_SEC:
            row = self.store.session_get(h)
            rec = self.store.login_get(row["username"]) if row else None
            if not row or not rec:
                with self.lock:
                    self._sessions.pop(h, None)
                return None
            saved = row["last_active"]
            ent = {"user": rec["username"], "role": rec["role"],
                   "created": row["created_at"],
                   "at": max(saved, ent["at"] if ent else 0),
                   "saved": saved, "checked": now}
        idle = self.idle_min() * 60
        if (idle and now - ent["at"] > idle) or \
                now - ent["created"] > SESSION_MAX_AGE_SEC:
            with self.lock:
                self._sessions.pop(h, None)
            self.store.session_delete(token_hash=h)
            log.info("🔐 hub login: %s signed out (timed out)", ent["user"])
            return None
        if active:
            ent["at"] = now
        if ent["at"] - ent["saved"] > SESSION_SAVE_SEC:
            self.store.session_touch(h, ent["at"])
            ent["saved"] = ent["at"]
        with self.lock:
            self._sessions[h] = ent
        return {"user": ent["user"], "role": ent["role"], "local": False}

    def state(self, who):
        """What /api/auth/state tells a page."""
        req = self.required()
        out = {"required": req,
               "signedIn": bool(who),
               "user": who["user"] if who else None,
               # lock off = everyone is the operator, as it always was
               "role": who["role"] if who else (None if req else ROLE_ADMIN)}
        if who or not req:
            out["idleMin"] = self.idle_min()
        return out
