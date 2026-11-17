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
  * it enters a configuration mode only once it is idle, which can be
    after it answered the request, and stays out of play until told to
    leave (§8.7, §9.7); it refuses a set that changes a parameter it marks
    canModRemote=false (G2S_OCX015, §9.15); its unsolicited commHostList
    carries only the hosts that changed (§8.13);
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


def item_42_a_request_past_its_time_to_live():
    print("— audit item 42: a request the machine held back past its own "
          "time-to-live (§1.18.3.2) is counted, and still processed")
    host = make_host()
    egm = joined(host, "BTB_ttl")
    a = egm.assoc

    def keep_alive(made, sent, ttl):
        egm.cid += 1
        return egm._deliver(
            f'<g2s:g2sMessage xmlns:g2s="{gh.SCHEMA_NS}">'
            f'<g2s:g2sBody g2s:egmId="{egm.egm_id}" g2s:hostId="1" '
            f'g2s:dateTimeSent="{sent}"><g2s:communications '
            f'g2s:deviceId="1" g2s:commandId="{egm.cid}" '
            f'g2s:sessionType="G2S_request" g2s:sessionId="{egm.cid}" '
            f'g2s:timeToLive="{ttl}" g2s:dateTime="{made}">'
            f'<g2s:keepAlive/></g2s:communications></g2s:g2sBody>'
            f'</g2s:g2sMessage>')

    # the machine's clock is years out; only its own two stamps are compared
    got = keep_alive("2019-03-01T10:00:00.000Z", "2019-03-01T10:00:01.000Z",
                     30000)
    check("a request sent a second after it was made is not stale, whatever "
          "the machine's clock says",
          a.stale_requests == 0 and egm.answer(got, "keepAliveAck")
          is not None, a.stale_requests)
    got = keep_alive("2019-03-01T10:00:00.000Z", "2019-03-01T10:05:00.000Z",
                     30000)
    check("one sent five minutes after it was made, with 30 s to live, is "
          "counted", a.stale_requests == 1
          and a.snapshot().get("staleRequests") == 1, a.stale_requests)
    check("and still answered: it may be a transaction the machine must not "
          "lose", egm.answer(got, "keepAliveAck") is not None
          and "errorCode" not in egm.reply, egm.reply)
    keep_alive("2019-03-01T10:00:00.000Z", "2019-03-01T10:05:00.000Z", 0)
    check("a time-to-live of 0 never runs out", a.stale_requests == 1,
          a.stale_requests)


# ---------------------------------------------------------------------------
# batch 3: configuration modes, option permissions, the ownership claim
# ---------------------------------------------------------------------------

def sent_since(host, mark):
    return [c for _l, x in host.sent[mark:] for c in commands(x)]


def names(cmds):
    return [name for _c, name, _a, _e in cmds]


def online(egm):
    egm.assoc.comms_state = "onLine"        # the handshake had completed
    return egm


def answer_to(egm, cls, command, body, device="1", where=None, sid=None,
              more=False):
    """The EGM's RESPONSE to the newest request of that name from the host
    (same sessionId, as §1.18.3.2 requires). `where` narrows it to the
    request that carried those attributes, `sid` names the request
    outright, and `more` marks the answer as one part with more to come."""
    if sid is None:
        sid = [s for s, r in egm.assoc.sent_requests.items()
               if r["command"] == command
               and all(r["attrs"].get(k) == v
                       for k, v in (where or {}).items())][-1]
    egm.cid += 1
    ts = gh.now_iso()
    return egm._deliver(
        f'<g2s:g2sMessage xmlns:g2s="{gh.SCHEMA_NS}">'
        f'<g2s:g2sBody g2s:egmId="{egm.egm_id}" g2s:hostId="1" '
        f'g2s:dateTimeSent="{ts}"><g2s:{cls} g2s:deviceId="{device}" '
        f'g2s:commandId="{egm.cid}" g2s:sessionType="G2S_response" '
        f'g2s:sessionId="{sid}" g2s:timeToLive="0" g2s:dateTime="{ts}"'
        + (' g2s:sessionMore="true"' if more else "") + ">"
        f'{body}</g2s:{cls}></g2s:g2sBody></g2s:g2sMessage>'), sid


def event(egm, code, cls="G2S_optionConfig", dev="1"):
    egm.event_id = getattr(egm, "event_id", 5000) + 1
    return egm.send(
        "eventHandler",
        f'<g2s:eventReport g2s:deviceClass="{cls}" g2s:deviceId="{dev}" '
        f'g2s:eventCode="{code}" g2s:eventId="{egm.event_id}" '
        f'g2s:eventDateTime="{egm.stamp()}" g2s:transactionId="0"/>')


def mode_status(on):
    return f'<g2s:optionConfigModeStatus g2s:enabled="{str(on).lower()}"/>'


def exits(cmds, which="enterOptionConfigMode"):
    return [a for _c, name, a, _e in cmds
            if name == which and a.get("enable") == "false"]


def items_5_and_8_a_machine_left_in_a_config_mode():
    print("— audit item 5: a denomination change whose write is never "
          "answered (§9.7: in the mode \"the EGM MUST NOT allow game play\")")
    host = make_host()
    egm = online(joined(host, "BTB_ceremony"))
    a = egm.assoc
    host.start_denom_ceremony(a, "1", [1000, 5000])
    drain(host, a)
    mark = len(host.sent)
    answer_to(egm, "optionConfig", "enterOptionConfigMode", mode_status(True),
              device="1")
    got = names(sent_since(host, mark))
    check("the mode opens and the write goes out",
          a.denom_ceremony["stage"] == "writing"
          and "setActiveDenoms" in got, got)
    # the machine acknowledges the write and says no more
    mark = len(host.sent)
    host.sweep_config_modes(now=time.time() + gh.CEREMONY_WRITE_DEADLINE_SEC
                            - 5)
    drain(host, a)
    check("inside the deadline nothing is done",
          a.denom_ceremony["stage"] == "writing"
          and not exits(sent_since(host, mark)))
    host.sweep_config_modes(now=time.time() + gh.CEREMONY_WRITE_DEADLINE_SEC
                            + 1)
    drain(host, a)
    check("past it, the exit is sent (it never was: the machine stayed "
          "locked until someone posted the exit by hand)",
          a.denom_ceremony["stage"] == "exiting"
          and len(exits(sent_since(host, mark))) == 1,
          (a.denom_ceremony, names(sent_since(host, mark))))

    print("— an exit the machine does not take is sent again")
    exit_sid = [s for s, r in a.sent_requests.items()
                if r["command"] == "enterOptionConfigMode"
                and r["attrs"].get("enable") == "false"][-1]
    egm.refuse("optionConfig", exit_sid, "G2S_APX011", device="1")
    check("an error answering the exit does not end the matter",
          a.denom_ceremony["stage"] == "exiting", a.denom_ceremony)
    mark = len(host.sent)
    host.sweep_config_modes(now=time.time() + 5)
    drain(host, a)
    check("not again within %d s" % gh.MODE_EXIT_RESEND_SEC,
          not exits(sent_since(host, mark)))
    a.mode_exit_sent["optionConfig"] -= gh.MODE_EXIT_RESEND_SEC + 1
    host.sweep_config_modes()
    drain(host, a)
    again = sent_since(host, mark)
    check("after that it goes again, with the mode asked for alongside",
          len(exits(again)) == 1
          and "getOptionConfigModeStatus" in names(again), names(again))
    answer_to(egm, "optionConfig", "getOptionConfigModeStatus",
              mode_status(False))
    check("and when the machine says the mode is off, the change is closed",
          a.denom_ceremony["stage"] == "done", a.denom_ceremony)

    print("— a machine that never leaves: the hub says so and keeps it "
          "visible")
    host.start_denom_ceremony(a, "1", [1000])
    drain(host, a)
    answer_to(egm, "optionConfig", "enterOptionConfigMode", mode_status(True))
    host.sweep_config_modes(now=time.time() + gh.CEREMONY_WRITE_DEADLINE_SEC
                            + 1)
    for _ in range(gh.CEREMONY_EXIT_TRIES + 2):
        a.mode_exit_sent["optionConfig"] = 0
        host.sweep_config_modes()
    drain(host, a)
    check("after %d exits the change is marked stuck, not forgotten"
          % gh.CEREMONY_EXIT_TRIES, a.denom_ceremony["stage"] == "stuck",
          a.denom_ceremony)

    print("— audit item 8: a machine that enters the mode LATER than it "
          "answered (§9.7 lets it wait for zero credits and an idle "
          "cabinet)")
    host = make_host()
    egm = online(joined(host, "BTB_late"))
    a = egm.assoc
    host.start_denom_ceremony(a, "1", [1000])
    drain(host, a)
    _got, enter_sid = answer_to(egm, "optionConfig", "enterOptionConfigMode",
                                mode_status(False))
    check("\"not yet\" leaves the change waiting",
          a.denom_ceremony["stage"] == "entering")
    mark = len(host.sent)
    event(egm, "G2S_OCE101")
    got = names(sent_since(host, mark))
    check("the event that says it HAS entered opens the change (nothing "
          "looked at it: the machine sat in the mode, the hub still "
          "waiting)", a.denom_ceremony["stage"] == "writing"
          and "setActiveDenoms" in got, (a.denom_ceremony["stage"], got))

    print("— a mode that nothing on the hub is driving")
    host = make_host()
    egm = online(joined(host, "BTB_orphan"))
    a = egm.assoc
    mark = len(host.sent)
    event(egm, "G2S_OCE101")
    check("the machine enters option-config mode with no change in flight "
          "(a hub restart forgot it, or the request had been given up on): "
          "the exit is sent", len(exits(sent_since(host, mark))) == 1,
          names(sent_since(host, mark)))
    mark = len(host.sent)
    host.enqueue_get_option_config_mode_status(a)
    drain(host, a)
    answer_to(egm, "optionConfig", "getOptionConfigModeStatus",
              mode_status(True))
    check("a second report straight after does not send a second exit",
          not exits(sent_since(host, mark)))
    event(egm, "G2S_OCE102")

    print("— a mode entered by hand through the API is left to the person "
          "who entered it")
    host.enqueue_enter_option_config_mode(a, enable=True, manual=True)
    drain(host, a)
    a.mode_exit_sent.clear()
    mark = len(host.sent)
    answer_to(egm, "optionConfig", "enterOptionConfigMode", mode_status(True))
    check("no exit is sent for it",
          not exits(sent_since(host, mark)) and "optionConfig"
          in a.mode_manual)
    host.enqueue_enter_option_config_mode(a, enable=False, manual=True)
    drain(host, a)
    check("their exit clears the mark", "optionConfig" not in a.mode_manual)

    print("— the commConfig mode likewise (§8.7)")
    mark = len(host.sent)
    event(egm, "G2S_CCE101", cls="G2S_commConfig")
    check("on with no ownership change in flight: the exit is sent",
          len(exits(sent_since(host, mark), "enterCommConfigMode")) == 1,
          names(sent_since(host, mark)))
    a.mode_exit_sent.clear()
    a.commconfig_stage = "changing"
    mark = len(host.sent)
    event(egm, "G2S_CCE101", cls="G2S_commConfig")
    check("on while a change is being made: left alone",
          not exits(sent_since(host, mark), "enterCommConfigMode"))


