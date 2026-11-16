"""
An AFT transfer always ends in a known result. SAS 6.02 audit of
2026-11-16, items 2, 5 (the exception 69 half) and 16.

A transfer cycle "begins when the gaming machine receives an initiating 72
long poll, and ends when the host retrieves the final transfer and
transaction receipt completion status codes" (§8.3). aft_transfer() waits
about three seconds, tries one cancel and returns UNCONFIRMED. Before this
batch that was the end of it on our side: nothing asked again, exception 69
was only logged, and nothing was kept across a restart. A transfer that
completed a moment later moved credits the hub never booked, and a machine
that keeps the document refused every transfer after it.

These run against ByTheBookAFTMachine (tests/by_the_book.py), which holds
the cycle open until its final status is read and acknowledged.
"""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import sas_host
from by_the_book import ByTheBookAFTMachine, frame
from core.sas_implied_ack import ImpliedAckTransport
from core.sas_poller import SASPoller
from modules.aft import aft_handler
from modules.aft.aft_handler import (
    AFTHost, AFTOutcome, AFTStatus, AFTTransferRequest, AFTTransferStatusData,
    TRANSFER_TYPE_EGM_TO_HOST, TRANSFER_TYPE_HOST_TO_EGM,
    judge_open_transfer, parse_aft_transfer_response)
from modules.aft.open_transfer import OpenTransferJournal
from transport.serial.sas_serial import MockSASSerialPort

NO_SLEEP = lambda _t: None
TXN = "CN123456789000001"
CENTS = 1000000                  # $10,000, the Bluebird's push of 2026-11-16
INITIATE_LEN = 75                # an initiating 0x72 frame for TXN


class _Clock:
    def __init__(self):
        self.t = 500.0

    def __call__(self):
        return self.t

    def tick(self, sec):
        self.t += sec


def _rig(journal=None, **machine_kw):
    machine = ByTheBookAFTMachine(**machine_kw)
    port = ImpliedAckTransport(MockSASSerialPort(machine))
    poller = SASPoller(port, address=1, meters=[])
    clock = _Clock()
    host = AFTHost(poller, asset_number=machine.asset,
                   registration_key=machine.key, pos_id=machine.pos,
                   sleep=NO_SLEEP, journal=journal, clock=clock)
    return machine, port, poller, host, clock


def _push(txn=TXN, cents=CENTS):
    return AFTTransferRequest(transaction_id=txn,
                              transfer_type=TRANSFER_TYPE_HOST_TO_EGM,
                              cashable_cents=cents)


def _pull(txn=TXN, cents=CENTS):
    return AFTTransferRequest(transaction_id=txn,
                              transfer_type=TRANSFER_TYPE_EGM_TO_HOST,
                              cashable_cents=cents)


def _initiates(port):
    """The initiating 0x72 polls that went out (not 72/FF, not 72/80)."""
    return [f for f, _ in port.sent_frames
            if len(f) > 4 and f[1] == 0x72 and f[3] in (0x00, 0x01)]


def _start_unconfirmed(host, request=None, **kw):
    result = host.transfer(request or _push(), **kw)
    assert result.outcome is AFTOutcome.UNCONFIRMED
    return result


# ---------------------------------------------------------------------------
# Item 16 — the short answers
# ---------------------------------------------------------------------------

