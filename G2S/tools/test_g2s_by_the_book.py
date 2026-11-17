#!/usr/bin/env python3
"""
test_g2s_by_the_book.py — a pretend EGM that keeps the rules of the G2S
Message Protocol v1.0.3 document, driving a REAL host.

The G2S counterpart of SAS/tests/by_the_book.py. The two machines on the
owner's floor forgive most of what the 2026-11-17 audit of g2s_host.py
against the document found, so they cannot show these faults. This EGM
does what the document says an EGM does in the uncommon cases:

  * after a memory reset it counts transactionIds from 1 again and says so
    in its commsOnLine (§1.21);
  * it re-sends a request it has no answer to, unchanged (§1.14.4);
  * it asks for no validation ids while its buffer is above the minimum,
    and uses the ids it holds in the order given (§21.15, §21.16);
  * a win it pays part of itself is split into egmPaid and request amounts
    (§11.12, Table 11.6), and it refuses a key-off for less than it asked;
  * it keeps sending commitTransfer until it is acknowledged, however long
    that takes (§22.31).

The host is a real G2SHost in a scratch data folder; nothing leaves the
process (what the host would POST to the EGM is captured instead).

In-process, stdlib only. Run:  python3 tools/test_g2s_by_the_book.py
Must end "RESULT: N passed, 0 failed".
"""

import logging
import os
import sys
import tempfile
import threading
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import g2s_host as gh  # noqa: E402

_p = _f = 0


def check(name, ok, detail=""):
    global _p, _f
    if ok:
        _p += 1
        print(f"  ✅ {name}")
    else:
        _f += 1
        print(f"  ❌ {name} {detail}")


def make_host():
    """A real host with its stores in a scratch folder. Outbound POSTs are
    captured in host.sent as (label, inner_xml)."""
    host = gh.G2SHost(data_dir=tempfile.mkdtemp(prefix="g2s_btb_"),
                      keepalive_ms=0, auto_clock=False, subscribe=False)
    host.sent = []

    def capture(assoc, inner_xml, label):
        host.sent.append((label, inner_xml))
        return True
    host.post_to_egm = capture
    return host


def drain(host, assoc):
    """Wait until the host's send worker has built and 'posted' everything
    queued so far for this machine."""
    done = threading.Event()
    host._enqueue(assoc, lambda a: done.set() or None)
    assert done.wait(5.0), "the host's send worker did not drain"


def commands(inner_xml):
    """[(class, command, {attr: value}, element)] in one host message."""
    out = []
    root = ET.fromstring(inner_xml)
    body = root.find(f"{{{gh.SCHEMA_NS}}}g2sBody")
    if body is None:
        return out
    for cls in body:
        for cmd in cls:
            attrs = {k.rsplit("}", 1)[-1]: v for k, v in cmd.attrib.items()}
            out.append((gh.localname(cls.tag), gh.localname(cmd.tag), attrs,
                        cmd))
    return out