HANDPAY_PARAMS = (("G2S_enabledRemoteCredit", "true", "false"),
                  ("G2S_disabledRemoteCredit", "false", "false"),
                  ("G2S_enabledRemoteHandpay", "true", "false"),
                  ("G2S_disabledRemoteHandpay", "false", "false"))


def handpay_options(params=HANDPAY_PARAMS, definitions=True, bare=False):
    """An optionList for G2S_handpay/1: (paramId, canModRemote, value)."""
    defs = ""
    if definitions:
        defs = ("<g2s:optionParameters><g2s:complexParameter "
                'g2s:paramId="G2S_handpayOptions">' + "".join(
                    f'<g2s:booleanParameter g2s:paramId="{p}"'
                    + ("" if bare else f' g2s:canModRemote="{mod}"') + "/>"
                    for p, mod, _v in params)
                + "</g2s:complexParameter></g2s:optionParameters>")
    vals = ('<g2s:optionCurrentValues><g2s:complexValue '
            'g2s:paramId="G2S_handpayOptions">' + "".join(
                f'<g2s:booleanValue g2s:paramId="{p}">{v}</g2s:booleanValue>'
                for p, _m, v in params)
            + "</g2s:complexValue></g2s:optionCurrentValues>")
    return ('<g2s:optionList><g2s:deviceOptions g2s:deviceClass="G2S_handpay"'
            ' g2s:deviceId="1"><g2s:optionGroup '
            'g2s:optionGroupId="G2S_handpayOptions"><g2s:optionItem '
            f'g2s:optionId="G2S_handpayOptions">{defs}{vals}</g2s:optionItem>'
            "</g2s:optionGroup></g2s:deviceOptions></g2s:optionList>")


def values_sent(cmds):
    """{paramId: value} of the newest setOptionChange the host sent."""
    for _c, name, _a, el in reversed(cmds):
        if name == "setOptionChange":
            return {v.get(f"{{{gh.SCHEMA_NS}}}paramId"): (v.text or "")
                    for v in el.iter()
                    if gh.localname(v.tag).endswith("Value")
                    and gh.localname(v.tag) != "complexValue"}
    return None


def item_22_what_the_machine_lets_a_host_change():
    print("— audit item 22: canModRemote (§9.15: \"If set to false, the "
          "host MUST NOT change the value of the parameter\")")
    host = make_host()
    egm = online(joined(host, "BTB_canmod"))
    a = egm.assoc
    mark = len(host.sent)
    host._maybe_arm_remote_handpay_on_join(a)
    drain(host, a)
    asked = [x for _c, name, x, _e in sent_since(host, mark)
             if name == "getOptionList"]
    check("the join's read of the handpay options asks for the "
          "definitions", asked and asked[-1].get("optionDetail") == "true",
          asked)
    mark = len(host.sent)
    answer_to(egm, "optionConfig", "getOptionList", handpay_options(),
              device="1")
    sent = values_sent(sent_since(host, mark))
    check("the parameters the machine allows are switched on",
          sent is not None and sent.get("G2S_enabledRemoteCredit") == "true"
          and sent.get("G2S_enabledRemoteHandpay") == "true", sent)
    check("the ones it marks canModRemote=false go back as the machine has "
          "them (the hub asked to change them, and a WMS Bluebird 2 refused "
          "the whole set at every join: G2S_OCX015)",
          sent is not None and sent.get("G2S_disabledRemoteCredit") == "false"
          and sent.get("G2S_disabledRemoteHandpay") == "false", sent)

    print("— one parameter, asked for by name")
    r = host.start_option_change_cycle(
        a, device_class="G2S_handpay", device_id="1",
        option_id="G2S_handpayOptions", param_id="G2S_disabledRemoteCredit",
        value="true")
    check("a change to a parameter the machine keeps to itself is refused "
          "here, with the reason", r.get("ok") is False
          and r.get("notModifiable") == ["G2S_disabledRemoteCredit"]
          and "at the machine" in r.get("error", ""), r)

    print("— the flag that matters, kept by the machine and off")
    host = make_host()
    egm = online(joined(host, "BTB_canmod_off"))
    a = egm.assoc
    host._maybe_arm_remote_handpay_on_join(a)
    drain(host, a)
    mark = len(host.sent)
    answer_to(egm, "optionConfig", "getOptionList", handpay_options(
        (("G2S_enabledRemoteCredit", "false", "false"),) + HANDPAY_PARAMS[1:]))
    r = host.enable_remote_handpay(a)
    check("nothing is sent, and the answer says where to turn it on",
          values_sent(sent_since(host, mark)) is None
          and r.get("ok") is False and "machine's own menu" in r["error"], r)

    print("— a machine already set as far as a host may set it (the "
          "Bluebird at every join)")
    host = make_host()
    egm = online(joined(host, "BTB_canmod_done"))
    a = egm.assoc
    host._maybe_arm_remote_handpay_on_join(a)
    drain(host, a)
    mark = len(host.sent)
    answer_to(egm, "optionConfig", "getOptionList", handpay_options((
        ("G2S_enabledRemoteCredit", "true", "true"),
        ("G2S_disabledRemoteCredit", "false", "false"),
        ("G2S_enabledRemoteHandpay", "true", "true"),
        ("G2S_disabledRemoteHandpay", "false", "false"))))
    r = host.enable_remote_handpay(a)
    check("nothing is sent and it is not reported as a failure: the two "
          "parameters left are the machine's to set",
          values_sent(sent_since(host, mark)) is None and r.get("ok") is True
          and r.get("alreadyEnabled") is True
          and r.get("notModifiable") == ["G2S_disabledRemoteCredit",
                                         "G2S_disabledRemoteHandpay"], r)

    print("— definitions that say nothing")
    host = make_host()
    egm = online(joined(host, "BTB_canmod_bare"))
    a = egm.assoc
    # the config-sync push a machine sends unasked, with bare definition
    # elements (a WMS Bluebird 2 sends them so)
    egm.send("optionConfig", handpay_options(bare=True), device="1")
    check("bare definitions in a list the machine sent unasked mark "
          "nothing as unchangeable", a.config_can_mod == {}, a.config_can_mod)
    mark = len(host.sent)
    r = host.enable_remote_handpay(a)
    sent = values_sent(sent_since(host, mark)) if not drain(host, a) else None
    sent = values_sent(sent_since(host, mark))
    check("so a change goes ahead as it always did",
          r.get("ok") is True and sent is not None
          and sent.get("G2S_disabledRemoteCredit") == "true", (r, sent))
    host2 = make_host()
    egm2 = online(joined(host2, "BTB_canmod_absent"))
    host2._maybe_arm_remote_handpay_on_join(egm2.assoc)
    drain(host2, egm2.assoc)
    answer_to(egm2, "optionConfig", "getOptionList",
              handpay_options(bare=True))
    check("in the answer to a read that ASKED for them, an absent "
          "canModRemote is false, as the document defines it",
          egm2.assoc.config_can_mod.get(
              "G2S_handpay/1/G2S_enabledRemoteCredit") is False,
          egm2.assoc.config_can_mod)


HEADER = {"propName": "The Den", "line1": None, "line2": None,
          "titleCash": None, "rev": 1}
TEXT_SET = (("G2S_propName", "true", "The Den"),)
TEXT_FACTORY = (("G2S_propName", "true", "YOUR ESTABLISHMENT"),)
ALL_ON = tuple((p, "true", "true") for p, _m, _v in HANDPAY_PARAMS)


def voucher_text(params=TEXT_SET, definitions=True):
    """An optionList for G2S_voucher/1's text fields: (paramId,
    canModRemote, value)."""
    defs = ""
    if definitions:
        defs = ("<g2s:optionParameters><g2s:complexParameter "
                'g2s:paramId="G2S_voucherTextFields">' + "".join(
                    f'<g2s:stringParameter g2s:paramId="{p}" '
                    f'g2s:canModRemote="{mod}"/>' for p, mod, _v in params)
                + "</g2s:complexParameter></g2s:optionParameters>")
    vals = ('<g2s:optionCurrentValues><g2s:complexValue '
            'g2s:paramId="G2S_voucherTextFields">' + "".join(
                f'<g2s:stringValue g2s:paramId="{p}">{v}</g2s:stringValue>'
                for p, _m, v in params)
            + "</g2s:complexValue></g2s:optionCurrentValues>")
    return ('<g2s:optionList><g2s:deviceOptions g2s:deviceClass="G2S_voucher"'
            ' g2s:deviceId="1"><g2s:optionGroup '
            'g2s:optionGroupId="G2S_voucherOptions"><g2s:optionItem '
            f'g2s:optionId="G2S_voucherTextFields">{defs}{vals}'
            "</g2s:optionItem></g2s:optionGroup></g2s:deviceOptions>"
            "</g2s:optionList>")


