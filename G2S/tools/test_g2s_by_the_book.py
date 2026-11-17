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
    that takes (§22.31);
  * it writes & < " inside an attribute as entity references (§1.11);
  * a host that answers G2S_MSX003 makes it start again with commsOnLine,
    and its inbound queue can be full (G2S_MSX006, §1.18.11);
  * an error answer carries the request's sessionId and no command
    (§1.18.3.3), a long list comes in several commands with sessionMore
    (§1.18.3.2), and it looks for sessionRetry on an answer it has had
    before.

The host is a real G2SHost in a scratch data folder; nothing leaves the
process (what the host would POST to the EGM is captured instead).

In-process, stdlib only. Run:  python3 tools/test_g2s_by_the_book.py
Must end "RESULT: N passed, 0 failed".
"""

import html
import logging
import os
import socket
import sys
import tempfile
import threading
import time
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


def egm_reply(egm_id, code=None):
    """What an EGM answers one of the host's POSTs with: a SOAP reply whose
    payload is a g2sAck, clean or carrying an errorCode."""
    err = f' g2s:errorCode="{code}"' if code else ""
    inner = (f'<g2s:g2sMessage xmlns:g2s="{gh.SCHEMA_NS}"><g2s:g2sAck '
             f'g2s:egmId="{egm_id}" g2s:hostId="1" '
             f'g2s:dateTimeSent="{gh.now_iso()}"{err}/></g2s:g2sMessage>')
    return ('<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/'
            f'soap/envelope/" xmlns:w="{gh.WSDL_NS}"><SOAP-ENV:Body>'
            f'<w:g2sResponse><w:g2sResponse>{html.escape(inner, quote=False)}'
            '</w:g2sResponse></w:g2sResponse></SOAP-ENV:Body>'
            '</SOAP-ENV:Envelope>')


def make_host():
    """A real host with its stores in a scratch folder. Nothing leaves the
    process: the one function that would open a socket to the EGM is
    replaced, and everything above it (post_to_egm, the send worker) is
    the host's own. host.sent holds ("", inner_xml) for every message that
    REACHED the pretend EGM and was taken by it; host.attempts counts the
    POSTs tried per machine. host.egm_mode[egm_id] says how the EGM's
    listener behaves:
        "ok"       answers with a clean g2sAck (the default)
        "refused"  the connection is refused: nothing goes out
        "silent"   the request goes out and no answer comes back
        ["refused", "ok", ...]   one entry per POST, the last one repeating
        "G2S_MSX006" (or any code)   answers with that errorCode, taking
                   none of the commands"""
    host = gh.G2SHost(data_dir=tempfile.mkdtemp(prefix="g2s_btb_"),
                      keepalive_ms=0, auto_clock=False, subscribe=False)
    host.sent = []
    host.attempts = {}
    host.egm_mode = {}

    def fake_post(assoc, url, path, body):
        host.attempts[assoc.egm_id] = host.attempts.get(assoc.egm_id, 0) + 1
        mode = host.egm_mode.get(assoc.egm_id, "ok")
        if isinstance(mode, list):
            if len(mode) > 1:
                host.egm_mode[assoc.egm_id] = mode[1:]
            mode = mode[0]
        assoc.post_reached = False
        if mode == "refused":
            raise ConnectionRefusedError("connection refused")
        assoc.post_reached = True
        if mode == "silent":
            raise socket.timeout("timed out")
        if mode != "ok":
            return 200, egm_reply(assoc.egm_id, mode)
        inner, _egm, _host = host.extract_escaped_inner(ET.fromstring(body))
        host.sent.append(("", host.inner_payload(inner)))
        return 200, egm_reply(assoc.egm_id)
    host._egm_http_post = fake_post
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
        self.raw = []               # the host messages the last send drew
        self.host_no = "1"          # the host number it has the hub under
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
            f'<g2s:g2sBody g2s:egmId="{self.egm_id}" '
            f'g2s:hostId="{self.host_no}" g2s:dateTimeSent="{ts}">'
            f'<g2s:{cls} g2s:deviceId="{device}" g2s:commandId="{cid}" '
            f'g2s:sessionType="{stype}" g2s:sessionId="{sid}" '
            f'g2s:timeToLive="30000" g2s:dateTime="{ts}">{child}'
            f'</g2s:{cls}></g2s:g2sBody></g2s:g2sMessage>')

    def _deliver(self, inner):
        before = len(self.host.sent)
        ack = self.host.handle_g2s_message(inner, self.egm_id)
        assert "g2sAck" in ack, ack
        self.reply = ack            # the host's own ack to this message
        drain(self.host, self.assoc)
        out = []
        self.raw = [xml for _label, xml in self.host.sent[before:]]
        for xml in self.raw:
            out.extend(commands(xml))
        return out

    def repeats(self):
        """{command: was it marked sessionRetry} for the last answers."""
        out = {}
        for xml in self.raw:
            body = ET.fromstring(xml).find(f"{{{gh.SCHEMA_NS}}}g2sBody")
            for cls in (body if body is not None else []):
                flag = gh.g2s_true(cls.get(f"{{{gh.SCHEMA_NS}}}sessionRetry"))
                for cmd in cls:
                    out[gh.localname(cmd.tag)] = flag
        return out

    def refuse(self, cls, session_id, code, device="1"):
        """An error response (§1.18.3.3): the class element with the
        request's sessionId and an errorCode, and NO command inside."""
        self.cid += 1
        ts = gh.now_iso()
        inner = (
            f'<g2s:g2sMessage xmlns:g2s="{gh.SCHEMA_NS}">'
            f'<g2s:g2sBody g2s:egmId="{self.egm_id}" '
            f'g2s:hostId="{self.host_no}" g2s:dateTimeSent="{ts}">'
            f'<g2s:{cls} g2s:deviceId="{device}" g2s:commandId="{self.cid}" '
            f'g2s:sessionType="G2S_response" g2s:sessionId="{session_id}" '
            f'g2s:timeToLive="0" g2s:dateTime="{ts}" '
            f'g2s:errorCode="{code}" g2s:errorText="refused"/>'
            f'</g2s:g2sBody></g2s:g2sMessage>')
        return self._deliver(inner)

    def over_soap(self, inner, wraps=1):
        """Deliver a message the way it really arrives: as the text of a
        SOAP element, read back by the host's own HTTP-side code."""
        text = inner
        for _ in range(wraps):
            text = html.escape(text, quote=False)
        envelope = (
            '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/'
            f'envelope/" xmlns:w="{gh.WSDL_NS}"><soap:Body><w:g2sRequest>'
            f'<w:g2sRequest>{text}</w:g2sRequest>'
            f'<w:g2sEgmId>{self.egm_id}</w:g2sEgmId>'
            '<w:g2sHostId>1</w:g2sHostId></w:g2sRequest></soap:Body>'
            '</soap:Envelope>')
        got, egm_id, _host = self.host.extract_escaped_inner(
            ET.fromstring(envelope))
        reply = self.host.handle_g2s_message(
            self.host.inner_payload(got), egm_id)
        drain(self.host, self.assoc)
        return reply

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


