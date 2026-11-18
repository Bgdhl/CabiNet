#!/usr/bin/env python3
"""
test_hub_tito.py — standalone gate for the hub TITO validation ledger
(hub_store schema v2) and the TitoAuthority union (g2s_host).

In-process, no live host, stdlib only. Run:  python3 tools/test_hub_tito.py
Must end "RESULT: N passed, 0 failed".
"""

import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hub_store import (HubStore, MAX_TICKET_MILLICENTS,   # noqa: E402
                       SCHEMA_VERSION)
from g2s_host import TitoAuthority                        # noqa: E402

_p = _f = 0


def check(name, ok, detail=""):
    global _p, _f
    if ok:
        _p += 1
        print(f"  ✅ {name}")
    else:
        _f += 1
        print(f"  ❌ {name} {detail}")


def fresh():
    d = tempfile.mkdtemp()
    return HubStore(os.path.join(d, "hub.db")), d


def operator_controls():
    """GitHub #10: set a ticket back to issued, and the reusable flag — on
    the hub ledger, on the G2S voucher store, and through the union."""
    from g2s_host import VoucherStore
    hs, d = fresh()
    M1, M2 = "sas:smib-a:1", "sas:smib-b:1"

    def issued(amount=500_000, origin="sas:smib-a"):
        m = hs.tito_mint(origin, 1, amount)
        hs.tito_record_issued(m["validationNumber"], amount, origin, 1)
        return m["canonical"]

    def redeem(c, machine=M1, txn=None):
        txn = txn or f"t-{c}-{machine}-{redeem.n}"
        redeem.n += 1
        a = hs.tito_authorize(machine, txn, c)
        if not a["authorized"]:
            return a, None
        return a, hs.tito_close(machine, txn, c, True)
    redeem.n = 0

    def row(c):
        return hs.tito_list(vid=c)["tickets"][0]

    print("— operator: set a ticket back to issued (hub ledger)")
    c = issued()
    check("a fresh ticket lists as single-use with no uses",
          row(c)["reusable"] is False and row(c)["uses"] == 0)
    a, st = redeem(c)
    check("redeems once", a["authorized"] and st == "redeemed")
    a, _ = redeem(c)
    check("then refuses as already redeemed",
          not a["authorized"] and "already redeemed" in a["reason"], a)
    r = hs.tito_reissue(c)
    check("reissue: redeemed -> issued",
          r == {"ok": True, "prior": "redeemed", "state": "issued"}, r)
    check("the row shows issued, with the one use kept on record",
          row(c)["state"] == "issued" and row(c)["uses"] == 1
          and row(c)["operatorAt"])
    a, st = redeem(c, M2)
    check("the same paper redeems again, for the issued amount",
          a["authorized"] and a["amountMc"] == 500_000 and st == "redeemed",
          (a, st))
    check("and is consumed again (a reissue is one more use, not forever)",
          row(c)["state"] == "redeemed" and row(c)["uses"] == 2)

    hs.tito_reissue(c)
    a = hs.tito_authorize(M1, "hold-1", c)
    r = hs.tito_reissue(c)
    check("reissue is REFUSED while a redemption is in progress",
          r["ok"] is False and r["prior"] == "redeemPending"
          and "in progress" in r["error"] and row(c)["state"]
          == "redeemPending", r)
    check("that redemption still closes normally afterwards",
          hs.tito_close(M1, "hold-1", c, True) == "redeemed")

    v = issued(8_000)
    hs.tito_void(v)
    check("reissue brings back a voided ticket",
          hs.tito_reissue(v)["prior"] == "void"
          and redeem(v)[0]["authorized"])
    m = hs.tito_mint("sas:smib-a", 1, 9_785_000)
    r = hs.tito_reissue(m["canonical"])
    check("reissue rescues a 'minted' ticket whose print the hub never "
          "heard (the issue #5 leftovers)",
          r["ok"] and r["prior"] == "minted"
          and redeem(m["canonical"])[0]["amountMc"] == 9_785_000, r)
    check("an unknown number is None, not an error",
          hs.tito_reissue("0199999999999999") is None
          and hs.tito_set_reusable("0199999999999999", True) is None)

    print("— operator: reusable ticket (hub ledger)")
    c = issued(1_000_000)
    r = hs.tito_set_reusable(c, True)
    check("marked reusable", r == {"ok": True, "reusable": True,
                                   "state": "issued"}, r)
    for i in range(1, 4):
        a, st = redeem(c, M1 if i % 2 else M2)
        check(f"use {i}: pays the full amount and the close says redeemed",
              a["authorized"] and a["amountMc"] == 1_000_000
              and st == "redeemed", (a, st))
        check(f"use {i}: straight back to issued, count {i}",
              row(c)["state"] == "issued" and row(c)["uses"] == i
              and row(c)["redeemedBy"], row(c))
    a1 = hs.tito_authorize(M1, "x1", c)
    a2 = hs.tito_authorize(M2, "x2", c)
    check("still one redemption at a time — a second machine is refused "
          "while the first holds it",
          a1["authorized"] and not a2["authorized"]
          and "already in process" in a2["reason"], a2)
    check("a rejected close leaves it issued and does not count as a use",
          hs.tito_close(M1, "x1", c, False) == "issued"
          and row(c)["uses"] == 3)
    r = hs.tito_set_reusable(c, False)
    a, st = redeem(c)
    check("unticking makes the next redemption consume it",
          r["reusable"] is False and st == "redeemed"
          and row(c)["state"] == "redeemed", row(c))
    r = hs.tito_set_reusable(c, True)
    check("ticking reusable on a REDEEMED ticket also sets it back to "
          "issued", r["state"] == "issued" and row(c)["state"] == "issued")
    v = issued(8_000)
    hs.tito_void(v)
    check("ticking reusable does not un-void a ticket",
          hs.tito_set_reusable(v, True)["state"] == "void"
          and not redeem(v)[0]["authorized"])

    print("— operator: reusable and reissued tickets never expire")
    hs.set_host_setting("ticket_expire_days", "1")
    e1, e2, e3 = issued(2_000), issued(3_000), issued(4_000)
    old = "2020-01-01T00:00:00"
    with hs.lock:
        hs._conn.execute("UPDATE tito_tickets SET issued_at=? WHERE "
                         "canonical IN (?,?,?)", (old, e1, e2, e3))
        hs._conn.commit()
    check("an old ticket shows expired", row(e1)["state"] == "expired")
    hs.tito_set_reusable(e1, True)
    hs.tito_reissue(e2)
    hs.tito_mint("sas:smib-a", 1, 77_000)      # a mint runs the reaper
    left = {t["canonical"] for t in hs.tito_list(limit=500)["tickets"]}
    check("the reaper deletes the untouched expired ticket", e3 not in left)
    check("but keeps the reusable one and the reissued one, as issued",
          e1 in left and e2 in left and row(e1)["state"] == "issued"
          and row(e2)["state"] == "issued")
    hs.set_host_setting("ticket_expire_days", "0")

    print("— operator: a satellite's ledger sync cannot undo it")
    c = issued(600_000)
    vn = row(c)["vn16"]
    redeem(c)
    hs.tito_reissue(c)
    stale = [{"vn": vn, "amountMc": 600_000, "state": "redeemed",
              "redeemedAt": "2026-01-01T00:00:00", "redeemedBy": "1"}]
    res = hs.tito_sync_merge("sas:smib-a", 0, stale)
    check("the satellite's old 'redeemed' does not re-consume a reissued "
          "ticket", row(c)["state"] == "issued" and res["updated"] == 0
          and res["conflicts"] == 0, (row(c)["state"], res))
    u = issued(700_000)
    res = hs.tito_sync_merge("sas:smib-a", 0, [dict(
        stale[0], vn=row(u)["vn16"], amountMc=700_000)])
    check("an untouched ticket still takes the satellite's redeemed "
          "(never-downgrade merge unchanged)",
          row(u)["state"] == "redeemed" and res["updated"] == 1, res)

    print("— operator: a fresh print on a reused number is a new ticket")
    c = issued(500_000)
    vn = row(c)["vn16"]
    hs.tito_set_reusable(c, True)
    hs.tito_set_reusable(c, False)
    redeem(c)
    hs.tito_set_reusable(c, True)
    hs.tito_void(c)
    out = hs.tito_record_issued(vn, 123_000, "sas:smib-a", 1)
    check("collision re-issue clears the reusable flag and the use count",
          out.get("collision") == "reissued" and row(c)["reusable"] is False
          and row(c)["uses"] == 0 and row(c)["operatorAt"] is None, row(c))

    print("— operator: ephemeral store refuses both")
    hs.ephemeral = True
    for name, fn in (("reissue", lambda: hs.tito_reissue(c)),
                     ("set_reusable",
                      lambda: hs.tito_set_reusable(c, True))):
        try:
            fn()
            check(f"ephemeral {name} refused", False)
        except RuntimeError:
            check(f"ephemeral {name} refused", True)
    hs.ephemeral = False

    print("— operator: a v14 database migrates with every ticket intact")
    d14 = tempfile.mkdtemp()
    p14 = os.path.join(d14, "hub.db")
    h14 = HubStore(p14)
    k = h14.tito_mint("sas:smib-a", 1, 250_000)
    h14.tito_record_issued(k["validationNumber"], 250_000, "sas:smib-a", 1)
    h14.close()
    conn = sqlite3.connect(p14)
    for col in ("reusable", "uses", "operator_at"):
        conn.execute(f"ALTER TABLE tito_tickets DROP COLUMN {col}")
    conn.execute("UPDATE schema_meta SET value='14' WHERE key='version'")
    conn.commit()
    conn.close()
    h14 = HubStore(p14)
    t14 = h14.tito_list(vid=k["canonical"])["tickets"][0]
    check("v14 -> v15: the ticket survives, single-use, zero uses",
          t14["state"] == "issued" and t14["reusable"] is False
          and t14["uses"] == 0 and t14["amountMc"] == 250_000, t14)
    check("and the new controls work on it",
          h14.tito_set_reusable(k["canonical"], True)["ok"])
    h14.close()
    h14 = HubStore(p14)
    check("reopening a migrated database is a no-op (rerun-safe)",
          h14.tito_list(vid=k["canonical"])["tickets"][0]["reusable"])
    h14.close()

    print("— operator: G2S voucher store")
    vs = VoucherStore(os.path.join(d, "voucher_state.json"))
    EGM = "IGT_AVP"

    def voucher(amt="500000"):
        _, items = vs.new_validation_list(EGM, 1)
        vid = items[0][0]
        voucher.n += 1
        vs.record_voucher(EGM, {"kind": "issue", "validationId": vid,
                                "transactionId": f"i{voucher.n}",
                                "voucherAmt": amt,
                                "creditType": "G2S_cashable"})
        return vid
    voucher.n = 0

    def vredeem(vid, txn):
        a = vs.authorize_redemption(EGM, txn, vid)
        if a["exc"]:
            return a, None
        return a, vs.close_redemption(EGM, txn, vid, "G2S_redeemed")

    def vrec(vid):
        return vs.api_vouchers(vid=vid)["vouchers"][0]

    g = voucher()
    check("a fresh voucher lists as single-use",
          vrec(g)["reusable"] is False and vrec(g)["uses"] == 0)
    vredeem(g, "r1")
    check("redeemed once, then refused",
          vredeem(g, "r2")[0]["exc"] == 2)
    r = vs.reissue_id(g)
    a, st = vredeem(g, "r3")
    check("reissue: redeemed -> issued, and it redeems again",
          r == {"ok": True, "prior": "redeemed", "state": "issued"}
          and a["exc"] == 0 and a["amt"] == "500000" and st == "redeemed",
          (r, a, st))
    vs.reissue_id(g)
    vs.authorize_redemption(EGM, "hold", g)
    check("reissue is refused while a redemption is in progress",
          vs.reissue_id(g)["ok"] is False
          and vrec(g)["state"] == "redeemPending")
    vs.close_redemption(EGM, "hold", g, "G2S_rejected")
    _, items = vs.new_validation_list(EGM, 1)
    check("an id no ticket was printed against cannot be reissued",
          vs.reissue_id(items[0][0])["ok"] is False)
    check("an unknown id is None",
          vs.reissue_id("9" * 18) is None
          and vs.set_reusable("9" * 18, True) is None)

    g = voucher("1000000")
    vs.set_reusable(g, True)
    for i in range(1, 4):
        a, st = vredeem(g, f"u{i}")
        check(f"reusable voucher use {i}: paid, back to issued",
              a["exc"] == 0 and st == "redeemed"
              and vrec(g)["state"] == "issued" and vrec(g)["uses"] == i,
              (a, st, vrec(g)))
    vs.merge_egm_log(EGM, [{"voucherAction": "G2S_redeem",
                            "transactionId": "old-9", "validationId": g,
                            "egmAction": "G2S_redeemed",
                            "voucherAmt": "1000000", "logSequence": "9"}])
    check("a redemption replayed from the EGM's log does not consume it",
          vrec(g)["state"] == "issued")
    vs.set_reusable(g, False)
    vredeem(g, "u9")
    check("unticked: the next redemption consumes it",
          vrec(g)["state"] == "redeemed")
    check("ticking reusable on a redeemed voucher sets it back to issued",
          vs.set_reusable(g, True)["state"] == "issued")
    vs2 = VoucherStore(os.path.join(d, "voucher_state.json"))
    check("the flag and count survive a restart",
          vs2.api_vouchers(vid=g)["vouchers"][0]["reusable"] is True
          and vs2.api_vouchers(vid=g)["vouchers"][0]["uses"] == 4)

    print("— operator: through the union authority")
    ta = TitoAuthority(vs, hs)
    c = issued(300_000)
    hs.tito_set_reusable(c, True)
    for i in (1, 2):
        dec = ta.authorize(M1, f"ta{i}", c)
        st = ta.close(M1, f"ta{i}", c, "G2S_redeemed")
        check(f"hub-ledger reusable ticket through the union, use {i}",
              dec["exc"] == 0 and dec["amt"] == "300000"
              and st == "redeemed" and row(c)["state"] == "issued",
              (dec, st))
    dec = ta.authorize("IGT_AVP", "tb1", g)
    st = ta.close("IGT_AVP", "tb1", g, "G2S_redeemed")
    check("G2S reusable voucher through the union",
          dec["exc"] == 0 and st == "redeemed"
          and vs.api_vouchers(vid=g)["vouchers"][0]["state"] == "issued")
    hs.close()