class TestShortAnswers:
    @pytest.mark.parametrize("body,status,receipt", [
        (bytes([0x00, 0xC0]), 0xC0, 0xFF),          # not compatible
        (bytes([0x00, 0xC1]), 0xC1, 0xFF),          # unsupported code
        (bytes([0x00, 0xFF, 0xFF]), 0xFF, 0xFF),    # no transfer information
    ])
    def test_they_parse(self, body, status, receipt):
        got = parse_aft_transfer_response(bytes([len(body)]) + body)
        assert (got.status, got.receipt_status) == (status, receipt)
        assert got.transaction_id == "" and got.cashable_cents is None
        assert not got.completed and not got.pending

    def test_a_length_between_is_still_refused(self):
        with pytest.raises(ValueError):
            parse_aft_transfer_response(bytes([10]) + bytes(10))

    def test_c0_to_an_initiating_poll_is_a_definite_refusal(self):
        """It used to read as silence and end UNCONFIRMED ('verify meters')
        when the machine had said plainly that it took nothing."""
        machine, port, poller, host, clock = _rig()
        machine.cycle = {"txn": "SOMEONE-ELSES", "type": 0x00,
                         "cents": 5, "status": 0x40}
        from modules.aft.aft_handler import aft_transfer
        result = aft_transfer(port, 1, _push(), machine.asset, machine.key,
                              sleep=NO_SLEEP, require_lock=False)
        assert result.outcome is AFTOutcome.REFUSED
        assert "NOT_COMPATIBLE_WITH_CURRENT_TRANSFER" in result.detail
        assert machine.refused_while_open == 1


# ---------------------------------------------------------------------------
# What an interrogation says about the open transfer
# ---------------------------------------------------------------------------

def _status(status, txn=TXN, cents=CENTS):
    return AFTTransferStatusData(0, status, 0xFF, 0x00, cents, 0, 0, 0,
                                 b"\x92\x10\x00\x00", txn)


PUSH = {"txnId": TXN, "kind": "aft_transfer", "cents": CENTS}
PULL = {"txnId": TXN, "kind": "aft_cashout", "cents": CENTS}
SHORT_FF = AFTTransferStatusData(0, 0xFF, 0xFF, 0, None, None, None, 0,
                                 b"", "")


class TestJudgeOpenTransfer:
    def test_no_answer_and_pending_leave_it_open(self):
        assert judge_open_transfer(PUSH, None) is None
        assert judge_open_transfer(PUSH, _status(0x40)) is None

    def test_completed_for_the_machines_amount(self):
        got = judge_open_transfer(PUSH, _status(0x00, cents=CENTS))
        assert (got["ok"], got["outcome"], got["amountCents"]) == (
            True, "completed", CENTS)
        got = judge_open_transfer(PUSH, _status(0x01, cents=250000))
        assert (got["ok"], got["outcome"], got["amountCents"]) == (
            True, "partial", 250000)

    def test_an_unreadable_amount_follows_the_live_rule(self):
        """A push falls back to what was asked for; a cash-out credits
        nothing, because a wallet credit cannot be taken back."""
        blank = _status(0x00, cents=None)
        assert judge_open_transfer(PUSH, blank)["amountCents"] == CENTS
        got = judge_open_transfer(PULL, blank)
        assert got["ok"] and got["amountCents"] == 0
        assert "NOT credited" in got["detail"]

    def test_cancelled_and_refused_move_nothing(self):
        got = judge_open_transfer(PUSH, _status(0x80))
        assert (got["ok"], got["outcome"], got["amountCents"]) == (
            False, "rolled_back", 0)
        got = judge_open_transfer(PUSH, _status(0x87))
        assert (got["ok"], got["outcome"], got["amountCents"]) == (
            False, "refused", 0)
        assert "UNABLE_TO_TRANSFER_AT_THIS_TIME" in got["detail"]

    def test_a_machine_that_does_not_know_it_took_nothing(self):
        got = judge_open_transfer(PUSH, SHORT_FF)
        assert (got["ok"], got["outcome"], got["amountCents"]) == (
            False, "not_taken", 0)
        assert "no transfer on record" in got["detail"]
        got = judge_open_transfer(PUSH, _status(0x00, txn="AN-OLDER-ONE"))
        assert got["outcome"] == "not_taken" and got["amountCents"] == 0
        assert "AN-OLDER-ONE" in got["detail"]


# ---------------------------------------------------------------------------
# The journal
# ---------------------------------------------------------------------------