def link_down(host, egm):
    """The hub finds it cannot reach the machine (its keepAlive POSTs have
    failed): the link is demoted from onLine to offline."""
    egm.assoc.comms_state = "onLine"    # the handshake had completed
    host.mark_link_down(egm.assoc, "the gate says so")


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


def host_asks(host, assoc, cls, command_xml, device="1"):
    """Have the host build a request of its own (as its send worker does)
    and return the sessionId it gave it."""
    with assoc.lock:
        sid = str(assoc.next_session_id())
        host.build_inner_request(assoc, cls, device, command_xml, sid,
                                 "30000")
    return sid


def item_4_entity_references():
    print("— audit item 4: & < and \" inside an attribute (§1.11 Table 1.1)")
    host = make_host()
    egm = Egm(host, "BTB_entities")
    egm.cid += 1
    inner = egm._message(
        "communications", "1",
        '<g2s:commsOnLine g2s:egmLocation='
        '"http://127.0.0.1:9/g2s?bank=A&amp;pos=&quot;7&quot;&amp;x=&lt;1"/>',
        egm.cid, egm.cid, "G2S_request")
    reply = egm.over_soap(inner)
    check("a well-formed message with &amp; &quot; &lt; in an attribute is "
          "taken (it was refused: G2S_MSX004, malformed XML)",
          "g2sAck" in reply and "MSX004" not in reply, reply)
    check("and the attribute is read as written",
          egm.assoc.egm_location == 'http://127.0.0.1:9/g2s?bank=A&pos="7"&x=<1',
          egm.assoc.egm_location)
    egm2 = Egm(host, "BTB_entities_twice")
    egm2.cid += 1
    plain = egm2._message(
        "communications", "1",
        '<g2s:commsOnLine g2s:egmLocation="http://127.0.0.1:9/"/>',
        egm2.cid, egm2.cid, "G2S_request")
    reply = egm2.over_soap(plain, wraps=2)
    check("a sender that wraps its payload twice is still read",
          "g2sAck" in reply and "MSX004" not in reply
          and egm2.assoc.comms_online_seen == 1, reply)
    reply = egm2.over_soap("<g2s:g2sMessage this is not XML")
    check("and XML that really is malformed is still refused with "
          "G2S_MSX004", "G2S_MSX004" in reply, reply)