class Egm:
    """One pretend EGM. Every send returns what the host answered with, as
    a list of (class, command, attrs, element)."""

    def __init__(self, host, egm_id):
        self.host = host
        self.egm_id = egm_id
        self.cid = 0                # commandId
        self.txn = 0                # the ONE transaction counter (§1.x)
        self.clock = 0              # ticks the date-times it stamps
        self.val_list = "0"         # validationListId it holds
        self.val_ids = []           # its buffer, in the order given
        self.last = None            # the last message, for a retry
        self.assoc = host.assoc(egm_id)

    # -- plumbing -------------------------------------------------------------

    def stamp(self):
        self.clock += 1
        return f"2026-11-17T10:{self.clock // 60:02d}:{self.clock % 60:02d}" \
               ".000-05:00"

    def next_txn(self):
        self.txn += 1
        return str(self.txn)

    def _message(self, cls, device, child, cid, sid, stype):
        ts = gh.now_iso()
        return (
            f'<g2s:g2sMessage xmlns:g2s="{gh.SCHEMA_NS}">'
            f'<g2s:g2sBody g2s:egmId="{self.egm_id}" g2s:hostId="1" '
            f'g2s:dateTimeSent="{ts}">'
            f'<g2s:{cls} g2s:deviceId="{device}" g2s:commandId="{cid}" '
            f'g2s:sessionType="{stype}" g2s:sessionId="{sid}" '
            f'g2s:timeToLive="30000" g2s:dateTime="{ts}">{child}'
            f'</g2s:{cls}></g2s:g2sBody></g2s:g2sMessage>')

    def _deliver(self, inner):
        before = len(self.host.sent)
        ack = self.host.handle_g2s_message(inner, self.egm_id)
        assert "g2sAck" in ack, ack
        drain(self.host, self.assoc)
        out = []
        for _label, xml in self.host.sent[before:]:
            out.extend(commands(xml))
        return out

    def send(self, cls, child, device="1", stype="G2S_request"):
        self.cid += 1
        self.last = self._message(cls, device, child, self.cid, self.cid,
                                  stype)
        return self._deliver(self.last)

    def resend(self):
        """The same request again, byte for byte: no answer reached us."""
        return self._deliver(self.last)

    def answer(self, got, command):
        hits = [a for _c, name, a, _e in got if name == command]
        return hits[-1] if hits else None

    # -- communications -------------------------------------------------------

    def comms_online(self, reset=False, true="true"):
        flags = "".join(f' g2s:{f}="{true}"' for f in (
            "deviceReset", "deviceChanged", "subscriptionLost",
            "metersReset")) if reset else ""
        return self.send(
            "communications",
            f'<g2s:commsOnLine g2s:egmLocation="http://127.0.0.1:9/"'
            f'{flags}/>')

    def memory_reset(self):
        """A RAM clear: transactions and buffers are gone, counters start
        again, and the next commsOnLine says so (§1.21)."""
        self.txn = 0
        self.val_list, self.val_ids = "0", []
        return self.comms_online(reset=True)

    # -- voucher ----------------------------------------------------------------

    def get_validation_data(self, num, expired=False):
        got = self.send(
            "voucher",
            f'<g2s:getValidationData g2s:configurationId="0" '
            f'g2s:validationListId="{self.val_list}" '
            f'g2s:numValidationIds="{num}" '
            f'g2s:valIdListExpired="{"true" if expired else "false"}"/>')
        for _c, name, attrs, el in got:
            if name != "validationData":
                continue
            ids = [i.get(f"{{{gh.SCHEMA_NS}}}validationId") for i in el]
            if gh.g2s_true(attrs.get("deleteCurrent")):
                self.val_ids = []               # §21.16
            self.val_ids.extend(ids)            # "following any existing"
            self.val_list = attrs.get("validationListId")
            return attrs, ids
        return None, []

    def issue_voucher(self, amount, txn=None):
        """Print a ticket with the next id in the buffer (§21.16: "MUST be
        used in the order provided")."""
        vid = self.val_ids.pop(0)
        txn = txn or self.next_txn()
        got = self.send(
            "voucher",
            f'<g2s:issueVoucher g2s:transactionId="{txn}" '
            f'g2s:validationId="{vid}" g2s:voucherAmt="{amount}" '
            f'g2s:creditType="G2S_cashable" '
            f'g2s:transferDateTime="{self.stamp()}" '
            f'g2s:egmAction="G2S_issued" g2s:voucherSequence="{txn}"/>')
        return vid, txn, self.answer(got, "issueVoucherAck")

    def redeem_voucher(self, vid, txn=None):
        txn = txn or self.next_txn()
        got = self.send(
            "voucher",
            f'<g2s:redeemVoucher g2s:transactionId="{txn}" '
            f'g2s:validationId="{vid}"/>')
        return txn, self.answer(got, "authorizeVoucher")

    def commit_voucher(self, txn, vid, amount, redeemed=True):
        got = self.send(
            "voucher",
            f'<g2s:commitVoucher g2s:transactionId="{txn}" '
            f'g2s:validationId="{vid}" g2s:voucherAmt="{amount}" '
            f'g2s:creditType="G2S_cashable" '
            f'g2s:transferAmt="{amount if redeemed else 0}" '
            f'g2s:transferDateTime="{self.stamp()}" '
            f'g2s:egmAction="{"G2S_redeemed" if redeemed else "G2S_rejected"}"'
            f' g2s:egmException="{0 if redeemed else 2}"/>')
        return self.answer(got, "commitVoucherAck")

    # -- handpay ----------------------------------------------------------------

    def handpay_request(self, request, egm_paid=0, txn=None, when=None):
        txn = txn or self.next_txn()
        self.lockup = {"txn": txn, "request": request, "paid": egm_paid,
                       "when": when or self.stamp()}
        got = self.send(
            "handpay",
            f'<g2s:handpayRequest g2s:transactionId="{txn}" '
            f'g2s:handpayType="G2S_gameWin" '
            f'g2s:handpayDateTime="{self.lockup["when"]}" '
            f'g2s:requestCashableAmt="{request}" '
            f'g2s:egmPaidCashableAmt="{egm_paid}" '
            f'g2s:remoteCredit="true" g2s:remoteHandpay="true"/>')
        return txn, self.answer(got, "handpayAck")

    def take_key_off(self, sent):
        """What a setRemoteKeyOff from the host does here. §11.14: the host
        MAY send other amounts, the EGM MAY answer an error; this one
        refuses a key-off that is not for what it asked."""
        attrs = self.answer(sent, "setRemoteKeyOff")
        if attrs is None:
            return "nothing sent"
        if attrs["transactionId"] != self.lockup["txn"]:
            return "G2S_JPX003"                 # not this handpay
        if int(attrs.get("keyOffCashableAmt", 0)) != self.lockup["request"]:
            return "G2S_JPX004"
        return "accepted"

    def keyed_off(self, txn, amount, how="G2S_remoteHandpay", when=None):
        got = self.send(
            "handpay",
            f'<g2s:keyedOff g2s:transactionId="{txn}" g2s:keyOffType="{how}" '
            f'g2s:keyOffCashableAmt="{amount}" '
            f'g2s:keyOffDateTime="{when or self.stamp()}"/>')
        return self.answer(got, "keyedOffAck")

    def handpay_log(self, entries):
        """An unsolicited handpayLogList, as the join-time read returns."""
        rows = "".join(
            f'<g2s:handpayLog g2s:logSequence="{i + 1}" g2s:deviceId="1" '
            f'g2s:transactionId="{e["txn"]}" g2s:handpayState="{e["state"]}" '
            f'g2s:handpayType="G2S_gameWin" '
            f'g2s:handpayDateTime="{e["when"]}" '
            f'g2s:requestCashableAmt="{e["request"]}" '
            f'g2s:egmPaidCashableAmt="{e.get("paid", 0)}" '
            f'g2s:keyOffType="G2S_unknown" g2s:remoteCredit="true"/>'
            for i, e in enumerate(entries))
        return self.send("handpay",
                         f"<g2s:handpayLogList>{rows}</g2s:handpayLogList>",
                         stype="G2S_response")

    # -- wat ----------------------------------------------------------------------

    def commit_transfer(self, txn, amount, direction="G2S_fromEgm",
                        request_id="0", exception=0):
        got = self.send(
            "wat",
            f'<g2s:commitTransfer g2s:transactionId="{txn}" '
            f'g2s:requestId="{request_id}" g2s:watDirection="{direction}" '
            f'g2s:transCashableAmt="{amount}" g2s:transPromoAmt="0" '
            f'g2s:transNonCashAmt="0" g2s:egmException="{exception}" '
            f'g2s:transDateTime="{self.stamp()}"/>')
        return self.answer(got, "commitTransferAck")