class TestJournal:
    def test_written_before_and_gone_after(self, tmp_path):
        path = str(tmp_path / "data" / "aft_open_transfer.json")
        j = OpenTransferJournal(path)
        assert j.current is None and not os.path.exists(path)
        j.open(TXN, "aft_transfer", 0x00, CENTS, "p1", "cmd7")
        on_disk = json.load(open(path))
        assert (on_disk["txnId"], on_disk["kind"], on_disk["cents"],
                on_disk["accountId"], on_disk["cmdId"]) == (
            TXN, "aft_transfer", CENTS, "p1", "cmd7")
        j.mark_reported("unconfirmed")
        again = OpenTransferJournal(path)            # a restart
        assert again.current["txnId"] == TXN
        assert again.current["reported"] == "unconfirmed"
        again.close()
        assert again.current is None and not os.path.exists(path)
        assert OpenTransferJournal(path).current is None

    def test_no_path_keeps_it_in_memory(self, tmp_path):
        j = OpenTransferJournal(None)
        j.open(TXN, "aft_transfer", 0x00, CENTS)
        assert j.current["txnId"] == TXN
        j.close()
        assert j.current is None and list(tmp_path.iterdir()) == []

    @pytest.mark.parametrize("junk", ["not json", "[]", "{}",
                                      '{"txnId": ""}', '{"txnId": 7}'])
    def test_a_file_it_cannot_read_is_set_aside_not_trusted(self, tmp_path,
                                                            junk):
        path = tmp_path / "aft_open_transfer.json"
        path.write_text(junk)
        j = OpenTransferJournal(str(path))
        assert j.current is None and j.corrupt
        assert not path.exists()
        assert [p.name for p in tmp_path.iterdir()][0].startswith(
            "aft_open_transfer.json.corrupt-")

    def test_if_it_cannot_be_written_the_transfer_is_not_sent(self, tmp_path):
        blocker = tmp_path / "data"
        blocker.write_text("a file where the folder should be")
        journal = OpenTransferJournal(str(blocker / "aft_open_transfer.json"))
        machine, port, poller, host, clock = _rig(journal=journal)
        result = host.transfer(_push())
        assert result.outcome is AFTOutcome.NOT_SAVED and not result.ok
        assert port.sent_frames == []                # nothing at all went out
        assert machine.started == []


# ---------------------------------------------------------------------------
# Item 2 — the transfer is followed to its end
# ---------------------------------------------------------------------------