def item_10_an_error_belongs_to_its_request():
    print("— audit item 10: an error answer is charged to the request it "
          "answers, found by its sessionId (§1.18.3.3)")
    host = make_host()
    egm = joined(host, "BTB_errors")
    a = egm.assoc

    # --- wat: a push in flight, and an unrelated read refused
    rid, rec = host.wat_store.new_request(
        egm.egm_id, "1", "house", "G2S_payCredit", 500_000, 0, 0)
    host._wat_mirror(a, rec)
    push_sid = host_asks(host, a, "wat",
                         f'<g2s:initiateRequest g2s:requestId="{rid}" '
                         f'g2s:watDirection="G2S_toEgm"/>')
    read_sid = host_asks(host, a, "wat", "<g2s:getWatStatus/>")
    egm.refuse("wat", read_sid, "G2S_APX003")
    state = (host.wat_store.find(egm.egm_id, request_id=rid) or {}).get(
        "state")
    check("a refused getWatStatus leaves the push in flight alone (it was "
          "parked as failed: 88 such refusals in a day on one floor)",
          state == "requested", state)
    egm.refuse("wat", "987654", "G2S_APX003")
    state = (host.wat_store.find(egm.egm_id, request_id=rid) or {}).get(
        "state")
    check("an error for a sessionId this host never used touches nothing",
          state == "requested", state)
    egm.refuse("wat", push_sid, "G2S_WTX005")
    state = (host.wat_store.find(egm.egm_id, request_id=rid) or {}).get(
        "state")
    check("the push's own refusal parks it", state == "error:G2S_WTX005",
          state)

    # --- wat: two pushes, the OLDER one refused
    rid1, rec1 = host.wat_store.new_request(
        egm.egm_id, "1", "house", "G2S_payCredit", 100_000, 0, 0)
    host._wat_mirror(a, rec1)
    sid1 = host_asks(host, a, "wat",
                     f'<g2s:initiateRequest g2s:requestId="{rid1}"/>')
    rid2, rec2 = host.wat_store.new_request(
        egm.egm_id, "1", "house", "G2S_payCredit", 200_000, 0, 0)
    host._wat_mirror(a, rec2)
    host_asks(host, a, "wat", f'<g2s:initiateRequest g2s:requestId="{rid2}"/>')
    egm.refuse("wat", sid1, "G2S_WTX005")
    s1 = host.wat_store.find(egm.egm_id, request_id=rid1)["state"]
    s2 = host.wat_store.find(egm.egm_id, request_id=rid2)["state"]
    check("with two pushes open, the refused one is parked and not the "
          "newest", (s1, s2) == ("error:G2S_WTX005", "requested"), (s1, s2))

    # --- handpay: a key-off in flight, and the log read refused
    txn, _ = egm.handpay_request(request=10_000_000)
    host.reset_newest_handpay(a)
    drain(host, a)
    key_sid = next(s for s, r in a.sent_requests.items()
                   if r["command"] == "setRemoteKeyOff")
    log_sid = host_asks(host, a, "handpay",
                        '<g2s:getHandpayLog g2s:lastSequence="0" '
                        'g2s:totalEntries="20"/>')
    egm.refuse("handpay", log_sid, "G2S_APX003")
    check("a refused getHandpayLog is not reported as a refused key-off",
          a.handpays_pending[txn]["state"] == "keyOffSent",
          a.handpays_pending[txn]["state"])
    egm.refuse("handpay", key_sid, "G2S_JPX002")
    check("the key-off's own refusal marks that lockup",
          a.handpays_pending[txn]["state"] == "error:G2S_JPX002",
          a.handpays_pending[txn]["state"])

    # --- optionConfig: a denomination ceremony waiting on the machine
    a.denom_ceremony = {"stage": "entering", "dev": "1"}
    enter_sid = host_asks(host, a, "optionConfig",
                          '<g2s:enterOptionConfigMode g2s:enable="true"/>')
    list_sid = host_asks(host, a, "optionConfig",
                         '<g2s:getOptionList g2s:deviceClass="G2S_handpay"/>')
    egm.refuse("optionConfig", list_sid, "G2S_APX003")
    check("a refused getOptionList does not fail a denomination ceremony "
          "that is waiting for the machine to open config mode (it did, and "
          "the machine then sat locked with nobody to close the mode)",
          a.denom_ceremony["stage"] == "entering", a.denom_ceremony)
    egm.refuse("optionConfig", enter_sid, "G2S_APX011")
    check("the refusal of the enterOptionConfigMode itself does",
          a.denom_ceremony["stage"] == "failed"
          and a.denom_ceremony.get("errorCode") == "G2S_APX011",
          a.denom_ceremony)

    # --- optionConfig: an option change (the ticket header) in flight
    a.optionconfig_stage = "setting"
    a.option_change = {"result": "sent", "optionId": "G2S_voucherOptions"}
    set_sid = host_asks(host, a, "optionConfig",
                        '<g2s:setOptionChange g2s:configurationId="5"/>')
    egm.refuse("optionConfig", list_sid, "G2S_APX003")
    check("nor an option change in flight",
          a.optionconfig_stage == "setting"
          and a.option_change["result"] == "sent", a.optionconfig_stage)
    egm.refuse("optionConfig", set_sid, "G2S_OCX004")
    check("its own refusal fails it",
          a.optionconfig_stage == "failed"
          and a.option_change["result"] == "error:G2S_OCX004",
          (a.optionconfig_stage, a.option_change))

    # --- commConfig: the ownership cycle
    a.commconfig_stage = "changing"
    chg_sid = host_asks(host, a, "commConfig",
                        '<g2s:setCommChange g2s:configurationId="9"/>')
    other_sid = host_asks(host, a, "commConfig", "<g2s:getCommHostList/>")
    egm.refuse("commConfig", other_sid, "G2S_APX003")
    check("a refused getCommHostList does not fail the ownership cycle",
          a.commconfig_stage == "changing", a.commconfig_stage)
    egm.refuse("commConfig", chg_sid, "G2S_CCX018")
    check("the refusal of setCommChange does",
          a.commconfig_stage == "failed", a.commconfig_stage)

    # --- bonus: a refused award
    host._bonus_award_update(a, "55", state="sent")
    award_sid = host_asks(host, a, "bonus",
                          '<g2s:setBonusAward g2s:bonusId="55" '
                          'g2s:bonusAwardAmt="1000000"/>')
    egm.refuse("bonus", award_sid, "G2S_BNX008")
    award = next(r for r in a.bonus_awards if str(r.get("bonusId")) == "55")
    check("a refused setBonusAward marks that award (it stayed 'sent' for "
          "good, inviting a second send)",
          award.get("state") == "error:G2S_BNX008", award)