def changed_devices(cmds):
    """The device class of every setOptionChange in there."""
    return [o.get(f"{{{gh.SCHEMA_NS}}}deviceClass")
            for _c, name, _a, el in cmds if name == "setOptionChange"
            for o in el.iter() if gh.localname(o.tag) == "option"]


def reads(cmds):
    return [(a.get("deviceClass"), a.get("optionDetail"))
            for _c, name, a, _e in cmds if name == "getOptionList"]


def a_join(host, egm):
    """What the join does once the device list is in: both re-asserts are
    armed, the ticket one first, as the hub does it."""
    host._maybe_push_ticket_header_on_join(egm.assoc)
    host._maybe_arm_remote_handpay_on_join(egm.assoc)
    drain(host, egm.assoc)


VOUCHER = {"deviceClass": "G2S_voucher"}
HANDPAY = {"deviceClass": "G2S_handpay"}


def item_22_the_order_the_answers_arrive_in():
    print("— audit item 22, as it went on the floor: the join makes two "
          "reads, and the ticket one is answered first")
    host = make_host()
    host.ticket_header = lambda: dict(HEADER)
    egm = online(joined(host, "BTB_order"))
    a = egm.assoc
    # the handpay options are already known: the machine sent its list
    # unasked, with definitions that say nothing (a WMS Bluebird 2)
    egm.send("optionConfig", handpay_options(bare=True))
    mark = len(host.sent)
    a_join(host, egm)
    check("the ticket options are read first, then the handpay ones, both "
          "with the definitions",
          reads(sent_since(host, mark)) == [("G2S_voucher", "true"),
                                            ("G2S_handpay", "true")],
          reads(sent_since(host, mark)))
    mark = len(host.sent)
    answer_to(egm, "optionConfig", "getOptionList", voucher_text(),
              where=VOUCHER)
    check("the ticket answer lands: nothing is asked of the handpay device "
          "on the strength of it (v0.3.20 sent the change here, before it "
          "had read what the machine lets a host change, and the Bluebird "
          "refused it: G2S_OCX015)",
          changed_devices(sent_since(host, mark)) == []
          and a.handpay_arm_pending is True,
          (changed_devices(sent_since(host, mark)), a.handpay_arm_pending))
    mark = len(host.sent)
    answer_to(egm, "optionConfig", "getOptionList", handpay_options(),
              where=HANDPAY)
    sent = values_sent(sent_since(host, mark))
    check("the handpay answer lands: the change goes now, and leaves what "
          "the machine keeps to itself as the machine has it",
          sent is not None and sent.get("G2S_enabledRemoteCredit") == "true"
          and sent.get("G2S_disabledRemoteCredit") == "false"
          and sent.get("G2S_disabledRemoteHandpay") == "false", sent)

    print("— the other way round: the ticket text waits for its own answer")
    host = make_host()
    host.ticket_header = lambda: dict(HEADER)
    egm = online(joined(host, "BTB_order_ticket"))
    a = egm.assoc
    egm.send("optionConfig", voucher_text(TEXT_FACTORY, definitions=False))
    a_join(host, egm)
    mark = len(host.sent)
    answer_to(egm, "optionConfig", "getOptionList", handpay_options(ALL_ON),
              where=HANDPAY)
    check("the handpay answer lands first: no ticket text is sent on it",
          changed_devices(sent_since(host, mark)) == []
          and a.ticket_push_pending is True,
          changed_devices(sent_since(host, mark)))
    mark = len(host.sent)
    answer_to(egm, "optionConfig", "getOptionList",
              voucher_text(TEXT_FACTORY), where=VOUCHER)
    check("the ticket answer lands: the header is sent",
          changed_devices(sent_since(host, mark)) == ["G2S_voucher"]
          and values_sent(sent_since(host, mark)).get("G2S_propName")
          == "The Den", sent_since(host, mark))

    print("— a memory clear between two joins (what the re-assert is for)")
    host = make_host()
    host.ticket_header = lambda: dict(HEADER)
    egm = online(joined(host, "BTB_order_cleared"))
    a = egm.assoc
    a_join(host, egm)
    answer_to(egm, "optionConfig", "getOptionList", voucher_text(),
              where=VOUCHER)
    mark = len(host.sent)
    answer_to(egm, "optionConfig", "getOptionList", handpay_options(ALL_ON),
              where=HANDPAY)
    check("first join, the machine is set: nothing is sent",
          changed_devices(sent_since(host, mark)) == []
          and a.handpay_arm_pending is False)
    egm.comms_online(reset=True)            # cleared, and back
    online(egm)
    a_join(host, egm)
    mark = len(host.sent)
    answer_to(egm, "optionConfig", "getOptionList", voucher_text(),
              where=VOUCHER)
    check("second join, the ticket answer lands: the re-assert does not "
          "judge the handpay options by what the hub kept from the first "
          "join (it did, found them on, and stood down)",
          a.handpay_arm_pending is True
          and changed_devices(sent_since(host, mark)) == [],
          a.handpay_arm_pending)
    mark = len(host.sent)
    answer_to(egm, "optionConfig", "getOptionList", handpay_options(
        tuple((p, "true", "false") for p, _m, _v in HANDPAY_PARAMS)),
        where=HANDPAY)
    sent = values_sent(sent_since(host, mark))
    check("the handpay answer shows them off again: they are switched "
          "back on", sent is not None
          and sent.get("G2S_enabledRemoteCredit") == "true", sent)
    host = make_host()
    host.ticket_header = lambda: dict(HEADER)
    egm = online(joined(host, "BTB_order_cleared_text"))
    a = egm.assoc
    a_join(host, egm)
    answer_to(egm, "optionConfig", "getOptionList", voucher_text(),
              where=VOUCHER)
    answer_to(egm, "optionConfig", "getOptionList", handpay_options(ALL_ON),
              where=HANDPAY)
    egm.comms_online(reset=True)
    online(egm)
    a_join(host, egm)
    mark = len(host.sent)
    answer_to(egm, "optionConfig", "getOptionList", handpay_options(ALL_ON),
              where=HANDPAY)
    check("the ticket text likewise: with the handpay answer first, it is "
          "not judged by the text kept from the first join",
          a.ticket_push_pending is True
          and changed_devices(sent_since(host, mark)) == [],
          a.ticket_push_pending)
    mark = len(host.sent)
    answer_to(egm, "optionConfig", "getOptionList",
              voucher_text(TEXT_FACTORY), where=VOUCHER)
    check("its own answer shows the factory text: the header is sent again",
          changed_devices(sent_since(host, mark)) == ["G2S_voucher"],
          changed_devices(sent_since(host, mark)))

    print("— a machine that refuses the read with the definitions")
    host = make_host()
    egm = online(joined(host, "BTB_order_refused"))
    a = egm.assoc
    egm.send("optionConfig", handpay_options(bare=True))
    host._maybe_arm_remote_handpay_on_join(a)
    drain(host, a)
    sid = [s for s, r in a.sent_requests.items()
           if r["command"] == "getOptionList"
           and r["attrs"].get("optionDetail") == "true"][-1]
    mark = len(host.sent)
    egm.refuse("optionConfig", sid, "G2S_OCX001")
    check("the values are read alone, as every version before read them, "
          "and nothing is changed yet",
          reads(sent_since(host, mark)) == [("G2S_handpay", "false")]
          and changed_devices(sent_since(host, mark)) == [],
          names(sent_since(host, mark)))
    mark = len(host.sent)
    answer_to(egm, "optionConfig", "getOptionList",
              handpay_options(definitions=False),
              where={"deviceClass": "G2S_handpay", "optionDetail": "false"})
    sent = values_sent(sent_since(host, mark))
    check("that answer lands: the re-assert runs as it always did",
          sent is not None and sent.get("G2S_enabledRemoteCredit") == "true"
          and a.handpay_arm_pending is False, sent)
    host = make_host()
    egm = online(joined(host, "BTB_order_refused_twice"))
    a = egm.assoc
    host._maybe_arm_remote_handpay_on_join(a)
    drain(host, a)
    sid = list(a.sent_requests)[-1]
    egm.refuse("optionConfig", sid, "G2S_OCX001")
    mark = len(host.sent)
    egm.refuse("optionConfig", list(a.sent_requests)[-1], "G2S_OCX001")
    check("the second read refused as well: it is not asked a third time",
          reads(sent_since(host, mark)) == [], reads(sent_since(host, mark)))


def change_status(cfg, txn, status, exc="0"):
    return (f'<g2s:optionChangeStatus g2s:configurationId="{cfg}" '
            f'g2s:transactionId="{txn}" g2s:changeStatus="{status}" '
            f'g2s:changeException="{exc}"/>')


def _a_change_in_flight(name):
    host = make_host()
    egm = online(joined(host, name))
    a = egm.assoc
    egm.send("optionConfig", handpay_options(definitions=False), device="1")
    r = host.start_option_change_cycle(
        a, device_class="G2S_handpay", device_id="1",
        option_id="G2S_handpayOptions", param_id="G2S_enabledRemoteCredit",
        value="true")
    drain(host, a)
    assert r.get("ok"), r
    return host, egm, a, str(r["configId"])


def cancels(cmds):
    return [a for _c, name, a, _e in cmds if name == "cancelOptionChange"]