class TestFollowedToItsEnd:
    def test_what_the_machine_does_when_nobody_reads_the_status(self):
        """The fault, on a machine that keeps the document. Our host gives
        up after its wait and one declined cancel; the transfer then
        completes. With no follow-up: exception 69 every fifteen seconds,
        no lock, and every later transfer answered C0."""
        machine, port, poller, host, clock = _rig()
        _start_unconfirmed(host)
        machine.complete()
        assert machine.credits == CENTS              # the credits ARE there
        for _ in range(3):
            assert port.transact(bytes([0x81])) == b"\x69"
            port.transact(bytes([0x80]))
            machine.fifteen_seconds_pass()
        port.transact(frame(1, 0x74, bytes([0x00, 0x01, 0x05, 0x00])))
        assert machine.locked is False and machine.locks_refused == 1

    def test_exception_69_brings_the_late_result(self):
        machine, port, poller, host, clock = _rig()
        _start_unconfirmed(host, kind="aft_transfer", account_id="p1",
                           cmd_id="cmd7")
        assert host.journal.current["txnId"] == TXN
        assert host.journal.current["reported"] == "unconfirmed"
        assert host.service_open() == []             # too soon, no 69 yet
        machine.complete()
        poller.poll_once()                           # 69 comes off the FIFO
        assert host.complete_seen is True
        ended = host.service_open()
        assert len(ended) == 1
        e = ended[0]
        assert (e["ok"], e["outcome"], e["amountCents"]) == (
            True, "completed", CENTS)
        assert (e["rec"]["txnId"], e["rec"]["accountId"], e["rec"]["cmdId"],
                e["rec"]["kind"]) == (TXN, "p1", "cmd7", "aft_transfer")
        assert host.journal.current is None
        # the machine's cycle ends once that answer is acknowledged
        poller.poll_once()
        assert machine.cycle is None and machine.last["txn"] == TXN
        machine.fifteen_seconds_pass()
        assert port.transact(bytes([0x81])) == b"\x00"   # no more 69
        assert host.service_open() == []

    def test_with_no_exception_the_wait_asks_anyway(self):
        machine, port, poller, host, clock = _rig()
        _start_unconfirmed(host)
        machine.complete()
        machine._69_due = False                      # the 69 is never seen
        waits = aft_handler.OPEN_RETRY_SEC
        clock.tick(waits[0] - 0.1)
        assert host.service_open() == []
        clock.tick(0.2)
        ended = host.service_open()
        assert [e["outcome"] for e in ended] == ["completed"]

    def test_while_it_is_still_pending_the_wait_grows(self):
        machine, port, poller, host, clock = _rig()
        _start_unconfirmed(host)
        asked = []
        for _ in range(5):
            before = len(port.sent_frames)
            clock.tick(1.0)
            for _ in range(60):                      # never spin for ever
                if len(port.sent_frames) != before:
                    break
                clock.tick(1.0)
                assert host.service_open() == []
            assert len(port.sent_frames) != before, "it stopped asking"
            asked.append(clock())
        gaps = [round(b - a) for a, b in zip(asked, asked[1:])]
        assert gaps == [5, 15, 15, 15]
        assert host.journal.current is not None      # never dropped

    def test_a_new_transfer_does_not_start_while_one_is_open(self):
        machine, port, poller, host, clock = _rig()
        _start_unconfirmed(host)
        sent = len(_initiates(port))
        result = host.transfer(_push(txn="CN999999999000002", cents=500))
        assert result.outcome is AFTOutcome.UNRESOLVED_EARLIER
        assert TXN in result.detail and "nothing sent" in result.detail
        assert len(_initiates(port)) == sent         # no second 0x72
        assert machine.refused_while_open == 0
        assert host.journal.current["txnId"] == TXN  # the first is kept

    def test_asking_first_lets_the_next_one_through_when_it_has_ended(self):
        machine, port, poller, host, clock = _rig(honours_cancel=False)
        _start_unconfirmed(host)
        machine.complete()
        machine._69_due = False
        result = host.transfer(_push(txn="CN999999999000002", cents=500))
        # the first ended on the way in and is waiting to be reported...
        ended = host.service_open()
        assert [(e["rec"]["txnId"], e["outcome"]) for e in ended] == [
            (TXN, "completed")]
        # ...and the second went out as a transfer of its own
        assert machine.started == [TXN, "CN999999999000002"]
        assert result.outcome is AFTOutcome.UNCONFIRMED
        assert host.journal.current["txnId"] == "CN999999999000002"

    def test_a_cancel_the_machine_honours_is_an_end_at_once(self):
        machine, port, poller, host, clock = _rig(honours_cancel=True)
        result = host.transfer(_push())
        assert result.outcome is AFTOutcome.ROLLED_BACK
        assert host.journal.current is None and machine.credits == 0

    def test_a_poll_the_machine_never_heard_ends_as_not_taken(self):
        machine, port, poller, host, clock = _rig(hears_initiate=False)
        _start_unconfirmed(host)
        clock.tick(aft_handler.OPEN_RETRY_SEC[0] + 0.1)
        ended = host.service_open()
        assert [(e["ok"], e["outcome"], e["amountCents"])
                for e in ended] == [(False, "not_taken", 0)]
        assert host.journal.current is None and machine.credits == 0

    def test_a_cash_out_that_lands_late_is_reported_with_its_amount(self):
        machine, port, poller, host, clock = _rig(credits=CENTS)
        _start_unconfirmed(host, _pull(), kind="aft_cashout",
                           account_id="p1", cmd_id="cashout-1")
        machine.complete()
        assert machine.credits == 0                  # it left the machine
        poller.poll_once()
        e = host.service_open()[0]
        assert (e["rec"]["kind"], e["ok"], e["outcome"],
                e["amountCents"]) == ("aft_cashout", True, "completed", CENTS)

    def test_exception_69_with_nothing_of_ours_open_is_still_read(self):
        machine, port, poller, host, clock = _rig()
        machine.cycle = {"txn": "FROM-BEFORE", "type": 0x00, "cents": 700,
                         "status": 0x00}
        machine._69_due = True
        poller.poll_once()
        ended = host.service_open()
        assert len(ended) == 1 and ended[0]["rec"] is None
        assert ended[0]["txnId"] == "FROM-BEFORE"
        poller.poll_once()
        assert machine.cycle is None                 # its cycle is over


    def test_a_repeated_69_for_a_transfer_we_saw_end_reports_nothing(self):
        machine, port, poller, host, clock = _rig()
        _start_unconfirmed(host)
        machine.complete()
        poller.poll_once()
        assert len(host.service_open()) == 1         # its end, once
        poller.poll_once()                           # acknowledged: closed
        assert machine.cycle is None
        machine.cycle, machine.last = machine.last, None   # a machine that
        machine._69_due = True                       # raises it once more
        poller.poll_once()
        assert host.complete_seen is True
        assert host.service_open() == []             # read again, not news
        assert host.complete_seen is False