def item_14_a_list_in_several_parts():
    print("— audit item 14: a descriptorList sent in several commands "
          "(sessionMore, §1.18.3.2)")
    host = make_host()
    egm = joined(host, "BTB_parts")
    a = egm.assoc

    def part(devices, more, sid="4321"):
        rows = "".join(
            f'<g2s:descriptor g2s:deviceClass="{c}" g2s:deviceId="{d}" '
            f'g2s:deviceActive="true"/>' for c, d in devices)
        egm.cid += 1
        ts = gh.now_iso()
        return egm._deliver(
            f'<g2s:g2sMessage xmlns:g2s="{gh.SCHEMA_NS}">'
            f'<g2s:g2sBody g2s:egmId="{egm.egm_id}" g2s:hostId="1" '
            f'g2s:dateTimeSent="{ts}"><g2s:communications g2s:deviceId="1" '
            f'g2s:commandId="{egm.cid}" g2s:sessionType="G2S_response" '
            f'g2s:sessionId="{sid}" g2s:timeToLive="0" g2s:dateTime="{ts}"'
            f'{" g2s:sessionMore=" + chr(34) + "true" + chr(34) if more else ""}>'
            f'<g2s:descriptorList>{rows}</g2s:descriptorList>'
            f'</g2s:communications></g2s:g2sBody></g2s:g2sMessage>')

    rx = a.descriptors_rx
    part([("G2S_cabinet", "1"), ("G2S_gamePlay", "1")], more=True)
    part([("G2S_gamePlay", "2"), ("G2S_wat", "1")], more=True)
    check("nothing is published until the last part is in",
          a.descriptors_rx == rx, a.descriptors_rx - rx)
    part([("G2S_wat", "2"), ("G2S_voucher", "1")], more=False)
    have = {(d["deviceClass"], d["deviceId"]) for d in a.descriptors}
    check("all six devices are in the inventory (it kept only the last "
          "part's two)", len(have) == 6 and ("G2S_cabinet", "1") in have
          and ("G2S_voucher", "1") in have, have)
    check("and the list arrived once", a.descriptors_rx == rx + 1)
    part([("G2S_cabinet", "1")], more=False, sid="4400")
    check("a later one-part list replaces it whole",
          len(a.descriptors) == 1, a.descriptors)