def item_26_a_set_the_hub_gives_up_on():
    print("— audit item 26: a validated set the hub is no longer driving "
          "is cancelled, not left pending on the machine (§9.16)")
    host, egm, a, cfg = _a_change_in_flight("BTB_cancel_auth")
    answer_to(egm, "optionConfig", "setOptionChange",
              change_status(cfg, "77", "G2S_pending"))
    check("(validated: the hub authorizes it)",
          a.optionconfig_stage == "authorizing")
    mark = len(host.sent)
    auth_sid = [s for s, r in a.sent_requests.items()
                if r["command"] == "authorizeOptionChange"][-1]
    egm.refuse("optionConfig", auth_sid, "G2S_OCX009", device="1")
    c = cancels(sent_since(host, mark))
    check("the authorize is refused: the set the machine still holds is "
          "cancelled (it was left there, the machine waiting on it)",
          a.optionconfig_stage == "failed" and len(c) == 1
          and c[0].get("transactionId") == "77"
          and c[0].get("configurationId") == cfg, (a.optionconfig_stage, c))

    host, egm, a, cfg = _a_change_in_flight("BTB_cancel_exc")
    mark = len(host.sent)
    answer_to(egm, "optionConfig", "setOptionChange",
              change_status(cfg, "78", "G2S_pending", exc="3"))
    c = cancels(sent_since(host, mark))
    check("pending with an exception: cancelled",
          len(c) == 1 and c[0].get("transactionId") == "78", c)

    host, egm, a, cfg = _a_change_in_flight("BTB_cancel_late")
    host.cancel_option_change(a)             # before any transactionId
    a.optionconfig_stage = "cancelled"
    mark = len(host.sent)
    answer_to(egm, "optionConfig", "setOptionChange",
              change_status(cfg, "79", "G2S_pending"))
    c = cancels(sent_since(host, mark))
    check("pending arriving after the change was given up here: cancelled "
          "now that its transactionId is known",
          len(c) == 1 and c[0].get("transactionId") == "79", c)

    host, egm, a, cfg = _a_change_in_flight("BTB_cancel_not")
    answer_to(egm, "optionConfig", "setOptionChange",
              change_status(cfg, "80", "G2S_pending"))
    mark = len(host.sent)
    egm.send("optionConfig", change_status(cfg, "80", "G2S_pending"),
             device="1")                     # the same status, pushed too
    check("a second pending status for a change that is being authorized "
          "does NOT cancel it", not cancels(sent_since(host, mark))
          and a.optionconfig_stage == "authorizing",
          names(sent_since(host, mark)))

    print("— a set the machine has authorized is waiting to apply")
    answer_to(egm, "optionConfig", "authorizeOptionChange",
              change_status(cfg, "80", "G2S_authorized"))
    a.option_change["progressTs"] -= 500
    check("it is not shown as timed out",
          a.snapshot().get("optionChange", {}).get("result") == "authorized",
          a.snapshot().get("optionChange"))
    r = host.start_option_change_cycle(
        a, device_class="G2S_handpay", device_id="1",
        option_id="G2S_handpayOptions", param_id="G2S_enabledRemoteHandpay",
        value="true")
    check("and a new change does not replace it",
          r.get("ok") is False and "waiting to apply" in r.get("error", ""),
          r)


def host_list(items):
    """items: [(hostIndex, hostId, attrs, [(tag, class, id, active)])]"""
    out = ""
    for index, hid, attrs, devs in items:
        extra = "".join(f' g2s:{k}="{v}"' for k, v in attrs.items())
        rows = "".join(
            f'<g2s:{tag} g2s:deviceClass="{c}" g2s:deviceId="{d}" '
            f'g2s:deviceActive="{act}"/>' for tag, c, d, act in devs)
        out += (f'<g2s:commHostItem g2s:hostIndex="{index}" '
                f'g2s:hostId="{hid}" g2s:hostRegistered="true"{extra}>'
                f'{rows}</g2s:commHostItem>')
    return f"<g2s:commHostList>{out}</g2s:commHostList>"


OUR_ITEM = (3, 1, {"hostLocation": "http://10.0.0.9:8081/G2S",
                   "requiredForPlay": "true", "timeToLive": "45000",
                   "noResponseTimer": "120000"},
            [("ownedDevice", "G2S_communications", "1", "true"),
             ("configDevice", "G2S_communications", "1", "true"),
             ("ownedDevice", "G2S_wat", "2", "false"),
             ("configDevice", "G2S_wat", "2", "false"),
             ("configDevice", "G2S_hopper", "1", "true"),
             ("guestDevice", "G2S_voucher", "1", "true"),
             ("guestDevice", "G2S_progressive", "1", "true")])


def items_21_24_25_37_the_ownership_claim():
    print("— audit items 21, 24, 37: the claim is built on what the "
          "machine holds for this host (§8.15: a setHostItem \"overwrite[s] "
          "any previous settings\")")
    host = make_host()
    egm = online(joined(host, "BTB_claim"))
    a = egm.assoc
    r = host.start_ownership_cycle(a)
    check("with the machine's host list not yet read, a claim is refused "
          "(it went ahead from a guessed list that left out the "
          "communications device)", isinstance(r, dict)
          and r.get("ok") is False and "getCommHostList" in r["error"], r)
    host.enqueue_get_comm_host_list(a)
    drain(host, a)
    answer_to(egm, "commConfig", "getCommHostList",
              host_list([OUR_ITEM, (1, 0, {}, [])]))
    r = host.start_ownership_cycle(a)
    drain(host, a)
    check("progressive is left as it is: the hub does not serve it",
          r.get("ok") and r["left"] == ["G2S_progressive/1"]
          and "G2S_progressive/1" not in r["claimed"], r)
    mark = len(host.sent)
    answer_to(egm, "commConfig", "enterCommConfigMode",
              '<g2s:commConfigModeStatus g2s:enabled="true" '
              'g2s:configurationId="0"/>')
    sent = sent_since(host, mark)
    item = next((el for _c, name, _a, el in sent
                 if name == "setCommChange"), None)
    shi = item[0] if item is not None else None
    at = {k.rsplit("}", 1)[-1]: v for k, v in shi.attrib.items()} \
        if shi is not None else {}
    check("the machine's own slot and the address it reaches us at go back "
          "unchanged (the hub sent its own idea of its address)",
          at.get("hostIndex") == "3"
          and at.get("hostLocation") == "http://10.0.0.9:8081/G2S", at)
    check("and so do the host's other settings (requiredForPlay was forced "
          "false, the rest fell to defaults)",
          at.get("requiredForPlay") == "true"
          and at.get("timeToLive") == "45000"
          and at.get("noResponseTimer") == "120000", at)
    rows = {(gh.localname(d.tag), d.get(f"{{{gh.SCHEMA_NS}}}deviceClass"),
             d.get(f"{{{gh.SCHEMA_NS}}}deviceId")):
            d.get(f"{{{gh.SCHEMA_NS}}}deviceActive")
            for d in (shi if shi is not None else [])}
    check("a device that was inactive stays inactive (every device was "
          "written active)",
          rows.get(("ownedDevice", "G2S_wat", "2")) == "false"
          and rows.get(("ownedDevice", "G2S_communications", "1")) == "true",
          rows)
    check("a device we are only configurator of is kept",
          ("configDevice", "G2S_hopper", "1") in rows
          and ("ownedDevice", "G2S_hopper", "1") not in rows, sorted(rows))
    check("the guest we asked for is claimed, the progressive stays guest",
          ("ownedDevice", "G2S_voucher", "1") in rows
          and ("guestDevice", "G2S_progressive", "1") in rows
          and ("ownedDevice", "G2S_progressive", "1") not in rows,
          sorted(rows))

    print("— audit item 8: once the change applies, the exit goes first")
    cfg = a.pending_config_id
    answer_to(egm, "commConfig", "setCommChange",
              f'<g2s:commChangeStatus g2s:configurationId="{cfg}" '
              f'g2s:transactionId="90" g2s:changeStatus="G2S_pending"/>')
    mark = len(host.sent)
    answer_to(egm, "commConfig", "authorizeCommChange",
              f'<g2s:commChangeStatus g2s:configurationId="{cfg}" '
              f'g2s:transactionId="90" g2s:changeStatus="G2S_applied"/>')
    order = names(sent_since(host, mark))
    check("enterCommConfigMode(enable=false) is the first thing queued (it "
          "was third, behind two reads a rejoin could drop it with)",
          order[:1] == ["enterCommConfigMode"]
          and len(exits(sent_since(host, mark), "enterCommConfigMode")) == 1,
          order)

    print("— audit item 25: a host list the machine sends unasked carries "
          "only the hosts that changed (§8.13)")
    egm.send("commConfig", host_list([(2, 5, {}, [
        ("ownedDevice", "G2S_printer", "1", "true")])]), device="1")
    check("our own entry is still there (the list replaced the whole "
          "table, and the hub forgot what it owns)",
          "1" in a.host_items and a.host_items["1"]["hostIndex"] == "3"
          and "5" in a.host_items, sorted(a.host_items))
    host.enqueue_get_comm_host_list(a)
    drain(host, a)
    answer_to(egm, "commConfig", "getCommHostList", host_list([OUR_ITEM]))
    check("the answer to our own getCommHostList is the whole table and "
          "replaces it", sorted(a.host_items) == ["1"], sorted(a.host_items))

# ---------------------------------------------------------------------------
# Batch 4: meters and game play

def meters_xml(*groups, kind="G2S_onDemand"):
    """A meterInfo. Each group is (element name, its attributes,
    {meterName: value})."""
    out = ""
    for tag, attrs, meters in groups:
        out += (f"<g2s:{tag} "
                + " ".join(f'g2s:{k}="{v}"' for k, v in attrs.items()) + ">"
                + "".join(f'<g2s:simpleMeter g2s:meterName="{n}" '
                          f'g2s:meterValue="{v}"/>'
                          for n, v in meters.items())
                + f"</g2s:{tag}>")
    return (f'<g2s:meterInfo g2s:meterInfoType="{kind}" '
            f'g2s:meterDateTime="{gh.now_iso()}">{out}</g2s:meterInfo>')


def cabinet_credits(cash=0, promo=0, non_cash=0):
    return ("deviceMeters", {"deviceClass": "G2S_cabinet", "deviceId": "1"},
            {"G2S_playerCashableAmt": cash, "G2S_playerPromoAmt": promo,
             "G2S_playerNonCashAmt": non_cash})


def pushed(egm, *groups):
    """A periodic meter report: a notification, sent unasked."""
    return egm.send("meters", meters_xml(*groups, kind="G2S_onPeriodic"),
                    stype="G2S_notification")