def joined(host, egm_id):
    egm = Egm(host, egm_id)
    egm.comms_online()
    return egm


def cash(host, account):
    return int(host.account_store.get(account)["cashableMillicents"])


# ---------------------------------------------------------------------------

def item_3_key_off_amount():
    print("— audit item 3: the key-off is for what the EGM asked (§11.12, "
          "Table 11.6: win = egmPaid + request)")
    host = make_host()
    egm = joined(host, "BTB_handpay")
    a = egm.assoc

    # a 2,000.00 win: 1,200.00 to the meter, 800.00 by hand
    txn, ack = egm.handpay_request(request=80_000_000, egm_paid=120_000_000)
    check("the handpayRequest is acknowledged with its transactionId",
          ack is not None and ack["transactionId"] == txn, ack)
    pend = a.handpays_pending.get(txn) or {}
    check("the lockup on the hub is the 800.00 the EGM asked for (it was 0: "
          "the 1,200.00 the EGM paid was taken off a second time)",
          pend.get("pendingCashableAmt") == 80_000_000, pend)
    before = len(host.sent)
    host.reset_newest_handpay(a)
    drain(host, a)
    sent = [c for _l, x in host.sent[before:] for c in commands(x)]
    ko = egm.answer(sent, "setRemoteKeyOff") or {}
    check("setRemoteKeyOff carries keyOffCashableAmt = the request amount",
          ko.get("keyOffCashableAmt") == "80000000", ko)
    check("the strict EGM accepts it (it refuses a key-off for less with "
          "G2S_JPX004 and stays locked)",
          egm.take_key_off(sent) == "accepted", egm.take_key_off(sent))

    # request larger than what the EGM paid: was 900 instead of 1,200
    txn2, _ = egm.handpay_request(request=120_000_000, egm_paid=30_000_000)
    check("request 1,200.00 with 300.00 EGM-paid: 1,200.00 is owed",
          a.handpays_pending[txn2]["pendingCashableAmt"] == 120_000_000,
          a.handpays_pending.get(txn2))

    # nothing EGM-paid: every handpay seen on a real machine so far
    txn3, _ = egm.handpay_request(request=50_000_000)
    check("nothing EGM-paid: unchanged",
          a.handpays_pending[txn3]["pendingCashableAmt"] == 50_000_000)

    # the same figures when a lockup is rebuilt from the EGM's log
    host2 = make_host()
    egm2 = joined(host2, "BTB_handpay_log")
    egm2.handpay_log([{"txn": "9", "state": "G2S_handpayPend",
                       "when": egm2.stamp(), "request": 80_000_000,
                       "paid": 120_000_000}])
    rec = egm2.assoc.handpays_pending.get("9") or {}
    check("a lockup recovered from the handpay log owes the request amount "
          "too", rec.get("pendingCashableAmt") == 80_000_000, rec)