def item_41_a_repeated_answer_says_so():
    print("— audit item 41: an answer given before is marked sessionRetry "
          "(§1.18.3.2)")
    host = make_host()
    egm = joined(host, "BTB_retry")
    egm.get_validation_data(20, expired=True)
    vid, _txn, _ack = egm.issue_voucher(1_000_000)
    first = egm.repeats().get("issueVoucherAck")
    egm.resend()
    again = egm.repeats().get("issueVoucherAck")
    check("issueVoucherAck: not marked the first time, marked the second",
          (first, again) == (False, True), (first, again))
    rtxn, _auth = egm.redeem_voucher(vid)
    first = egm.repeats().get("authorizeVoucher")
    egm.resend()
    again = egm.repeats().get("authorizeVoucher")
    check("authorizeVoucher likewise", (first, again) == (False, True),
          (first, again))
    egm.commit_voucher(rtxn, vid, 1_000_000)
    first = egm.repeats().get("commitVoucherAck")
    egm.resend()
    check("commitVoucherAck likewise",
          (first, egm.repeats().get("commitVoucherAck")) == (False, True))
    txn, _ = egm.handpay_request(request=5_000_000)
    first = egm.repeats().get("handpayAck")
    egm.resend()
    check("handpayAck likewise",
          (first, egm.repeats().get("handpayAck")) == (False, True))
    egm.keyed_off(txn, 5_000_000, how="G2S_localCredit")
    first = egm.repeats().get("keyedOffAck")
    egm.resend()
    check("keyedOffAck likewise",
          (first, egm.repeats().get("keyedOffAck")) == (False, True))
    _cash_out(host, egm, "900", 1_000_000, "house")
    egm.commit_transfer("900", 1_000_000)
    first = egm.repeats().get("commitTransferAck")
    egm.resend()
    check("commitTransferAck likewise",
          (first, egm.repeats().get("commitTransferAck")) == (False, True))