def asks(cmds, name):
    return [a for _c, n, a, _e in cmds if n == name]


def item_6_the_credit_figure_counts_all_three():
    print("— audit item 6: the credit on a machine is all three credit "
          "types (§5.9: \"The credit meter displayed to the player is the "
          "total of the three types of credits\")")
    host = make_host()
    egm = online(joined(host, "BTB_credits"))
    a = egm.assoc
    pushed(egm, cabinet_credits(non_cash=50000))
    check("restricted credit alone counts as credit on the machine (it "
          "read as an empty machine)", host._machine_credits_mc(a) == 50000,
          host._machine_credits_mc(a))
    seat = {"kind": "g2s"}
    check("the tournament's empty-machine test sees it, with no need to "
          "ask", host._tournament_clear_confirmed(
              "BTB_credits", seat, time.time()) is True
          and "clearAskedAt" not in seat, seat)
    mark = len(host.sent)
    host._tournament_pull_seat("BTB_credits", seat, slot="clear")
    drain(host, a)
    req = asks(sent_since(host, mark), "initiateRequest")
    check("so the clear pulls it off before the stake goes on (it was "
          "settled as $0 and the stake was put on top)",
          (seat.get("clear") or {}).get("state") == "pulling" and req
          and req[-1].get("watDirection") == "G2S_fromEgm"
          and req[-1].get("reqNonCashAmt") == "50000"
          and req[-1].get("reqCashableAmt") == "0", (seat, req))

    host = make_host()
    egm = online(joined(host, "BTB_credits3"))
    a = egm.assoc
    pushed(egm, cabinet_credits(cash=30000, promo=2000, non_cash=5000))
    mark = len(host.sent)
    r = host.start_wat_credit_pull(a, "house")
    drain(host, a)
    req = asks(sent_since(host, mark), "initiateRequest")
    check("a pull asks for each type as its own meter reads (it asked "
          "for the cashable alone and left the rest on the machine)",
          r.get("askedMc") == 37000
          and r.get("askedBucketsMc") == [30000, 2000, 5000] and req
          and (req[-1].get("reqCashableAmt"), req[-1].get("reqPromoAmt"),
               req[-1].get("reqNonCashAmt")) == ("30000", "2000", "5000"),
          (r, req))
    host = make_host()
    egm = online(joined(host, "BTB_credits0"))
    pushed(egm, cabinet_credits())
    r = host.start_wat_credit_pull(egm.assoc, "house")
    check("all three at zero is still an empty machine",
          r.get("ok") is False and "nothing on the glass" in r["error"], r)


def item_18_a_meter_answer_belongs_to_its_read():
    print("— audit item 18: only the answer to its own read confirms an "
          "empty machine (Table 5.10: meterInfoType says what caused a "
          "report, meterDateTime is when \"the meter snapshot was taken\")")
    host = make_host()
    egm = online(joined(host, "BTB_meters"))
    a = egm.assoc
    # two reads already out and not yet answered: one of the cabinet, one
    # of the games
    host.enqueue_get_meter_info(a, device_class="G2S_cabinet",
                                device_id="-1")
    host.enqueue_get_meter_info(a, device_class="G2S_gamePlay",
                                device_id="-1")
    drain(host, a)
    old_cabinet, games = list(a.meter_reads)[-2:]
    time.sleep(0.02)
    seat = {"kind": "g2s"}
    name = "BTB_meters"

    def confirmed():
        return host._tournament_clear_confirmed(name, seat, time.time())
    mark = len(host.sent)
    first = confirmed()
    drain(host, a)
    check("a machine that reads empty is asked before it is believed",
          first is False
          and names(sent_since(host, mark)) == ["getMeterInfo"],
          names(sent_since(host, mark)))
    own = list(a.meter_reads)[-1]
    empty = meters_xml(cabinet_credits())
    pushed(egm, cabinet_credits())
    check("a periodic report that arrives after the question is not the "
          "answer: it was taken before it was sent (it was counted)",
          confirmed() is False)
    got = egm.send("meters", meters_xml(cabinet_credits(), kind="G2S_onEOD"))
    check("nor is an end-of-day report (which is still acknowledged)",
          confirmed() is False and "meterInfoAck" in names(got), names(got))
    answer_to(egm, "meters", "getMeterInfo", meters_xml((
        "deviceMeters", {"deviceClass": "G2S_gamePlay", "deviceId": "1"},
        {"G2S_wageredAmt": 5})), sid=games)
    check("nor the answer to a read about the games, which carries no "
          "cabinet meter", confirmed() is False)
    answer_to(egm, "meters", "getMeterInfo", empty, sid=old_cabinet)
    check("nor the answer to a cabinet read made before the question",
          confirmed() is False)
    host.enqueue_get_meter_info(a, device_class="G2S_gamePlay",
                                device_id="-1")
    drain(host, a)
    answer_to(egm, "meters", "getMeterInfo", meters_xml((
        "deviceMeters", {"deviceClass": "G2S_gamePlay", "deviceId": "1"},
        {"G2S_wageredAmt": 6})), sid=list(a.meter_reads)[-1])
    check("nor the answer to a read about the games made after it",
          confirmed() is False)
    answer_to(egm, "meters", "getMeterInfo", empty, sid=own, more=True)
    check("the first part of its own answer, with more to come: not yet",
          confirmed() is False)
    answer_to(egm, "meters", "getMeterInfo", empty, sid=own)
    check("the last part of its own answer: the machine is empty",
          confirmed() is True)


def item_19_a_breakdown_is_not_the_device_total():
    print("— audit item 19: wager, denomination and currency breakdowns "
          "use the device's own meter names (Tables 5.16-5.18)")
    host = make_host()
    egm = online(joined(host, "BTB_groups"))
    a = egm.assoc
    game = {"deviceClass": "G2S_gamePlay", "deviceId": "1"}
    notes = {"deviceClass": "G2S_noteAcceptor", "deviceId": "1"}
    groups = (
        ("deviceMeters", game, {"G2S_wageredAmt": 1000}),
        ("wagerMeters", dict(game, wagerCategory="2"),
         {"G2S_wageredAmt": 400, "G2S_playedCnt": 7}),
        ("gameDenomMeters", dict(game, denomId="1000"),
         {"G2S_wageredAmt": 250}),
        ("deviceMeters", notes, {"G2S_currencyInAmt": 9000}),
        ("currencyMeters", dict(notes, currencyType="G2S_note",
                                currencyId="USD", denomId="500000"),
         {"G2S_currencyInAmt": 500}))
    pushed(egm, *groups)
    check("the game's wagered total stays the device's (the last "
          "breakdown line replaced it)",
          a.meters.get("G2S_gamePlay/1/G2S_wageredAmt") == "1000",
          a.meters.get("G2S_gamePlay/1/G2S_wageredAmt"))
    check("a meter that exists only per wager category does not appear "
          "as a device meter", "G2S_gamePlay/1/G2S_playedCnt" not in a.meters)
    check("the note acceptor's total is not replaced by one "
          "denomination's line",
          a.meters.get("G2S_noteAcceptor/1/G2S_currencyInAmt") == "9000",
          a.meters.get("G2S_noteAcceptor/1/G2S_currencyInAmt"))
    egm.event_id = getattr(egm, "event_id", 5000) + 1
    egm.send(
        "eventHandler",
        '<g2s:eventReport g2s:deviceClass="G2S_gamePlay" g2s:deviceId="1" '
        f'g2s:eventCode="G2S_GPE112" g2s:eventId="{egm.event_id}" '
        f'g2s:eventDateTime="{egm.stamp()}" g2s:transactionId="0">'
        "<g2s:meterList>" + meters_xml(
            ("deviceMeters", game, {"G2S_wageredAmt": 1100}),
            ("gameDenomMeters", dict(game, denomId="1000"),
             {"G2S_wageredAmt": 350}), kind="G2S_onEvent")
        + "</g2s:meterList></g2s:eventReport>")
    check("the same in the meters that come with an event",
          a.meters.get("G2S_gamePlay/1/G2S_wageredAmt") == "1100",
          a.meters.get("G2S_gamePlay/1/G2S_wageredAmt"))


def denom_list(*parts):
    """A gameDenomList: ('d', denomId, active) for a single,
    ('r', min, max, interval or None, active) for a range."""
    out = ""
    for part in parts:
        if part[0] == "d":
            out += (f'<g2s:gameDenom g2s:denomId="{part[1]}" '
                    f'g2s:active="{part[2]}"/>')
        else:
            step = "" if part[3] is None else \
                f' g2s:denomInterval="{part[3]}"'
            out += (f'<g2s:gameRange g2s:denomMin="{part[1]}" '
                    f'g2s:denomMax="{part[2]}"{step} '
                    f'g2s:active="{part[4]}"/>')
    return f"<g2s:gameDenomList>{out}</g2s:gameDenomList>"


def read_denoms(host, egm, body, dev="1"):
    host.enqueue_get_game_denoms(egm.assoc, dev)
    drain(host, egm.assoc)
    return answer_to(egm, "gamePlay", "getGameDenoms", body, device=dev)