def restricted_promo_flag():
    """A satellite reports what the machine said a ticket is (SAS 6.02
    Table 15.13c). 01 and 03 are restricted promotional tickets: flagged in
    the list, redeemable like any other (2026-11-16)."""
    print("— restricted promotional tickets are flagged, not refused")
    hs, _d = fresh()

    def row(c):
        return hs.tito_list(vid=c)["tickets"][0]

    cash = hs.tito_record_issued("0189157427998805", 942_500_000,
                                 "sas:smib-a", 1,
                                 extra={"validationType": 0})["canonical"]
    promo = hs.tito_record_issued("0189157427998806", 5_000_000,
                                  "sas:smib-a", 1,
                                  extra={"validationType": 1})["canonical"]
    aft = hs.tito_record_issued("0189157427998807", 5_000_000,
                                "sas:smib-a", 1,
                                extra={"validationType": 3})["canonical"]
    plain = hs.tito_record_issued("0189157427998808", 5_000_000,
                                  "sas:smib-a", 1)["canonical"]
    check("a cash ticket is not flagged",
          row(cash)["restrictedPromo"] is False
          and row(cash)["validationType"] == 0, row(cash))
    check("a restricted promotional ticket is flagged",
          row(promo)["restrictedPromo"] is True
          and row(promo)["validationType"] == 1, row(promo))
    check("...and so is a restricted ticket from an AFT transfer",
          row(aft)["restrictedPromo"] is True, row(aft))
    check("a ticket whose kind the hub was never told is not flagged",
          row(plain)["restrictedPromo"] is False
          and row(plain)["validationType"] is None, row(plain))
    a = hs.tito_authorize("IGT_AVP", "txnP", promo)
    check("a flagged ticket still redeems, for its amount",
          a["authorized"] and a["amountMc"] == 5_000_000, a)
    check("...and the decision says to credit it as restricted",
          a.get("restrictedPromo") is True, a)
    again = hs.tito_authorize("IGT_AVP", "txnP", promo)
    check("...on a retry of the same redemption too",
          again["authorized"] and again["retry"]
          and again.get("restrictedPromo") is True, again)
    ac = hs.tito_authorize("IGT_AVP", "txnC", cash)
    check("a cash ticket's decision does not",
          ac["authorized"] and ac.get("restrictedPromo") is False, ac)
    hs.tito_close("IGT_AVP", "txnC", cash, False)
    import g2s_host as _G
    check("the engine credits it as G2S_nonCash, a cash ticket as "
          "G2S_cashable",
          _G.TitoAuthority._adapt(a)["creditType"] == "G2S_nonCash"
          and _G.TitoAuthority._adapt(ac)["creditType"] == "G2S_cashable",
          (_G.TitoAuthority._adapt(a), _G.TitoAuthority._adapt(ac)))
    hs.tito_close("IGT_AVP", "txnP", promo, True)
    check("...and keeps its flag once redeemed",
          row(promo)["state"] == "redeemed"
          and row(promo)["restrictedPromo"] is True, row(promo))

    # a minted number (system validation) that prints as restricted
    m = hs.tito_mint("sas:smib-a", 1, 7_000_000)
    r = hs.tito_record_issued(m["validationNumber"], 7_000_000, "sas:smib-a",
                              1, extra={"validationType": 1})
    check("a minted number that prints as restricted is flagged",
          row(r["canonical"])["restrictedPromo"] is True, row(r["canonical"]))

    # the number of a spent ticket comes round again on new paper
    again = hs.tito_record_issued("0189157427998806", 6_000_000, "sas:smib-a",
                                  1, extra={"validationType": 0})
    check("a re-issued number takes the NEW paper's kind",
          again.get("collision") == "reissued"
          and row(promo)["restrictedPromo"] is False
          and row(promo)["validationType"] == 0, (again, row(promo)))

    for junk in ({"validationType": True}, {"validationType": 999},
                 {"validationType": "1"}, {"other": 1}):
        c = hs.tito_record_issued(
            "02" + str(abs(hash(str(junk))) % 10 ** 14).zfill(14), 1_000_000,
            "sas:smib-a", 1, extra=junk)["canonical"]
        check(f"junk kind {junk!r} is no kind at all",
              row(c)["restrictedPromo"] is False
              and row(c)["validationType"] is None, row(c))