def item_1_vouchers_after_a_memory_reset():
    print("— audit item 1: a ticket printed after a memory reset, on a "
          "transactionId the EGM has used before (§1.21)")
    host = make_host()
    egm = joined(host, "BTB_voucher")
    store = host.voucher_store

    egm.get_validation_data(20, expired=True)
    vid_a, txn_a, ack = egm.issue_voucher(1_000_000)
    check("ticket A (txn 1) is recorded as issued",
          ack is not None and txn_a == "1"
          and store.state["issuedIds"][vid_a]["state"] == "issued")

    # a real retry: the same command again
    n = len(store.state["vouchers"])
    got = egm.resend()
    check("the same issueVoucher sent again is acknowledged and not "
          "recorded twice",
          egm.answer(got, "issueVoucherAck") is not None
          and len(store.state["vouchers"]) == n)

    # the EGM's memory is cleared: counters restart, new ids from the host
    egm.memory_reset()
    attrs, ids = egm.get_validation_data(20, expired=True)
    check("after the reset the EGM is given a fresh list",
          len(ids) == 20 and attrs is not None)
    vid_b, txn_b, ack = egm.issue_voucher(2_500_000)
    info_b = store.state["issuedIds"].get(vid_b) or {}
    check("ticket B lands on transactionId 1 again and is RECORDED (it was "
          "taken for a retry of ticket A: acknowledged, never issued)",
          txn_b == "1" and ack is not None
          and info_b.get("state") == "issued"
          and info_b.get("voucherAmt") == "2500000", info_b)
    _t, auth = egm.redeem_voucher(vid_b)
    check("ticket B is authorized when it comes back, for its own amount "
          "(it was refused: hostException 4)",
          auth is not None and auth.get("voucherAmt") == "2500000"
          and "hostException" not in auth, auth)

    print("— audit item 1: a redemption closed on a reused transactionId")
    host = make_host()
    egm = joined(host, "BTB_commit")
    other = joined(host, "BTB_commit_other")
    store = host.voucher_store
    egm.get_validation_data(20, expired=True)
    v1, _t, _a = egm.issue_voucher(1_000_000)          # txn 1
    r1, auth = other.redeem_voucher(v1)                 # other's txn 1
    other.commit_voucher(r1, v1, 1_000_000, redeemed=True)
    check("ticket 1 printed here is redeemed at the other machine",
          store.state["issuedIds"][v1]["state"] == "redeemed")
    v2, _t, _a = egm.issue_voucher(3_000_000)          # txn 2
    other.memory_reset()                                # its counter restarts
    r2, auth = other.redeem_voucher(v2)                 # txn 1 again
    check("the other machine's next redemption reuses transactionId 1 and "
          "is authorized", r2 == "1" and auth is not None
          and auth.get("voucherAmt") == "3000000", auth)
    other.commit_voucher(r2, v2, 3_000_000, redeemed=False)
    check("its commit (rejected) is NOT taken for the earlier commit: the "
          "ticket goes back to issued and can be presented again",
          store.state["issuedIds"][v2]["state"] == "issued",
          store.state["issuedIds"][v2])