def item_7_a_range_of_denominations():
    print("— audit item 7: a denomination range stands for the series it "
          "spans (§6.3: it \"can substitute for a series of denomIds in "
          "all cases where a list of denomIds can be used\")")
    host = make_host()
    egm = online(joined(host, "BTB_range"))
    a = egm.assoc
    # the document's own example answer (§6.24.5.2), plus two singles
    read_denoms(host, egm, denom_list(
        ("r", 1000, 5000, None, "true"),
        ("d", 10000, "false"), ("d", 25000, "true")))
    g = a.game_play["1"]
    check("each denomination in the range is listed (the range was kept "
          "aside and read by nothing: no chip on the Games page)",
          [d["denomId"] for d in g["denoms"]] == [
              "1000", "2000", "3000", "4000", "5000", "10000", "25000"],
          [d["denomId"] for d in g["denoms"]])
    check("and counted as active when the range is (it read as one "
          "active denomination)", g["activeDenoms"] == [
              "1000", "2000", "3000", "4000", "5000", "25000"],
          g["activeDenoms"])
    # a change the machine answers with a range
    host.enqueue_get_game_denoms(a, "1")
    drain(host, a)
    sid = list(a.sent_requests)[-1]
    with a.lock:
        a.game_play["1"]["denomChange"] = {
            "result": "sent", "sessionId": sid,
            "denoms": ["1000", "2000", "3000"]}
    answer_to(egm, "gamePlay", "getGameDenoms", denom_list(
        ("r", 1000, 3000, None, "true"), ("r", 4000, 5000, None, "false"),
        ("d", 10000, "false"), ("d", 25000, "false")), sid=sid)
    check("a change the machine echoes as a range is read as accepted (it "
          "read as a disagreement)",
          a.game_play["1"]["denomChange"]["result"] == "accepted",
          a.game_play["1"]["denomChange"])

    print("— the interval, and a denomination named both ways")
    read_denoms(host, egm, denom_list(
        ("r", 5000, 25000, 10000, "true"), ("d", 15000, "false")))
    g = a.game_play["1"]
    check("the range runs in steps of its interval",
          [d["denomId"] for d in g["denoms"]] == ["5000", "15000", "25000"],
          g["denoms"])
    check("a denomination also named on its own is as its own entry says",
          g["activeDenoms"] == ["5000", "25000"], g["activeDenoms"])

    print("— a range that cannot be listed")
    read_denoms(host, egm, denom_list(
        ("r", 1, 500000, 1, "true"), ("d", 1000, "true")))
    g = a.game_play["1"]
    mark = len(host.sent)
    r = host.start_denom_ceremony(a, device_id="1", denoms=[1000])
    drain(host, a)
    check("it is said, and a denomination change for that game is "
          "refused: it would switch off whatever is active in the range",
          bool(g.get("denomsPartial")) and r.get("ok") is False
          and "range" in r.get("error", "")
          and sent_since(host, mark) == [], (g.get("denomsPartial"), r))
    read_denoms(host, egm, denom_list(("d", 1000, "true")))
    check("a later list that is all there clears it",
          not a.game_play["1"].get("denomsPartial"))


def game_status(theme="T1", paytable="P1", **more):
    extra = "".join(f' g2s:{k}="{v}"' for k, v in more.items())
    return (f'<g2s:gamePlayStatus g2s:themeId="{theme}" '
            f'g2s:paytableId="{paytable}"{extra}/>')


def game_profile(theme="T1", paytable="P1", **more):
    extra = "".join(f' g2s:{k}="{v}"' for k, v in more.items())
    return (f'<g2s:gamePlayProfile g2s:themeId="{theme}" '
            f'g2s:paytableId="{paytable}" g2s:maxWagerCredits="5"{extra}>'
            '<g2s:wagerCategoryList><g2s:wagerCategoryItem '
            'g2s:wagerCategory="1" g2s:theoPaybackPct="9000"/>'
            "</g2s:wagerCategoryList><g2s:winLevelList><g2s:winLevelItem "
            'g2s:winLevelIndex="1"/></g2s:winLevelList></g2s:gamePlayProfile>')


def a_known_game(host, egm, dev="1", **profile):
    a = egm.assoc
    host.enqueue_get_game_play_status(a, dev)
    drain(host, a)
    answer_to(egm, "gamePlay", "getGamePlayStatus", game_status(),
              device=dev)
    host.enqueue_get_game_play_profile(a, dev)
    drain(host, a)
    answer_to(egm, "gamePlay", "getGamePlayProfile", game_profile(**profile),
              device=dev)


def item_20_a_game_whose_configuration_changed():
    print("— audit item 20: a game's profile is not fixed for the life of "
          "its device id (§6.13: its options \"can be set locally at the "
          "EGM or remotely via a configuration server\")")
    host = make_host()
    egm = online(joined(host, "BTB_profile"))
    a = egm.assoc
    a_known_game(host, egm)
    mark = len(host.sent)
    event(egm, "G2S_GPE006", cls="G2S_gamePlay")
    check("G2S_GPE006, configuration changed at the machine: status, "
          "profile and denominations are read again (the status alone was)",
          sorted(names(sent_since(host, mark))) == [
              "eventAck", "getGameDenoms", "getGamePlayProfile",
              "getGamePlayStatus"], names(sent_since(host, mark)))
    mark = len(host.sent)
    event(egm, "G2S_GPE005", cls="G2S_gamePlay")
    check("a second such event straight after does not ask twice",
          "getGamePlayProfile" not in names(sent_since(host, mark)),
          names(sent_since(host, mark)))
    a_known_game(host, egm, dev="2")
    mark = len(host.sent)
    event(egm, "G2S_GPE005", cls="G2S_gamePlay", dev="2")
    check("G2S_GPE005, configuration changed by a host, on another game: "
          "the same three reads",
          sorted(names(sent_since(host, mark))) == [
              "eventAck", "getGameDenoms", "getGamePlayProfile",
              "getGamePlayStatus"], names(sent_since(host, mark)))
    mark = len(host.sent)
    event(egm, "G2S_GPE103", cls="G2S_gamePlay")
    check("a spin is not a configuration change",
          names(sent_since(host, mark)) == ["eventAck"],
          names(sent_since(host, mark)))

    print("— a status that names another paytable than the profile held")
    host = make_host()
    egm = online(joined(host, "BTB_profile2"))
    a = egm.assoc
    a_known_game(host, egm)
    host.enqueue_get_game_play_status(a, "1")
    drain(host, a)
    mark = len(host.sent)
    answer_to(egm, "gamePlay", "getGamePlayStatus",
              game_status(paytable="P2"))
    check("the profile for the old paytable is dropped and read again (the "
          "Games page showed the new paytable with the old one's payback "
          "and maximum wager)", "profile" not in a.game_play["1"]
          and sorted(names(sent_since(host, mark))) == [
              "getGameDenoms", "getGamePlayProfile"],
          names(sent_since(host, mark)))
    answer_to(egm, "gamePlay", "getGamePlayProfile",
              game_profile(paytable="P2"))
    check("the new one is kept", a.game_play["1"]["profile"][
        "paytableId"] == "P2")
    # a machine whose profile goes on naming the other one
    a.gp_reread_at.clear()
    host.enqueue_get_game_play_status(a, "1")
    drain(host, a)
    answer_to(egm, "gamePlay", "getGamePlayStatus",
              game_status(paytable="P3"))
    answer_to(egm, "gamePlay", "getGamePlayProfile",
              game_profile(paytable="P2"))
    a.gp_reread_at.clear()
    host.enqueue_get_game_play_status(a, "1")
    drain(host, a)
    mark = len(host.sent)
    answer_to(egm, "gamePlay", "getGamePlayStatus",
              game_status(paytable="P3"))
    check("a profile that still names the other paytable after being read "
          "again is the machine's answer: kept, not asked for in a loop",
          "profile" in a.game_play["1"]
          and sent_since(host, mark) == [], names(sent_since(host, mark)))

    print("— the same, when the status comes with an event")
    host = make_host()
    egm = online(joined(host, "BTB_profile3"))
    a = egm.assoc
    a_known_game(host, egm)
    egm.event_id = getattr(egm, "event_id", 5000) + 1
    mark = len(host.sent)
    egm.send(
        "eventHandler",
        '<g2s:eventReport g2s:deviceClass="G2S_gamePlay" g2s:deviceId="1" '
        f'g2s:eventCode="G2S_GPE113" g2s:eventId="{egm.event_id}" '
        f'g2s:eventDateTime="{egm.stamp()}" g2s:transactionId="0">'
        '<g2s:deviceList><g2s:statusInfo g2s:deviceClass="G2S_gamePlay" '
        'g2s:deviceId="1">' + game_status(paytable="P7")
        + "</g2s:statusInfo></g2s:deviceList></g2s:eventReport>")
    check("the profile is dropped and read again there too",
          "profile" not in a.game_play["1"]
          and "getGamePlayProfile" in names(sent_since(host, mark)),
          names(sent_since(host, mark)))


def paytable_options(dev):
    """One game device as the machine's option list names it."""
    return ('<g2s:deviceOptions g2s:deviceClass="G2S_gamePlay" '
            f'g2s:deviceId="{dev}"><g2s:optionGroup '
            'g2s:optionGroupId="G2S_gamePlayOptions"><g2s:optionItem '
            'g2s:optionId="G2S_protocolOptions"><g2s:optionCurrentValues>'
            '<g2s:complexValue g2s:paramId="G2S_protocolParams">'
            '<g2s:booleanValue g2s:paramId="G2S_requiredForPlay">false'
            "</g2s:booleanValue></g2s:complexValue>"
            "</g2s:optionCurrentValues></g2s:optionItem></g2s:optionGroup>"
            "</g2s:deviceOptions>")