def item_40_the_host_number():
    print("— audit item 40: a machine that has the hub under another host "
          "number (§1.18.1)")
    host = make_host()
    egm = Egm(host, "BTB_host2")
    egm.host_no = "2"
    egm.comms_online()
    check("it is let in (a refusal would cut off a machine that works "
          "today)", egm.assoc.comms_online_seen == 1)
    check("and the status says which number it uses",
          egm.assoc.snapshot().get("hostIdSeen") == "2",
          egm.assoc.snapshot().get("hostIdSeen"))
    ours = joined(host, "BTB_host1")
    check("a machine using the hub's own number shows nothing",
          ours.assoc.snapshot().get("hostIdSeen") is None)


def took(host, egm, command):
    """How many times `command` reached the pretend EGM and was taken."""
    return sum(1 for _l, x in host.sent for _c, name, _a, _e in commands(x)
               if name == command and egm.egm_id in x)


def item_12_not_established():
    print("— audit item 12: nothing but the handshake is processed until "
          "communications are established (§1.18.2), or once the machine "
          "cannot be reached (§1.18.1)")
    host = make_host()
    egm = Egm(host, "BTB_gate")
    host.egm_mode[egm.egm_id] = "refused"       # our commsOnLineAck fails
    egm.comms_online()
    check("a commsOnLineAck that never went out leaves the link not "
          "established", egm.assoc.established is False)
    n = len(host.voucher_store.state["egms"])
    egm.get_validation_data(20, expired=True)
    check("a request from the machine is then answered G2S_MSX003 and not "
          "processed (it was acknowledged and processed, with the answer "
          "posted to an address that does not answer)",
          "G2S_MSX003" in egm.reply
          and len(host.voucher_store.state["egms"]) == n
          and took(host, egm, "validationData") == 0, egm.reply)
    host.egm_mode[egm.egm_id] = "ok"
    egm.comms_online()                           # which makes it start again
    attrs, ids = egm.get_validation_data(20, expired=True)
    check("after a commsOnLine that IS answered, the same request is "
          "served", egm.assoc.established is True and len(ids) == 20
          and "MSX003" not in egm.reply, (egm.assoc.established, len(ids)))

    host.egm_mode[egm.egm_id] = "refused"
    egm.comms_online()
    check("a new commsOnLine starts over: not established until ITS ack "
          "is answered", egm.assoc.established is False)
    host.egm_mode[egm.egm_id] = "ok"

    print("— the machine's commsDisabled is proof that our ack arrived")
    egm = Egm(host, "BTB_gate_disabled")
    host.egm_mode[egm.egm_id] = "silent"         # sent, no answer seen
    egm.comms_online()
    check("(the ack went out and drew no answer: not established yet)",
          egm.assoc.established is False)
    host.egm_mode[egm.egm_id] = "ok"
    egm.send("communications", "<g2s:commsDisabled/>")
    check("commsDisabled is taken, not refused, and establishes the link",
          "MSX003" not in egm.reply and egm.assoc.established is True,
          egm.reply)

    print("— a link found down, and a machine that says it is closing")
    egm = joined(host, "BTB_gate_down")
    egm.get_validation_data(20, expired=True)
    link_down(host, egm)
    egm.send("communications", "<g2s:keepAlive/>")
    check("once the hub cannot reach the machine, its keepAlive is answered "
          "G2S_MSX003, which tells it to start again with its current "
          "location", "G2S_MSX003" in egm.reply, egm.reply)
    egm.comms_online()
    egm.send("communications", "<g2s:keepAlive/>")
    check("and after its commsOnLine it is taken again",
          "MSX003" not in egm.reply and egm.assoc.established is True)
    egm.send("communications", '<g2s:commsClosing g2s:reason="powerDown"/>')
    egm.send("communications", "<g2s:keepAlive/>")
    check("after commsClosing the next thing it must send is commsOnLine",
          "G2S_MSX003" in egm.reply, egm.reply)

    print("— a probe that gets through restores the link without a new "
          "handshake")
    egm = joined(host, "BTB_gate_probe")
    link_down(host, egm)
    check("(the link is down: not established)",
          egm.assoc.comms_state == "offline" and not egm.assoc.established)
    host.post_to_egm(egm.assoc, "<probe/>", "keepAlive(cid=1,sid=1)")
    egm.send("communications", "<g2s:keepAlive/>")
    check("the machine's messages are taken again",
          egm.assoc.comms_state == "onLine" and egm.assoc.established
          and "MSX003" not in egm.reply, (egm.assoc.comms_state, egm.reply))

    print("— a hub that has just started still asks for commsOnLine first")
    fresh = Egm(make_host(), "BTB_gate_fresh")
    fresh.send("communications", "<g2s:commsDisabled/>")
    check("commsDisabled before any commsOnLine is still G2S_MSX003",
          "G2S_MSX003" in fresh.reply, fresh.reply)