def item_1_handpays_after_a_memory_reset():
    print("— audit item 1: a lockup after a memory reset, on a "
          "transactionId the EGM has used before")
    host = make_host()
    egm = joined(host, "BTB_hp_reuse")
    a = egm.assoc
    acct, _ = host.account_store.create("Player One")
    with host.companion_lock:
        host.card_sessions[egm.egm_id] = {"accountId": acct["id"],
                                          "name": "Player One"}

    t1, _ = egm.handpay_request(request=10_000_000)
    got = egm.resend()
    check("the same handpayRequest sent again is acknowledged, one lockup",
          egm.answer(got, "handpayAck") is not None
          and len(a.handpays_pending) == 1)
    egm.keyed_off(t1, 10_000_000)
    check("hand-paid: the wallet gets 100.00",
          cash(host, acct["id"]) == 10_000_000, cash(host, acct["id"]))
    egm.resend()
    check("the same keyedOff sent again pays nothing more",
          cash(host, acct["id"]) == 10_000_000, cash(host, acct["id"]))

    egm.memory_reset()
    t2, ack = egm.handpay_request(request=40_000_000)
    check("the new lockup reuses transactionId 1 and is ON THE HUB (it was "
          "acknowledged and dropped: the machine sat locked with nothing "
          "to reset)",
          t2 == t1 and ack is not None and t2 in a.handpays_pending
          and a.handpays_pending[t2]["pendingCashableAmt"] == 40_000_000,
          a.handpays_pending)
    egm.keyed_off(t2, 40_000_000)
    check("and its hand-pay reaches the wallet (it was skipped as already "
          "paid)", cash(host, acct["id"]) == 50_000_000,
          cash(host, acct["id"]))

    egm.memory_reset()
    before = cash(host, acct["id"])
    egm.keyed_off(egm.next_txn(), 6_000_000, how="G2S_localHandpay")
    check("a key-off by hand that reuses transactionId 1, with no request "
          "seen first, is a new handpay and is paid (it was taken for a "
          "retry of the first one)",
          cash(host, acct["id"]) == before + 6_000_000,
          (cash(host, acct["id"]), before))

    print("— a lockup that died with the EGM's memory does not stay on the "
          "hub in place of the new one")
    t3, _ = egm.handpay_request(request=7_000_000)      # txn 2, pending
    egm.memory_reset()
    egm.next_txn()                                      # txn 1 used by a game
    t4, _ = egm.handpay_request(request=9_000_000)      # txn 2 again
    check("the hub shows the new 90.00 lockup, not the dead 70.00 one",
          t4 == t3 and a.handpays_pending[t4]["pendingCashableAmt"]
          == 9_000_000 and len(a.handpays_pending) == 1, a.handpays_pending)
    check("the dead one is kept in the history, marked",
          any(h.get("state") == "lostAtEgmReset"
              and h.get("pendingCashableAmt") == 7_000_000
              for h in a.handpays))

    print("— the reset flags are XML booleans: 1 is true (Appendix A)")
    host = make_host()
    egm = joined(host, "BTB_flags")
    rid, _rec = host.wat_store.new_request(
        egm.egm_id, "1", "house", "G2S_payCredit", 500_000, 0, 0)
    egm.comms_online(reset=True, true="1")
    rec = host.wat_store.find(egm.egm_id, request_id=rid) or {}
    check('deviceReset="1" is read as set (an unanswered host push is '
          'written off, as with "true")', rec.get("state") == "abandoned",
          rec.get("state"))