# ---------------------------------------------------------------------------
# Across a restart
# ---------------------------------------------------------------------------

class TestAcrossARestart:
    def test_the_next_start_finishes_what_the_last_one_began(self, tmp_path):
        path = str(tmp_path / "aft_open_transfer.json")
        machine, port, poller, host, clock = _rig(
            journal=OpenTransferJournal(path))
        _start_unconfirmed(host, kind="aft_transfer", account_id="house",
                           cmd_id="cmd1")
        assert os.path.exists(path)
        machine.complete()                           # while we are "down"
        # a new process: new poller, new host, the same file and machine
        port2 = ImpliedAckTransport(MockSASSerialPort(machine))
        poller2 = SASPoller(port2, address=1, meters=[])
        host2 = AFTHost(poller2, asset_number=machine.asset,
                        registration_key=machine.key, sleep=NO_SLEEP,
                        journal=OpenTransferJournal(path), clock=_Clock())
        assert host2.journal.current["txnId"] == TXN
        poller2.poll_once()          # the loop's first poll (it syncs first)
        ended = host2.service_open()                 # ...then asks at once
        assert [(e["rec"]["txnId"], e["rec"]["cmdId"], e["outcome"],
                 e["amountCents"]) for e in ended] == [
            (TXN, "cmd1", "completed", CENTS)]
        assert not os.path.exists(path)

    def test_a_crash_before_the_poll_went_out_ends_as_not_taken(self,
                                                                tmp_path):
        path = str(tmp_path / "aft_open_transfer.json")
        OpenTransferJournal(path).open(TXN, "aft_transfer", 0x00, CENTS)
        machine, port, poller, host, clock = _rig(
            journal=OpenTransferJournal(path))
        ended = host.service_open()
        assert [(e["outcome"], e["amountCents"]) for e in ended] == [
            ("not_taken", 0)]
        assert machine.credits == 0 and not os.path.exists(path)


# ---------------------------------------------------------------------------
# What the hub is told
# ---------------------------------------------------------------------------

class _Reporter:
    def __init__(self):
        self.records = []

    def record_result(self, record):
        self.records.append(record)