def item_11_a_message_that_did_not_go_out():
    print("— audit item 11: a message that did not reach the machine is "
          "sent again (§1.18.2)")
    gh.RESEND_AFTER_SEC = 0.01
    gh.QUEUE_FULL_RESEND_SEC = 0.01
    host = make_host()
    egm = joined(host, "BTB_resend")
    a = egm.assoc
    egm.get_validation_data(20, expired=True)
    vid, _txn, _ack = egm.issue_voucher(1_000_000)

    host.attempts[egm.egm_id] = 0
    host.egm_mode[egm.egm_id] = ["refused", "ok"]
    _t, auth = egm.redeem_voucher(vid)
    check("the listener refused the first POST: the authorizeVoucher is "
          "sent again and arrives (it was dropped, and the ticket sat in "
          "escrow until the machine gave up)",
          auth is not None and host.attempts[egm.egm_id] == 2
          and took(host, egm, "authorizeVoucher") == 1,
          (auth, host.attempts[egm.egm_id]))
    stamps = [x for _l, x in host.sent if "authorizeVoucher" in x]
    check("with the same commandId, so the machine can tell a repeat",
          len(stamps) == 1 and a.post_verdict == "acked", a.post_verdict)

    host.attempts[egm.egm_id] = 0
    host.egm_mode[egm.egm_id] = "refused"
    egm.send("communications", "<g2s:keepAlive/>")
    check("it is tried %d times in all, then left" % gh.RESEND_TRIES,
          host.attempts[egm.egm_id] == gh.RESEND_TRIES,
          host.attempts[egm.egm_id])

    host.attempts[egm.egm_id] = 0
    host.egm_mode[egm.egm_id] = "silent"
    egm.send("communications", "<g2s:keepAlive/>")
    check("a POST that went out and drew no answer is NOT sent again: the "
          "machine may have acted on it", host.attempts[egm.egm_id] == 1
          and a.post_verdict == "no_answer",
          (host.attempts[egm.egm_id], a.post_verdict))

    host.attempts[egm.egm_id] = 0
    host.egm_mode[egm.egm_id] = "refused"
    host.post_to_egm(a, "<probe/>", "keepAlive(cid=5,sid=5)")
    host._enqueue(a, lambda x: ("<probe/>", "keepAlive(cid=6,sid=6)"))
    drain(host, a)
    check("the hub's own keepAlive is never sent again (the next one is "
          "ten seconds away)", host.attempts[egm.egm_id] == 2,
          host.attempts[egm.egm_id])

    host.attempts[egm.egm_id] = 0
    limit, gh.RESEND_ONLY_IF_FAILED_WITHIN = \
        gh.RESEND_ONLY_IF_FAILED_WITHIN, -1.0
    egm.send("communications", "<g2s:keepAlive/>")
    gh.RESEND_ONLY_IF_FAILED_WITHIN = limit
    check("nor is one whose connection attempt took long to fail (the "
          "machine is off, not restarting)", host.attempts[egm.egm_id] == 1,
          host.attempts[egm.egm_id])

    print("— a new handshake, or a link found down, ends the re-sending")
    host.egm_mode[egm.egm_id] = "refused"
    gh.RESEND_AFTER_SEC = 0.4
    host.attempts[egm.egm_id] = 0
    egm.cid += 1
    host.handle_g2s_message(
        egm._message("communications", "1", "<g2s:keepAlive/>", egm.cid,
                     egm.cid, "G2S_request"), egm.egm_id)
    deadline = time.time() + 3
    while host.attempts[egm.egm_id] < 1 and time.time() < deadline:
        time.sleep(0.01)
    link_down(host, egm)
    drain(host, a)
    gh.RESEND_AFTER_SEC = 0.01
    check("the message waiting to go again is dropped when the link is "
          "marked down", host.attempts[egm.egm_id] == 1,
          host.attempts[egm.egm_id])