def item_20_a_machine_whose_game_was_replaced():
    print("— audit item 20, as it stood on the floor: one game on the "
          "machine, its game devices that game's paytables, and the game "
          "replaced")
    host = make_host()
    egm = online(joined(host, "BTB_replaced"))
    a = egm.assoc
    egm.send("optionConfig", "<g2s:optionList>" + "".join(
        paytable_options(d) for d in "123") + "</g2s:optionList>")
    for dev, pay in (("1", "OLD-88"), ("2", "OLD-92"), ("3", "OLD-96")):
        host.enqueue_get_game_play_status(a, dev)
        drain(host, a)
        answer_to(egm, "gamePlay", "getGamePlayStatus",
                  game_status(theme="OLD", paytable=pay), device=dev)
    a.gp_resweep_at = 0.0
    kept = gh.GP_RESCAN_DESC_WAIT_SEC
    gh.GP_RESCAN_DESC_WAIT_SEC = 0.3
    try:
        # the paytable the machine is set to is played: its status comes
        # with the event, naming the new game
        egm.event_id = getattr(egm, "event_id", 5000) + 1
        mark = len(host.sent)
        egm.send(
            "eventHandler",
            '<g2s:eventReport g2s:deviceClass="G2S_gamePlay" '
            f'g2s:deviceId="3" g2s:eventCode="G2S_GPE113" '
            f'g2s:eventId="{egm.event_id}" '
            f'g2s:eventDateTime="{egm.stamp()}" g2s:transactionId="0">'
            '<g2s:deviceList><g2s:statusInfo g2s:deviceClass="G2S_gamePlay" '
            'g2s:deviceId="3">' + game_status(theme="NEW", paytable="NEW-96")
            + "</g2s:statusInfo></g2s:deviceList></g2s:eventReport>")
        time.sleep(0.9)
        drain(host, a)
        asked = [name for name in names(sent_since(host, mark))
                 if name in ("getDescriptor", "getGamePlayStatus")]
        check("the other paytables are read again too: they send no "
              "events of their own, and went on showing the old game "
              "(eleven of them for four months on a Bluebird)",
              asked.count("getDescriptor") == 1
              and asked.count("getGamePlayStatus") == 3, asked)
        # the answers name the new game as well; no second sweep follows
        mark = len(host.sent)
        for dev, pay in (("1", "NEW-88"), ("2", "NEW-92")):
            host.enqueue_get_game_play_status(a, dev)
            drain(host, a)
            answer_to(egm, "gamePlay", "getGamePlayStatus",
                      game_status(theme="NEW", paytable=pay), device=dev)
        time.sleep(0.5)
        drain(host, a)
        check("and their own answers, naming the new game, do not start "
              "it over", "getDescriptor" not in names(sent_since(host, mark))
              and [a.game_play[d]["themeId"] for d in "123"]
              == ["NEW"] * 3, names(sent_since(host, mark)))
        a.gp_resweep_at = 0.0
        mark = len(host.sent)
        host.enqueue_get_game_play_status(a, "1")
        drain(host, a)
        answer_to(egm, "gamePlay", "getGamePlayStatus",
                  game_status(theme="NEW", paytable="NEW-90"), device="1")
        time.sleep(0.5)
        drain(host, a)
        check("a paytable changed under the same game is not a replaced "
              "game", "getDescriptor" not in names(sent_since(host, mark)),
              names(sent_since(host, mark)))
        mark = len(host.sent)
        host.enqueue_get_game_play_status(a, "2")
        drain(host, a)
        answer_to(egm, "gamePlay", "getGamePlayStatus",
                  game_status(theme="THIRD", paytable="T-92"), device="2")
        time.sleep(0.9)
        drain(host, a)
        got = names(sent_since(host, mark))
        check("a replaced game found in the answer to a read, not in an "
              "event, starts the same sweep",
              got.count("getDescriptor") == 1
              and got.count("getGamePlayStatus") == 4, got)
        a.gp_resweep_at = 0.0
        mark = len(host.sent)
        host.enqueue_get_game_play_status(a, "9")
        drain(host, a)
        answer_to(egm, "gamePlay", "getGamePlayStatus",
                  game_status(theme="THIRD", paytable="T-99"), device="9")
        time.sleep(0.5)
        drain(host, a)
        check("a device heard from for the first time is not a replaced "
              "game", "getDescriptor" not in names(sent_since(host, mark)),
              names(sent_since(host, mark)))
    finally:
        gh.GP_RESCAN_DESC_WAIT_SEC = kept


def with_status(egm, dev, theme, paytable, code="G2S_GPE113"):
    """A game event from that device carrying its status, as a machine
    sends one for the game being played."""
    egm.event_id = getattr(egm, "event_id", 5000) + 1
    return egm.send(
        "eventHandler",
        '<g2s:eventReport g2s:deviceClass="G2S_gamePlay" '
        f'g2s:deviceId="{dev}" g2s:eventCode="{code}" '
        f'g2s:eventId="{egm.event_id}" '
        f'g2s:eventDateTime="{egm.stamp()}" g2s:transactionId="0">'
        '<g2s:deviceList><g2s:statusInfo g2s:deviceClass="G2S_gamePlay" '
        f'g2s:deviceId="{dev}">' + game_status(theme=theme, paytable=paytable)
        + "</g2s:statusInfo></g2s:deviceList></g2s:eventReport>")


def several_games(host, name, themes):
    egm = online(joined(host, name))
    a = egm.assoc
    egm.send("optionConfig", "<g2s:optionList>" + "".join(
        paytable_options(str(i + 1)) for i in range(len(themes)))
        + "</g2s:optionList>")
    for i, theme in enumerate(themes):
        a_known_game(host, egm, dev=str(i + 1))
        host.enqueue_get_game_play_status(a, str(i + 1))
        drain(host, a)
        answer_to(egm, "gamePlay", "getGamePlayStatus",
                  game_status(theme=theme, paytable=theme + "-94"),
                  device=str(i + 1))
        answer_to(egm, "gamePlay", "getGamePlayProfile",
                  game_profile(theme=theme, paytable=theme + "-94"),
                  device=str(i + 1))
    time.sleep(0.5)
    drain(host, a)
    a.gp_resweep_at = 0.0
    a.gp_auto_sweeps = []
    a.gp_reread_at.clear()
    return egm


def item_20_no_reading_in_a_loop():
    print("— a machine with several games: a player going from one to "
          "another changes nothing under any device (§6.1: one device per "
          "game and paytable)")
    host = make_host()
    kept = gh.GP_RESCAN_DESC_WAIT_SEC
    gh.GP_RESCAN_DESC_WAIT_SEC = 0.2
    try:
        egm = several_games(host, "BTB_chest", ["A", "B", "C"])
        a = egm.assoc
        mark = len(host.sent)
        for dev, theme in (("1", "A"), ("2", "B"), ("3", "C"), ("1", "A"),
                           ("3", "C"), ("2", "B"), ("1", "A")):
            with_status(egm, dev, theme, theme + "-94")
        time.sleep(0.5)
        drain(host, a)
        got = [n for n in names(sent_since(host, mark)) if n != "eventAck"]
        check("seven games played across three titles: nothing is read "
              "again (no sweep, no profile, no denominations)",
              got == [], got)

        print("— the worst a machine could do: report a different game "
              "under the same device every time")
        sweeps = 0
        for n in range(6):
            a.gp_resweep_at = 0.0           # as if ten minutes had passed
            mark = len(host.sent)
            with_status(egm, "1", f"OTHER{n}", f"OTHER{n}-94")
            sweeps += names(sent_since(host, mark)).count("getDescriptor")
            time.sleep(0.4)
            drain(host, a)
        check("the hub sweeps the machine three times in a day and then "
              "leaves it to the Refresh button", sweeps == 3, sweeps)
        a.gp_auto_sweeps = [time.time() - 90000] * 3    # yesterday's
        a.gp_resweep_at = 0.0
        mark = len(host.sent)
        with_status(egm, "1", "TOMORROW", "TOMORROW-94")
        again = names(sent_since(host, mark)).count("getDescriptor")
        time.sleep(0.4)
        drain(host, a)
        check("a day later it will sweep again", again == 1, again)
        mark = len(host.sent)
        for n in range(4):
            with_status(egm, "1", f"AGAIN{n}", f"AGAIN{n}-94")
        time.sleep(0.4)
        drain(host, a)
        got = names(sent_since(host, mark))
        check("and that one device's profile is asked for once in 30 "
              "seconds however often it changes",
              got.count("getDescriptor") == 0
              and got.count("getGamePlayProfile") <= 1, got)
    finally:
        gh.GP_RESCAN_DESC_WAIT_SEC = kept


def item_52_a_game_the_machine_needs():
    print("— audit item 52: requiredForPlay (Table 6.6: \"the EGM MUST be "
          "disabled if either egmEnabled or hostEnabled is set to false\")")
    host = make_host()
    egm = online(joined(host, "BTB_required"))
    a = egm.assoc
    a_known_game(host, egm, dev="1", requiredForPlay="true")
    a_known_game(host, egm, dev="2")
    a_known_game(host, egm, dev="3", requiredForPlay="1")
    check("it is kept with the game's profile, for the Games page to say "
          "before a Disable (it was dropped)",
          [a.game_play[d]["profile"]["requiredForPlay"] for d in "123"]
          == ["true", "false", "true"],
          [a.game_play[d]["profile"].get("requiredForPlay") for d in "123"])
    shown = a.snapshot()["gamePlay"]
    check("and shown per game",
          [shown[d]["requiredForPlay"] for d in "123"]
          == ["true", "false", "true"])

    print("— a game whose profile was read before the hub kept it")
    host = make_host()
    egm = online(joined(host, "BTB_required_old"))
    a = egm.assoc
    for dev in ("1", "2"):
        a_known_game(host, egm, dev=dev)
        with a.lock:        # as a profile stored by an earlier version
            a.game_play[dev]["profile"].pop("requiredForPlay")

    def protocol_options(dev, value):
        return ('<g2s:deviceOptions g2s:deviceClass="G2S_gamePlay" '
                f'g2s:deviceId="{dev}"><g2s:optionGroup '
                'g2s:optionGroupId="G2S_gamePlayOptions"><g2s:optionItem '
                'g2s:optionId="G2S_protocolOptions"><g2s:optionCurrentValues>'
                '<g2s:complexValue g2s:paramId="G2S_protocolParams">'
                '<g2s:booleanValue g2s:paramId="G2S_requiredForPlay">'
                f'{value}</g2s:booleanValue></g2s:complexValue>'
                "</g2s:optionCurrentValues></g2s:optionItem>"
                "</g2s:optionGroup></g2s:deviceOptions>")
    egm.send("optionConfig", "<g2s:optionList>" + protocol_options("1", "true")
             + protocol_options("2", "false") + "</g2s:optionList>")
    shown = a.snapshot()["gamePlay"]
    check("the machine's option list says it for every game, so it shows "
          "without the profile being read again",
          [shown[d]["requiredForPlay"] for d in "12"] == ["true", "false"],
          [shown[d].get("requiredForPlay") for d in "12"])
    check("read back from what the hub keeps of the option list after a "
          "restart", gh.games_required_for_play(a.config_options)
          == {"1": "true", "2": "false"},
          gh.games_required_for_play(a.config_options))