class TestWhatTheHubIsTold:
    def _ended(self, kind="aft_transfer", ok=True, outcome="completed",
               amount=CENTS):
        return {"rec": {"txnId": TXN, "kind": kind, "cents": CENTS,
                        "accountId": "p1", "cmdId": "cmd7"},
                "ok": ok, "outcome": outcome, "amountCents": amount,
                "detail": "full transfer successful"}

    def test_a_late_push_has_the_shape_the_hub_books(self):
        """g2s_host._settle_aft_credit_pushes: type aft_transfer, ok,
        outcome completed/partial, a txnId, amountCents > 0, accountId."""
        r = sas_host.late_transfer_record(self._ended())
        assert (r["type"], r["ok"], r["outcome"], r["txnId"],
                r["amountCents"], r["accountId"], r["cents"]) == (
            "aft_transfer", True, "completed", TXN, CENTS, "p1", CENTS)
        assert r["id"] == f"late-{TXN}" and r["lateFor"] == "cmd7"
        assert r["late"] is True and r["result"] == "ack"
        assert r["detail"].startswith("settled late — ")

    def test_a_late_cash_out_too(self):
        r = sas_host.late_transfer_record(self._ended(kind="aft_cashout"))
        assert (r["type"], r["ok"], r["outcome"], r["txnId"],
                r["amountCents"], r["accountId"]) == (
            "aft_cashout", True, "completed", TXN, CENTS, "p1")
        assert "cents" not in r

    def test_an_end_that_moved_nothing_books_nothing(self):
        r = sas_host.late_transfer_record(self._ended(
            ok=False, outcome="not_taken", amount=0))
        assert (r["ok"], r["outcome"], r["amountCents"], r["result"]) == (
            False, "not_taken", 0, "not_taken")

    def test_it_reaches_the_report(self):
        reporter = _Reporter()
        sas_host.note_transfer_end(self._ended(), reporter)
        assert [r["txnId"] for r in reporter.records] == [TXN]

    def test_a_transfer_that_was_never_ours_is_logged_not_reported(self):
        reporter = _Reporter()
        sas_host.note_transfer_end(
            {"rec": None, "ok": True, "outcome": "foreign",
             "txnId": "FROM-BEFORE", "amountCents": 700,
             "detail": "exception 69 for a transfer this satellite has no "
                       "record of starting"}, reporter)
        assert reporter.records == []


# ---------------------------------------------------------------------------
# The whole satellite program: sas_host.main() against a stub hub
# ---------------------------------------------------------------------------

_FULL_RUN = {}


def _run_late_push():
    """One hub push command against a by-the-book machine that holds the
    transfer pending, declines the cancel, and completes it five general
    polls after our host has given up. Takes about four seconds: the
    transfer's own wait runs on the real clock."""
    if _FULL_RUN:
        return _FULL_RUN
    from test_sas_host_commands import _run_scripted
    served = {"done": False}

    def script(i, reports):
        if not served["done"] and any(r.get("online") for r in reports):
            served["done"] = True
            return {"commands": [{"id": "push-1", "type": "aft_transfer",
                                  "cents": 2500, "accountId": "p1"}]}
        return {}

    saved = aft_handler.OPEN_RETRY_SEC
    aft_handler.OPEN_RETRY_SEC = (0.2, 0.2, 0.2)
    try:
        run = _run_scripted(
            "latepush", lambda: ByTheBookAFTMachine(complete_after=5),
            script, max_polls=500)
    finally:
        aft_handler.OPEN_RETRY_SEC = saved
    _FULL_RUN.update(run)
    return _FULL_RUN


class TestTheWholeSatelliteProgram:
    def test_the_command_answers_unconfirmed_first(self):
        run = _run_late_push()
        first = run["by_id"]["push-1"]
        assert (first["type"], first["ok"], first["outcome"],
                first["amountCents"]) == ("aft_transfer", False,
                                          "unconfirmed", 0)
        assert first["accountId"] == "p1"

    def test_then_the_loop_reports_the_late_end_with_the_same_txn(self):
        run = _run_late_push()
        first = run["by_id"]["push-1"]
        late = run["by_id"][f"late-{first['txnId']}"]
        assert (late["type"], late["ok"], late["outcome"], late["txnId"],
                late["amountCents"], late["accountId"], late["lateFor"]) == (
            "aft_transfer", True, "completed", first["txnId"], 2500, "p1",
            "push-1")
        # ...and the hub was sent it in a report
        assert any(r.get("id") == late["id"]
                   for rep in run["reports"]
                   for r in rep.get("commandResults") or [])

    def test_the_machine_ends_with_the_credits_and_a_closed_cycle(self):
        run = _run_late_push()
        machine = run["machine"]
        assert machine.credits == 2500
        assert machine.cycle is None and len(machine.started) == 1
        assert machine.refused_while_open == 0