def item_2_a_list_the_host_did_not_issue():
    print("— audit item 2: an EGM holding a validation list this host did "
          "not issue, asking for no ids (§21.15, §21.16)")
    host = make_host()
    egm = joined(host, "BTB_stale")
    store = host.voucher_store
    # the EGM last got its ids from another hub, or from a voucher store
    # that has since been replaced: a full buffer under a list id we do
    # not know. Above its minimum it asks for nothing.
    egm.val_list = "999"
    egm.val_ids = [f"{i:018d}" for i in range(1, 21)]
    attrs, ids = egm.get_validation_data(0)
    check("the answer replaces the buffer: deleteCurrent=true and a full "
          "list (it was the unknown list id handed back, deleteCurrent="
          "false, no ids)",
          attrs is not None and gh.g2s_true(attrs.get("deleteCurrent"))
          and len(ids) == 20, (attrs, len(ids)))
    check("under a list id this host issued",
          attrs is not None and attrs.get("validationListId")
          == str(store.last_list_id(egm.egm_id))
          and attrs.get("validationListId") != "999", attrs)
    check("the EGM's buffer is now ours alone",
          egm.val_ids == ids and egm.val_list != "999")
    vid, _txn, _ack = egm.issue_voucher(1_500_000)
    _t, auth = egm.redeem_voucher(vid)
    check("the next ticket it prints is one we can redeem",
          auth is not None and auth.get("voucherAmt") == "1500000"
          and "hostException" not in auth, auth)

    egm.assoc.voucher_reconciling = True
    attrs, ids = egm.get_validation_data(0)
    check("holding OUR list and asking for none: an empty answer, nothing "
          "deleted, the same list id",
          attrs is not None and ids == []
          and not gh.g2s_true(attrs.get("deleteCurrent"))
          and attrs.get("validationListId") == egm.val_list, attrs)
    check("which also ends a reconcile that was waiting for it",
          egm.assoc.voucher_reconciling is False)

    print("— the maxValIds the EGM reported sets the size of the replacement")
    host = make_host()
    egm = joined(host, "BTB_stale_profile")
    egm.assoc.voucher_device["profile"] = {"maxValIds": "35"}
    egm.val_list, egm.val_ids = "4242", ["1" * 18]
    attrs, ids = egm.get_validation_data(0)
    check("35 ids for an EGM whose profile says its buffer holds 35",
          len(ids) == 35 and gh.g2s_true((attrs or {}).get("deleteCurrent")),
          len(ids))

    print("— a reconcile left in flight does not survive a new handshake")
    egm.assoc.voucher_reconciling = True
    egm.comms_online()
    check("commsOnLine clears it", egm.assoc.voucher_reconciling is False)


def _cash_out(host, egm, txn, amount, account):
    """An EGM-initiated cash-out to an account, authorized: the state a
    commitTransfer arrives in."""
    host.wat_store.on_initiate(egm.egm_id, txn, "0", "1", "G2S_fromEgm",
                               "G2S_payCredit", (amount, 0, 0), amount,
                               account)
    host.wat_store.on_authorize(egm.egm_id, txn, account, (amount, 0, 0),
                                0, None)