def item_27_booleans_written_as_1_and_0():
    print("— audit item 27: a boolean may be written 1 or 0 (Appendix C: "
          "XML Schema's boolean; the document's own examples write "
          "sessionRetry=\"0\")")
    check("the two spellings are put into one",
          [gh.g2s_bool(v) for v in ("true", "TRUE", "1", "false", "0",
                                    None, "maybe")]
          == ["true", "true", "true", "false", "false", "false", "false"]
          and gh.g2s_bool(None, "true") == "true")
    eq = gh.option_values_equal
    check("an option at 1 is at its target of true, and 0 is not",
          eq("1", "true") and eq("true", "1") and eq("0", "false")
          and not eq("0", "true") and not eq("1", "false"))
    check("numbers and text are still compared as they are written",
          eq("1", "1") and not eq("1", "2") and not eq("abc", "ABC")
          and eq(" True ", "true") and not eq("10", "true"))
    check("a handpay's own flags likewise",
          gh.pick_remote_key_off_type({"remoteCredit": "1"})
          == "G2S_remoteCredit")

    host = make_host()
    egm = online(joined(host, "BTB_bool"))
    a = egm.assoc
    host.enqueue_get_option_config_mode_status(a)
    drain(host, a)
    mark = len(host.sent)
    answer_to(egm, "optionConfig", "getOptionConfigModeStatus",
              '<g2s:optionConfigModeStatus g2s:enabled="1"/>')
    check("enabled=\"1\": the machine IS in the option configuration "
          "mode, and is taken out (it read as out, and stayed in with "
          "play stopped)", len(exits(sent_since(host, mark))) == 1,
          names(sent_since(host, mark)))
    host.enqueue_get_comm_config_mode_status(a)
    drain(host, a)
    mark = len(host.sent)
    answer_to(egm, "commConfig", "getCommConfigModeStatus",
              '<g2s:commConfigModeStatus g2s:enabled="1" '
              'g2s:configurationId="0"/>')
    check("the same for the communications configuration mode",
          len(exits(sent_since(host, mark), "enterCommConfigMode")) == 1,
          names(sent_since(host, mark)))

    print("— a game switched off, answered with hostEnabled=\"0\"")
    a_known_game(host, egm)
    host.enqueue_set_game_play_state(a, enable=False, disable_text="off",
                                     device_id="1")
    drain(host, a)
    answer_to(egm, "gamePlay", "setGamePlayState",
              game_status(hostEnabled="0", egmEnabled="1"))
    g = a.game_play["1"]
    check("it is read as off and the change as confirmed (it read as a "
          "disagreement, and the game showed live)",
          g["hostEnabled"] == "false" and g["egmEnabled"] == "true"
          and g["stateChange"]["result"] == "confirmed", g["stateChange"])
    read_denoms(host, egm, denom_list(("d", 1000, "1"), ("d", 2000, "0")))
    check("a denomination with active=\"1\" is active (every one read as "
          "inactive, so the editor started from an empty set)",
          a.game_play["1"]["activeDenoms"] == ["1000"],
          a.game_play["1"]["activeDenoms"])

    print("— the cabinet, and the handpay settings")
    host.enqueue_set_cabinet_state(a, enable=False, disable_text="off")
    drain(host, a)
    answer_to(egm, "cabinet", "setCabinetState",
              '<g2s:cabinetStatus g2s:hostEnabled="0" g2s:egmEnabled="1" '
              'g2s:logicDoorOpen="1" g2s:egmState="G2S_hostDisabled"/>')
    check("a machine disable answered hostEnabled=\"0\" is confirmed, and "
          "a door written \"1\" is open",
          a.cabinet_state_change.get("result") == "confirmed"
          and a.cabinet["hostEnabled"] == "false"
          and a.cabinet["logicDoorOpen"] == "true", a.cabinet_state_change)
    host._maybe_arm_remote_handpay_on_join(a)
    drain(host, a)
    mark = len(host.sent)
    answer_to(egm, "optionConfig", "getOptionList", handpay_options(
        tuple((p, "true", "1") for p, _m, _v in HANDPAY_PARAMS)))
    check("handpay settings already on, written \"1\": nothing is sent "
          "(a change went out at every join and each was marked as not "
          "verified), and the page's lamp reads on",
          values_sent(sent_since(host, mark)) is None
          and a.snapshot().get("handpayRemoteCreditAllowed") == "true",
          a.snapshot().get("handpayRemoteCreditAllowed"))

    print("— the same spelling in the other classes' answers")
    host.enqueue_get_wat_status(a, "1")
    drain(host, a)
    answer_to(egm, "wat", "getWatStatus",
              '<g2s:watStatus g2s:cashOutToWat="1" g2s:hostEnabled="0"/>')
    w = a.wat_devices["1"]
    check("transfers: cash-out to the host \"1\" is on, hostEnabled "
          "\"0\" is off", w["cashOutToWat"] == "true"
          and w["hostEnabled"] == "false" and w["egmEnabled"] == "true", w)
    host.enqueue_get_note_acceptor_status(a, "1")
    drain(host, a)
    answer_to(egm, "noteAcceptor", "getNoteAcceptorStatus",
              '<g2s:noteAcceptorStatus g2s:hostEnabled="0" '
              'g2s:doorOpen="1" g2s:stackerFull="0"/>')
    n = a.note_acceptor["status"]
    check("note acceptor: a fault written \"1\" is raised",
          n["doorOpen"] == "true" and n["stackerFull"] == "false"
          and n["hostEnabled"] == "false", n)
    host.enqueue_get_note_acceptor_profile(a, "1")
    drain(host, a)
    answer_to(egm, "noteAcceptor", "getNoteAcceptorProfile",
              '<g2s:noteAcceptorProfile g2s:noteEnabled="true" '
              'g2s:voucherEnabled="true" g2s:requiredForPlay="1">'
              '<g2s:noteAcceptorData g2s:currencyId="USD" '
              'g2s:denomId="100000" g2s:baseCashableAmt="100000" '
              'g2s:noteActive="1"/><g2s:noteAcceptorData '
              'g2s:currencyId="USD" g2s:denomId="500000" '
              'g2s:baseCashableAmt="500000" g2s:noteActive="0"/>'
              "</g2s:noteAcceptorProfile>")
    check("a note with noteActive=\"1\" is one it takes",
          len(a.note_acceptor.get("activeNotes") or []) == 1
          and a.note_acceptor["profile"]["requiredForPlay"] == "true",
          a.note_acceptor.get("activeNotes"))
    egm.get_validation_data(5)
    got = egm.send(
        "voucher",
        f'<g2s:getValidationData g2s:configurationId="0" '
        f'g2s:validationListId="{egm.val_list}" g2s:numValidationIds="5" '
        'g2s:valIdListExpired="1"/>')
    data = egm.answer(got, "validationData") or {}
    check("a ticket id list the machine reports expired with \"1\" is "
          "replaced", gh.g2s_true(data.get("deleteCurrent")), data)


def item_16_a_currency_without_two_decimal_places():
    print("— audit item 16: amounts are in thousandths of the currency's "
          "minor unit, and the decimal places are the currency's own "
          "(§3.2; the 100000 = $1.00 table is \"an example for US "
          "Dollars\")")
    check("the hub says by how much such a machine's figures are off",
          "100 times too small" in (gh.currency_scale_note("JPY") or "")
          and "10 times too large" in (gh.currency_scale_note("kwd") or "")
          and gh.currency_scale_note("USD") is None
          and gh.currency_scale_note("EUR") is None
          and gh.currency_scale_note("XYZ") is None
          and gh.currency_scale_note(None) is None)
    host = make_host()
    egm = online(joined(host, "BTB_yen"))
    a = egm.assoc
    try:
        host.enqueue_get_cabinet_profile(a)
        drain(host, a)
        answer_to(egm, "cabinet", "getCabinetProfile",
                  '<g2s:cabinetProfile g2s:currencyId="JPY" '
                  'g2s:localeId="ja_JP"/>')
        check("a machine in yen: its record says so, and the yen sign is "
              "not put in front of figures that are 100 times out",
              "JPY" in a.cabinet.get("currencyNote", "")
              and gh.money_symbol() != "¥",
              (a.cabinet.get("currencyNote"), gh.money_symbol()))
        host.enqueue_get_cabinet_profile(a)
        drain(host, a)
        answer_to(egm, "cabinet", "getCabinetProfile",
                  '<g2s:cabinetProfile g2s:currencyId="EUR" '
                  'g2s:localeId="de_DE"/>')
        check("a machine in euros: no note, and its sign is used as before",
              "currencyNote" not in a.cabinet and gh.money_symbol() == "€",
              (a.cabinet.get("currencyNote"), gh.money_symbol()))
    finally:
        gh._MONEY["symbol"], gh._MONEY["source"] = "$", "default"



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
    item_42_a_request_past_its_time_to_live()
    items_5_and_8_a_machine_left_in_a_config_mode()
    item_22_what_the_machine_lets_a_host_change()
    item_22_the_order_the_answers_arrive_in()
    item_26_a_set_the_hub_gives_up_on()
    items_21_24_25_37_the_ownership_claim()
    item_6_the_credit_figure_counts_all_three()
    item_18_a_meter_answer_belongs_to_its_read()
    item_19_a_breakdown_is_not_the_device_total()
    item_7_a_range_of_denominations()
    item_20_a_game_whose_configuration_changed()
    item_20_a_machine_whose_game_was_replaced()
    item_20_no_reading_in_a_loop()
    item_52_a_game_the_machine_needs()
    item_27_booleans_written_as_1_and_0()
    item_16_a_currency_without_two_decimal_places()
    print("=" * 50)
    print(f"RESULT: {_p} passed, {_f} failed")
    return 1 if _f else 0


if __name__ == "__main__":
    sys.exit(main())