def item_13_the_machines_queue_is_full():
    print("— audit item 13: G2S_MSX006, the machine's inbound queue is full "
          "(§1.18.11)")
    gh.RESEND_AFTER_SEC = 0.01
    gh.QUEUE_FULL_RESEND_SEC = 0.01
    host = make_host()
    egm = joined(host, "BTB_full")
    a = egm.assoc
    egm.get_validation_data(20, expired=True)
    vid, _txn, _ack = egm.issue_voucher(1_000_000)
    host.attempts[egm.egm_id] = 0
    host.egm_mode[egm.egm_id] = ["G2S_MSX006", "G2S_MSX006", "ok"]
    _t, auth = egm.redeem_voucher(vid)
    check("a message answered MSX006 twice is sent a third time and taken "
          "(its commands were dropped, with only a line in the log)",
          auth is not None and host.attempts[egm.egm_id] == 3
          and took(host, egm, "authorizeVoucher") == 1,
          (auth, host.attempts[egm.egm_id]))

    host.attempts[egm.egm_id] = 0
    host.egm_mode[egm.egm_id] = "G2S_MSX006"
    egm.send("communications", "<g2s:keepAlive/>")
    check("a queue that stays full is tried %d times" % gh.QUEUE_FULL_TRIES,
          host.attempts[egm.egm_id] == gh.QUEUE_FULL_TRIES
          and a.post_verdict == "queue_full",
          (host.attempts[egm.egm_id], a.post_verdict))

    host.attempts[egm.egm_id] = 0
    host.egm_mode[egm.egm_id] = "G2S_MSX004"
    egm.send("communications", "<g2s:keepAlive/>")
    check("any other code in the ack is the machine refusing the message, "
          "and it is not sent again", host.attempts[egm.egm_id] == 1
          and a.post_verdict == "error:G2S_MSX004",
          (host.attempts[egm.egm_id], a.post_verdict))

    print("— reading the code out of the machine's reply")
    code = gh.G2SHost.ack_error_code
    check("a clean ack", code(egm_reply("X")) == "G2S_none")
    check("G2S_MSX006", code(egm_reply("X", "G2S_MSX006")) == "G2S_MSX006")
    check("a vendor's own code, which the old pattern could not see",
          code(egm_reply("X", "IGT_MSX900")) == "IGT_MSX900")
    check("a reply with no ack at all", code("<html>busy</html>") is None)
    check("a reply that is not XML still falls back to the old pattern",
          code('garbage g2sAck errorCode="G2S_MSX004" <<<') == "G2S_MSX004")


def main():
    logging.basicConfig(level=logging.CRITICAL + 1)
    logging.getLogger("g2s_host").setLevel(logging.CRITICAL + 1)
    logging.getLogger("g2s_wire").setLevel(logging.CRITICAL + 1)
    item_3_key_off_amount()
    item_1_vouchers_after_a_memory_reset()
    item_1_handpays_after_a_memory_reset()
    item_2_a_list_the_host_did_not_issue()
    item_9_a_commit_retried_after_a_long_absence()
    item_4_entity_references()
    item_10_an_error_belongs_to_its_request()
    item_14_a_list_in_several_parts()
    item_41_a_repeated_answer_says_so()
    item_40_the_host_number()
    item_12_not_established()
    item_11_a_message_that_did_not_go_out()
    item_13_the_machines_queue_is_full()
    print("=" * 50)
    print(f"RESULT: {_p} passed, {_f} failed")
    return 1 if _f else 0


if __name__ == "__main__":
    sys.exit(main())
