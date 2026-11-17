#!/usr/bin/env python3
"""Standalone gate for the House-funded SAS AFT credit-push settlement
(G2SHost._settle_aft_credit_pushes) — the SAS peer of the G2S WAT escrow.

This is MONEY code: the funding account (the satellite's echoed accountId,
House when absent) is debited when a satellite reports a CONFIRMED
aft_transfer, and the satellite re-reports its last ~20 command results
every ~1 s, so the settlement MUST be idempotent (no double-debit) even
across a hub restart. These checks pin exactly that, plus the R1 funding
parity: echoed player debits, the short-player House fallback (with a
naming note), and old-satellite/junk-accountId degradation to House.

Run: python3 G2S/tools/test_aft_push.py   (expects "RESULT: N passed, 0 failed")
No live host, no network — a real AccountStore on a temp file + the real
method bound to a lightweight stand-in (the full engine init is irrelevant to
the settlement logic).
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import g2s_host as G  # noqa: E402

_passed = 0
_failed = 0


def check(label, cond):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ✅ {label}")
    else:
        _failed += 1
        print(f"  ❌ {label}")


def _stub(store):
    class Stub:
        SAS_AFT_MAX_CENTS = 1_000_000
        _settle_aft_credit_pushes = G.G2SHost._settle_aft_credit_pushes

        def __init__(self):
            self.account_store = store
            self._aft_pushes_settled = set()
    return Stub()


def _house_cash(store):
    return store.get("house")["cashableMillicents"]


def main():
    tmp = tempfile.mkdtemp(prefix="aftpush_")
    store = G.AccountStore(os.path.join(tmp, "accounts.json"))
    eng = _stub(store)

    check("fresh House starts at 0", _house_cash(store) == 0)

    # a CONFIRMED $5.00 push -> House debited -$5.00 (cents*1000 millicents)
    ok5 = {"type": "aft_transfer", "ok": True, "outcome": "completed",
           "amountCents": 500, "txnId": "CN111"}
    eng._settle_aft_credit_pushes([ok5], "bb2/1")
    check("confirmed $5.00 debits House -500000 mc",
          _house_cash(store) == -500000)

    # idempotent across the satellite's ~every-5s re-report
    for _ in range(4):
        eng._settle_aft_credit_pushes([ok5], "bb2/1")
    check("4 re-reports do NOT double-debit", _house_cash(store) == -500000)

    # idempotent across a hub restart (fresh seen-set; the ledger once=True
    # ref is the hard guard)
    eng_restarted = _stub(store)
    eng_restarted._settle_aft_credit_pushes([ok5], "bb2/1")
    check("re-report after a hub restart does NOT double-debit",
          _house_cash(store) == -500000)

    # a REFUSED push must not move money
    eng._settle_aft_credit_pushes(
        [{"type": "aft_transfer", "ok": False, "outcome": "refused",
          "amountCents": 0, "txnId": "CN222"}], "bb2/1")
    check("a refused push does not debit", _house_cash(store) == -500000)

    # an UNCONFIRMED push (ok True but outcome not completed/partial) must not
    # move money — the credits may or may not have landed; debit only on a
    # positive terminal.
    eng._settle_aft_credit_pushes(
        [{"type": "aft_transfer", "ok": True, "outcome": "unconfirmed",
          "amountCents": 300, "txnId": "CN333"}], "bb2/1")
    check("an unconfirmed push does not debit", _house_cash(store) == -500000)

    # a PARTIAL success debits the amount that actually LANDED
    eng._settle_aft_credit_pushes(
        [{"type": "aft_transfer", "ok": True, "outcome": "partial",
          "amountCents": 250, "txnId": "CN444"}], "bb2/1")
    check("a distinct $2.50 partial stacks to -750000 mc",
          _house_cash(store) == -750000)

    # an over-ceiling amount is refused (report-tamper guard)
    eng._settle_aft_credit_pushes(
        [{"type": "aft_transfer", "ok": True, "outcome": "completed",
          "amountCents": 2_000_000, "txnId": "CN555"}], "bb2/1")
    check("an over-ceiling amount is NOT debited",
          _house_cash(store) == -750000)

    # a bad/negative/absent amount is skipped, not crashed
    for bad in ({"type": "aft_transfer", "ok": True, "outcome": "completed",
                 "amountCents": 0, "txnId": "CN666"},
                {"type": "aft_transfer", "ok": True, "outcome": "completed",
                 "amountCents": -5, "txnId": "CN777"},
                {"type": "aft_transfer", "ok": True, "outcome": "completed",
                 "txnId": "CN888"},
                {"type": "aft_transfer", "ok": True, "outcome": "completed",
                 "amountCents": 100, "txnId": ""}):
        eng._settle_aft_credit_pushes([bad], "bb2/1")
    check("bad/absent amounts and empty txnIds are skipped",
          _house_cash(store) == -750000)

    # non-aft records are ignored
    eng._settle_aft_credit_pushes([{"type": "legacy_bonus", "result": "ack"},
                                   {"type": "handpay_reset", "ok": True}],
                                  "bb2/1")
    check("non-aft_transfer records are ignored", _house_cash(store) == -750000)

    # malformed inputs never raise
    try:
        eng._settle_aft_credit_pushes(None, "bb2/1")
        eng._settle_aft_credit_pushes(["not a dict", 42, None], "bb2/1")
        eng._settle_aft_credit_pushes([{}], "bb2/1")
        raised = False
    except Exception:  # noqa: BLE001
        raised = True
    check("malformed commandResults never raise", not raised)

    # the ledger holds exactly the two real movements, correct refs+amounts
    led = [e for e in store.state["ledger"]
           if str(e.get("ref", "")).startswith("aftpush:")]
    check("ledger has exactly 2 aftpush movements", len(led) == 2)
    check("ledger refs + amounts are correct",
          sorted((e["ref"], e["deltaMillicents"]) for e in led)
          == [("aftpush:CN111", -500000), ("aftpush:CN444", -250000)])

    # ---- funding parity (echoed accountId) --------------------------------
    print("— funding parity (echoed accountId)")
    house_before = _house_cash(store)
    p, _ = store.create("Ruth")
    pid = p["id"]
    store.adjust(pid, d_cash=1_000_000)          # $10 bankroll

    # (a) the echoed player pays; House untouched
    eng._settle_aft_credit_pushes(
        [{"type": "aft_transfer", "ok": True, "outcome": "completed",
          "amountCents": 500, "txnId": "CNP01", "accountId": pid}], "bb2/1")
    check("echoed player debited $5.00",
          store.get(pid)["cashableMillicents"] == 500_000)
    check("House untouched when the player pays",
          _house_cash(store) == house_before)

    # (a') player-path idempotency: re-reports + a hub restart
    for _ in range(3):
        eng._settle_aft_credit_pushes(
            [{"type": "aft_transfer", "ok": True, "outcome": "completed",
              "amountCents": 500, "txnId": "CNP01", "accountId": pid}],
            "bb2/1")
    _stub(store)._settle_aft_credit_pushes(
        [{"type": "aft_transfer", "ok": True, "outcome": "completed",
          "amountCents": 500, "txnId": "CNP01", "accountId": pid}], "bb2/1")
    check("player re-settles (3x + restart) do NOT double-debit",
          store.get(pid)["cashableMillicents"] == 500_000
          and _house_cash(store) == house_before)

    # (b) short player -> House covers it, note names the player
    eng._settle_aft_credit_pushes(
        [{"type": "aft_transfer", "ok": True, "outcome": "completed",
          "amountCents": 2000, "txnId": "CNP02", "accountId": pid}], "bb2/1")
    check("short player is NOT partially debited",
          store.get(pid)["cashableMillicents"] == 500_000)
    check("House covered the short player's $20.00",
          _house_cash(store) == house_before - 2_000_000)
    rows = [e for e in store.state["ledger"]
            if e.get("ref") == "aftpush:CNP02"]
    check("exactly ONE ledger row for the fallback", len(rows) == 1)
    check("the fallback row debits House and names the short player",
          rows and rows[0]["accountId"] == "house"
          and pid in rows[0]["note"])

    # (b') fallback idempotency across a hub restart (ref reused, once=True
    # — the refused player adjust wrote no ledger row, so the single House
    # row blocks the re-run)
    _stub(store)._settle_aft_credit_pushes(
        [{"type": "aft_transfer", "ok": True, "outcome": "completed",
          "amountCents": 2000, "txnId": "CNP02", "accountId": pid}], "bb2/1")
    check("fallback re-settle after restart does NOT double-debit",
          store.get(pid)["cashableMillicents"] == 500_000
          and _house_cash(store) == house_before - 2_000_000)

    # (c) old-satellite compat: no accountId -> House, exactly as before
    eng._settle_aft_credit_pushes(
        [{"type": "aft_transfer", "ok": True, "outcome": "completed",
          "amountCents": 100, "txnId": "CNP03"}], "bb2/1")
    check("no accountId (old satellite) debits House",
          _house_cash(store) == house_before - 2_100_000)
    check("no accountId leaves the player untouched",
          store.get(pid)["cashableMillicents"] == 500_000)

    # (d) junk accountId types degrade to House, never raise
    try:
        for i, junk in enumerate((42, "", True, "   ")):
            eng._settle_aft_credit_pushes(
                [{"type": "aft_transfer", "ok": True, "outcome": "completed",
                  "amountCents": 10, "txnId": f"CNP0{4 + i}",
                  "accountId": junk}], "bb2/1")
        junk_raised = False
    except Exception:  # noqa: BLE001
        junk_raised = True
    check("junk accountId types never raise", not junk_raised)
    check("junk accountId types all debited House",
          _house_cash(store) == house_before - 2_140_000)

    # (e) unknown account -> House fallback + naming note
    eng._settle_aft_credit_pushes(
        [{"type": "aft_transfer", "ok": True, "outcome": "completed",
          "amountCents": 25, "txnId": "CNP08", "accountId": "p999"}], "bb2/1")
    check("unknown account debits House",
          _house_cash(store) == house_before - 2_165_000)
    rows = [e for e in store.state["ledger"]
            if e.get("ref") == "aftpush:CNP08"]
    check("unknown-account fallback: one House row naming p999",
          len(rows) == 1 and rows[0]["accountId"] == "house"
          and "p999" in rows[0]["note"])

    print("— restricted promotional pushes come off the restricted balance")
    # 2026-11-16: the satellite's record says which KIND of credit it moved.
    def non(acct):
        return store.get(acct)["nonCashMillicents"]
    cash_before, non_before = _house_cash(store), non("house")
    eng._settle_aft_credit_pushes(
        [{"type": "aft_transfer", "ok": True, "outcome": "completed",
          "amountCents": 500, "txnId": "CNR01", "creditType": "restricted"}],
        "bb2/1")
    check("House restricted balance debited $5.00",
          non("house") == non_before - 500_000)
    check("...and House cash untouched", _house_cash(store) == cash_before)
    rows = [e for e in store.state["ledger"]
            if e.get("ref") == "aftpush:CNR01"]
    check("one ledger row, in the restricted bucket, saying what it was",
          len(rows) == 1 and rows[0].get("kind") == "nonCash"
          and "restricted promotional" in rows[0]["note"])
    eng._settle_aft_credit_pushes(
        [{"type": "aft_transfer", "ok": True, "outcome": "completed",
          "amountCents": 500, "txnId": "CNR01", "creditType": "restricted"}],
        "bb2/1")
    check("the same record again books nothing more",
          non("house") == non_before - 500_000)

    q, _ = store.create("Promo Pat")
    qid = q["id"]
    store.adjust(qid, d_cash=1_000_000, d_non=300_000)   # $10 cash, $3 promo
    eng._settle_aft_credit_pushes(
        [{"type": "aft_transfer", "ok": True, "outcome": "completed",
          "amountCents": 200, "txnId": "CNR02", "accountId": qid,
          "creditType": "restricted"}], "bb2/1")
    check("a player's restricted push comes off their restricted balance",
          non(qid) == 100_000
          and store.get(qid)["cashableMillicents"] == 1_000_000)
    house_non = non("house")
    eng._settle_aft_credit_pushes(
        [{"type": "aft_transfer", "ok": True, "outcome": "completed",
          "amountCents": 200, "txnId": "CNR03", "accountId": qid,
          "creditType": "restricted"}], "bb2/1")
    check("a player short on restricted is not dipped into for cash: "
          "House's restricted balance covers it",
          non(qid) == 100_000
          and store.get(qid)["cashableMillicents"] == 1_000_000
          and non("house") == house_non - 200_000)
    eng._settle_aft_credit_pushes(
        [{"type": "aft_transfer", "ok": True, "outcome": "completed",
          "amountCents": 100, "txnId": "CNR04", "accountId": qid,
          "creditType": "cashable"}], "bb2/1")
    check("creditType cashable (and none at all) is a cash push as before",
          store.get(qid)["cashableMillicents"] == 900_000
          and non(qid) == 100_000)

    print("— a cash-out with restricted credit lands in both balances")
    # 2026-11-16: the satellite pulls restricted credit off the machine
    # with the cash and reports it as restrictedCents.
    class CashStub:
        SAS_AFT_MAX_CENTS = 1_000_000
        _settle_aft_cashouts = G.G2SHost._settle_aft_cashouts

        def __init__(self, pin):
            self.account_store = store
            self._aft_cashouts_settled = set()
            self._aft_cashout_unconfirmed = set()
            self.pin, self.flags, self.disarmed = pin, [], []

        def _sas_cashout_pin(self, key):
            return self.pin

        def _flag_unhomed_cashout(self, *a, **k):
            self.flags.append((a, k))

        def _disarm_sas_leg(self, key):
            self.disarmed.append(key)

        def _tournament_pin_consumed(self, key):
            pass

    w, _ = store.create("Wally")
    wid = w["id"]
    co = CashStub(wid)

    def out(txn, cents, restricted=None):
        rec = {"type": "aft_cashout", "ok": True, "outcome": "completed",
               "txnId": txn, "amountCents": cents}
        if restricted is not None:
            rec["restrictedCents"] = restricted
        co._settle_aft_cashouts([rec], "bb2/1")

    def bal():
        a = store.get(wid)
        return a["cashableMillicents"], a["nonCashMillicents"]

    out("CO1", 2500, 1500)
    check("cash to cash, restricted to restricted", bal() == (2_500_000,
                                                              1_500_000))
    rows = [e for e in store.state["ledger"] if e.get("ref") == "aftcashout:CO1"]
    check("two ledger rows under the one ref, one per balance",
          sorted(e.get("kind") for e in rows) == ["cashable", "nonCash"])
    out("CO1", 2500, 1500)
    check("the same record again books nothing more",
          bal() == (2_500_000, 1_500_000))
    out("CO2", 0, 700)
    check("restricted credit alone is a real cash-out, not an "
          "'amount unconfirmed' hold",
          bal() == (2_500_000, 2_200_000) and co.flags == [])
    out("CO3", 300)
    check("no restrictedCents at all is a cash-only cash-out as before",
          bal() == (2_800_000, 2_200_000))
    out("CO4", 0, 0)
    check("nothing readable in either is still held loudly, not booked",
          bal() == (2_800_000, 2_200_000) and len(co.flags) == 1)
    for junk in (True, -5, "9", 1.5):
        out(f"COJ{junk}", 100, junk)
    check("a junk restricted figure books the cash and no restricted",
          bal() == (3_200_000, 2_200_000))

    print("— the player's glass fund can load restricted credit")
    class GlassStub:
        SAS_AFT_MAX_CENTS = 1_000_000
        GLASS_FUND_RESERVE_TTL = 30
        glass_wallet_fund = G.G2SHost.glass_wallet_fund
        _glass_fund_reserved_mc = G.G2SHost._glass_fund_reserved_mc

        def __init__(self):
            import itertools, threading
            self.account_store = store
            self.sas_links = {"EGM1": "bb2/1"}
            self.sas_lock = threading.Lock()
            self.sas_machines = {"bb2/1": {"smibId": "smib-bb2"}}
            self._glass_fund_lock = threading.Lock()
            self._glass_fund_reserves = []
            self._fob_seq = itertools.count(1)
            self.sent = []

        def sas_enqueue_command(self, smib, cmd):
            self.sent.append((smib, cmd))
            return {"ok": True, "id": cmd["id"]}

    gs = GlassStub()
    g, _ = store.create("Glass Gus")
    gid = g["id"]
    store.adjust(gid, d_cash=500_000, d_non=200_000)     # $5 cash, $2 restricted
    code, body = gs.glass_wallet_fund("EGM1", gid, 100, restricted=True)
    check("restricted fund queues an aft_transfer marked restricted",
          code == 200 and gs.sent[-1][1]["creditType"] == "restricted"
          and gs.sent[-1][1]["cents"] == 100
          and gs.sent[-1][1]["accountId"] == gid)
    code, body = gs.glass_wallet_fund("EGM1", gid, 300, restricted=True)
    check("more restricted than the player has is refused, whatever their "
          "cash", code == 400 and len(gs.sent) == 1)
    code, body = gs.glass_wallet_fund("EGM1", gid, 300)
    check("a cash fund is unchanged and carries no creditType",
          code == 200 and "creditType" not in gs.sent[-1][1])

    print("— the bank can give a player restricted promotional credit")
    class PlayerStub:
        player_action = G.G2SHost.player_action
        _FUND_REF_SEQ = G.G2SHost._FUND_REF_SEQ

        def __init__(self):
            self.account_store = store
            self.hub_store = None       # the fund action never touches it

        def house_allow_negative(self):
            return False            # floor mode ON: cash gifts are gated

    ps = PlayerStub()
    f, _ = store.create("Fund Fay")
    fid = f["id"]
    house_non = store.get("house")["nonCashMillicents"]
    house_cash = _house_cash(store)
    r = ps.player_action({"action": "fund", "accountId": fid, "cents": 700,
                          "restricted": True})
    check("a restricted fund lands in the player's restricted balance",
          r.get("ok") is True and r.get("restricted") is True
          and store.get(fid)["nonCashMillicents"] == 700_000
          and store.get(fid)["cashableMillicents"] == 0)
    check("...comes off the bank's restricted balance, not its cash, and "
          "is not stopped by floor mode",
          store.get("house")["nonCashMillicents"] == house_non - 700_000
          and _house_cash(store) == house_cash)
    r = ps.player_action({"action": "fund", "accountId": fid, "cents": -200,
                          "restricted": True})
    check("a restricted pull-back returns it to the bank",
          r.get("ok") is True
          and store.get(fid)["nonCashMillicents"] == 500_000
          and store.get("house")["nonCashMillicents"] == house_non - 500_000)
    r = ps.player_action({"action": "fund", "accountId": fid, "cents": -900,
                          "restricted": True})
    check("pulling back more restricted than the player has is refused",
          r.get("ok") is False
          and store.get(fid)["nonCashMillicents"] == 500_000)
    rows = [e for e in store.state["ledger"]
            if e.get("accountId") == fid and e.get("kind") == "nonCash"]
    check("the ledger rows say restricted promotional",
          len(rows) == 2 and all("restricted promotional" in e["note"]
                                 for e in rows))

    print("=" * 50)
    print(f"RESULT: {_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