def item_9_a_commit_retried_after_a_long_absence():
    print("— audit item 9: commitTransfer is re-sent until acknowledged, "
          "with no time limit (§22.31)")
    host = make_host()
    egm = joined(host, "BTB_wat_a")
    busy = joined(host, "BTB_wat_b")
    acct, _ = host.account_store.create("Player One")
    aid = acct["id"]

    _cash_out(host, egm, "7", 5_000_000, aid)
    ack = egm.commit_transfer("7", 5_000_000)
    the_commit = egm.last
    check("the cash-out is credited once and acknowledged",
          ack is not None and ack["transactionId"] == "7"
          and cash(host, aid) == 5_000_000, cash(host, aid))
    rec = host.wat_store.find(egm.egm_id, txn_id="7") or {}
    check("the transfer's record says its money has been moved",
          "commit" in (rec.get("legs") or []), rec.get("legs"))
    egm.resend()
    check("the same commit sent again credits nothing",
          cash(host, aid) == 5_000_000, cash(host, aid))

    # our ack never arrived and the machine went off the network. The
    # floor carried on: the ledger moved well past the entry, and other
    # machines made more transfers than one machine's ring holds.
    host.account_store.KEEP_LEDGER = 5
    for i in range(8):
        host.account_store.adjust("house", 1000, 0, 0, note="floor traffic",
                                  ref=f"traffic:{i}")
    check("(the ledger no longer holds the cash-out's entry)",
          not any("txn7" in str(e.get("ref"))
                  for e in host.account_store.state["ledger"]))
    host.wat_store.KEEP_TRANSFERS = 5
    for i in range(1, 9):
        _cash_out(host, busy, str(100 + i), 100_000, aid)
        busy.commit_transfer(str(100 + i), 100_000)
    credited = cash(host, aid)
    check("(the other machine's eight transfers were credited)",
          credited == 5_000_000 + 8 * 100_000, credited)

    egm.comms_online()                      # the machine is back
    got = egm._deliver(the_commit)          # and still owes its commit
    check("its commit is acknowledged again",
          egm.answer(got, "commitTransferAck") is not None)
    check("and the wallet is NOT credited a second time (it was: the "
          "record had been pushed out by other machines' transfers, or the "
          "ledger entry had rolled off)",
          cash(host, aid) == credited, (cash(host, aid), credited))
    check("the absent machine's record was still there",
          host.wat_store.find(egm.egm_id, txn_id="7") is not None)

    print("— each machine keeps its own newest records")
    mine = [r for r in host.wat_store.state["transfers"]
            if r.get("egmId") == busy.egm_id]
    check("the busy machine is trimmed to its own limit",
          len(mine) == 5, len(mine))

    print("— a transfer with money in escrow is never dropped")
    host = make_host()
    egm = joined(host, "BTB_wat_escrow")
    host.wat_store.KEEP_TRANSFERS = 3
    host.wat_store.on_initiate(egm.egm_id, "50", "0", "1", "G2S_toEgm",
                               "G2S_payCredit", (2_000_000, 0, 0),
                               2_000_000, "house")
    host.account_store.adjust(
        "house", -2_000_000, 0, 0, note="escrow",
        ref=host._wat_ref(egm.egm_id,
                          host.wat_store.find(egm.egm_id, txn_id="50"),
                          "escrow"))
    host.wat_store.on_authorize(egm.egm_id, "50", "house", (2_000_000, 0, 0),
                                0, (2_000_000, 0, 0))
    for i in range(1, 8):
        _cash_out(host, egm, str(60 + i), 1000, "house")
        egm.commit_transfer(str(60 + i), 1000)
    check("seven later transfers on the same machine did not push it out",
          host.wat_store.find(egm.egm_id, txn_id="50") is not None)
    house = cash(host, "house")
    egm.commit_transfer("50", 0, direction="G2S_toEgm", exception=3)
    check("so when the machine reports the push failed, the escrow is "
          "refunded", cash(host, "house") == house + 2_000_000,
          (cash(host, "house"), house))
    the_commit = egm.last
    acct, _ = host.account_store.create("Someone Else")
    host.account_store.KEEP_LEDGER = 3
    for i in range(6):
        host.account_store.adjust(acct["id"], 1000, 0, 0, note="traffic",
                                  ref=f"traffic:{i}")
    egm._deliver(the_commit)
    check("once, even after the ledger has moved past the refund's entry",
          cash(host, "house") == house + 2_000_000,
          (cash(host, "house"), house))

    print("— a transactionId reused after a memory reset is a new transfer")
    host = make_host()
    egm = joined(host, "BTB_wat_reuse")
    acct, _ = host.account_store.create("Player One")
    aid = acct["id"]
    _cash_out(host, egm, "1", 5_000_000, aid)
    egm.commit_transfer("1", 5_000_000)
    egm.memory_reset()
    _cash_out(host, egm, "1", 3_000_000, aid)
    egm.commit_transfer("1", 3_000_000)
    check("both cash-outs are credited (the note on the first record does "
          "not cover the second)", cash(host, aid) == 8_000_000,
          cash(host, aid))
    both = [r for r in host.wat_store.state["transfers"]
            if r.get("egmId") == egm.egm_id]
    old, new = both[0], both[1]
    old["legs"], new["legs"] = [], []
    host.wat_store.mark_leg(egm.egm_id, "1", old["createdAt"], "refund")
    check("a movement is noted on the record it was made for, found by its "
          "createdAt as well as its transactionId",
          old["legs"] == ["refund"] and new["legs"] == []
          and old["createdAt"] != new["createdAt"], (old, new))


def main():
    logging.basicConfig(level=logging.CRITICAL)
    logging.getLogger("g2s_host").setLevel(logging.CRITICAL)
    logging.getLogger("g2s_wire").setLevel(logging.CRITICAL)
    item_3_key_off_amount()
    item_1_vouchers_after_a_memory_reset()
    item_1_handpays_after_a_memory_reset()
    item_2_a_list_the_host_did_not_issue()
    item_9_a_commit_retried_after_a_long_absence()
    print("=" * 50)
    print(f"RESULT: {_p} passed, {_f} failed")
    return 1 if _f else 0


if __name__ == "__main__":
    sys.exit(main())