def main():
    hs, d = fresh()

    print("— schema migration (tito tables present)")
    check("schema version is at least 2 (tito)", SCHEMA_VERSION >= 2)
    with hs.lock:
        tables = {r["name"] for r in hs._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    check("tito tables created",
          {"tito_tickets", "tito_meta"} <= tables, tables)

    print("— v1 db migrates forward without losing names")
    d1 = tempfile.mkdtemp()
    p1 = os.path.join(d1, "hub.db")
    conn = sqlite3.connect(p1)
    conn.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO schema_meta VALUES ('version', '1')")
    conn.execute("CREATE TABLE machines (machine_key TEXT PRIMARY KEY, "
                 "protocol TEXT NOT NULL, nickname TEXT, first_seen TEXT "
                 "NOT NULL, last_seen TEXT NOT NULL, meta TEXT)")
    conn.execute("INSERT INTO machines VALUES ('m1','SAS','Bluebird',"
                 "'2026-01-01','2026-01-01',NULL)")
    conn.commit()
    conn.close()
    hs1 = HubStore(p1)
    check("v1 names survive the v2 migration",
          hs1.get_nickname("m1") == "Bluebird")
    check("v2 tables exist after migration",
          hs1.tito_counts() == {"total": 0}, hs1.tito_counts())
    hs1.close()

    print("— mint")
    m = hs.tito_mint("sas:smib-bb2", 1, 10_000)
    check("first mint is sid01 seq1",
          m["validationNumber"] == "0100000000000001" and not m["reused"], m)
    check("canonical = sid + vn16",
          m["canonical"] == "01" + m["validationNumber"])
    m2 = hs.tito_mint("sas:smib-bb2", 1, 10_000)
    check("same origin+addr+amount REUSES the open mint",
          m2["reused"] and m2["validationNumber"] == m["validationNumber"])
    m3 = hs.tito_mint("sas:smib-bb2", 1, 20_000)
    check("different amount mints a new number",
          not m3["reused"] and m3["seq"] == 2, m3)
    try:
        hs.tito_mint("x", 1, MAX_TICKET_MILLICENTS + 1)
        check("over-ceiling mint raises", False)
    except ValueError:
        check("over-ceiling mint raises", True)
    try:
        hs.tito_mint("x", 1, 0)
        check("zero-amount mint raises", False)
    except ValueError:
        check("zero-amount mint raises", True)

    print("— mint uniqueness vs occupied numbers (synced fallback rows)")
    with hs.lock:
        hs._conn.execute(
            "INSERT INTO tito_tickets (canonical, vn16, state, amount_mc) "
            "VALUES ('010100000000000003', '0100000000000003', 'issued', "
            "1000)")
        hs._conn.commit()
    m4 = hs.tito_mint("sas:smib-bb2", 1, 30_000)
    check("mint skips an occupied seq", m4["seq"] == 4, m4)

    print("— open-mint spam guard")
    from hub_store import MAX_OPEN_MINTS
    hs_s, _ds = fresh()
    with hs_s.lock:
        hs_s._conn.executemany(
            "INSERT INTO tito_tickets (canonical, vn16, state, amount_mc, "
            "origin, address, minted_at) VALUES (?,?,'minted',1000,'spam',"
            "1,'2099-01-01T00:00:00')",
            [(f"99{i:016d}", f"{i:016d}") for i in range(MAX_OPEN_MINTS)])
        hs_s._conn.commit()
    try:
        hs_s.tito_mint("sas:legit", 1, 1000)
        check("open-mint cap refuses new mints", False)
    except RuntimeError:
        check("open-mint cap refuses new mints", True)
    with hs_s.lock:
        hs_s._conn.execute("UPDATE tito_tickets SET amount_mc=2000 WHERE "
                           "vn16='0000000000000005'")
        hs_s._conn.commit()
    reuse = hs_s.tito_mint("spam", 1, 2000)
    check("reuse still works at the cap", reuse["reused"], reuse)
    hs_s.close()

    print("— ephemeral store refuses to mint")
    hs_e, _de = fresh()
    hs_e.ephemeral = True
    try:
        hs_e.tito_mint("x", 1, 1000)
        check("ephemeral mint refused", False)
    except RuntimeError:
        check("ephemeral mint refused", True)
    hs_e.close()

    print("— record issued")
    r = hs.tito_record_issued(m["validationNumber"], 10_000,
                              "sas:smib-bb2", 1, ticket_number=1,
                              source="egm_4D")
    check("minted row reconciles to issued",
          r["state"] == "issued" and not r["duplicate"], r)
    r2 = hs.tito_record_issued(m["validationNumber"], 10_000,
                               "sas:smib-bb2", 1)
    check("re-report is duplicate, state unchanged",
          r2["duplicate"] and r2["state"] == "issued", r2)
    r3 = hs.tito_record_issued("0200000000000009", 5_000, "sas:smib-bb2", 1)
    check("unminted capture inserts issued",
          r3["state"] == "issued" and r3["canonical"]
          == "020200000000000009", r3)
    try:
        hs.tito_record_issued("0000000000000000", 1000, "x", 1)
        check("all-zero vn raises", False)
    except ValueError:
        check("all-zero vn raises", True)

    print("— collision containment (reused validation number)")
    # A vn issued, redeemed, then reused by a fresh ticket of a DIFFERENT
    # amount must RE-ISSUE (never silently drop -> the ticket-stuck bug).
    cvn = "0055500000000042"
    hs.tito_record_issued(cvn, 300_500_000, "sas:smib-bb2", 1)   # $3,005
    ccan = hs._predict_canonical(cvn)
    hs.tito_authorize("AVP", "ct1", ccan)
    hs.tito_close("AVP", "ct1", ccan, True)                      # redeemed
    rc = hs.tito_record_issued(cvn, 2_500_000, "sas:smib-bb2", 1)  # fresh $25
    check("collision over redeemed re-issues (not dropped)",
          rc.get("collision") == "reissued" and rc["state"] == "issued"
          and not rc["duplicate"], rc)
    ac = hs.tito_authorize("BB2", "ct2", ccan)
    check("re-issued collision ticket redeems for its REAL amount",
          ac["authorized"] and ac["amountMc"] == 2_500_000, ac)
    hs.tito_close("BB2", "ct2", ccan, True)
    # Same amount over a redeemed row is a stale re-read -> unchanged.
    rc2 = hs.tito_record_issued(cvn, 2_500_000, "sas:smib-bb2", 1)
    check("same-amount re-read stays duplicate (no resurrection)",
          rc2["duplicate"] and rc2["state"] == "redeemed"
          and not rc2.get("collision"), rc2)
    # Collision over a LIVE ticket must NOT overwrite the valid paper.
    lvn = "0066600000000007"
    hs.tito_record_issued(lvn, 12_345_000, "sas:smib-bb2", 1)    # live issued
    rc3 = hs.tito_record_issued(lvn, 2_500_000, "sas:smib-bb2", 1)
    lcan = hs._predict_canonical(lvn)
    la = hs.tito_authorize("BB2", "lt1", lcan)
    check("collision over live ticket flags conflict, keeps live amount",
          rc3.get("collision") == "conflict" and rc3["duplicate"]
          and la["amountMc"] == 12_345_000, (rc3, la))

    print("— authorize / holder semantics")
    can = m["canonical"]
    a = hs.tito_authorize("IGT_AVP", "txn1", can)
    check("cross-machine authorize by canonical",
          a["authorized"] and a["amountMc"] == 10_000, a)
    a_retry = hs.tito_authorize("IGT_AVP", "txn1", can)
    check("same holder retry re-draws identically",
          a_retry["authorized"] and a_retry["retry"], a_retry)
    a_other = hs.tito_authorize("sas:smib-bb2:1", "txnX", can)
    check("second machine blocked while pending",
          not a_other["authorized"]
          and "already in process" in a_other["reason"], a_other)
    a_minted = hs.tito_authorize("M", "t", m3["canonical"])
    check("minted-but-never-printed rejects",
          not a_minted["authorized"] and "printed" in a_minted["reason"],
          a_minted)
    a_unknown = hs.tito_authorize("M", "t", "999999999999999999")
    check("unknown rejects", not a_unknown["authorized"], a_unknown)

    print("— amount is issued-authoritative")
    a_rep = hs.tito_authorize("M2", "t2", "020200000000000009",
                              reported_mc=999_000)
    check("mismatched report noted, issued amount wins",
          a_rep["authorized"] and a_rep["amountMc"] == 5_000
          and "overrides" in a_rep["reason"], a_rep)
    hs.tito_close("M2", "t2", "020200000000000009", False)

    print("— close")
    st = hs.tito_close("WRONG", "txn1", can, True)
    check("non-holder cannot consume", st == "redeemPending", st)
    st = hs.tito_close("IGT_AVP", "wrongtxn", can, True)
    check("wrong txn cannot consume", st == "redeemPending", st)
    st = hs.tito_close("IGT_AVP", "txn1", can, True)
    check("holder consumes -> redeemed", st == "redeemed")
    st = hs.tito_close("IGT_AVP", "txn1", can, True)
    check("duplicate close idempotent", st == "redeemed")
    a_after = hs.tito_authorize("M", "t9", can)
    check("redeemed ticket rejects re-authorize",
          not a_after["authorized"]
          and "already redeemed" in a_after["reason"], a_after)
    # reject-reset path
    b = hs.tito_mint("sas:smib-bb2", 1, 7_000)
    hs.tito_record_issued(b["validationNumber"], 7_000, "sas:smib-bb2", 1)
    hs.tito_authorize("M3", "t3", b["canonical"])
    st = hs.tito_close("M3", "t3", b["canonical"], False)
    check("rejected close resets to issued", st == "issued")
    a_again = hs.tito_authorize("M4", "t4", b["canonical"])
    check("reset ticket redeemable elsewhere", a_again["authorized"])
    hs.tito_close("M4", "t4", b["canonical"], True)

    print("— canonical learning (trailing-16 fallback)")
    hs.tito_record_issued("0300000000000011", 4_000, "sas:x", 2)
    a_learn = hs.tito_authorize("M5", "t5", "990300000000000011")
    check("18-digit scan with wrong predicted prefix authorizes",
          a_learn["authorized"], a_learn)
    with hs.lock:
        row = hs._conn.execute("SELECT canonical FROM tito_tickets WHERE "
                               "vn16='0300000000000011'").fetchone()
    check("row learned the scanned canonical",
          row["canonical"] == "990300000000000011", dict(row))
    hs.tito_close("M5", "t5", "990300000000000011", True)

    print("— sync merge")
    r = hs.tito_sync_merge("sas:smib-bb2", 6, [
        {"vn": "0200000000000021", "amountMc": 15_000, "state": "issued",
         "address": 1},
        {"vn": m["validationNumber"], "amountMc": 10_000, "state": "issued",
         "address": 1},                       # hub already redeemed: conflict
    ])
    check("sync inserts the new, keeps the hub's terminal state",
          r["inserted"] == 1 and r["conflicts"] == 1, r)
    a_syn = hs.tito_authorize("M6", "t6", "020200000000000021")
    check("synced fallback ticket authorizes", a_syn["authorized"]
          and a_syn["amountMc"] == 15_000, a_syn)
    hs.tito_close("M6", "t6", "020200000000000021", True)
    # never-downgrade: satellite says redeemed, hub had issued -> upgrade
    c2 = hs.tito_mint("sas:smib-bb2", 1, 3_000)
    hs.tito_record_issued(c2["validationNumber"], 3_000, "sas:smib-bb2", 1)
    r = hs.tito_sync_merge("sas:smib-bb2", 0, [
        {"vn": c2["validationNumber"], "amountMc": 3_000,
         "state": "redeemed", "redeemedBy": 1}])
    check("sync upgrades issued -> redeemed", r["updated"] == 1, r)
    # seq floor
    with hs.lock:
        cur = int(hs._conn.execute(
            "SELECT v FROM tito_meta WHERE k='seq_01'").fetchone()["v"])
    check("seq floored to the satellite's reported seq", cur >= 6, cur)
    # a replayed close (identical holder identity, as the /api/tito/sync
    # edge rebuilds it) consumes the ticket
    c3 = hs.tito_mint("sas:smib-bb2", 1, 2_000)
    hs.tito_record_issued(c3["validationNumber"], 2_000, "sas:smib-bb2", 1)
    hs.tito_authorize("sas:smib-bb2:1", f"sas:smib-bb2:1:{c3['canonical']}",
                      c3["canonical"])
    st = hs.tito_close("sas:smib-bb2:1",
                       f"sas:smib-bb2:1:{c3['canonical']}",
                       c3["canonical"], True)
    check("replayed close (same holder identity) consumes the ticket",
          st == "redeemed", st)

    print("— sync hardening (review 2026-07-08)")
    # incoming redeemPending is coerced to issued (a holderless pending
    # row would be permanently wedged)
    r = hs.tito_sync_merge("sas:smib-bb2", 0, [
        {"vn": "0400000000000031", "amountMc": 6_000,
         "state": "redeemPending", "address": 1}])
    check("incoming redeemPending inserted as issued", r["inserted"] == 1)
    a_c = hs.tito_authorize("MC", "tc", "040400000000000031")
    check("coerced ticket authorizes cleanly", a_c["authorized"], a_c)
    hs.tito_close("MC", "tc", "040400000000000031", True)
    # hostile validationSeq is capped, not honored
    from hub_store import MAX_SYNC_SEQ
    hs.tito_sync_merge("sas:evil", 10 ** 14 - 1, [])
    with hs.lock:
        cur = int(hs._conn.execute(
            "SELECT v FROM tito_meta WHERE k='seq_01'").fetchone()["v"])
    check("hostile seq floor capped", cur == MAX_SYNC_SEQ, cur)
    m_after = hs.tito_mint("sas:smib-bb2", 1, 55_000)
    check("mint still works after the capped floor",
          m_after["seq"] == MAX_SYNC_SEQ + 1, m_after)
    # a 2^63+ ticketNumber must not abort the merge (OverflowError)
    r = hs.tito_sync_merge("sas:smib-bb2", 0, [
        {"vn": "0500000000000001", "amountMc": 1_000, "state": "issued",
         "ticketNumber": 2 ** 70, "address": 1},
        {"vn": "0500000000000002", "amountMc": 1_000, "state": "issued",
         "address": 1}])
    check("oversized ticketNumber skipped, merge continues",
          r["inserted"] == 2, r)

    print("— mint reuse TTL")
    stale = hs.tito_mint("sas:ttl", 1, 9_000)
    with hs.lock:
        hs._conn.execute("UPDATE tito_tickets SET minted_at="
                         "'2026-01-01T00:00:00' WHERE canonical=?",
                         (stale["canonical"],))
        hs._conn.commit()
    fresh_m = hs.tito_mint("sas:ttl", 1, 9_000)
    check("stale open mint is NOT reused (TTL)",
          not fresh_m["reused"]
          and fresh_m["validationNumber"] != stale["validationNumber"],
          fresh_m)

    print("— seq high-water sidecar (quarantine double-mint guard)")
    check("sidecar file written on mint",
          os.path.exists(hs.seq_sidecar), hs.seq_sidecar)
    import json as _json
    side = _json.load(open(hs.seq_sidecar))
    check("sidecar carries the current seq_01",
          int(side.get("seq_01", 0)) >= MAX_SYNC_SEQ + 1, side)
    # simulate a quarantine-reset: fresh db, same sidecar path
    hs.close()
    os.remove(os.path.join(d, "hub.db"))
    for sfx in ("-wal", "-shm"):
        try:
            os.remove(os.path.join(d, "hub.db") + sfx)
        except OSError:
            pass
    hs = HubStore(os.path.join(d, "hub.db"))
    m_re = hs.tito_mint("sas:smib-bb2", 1, 60_000)
    check("fresh db after reset does NOT re-mint old numbers",
          m_re["seq"] > MAX_SYNC_SEQ + 1, m_re)

    print("— ephemeral refuses every money write")
    hs.ephemeral = True
    a_e = hs.tito_authorize("M", "t", "010100000000000001")
    check("ephemeral authorize fail-safe rejects",
          not a_e["authorized"] and "ephemeral" in a_e["reason"], a_e)
    for name, fn in (
            ("record", lambda: hs.tito_record_issued(
                "0600000000000001", 1000, "x", 1)),
            ("close", lambda: hs.tito_close("M", "t", "x" * 8, True)),
            ("sync", lambda: hs.tito_sync_merge("x", 0, [])),
            ("void", lambda: hs.tito_void("010100000000000001"))):
        try:
            fn()
            check(f"ephemeral {name} refused", False)
        except RuntimeError:
            check(f"ephemeral {name} refused", True)
    hs.ephemeral = False

    print("— void")
    v = hs.tito_mint("sas:smib-bb2", 1, 8_000)
    hs.tito_record_issued(v["validationNumber"], 8_000, "sas:smib-bb2", 1)
    prior = hs.tito_void(v["canonical"])
    check("void returns prior state", prior == "issued", prior)
    a_v = hs.tito_authorize("M", "t", v["canonical"])
    check("voided ticket rejects",
          not a_v["authorized"] and "voided" in a_v["reason"], a_v)

    print("— counts")
    counts = hs.tito_counts()
    check("counts total matches sum",
          counts["total"] == sum(v for k, v in counts.items()
                                 if k != "total"), counts)

    print("— tito_list (GET /api/tito/tickets backend)")
    hs_l, _dl = fresh()
    check("empty db lists an empty envelope",
          hs_l.tito_list() == {"total": 0, "tickets": []})
    # Four poked rows, one per interesting state, timestamps controlled
    # (and far in the past so a LIVE mint below sorts first). vn16s use
    # sid 77 so they can't collide with the live mint's sid-01 sequence.
    with hs_l.lock:
        hs_l._conn.executemany(
            "INSERT INTO tito_tickets (canonical, vn16, origin, address, "
            "amount_mc, state, ticket_number, pending_json, minted_at, "
            "issued_at, redeemed_at, redeemed_by) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", [
                ("777700000000000001", "7700000000000001", "sas:smib-bb2",
                 1, 5_000, "minted", None, None,
                 "2020-01-03T10:00:00", None, None, None),
                ("777700000000000002", "7700000000000002", "sas:smib-bb2",
                 1, 10_000, "issued", 7, None, "2020-01-01T09:00:00",
                 "2020-01-04T11:00:00", None, None),
                ("777700000000000003", "7700000000000003", "sas:smib-bb2",
                 1, 15_000, "redeemed", None, None, "2020-01-01T09:00:00",
                 "2020-01-02T09:30:00", "2020-01-05T12:00:00", "IGT_AVP"),
                ("777700000000000004", "7700000000000004", "sas:smib-bb2",
                 1, 20_000, "redeemPending", None,
                 '{"machine":"sas:x:1","txn":"t","amountMc":20000,'
                 '"at":"2020-01-02T08:00:00"}', "2020-01-01T09:00:00",
                 "2020-01-02T08:00:00", None, None)])
        hs_l._conn.commit()
    r = hs_l.tito_list()
    check("envelope is exactly {total, tickets}",
          set(r) == {"total", "tickets"}, sorted(r))
    check("total counts every row (all states show)",
          r["total"] == 4 and len(r["tickets"]) == 4, r["total"])
    want_keys = {"canonical", "vn16", "state", "amountMc", "origin",
                 "address", "ticketNumber", "mintedAt", "issuedAt",
                 "redeemedAt", "redeemedBy", "pending",
                 "reusable", "uses", "operatorAt",
                 "validationType", "restrictedPromo", "goodThrough"}
    check("row keys match the contract exactly",
          all(set(t) == want_keys for t in r["tickets"]),
          [sorted(t) for t in r["tickets"][:1]])
    check("newest-first by COALESCE(redeemed, issued, minted)",
          [t["canonical"][-1] for t in r["tickets"]]
          == ["3", "2", "1", "4"],
          [t["canonical"] for t in r["tickets"]])
    check("amountMc carries millicents from amount_mc",
          r["tickets"][0]["amountMc"] == 15_000, r["tickets"][0])
    check("pending only on the redeemPending row (null-safe elsewhere)",
          r["tickets"][3]["pending"] == {
              "machine": "sas:x:1", "txn": "t", "amountMc": 20000,
              "at": "2020-01-02T08:00:00"}
          and all(t["pending"] is None for t in r["tickets"][:3]),
          [t["pending"] for t in r["tickets"]])
    rs = hs_l.tito_list(state="issued")
    check("state filter is exact + total counts the filtered set",
          rs["total"] == 1
          and rs["tickets"][0]["canonical"] == "777700000000000002", rs)
    check("unknown state filters to empty, no error",
          hs_l.tito_list(state="nope") == {"total": 0, "tickets": []})
    rv = hs_l.tito_list(vid="777700000000000003")
    check("vid lookup by canonical",
          rv["total"] == 1
          and rv["tickets"][0]["vn16"] == "7700000000000003", rv)
    rv = hs_l.tito_list(vid="7700000000000003")
    check("vid lookup by vn16 hits the same row",
          rv["total"] == 1
          and rv["tickets"][0]["canonical"] == "777700000000000003", rv)
    check("vid miss is empty, not an error",
          hs_l.tito_list(vid="999999999999999999")
          == {"total": 0, "tickets": []})
    rv = hs_l.tito_list(vid="7700000000000003", state="minted", limit=0)
    check("vid short-circuits state/limit (api_vouchers parity)",
          rv["total"] == 1, rv)
    rp = hs_l.tito_list(limit=2)
    check("limit pages; total stays the full filtered count",
          rp["total"] == 4 and len(rp["tickets"]) == 2
          and rp["tickets"][0]["canonical"].endswith("3"), rp)
    check("offset continues the page",
          [t["canonical"][-1]
           for t in hs_l.tito_list(limit=2, offset=2)["tickets"]]
          == ["1", "4"])
    check("limit clamps low to 1",
          len(hs_l.tito_list(limit=0)["tickets"]) == 1)
    check("limit clamps high to 500 (no crash)",
          hs_l.tito_list(limit=10 ** 9)["total"] == 4)
    check("negative offset clamps to 0",
          hs_l.tito_list(offset=-5)
          ["tickets"][0]["canonical"].endswith("3"))
    check("uncastable limit/offset degrade to defaults",
          hs_l.tito_list(limit=None, offset="x")["total"] == 4)
    # a LIVE mint flows straight through (note: the mint's TTL sweep
    # prunes the 2020-dated poked 'minted' row — expected, still 4 rows)
    lm = hs_l.tito_mint("sas:smib-bb2", 1, 25_000)
    rl = hs_l.tito_list(vid=lm["canonical"])
    check("a live mint appears via vid",
          rl["total"] == 1 and rl["tickets"][0]["state"] == "minted"
          and rl["tickets"][0]["amountMc"] == 25_000, rl)
    check("live mint sorts first (fresh minted_at)",
          hs_l.tito_list()["tickets"][0]["canonical"] == lm["canonical"])
    # degrade path: a dead connection must answer empty, never raise
    hs_l.close()
    check("db fault degrades to an empty envelope (never raises)",
          hs_l.tito_list() == {"total": 0, "tickets": []})

    print("— TitoAuthority union")

    class FakeVS:
        def __init__(self):
            self.ids = {"473829105628374651"}
            self.calls = []

        def has_id(self, v):
            return v in self.ids

        def authorize_redemption(self, m, t, v):
            self.calls.append(("auth", m, t, v))
            return {"exc": 0, "amt": "50000", "creditType": "G2S_cashable",
                    "retry": False, "reason": "authorized"}

        def close_redemption(self, m, t, v, a):
            self.calls.append(("close", m, t, v, a))
            return "redeemed"

    vs = FakeVS()
    ta = TitoAuthority(vs, hs)
    dec = ta.authorize("E", "t1", "473829105628374651")
    check("G2S-issued id delegates to the VoucherStore",
          dec["exc"] == 0 and vs.calls[-1][0] == "auth", dec)
    d4 = hs.tito_mint("sas:smib-bb2", 1, 40_000)
    hs.tito_record_issued(d4["validationNumber"], 40_000, "sas:smib-bb2", 1)
    dec = ta.authorize("IGT_AVP", "t2", d4["canonical"])
    check("SAS ticket resolves from the hub ledger in voucher dialect",
          dec["exc"] == 0 and dec["amt"] == "40000"
          and dec["creditType"] == "G2S_cashable", dec)
    st = ta.close("IGT_AVP", "t2", d4["canonical"], "G2S_redeemed")
    check("union close consumes in the hub ledger", st == "redeemed")
    dec = ta.authorize("E", "t3", "111111111111111111")
    check("unknown to both ledgers -> exc 4", dec["exc"] == 4, dec)
    dec = ta.authorize("E2", "t4", d4["canonical"])
    check("redeemed hub ticket -> exc 2 in voucher dialect",
          dec["exc"] == 2, dec)

    hs.close()

    # -- v12 expired-ticket reaper: stamps, immortality, grace, display ----
    print("\n— v12 expired-ticket reaper")
    import sqlite3 as _sql
    hs, d = fresh()

    import datetime as _dtm

    def _days_back(days_ago, fmt="%Y-%m-%dT%H:%M:%S"):
        # the same time of day, that many DATES back. Not seconds: across
        # a clock change "30 x 86400 s ago" is an hour off, and from 11 pm
        # to midnight that lands on the wrong date (seen 2026-11-17, with
        # the clocks having gone back on Nov 1: this gate failed on the
        # hub at 23:05 and stopped an update).
        return (_dtm.datetime.now()
                - _dtm.timedelta(days=days_ago)).strftime(fmt)

    def _issue(days_ago, expire_days, amt=10_000):
        m = hs.tito_mint("sas:smib-bb2", 1, amt)
        hs.tito_record_issued(m["validationNumber"], amt, "sas:smib-bb2", 1)
        back = _days_back(days_ago)
        with hs.lock:
            hs._conn.execute(
                "UPDATE tito_tickets SET issued_at=?, expire_days=? "
                "WHERE canonical=?", (back, expire_days, m["canonical"]))
            hs._conn.commit()
        return m["canonical"]

    hs.set_host_setting("ticket_expire_days", "30")
    m = hs.tito_mint("sas:smib-bb2", 1, 5_000)
    hs.tito_record_issued(m["validationNumber"], 5_000, "sas:smib-bb2", 1)
    with hs.lock:
        row = hs._conn.execute(
            "SELECT expire_days FROM tito_tickets WHERE canonical=?",
            (m["canonical"],)).fetchone()
    check("issue stamps expire_days from the CURRENT setting",
          row["expire_days"] == 30, dict(row))
    hs.set_host_setting("ticket_expire_days", "")
    m0 = hs.tito_mint("sas:smib-bb2", 1, 5_000)
    hs.tito_record_issued(m0["validationNumber"], 5_000, "sas:smib-bb2", 1)
    with hs.lock:
        row = hs._conn.execute(
            "SELECT expire_days FROM tito_tickets WHERE canonical=?",
            (m0["canonical"],)).fetchone()
    check("setting 0/absent stamps NULL (paper printed no expiration)",
          row["expire_days"] is None, dict(row))

    immortal = _issue(days_ago=400, expire_days=None)   # pre-v12/never row
    fresh_exp = _issue(days_ago=10, expire_days=30)     # not yet expired
    last_day = _issue(days_ago=30, expire_days=30)      # its date is today
    # and another such, printed one second after midnight that day: by the
    # hour its 30 days are up; by the date on it, it is good all of today
    early = _issue(days_ago=30, expire_days=30)
    first_second = _days_back(30, "%Y-%m-%dT00:00:01")
    with hs.lock:
        hs._conn.execute("UPDATE tito_tickets SET issued_at=? "
                         "WHERE canonical=?", (first_second, early))
        hs._conn.commit()
    day_after = _issue(days_ago=31, expire_days=30)     # its date was yesterday
    kept = _issue(days_ago=35, expire_days=30)          # expired 5 days ago
    dead = _issue(days_ago=90, expire_days=30)          # expired 60 days ago
    sweep = hs.tito_expiry_sweep()
    check("the nightly sweep removes ONLY rows that expired more than "
          "EXPIRED_KEEP_DAYS ago, and says what it found",
          sweep == {"expiredAtMidnight": 1, "expired": 2, "removed": 1},
          sweep)
    with hs.lock:
        left = {r["canonical"] for r in hs._conn.execute(
            "SELECT canonical FROM tito_tickets WHERE state='issued'"
        ).fetchall()}
    check("never-expire row is immortal at any age", immortal in left)
    check("unexpired row survives", fresh_exp in left)
    check("an expired row is kept a while, for the Tickets page to show",
          kept in left and day_after in left)
    check("a row that expired long ago is GONE (unknown = what the paper "
          "promised)", dead not in left)

    lst = hs.tito_list(state="expired")
    check("?state=expired filter finds the expired rows",
          lst["total"] == 2 and {t2["canonical"] for t2 in lst["tickets"]}
          == {kept, day_after}, lst["total"])
    rows_by = {t2["canonical"]: t2 for t2 in hs.tito_list(limit=100)["tickets"]}
    all_rows = {k: v["state"] for k, v in rows_by.items()}
    check("list derives 'expired' for a row past its date",
          all_rows.get(kept) == "expired"
          and all_rows.get(day_after) == "expired")
    check("list keeps 'issued' for unexpired + immortal rows, and for a "
          "ticket on its last day",
          all_rows.get(fresh_exp) == "issued"
          and all_rows.get(immortal) == "issued"
          and all_rows.get(last_day) == "issued")
    import datetime as _dt
    check("the list says the date each ticket is good through (None for "
          "one that never expires)",
          rows_by[last_day]["goodThrough"] == _dt.date.today().isoformat()
          and rows_by[immortal]["goodThrough"] is None,
          (rows_by[last_day]["goodThrough"], rows_by[immortal]["goodThrough"]))
    dec = hs.tito_authorize("IGT_AVP", "tg", kept)
    check("an expired ticket is REFUSED when presented, and the refusal "
          "says expired (no grace: it used to redeem for 30 more days)",
          dec.get("authorized") is False and dec.get("why") == "expired"
          and "good through" in dec.get("reason", ""), dec)
    dec = hs.tito_authorize("IGT_AVP", "td", day_after)
    check("refused from the first midnight after its date",
          dec.get("authorized") is False and dec.get("why") == "expired",
          dec)
    dec = hs.tito_authorize("IGT_AVP", "tl", last_day)
    check("a ticket is good THROUGH the date on it: redeemed on its last "
          "day", dec.get("authorized") is True, dec)
    dec = hs.tito_authorize("IGT_AVP", "te", early)
    check("whatever the hour it was printed at",
          dec.get("authorized") is True, dec)
    dec = hs.tito_authorize("IGT_AVP", "ti", immortal)
    check("a ticket with no expiry stamp is redeemed at any age",
          dec.get("authorized") is True, dec)
    r2 = hs.tito_reissue(kept)
    dec = hs.tito_authorize("IGT_AVP", "tr", kept)
    check("an expired ticket the operator reissues is good again",
          bool(r2) and dec.get("authorized") is True, (r2, dec))
    hs.close()

    operator_controls()
    restricted_promo_flag()

    print("=" * 50)
    print(f"RESULT: {_p} passed, {_f} failed")
    return 1 if _f else 0


if __name__ == "__main__":
    sys.exit(main())
