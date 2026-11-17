"""
Records a machine hands over once: the 0x4D validation record and the 0x1B
handpay record. SAS 6.02 audit of 2026-11-16, items 1 and 3.

Item 1. A machine deletes one of these when the host's next poll is an
implied ACK (§3.1 Table 3.1, §15.10, §7.8.1). When the answer arrived
damaged, the document has the host send the SAME poll again (§3.2). We sent
a different one, which is the ACK, so the only copy of a printed ticket was
thrown away by the machine and the hub refused the paper as an unknown
number.

Item 3. The validation buffer also holds handpays, handpay receipts and
ticket-in records, and the machine raises the same exceptions for them
(§15, Table 15.13c). Every one of them was stored as a redeemable ticket.
A restricted ("playable only") ticket IS a ticket and is still stored.

These run against tests/by_the_book.py, a pretend machine that keeps the
document's rules, through the same ImpliedAckTransport the satellite uses.
"""

import collections
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import sas_host
from by_the_book import CUT, DAMAGE, DROP, ByTheBookMachine, frame
from core.sas_implied_ack import (
    ACK_POLL, NACK_SENDS, ImpliedAckTransport, transact_reread)
from core.sas_poller import SASPoller
from core.sas_ticket_store import ISSUED, TicketStore
from core.sas_tito_host import (
    CASH_TICKET_TYPES, PLAYABLE_ONLY_TICKET_TYPES, VALIDATION_TYPES,
    SASTITOHost, TicketCaptureOutcome, capture_printed_ticket,
    validation_kind)
from transport.serial.sas_serial import MockSASSerialPort

NO_SLEEP = lambda _t: None
VN = "0189157427998805"          # the Bluebird's ticket of 2026-11-16
AMOUNT = 942500                  # $9,425.00
GENERAL = bytes([0x81])          # general poll, address 1
READ = frame(1, 0x4D, b"\x00")   # 0x4D function 00
LOOK = frame(1, 0x4D, b"\xFF")   # 0x4D function FF
READ_3D = bytes([0x01, 0x3D])    # the standard-validation read (type R)
READ_1B = bytes([0x01, 0x1B])


def _store(tmp_path):
    return TicketStore(str(tmp_path / "tickets.json"))


def _line(machine):
    return ImpliedAckTransport(MockSASSerialPort(machine))


def _sent(port):
    return [bytes(f) for f, _ in port.sent_frames]


def _printed(**kw):
    """A by-the-book machine that has just printed one ticket and had its
    0x3D taken by a general poll, as the poller would have."""
    ticket = {k: kw.pop(k) for k in ("validation_type", "ticket_number")
              if k in kw}
    machine = ByTheBookMachine(**kw)
    machine.print_ticket(VN, AMOUNT, **{"ticket_number": 42, **ticket})
    port = _line(machine)
    assert port.transact(GENERAL)[0] in (0x3D, 0x3E)
    return machine, port


def _capture(port, store):
    return capture_printed_ticket(port, 1, store, sleep=NO_SLEEP)


# ---------------------------------------------------------------------------
# §3.2 — ask again
# ---------------------------------------------------------------------------

GOOD = frame(1, 0x4D, bytes(31))
BAD = GOOD[:-1] + bytes([GOOD[-1] ^ 0xFF])


class _Scripted:
    def __init__(self, *answers):
        self.answers, self.sent = list(answers), []

    def transact(self, raw, *a, **k):
        self.sent.append(bytes(raw))
        return self.answers.pop(0) if self.answers else b""


class TestAskAgain:
    def test_an_intact_answer_is_taken_at_once(self):
        t = _Scripted(GOOD)
        assert transact_reread(t, READ) == (GOOD, True, 1)
        assert t.sent == [READ]

    @pytest.mark.parametrize("first", [
        BAD,                              # bad CRC
        GOOD[:17],                        # cut short
        b"",                              # never arrived
        bytes([0x01, 0x00]),              # busy
        frame(2, 0x4D, bytes(31)),        # another machine's
        frame(1, 0x3D, bytes(9)),         # another command's
    ])
    def test_anything_else_is_asked_for_again_with_the_same_poll(self, first):
        t = _Scripted(first, GOOD)
        resp, heard, sent = transact_reread(t, READ)
        assert (resp, sent) == (GOOD, 2)
        assert t.sent == [READ, READ]

    def test_three_sends_and_no_more(self):
        t = _Scripted(BAD, BAD, BAD, GOOD, GOOD)
        resp, heard, sent = transact_reread(t, READ)
        assert (resp, heard, sent) == (BAD, True, NACK_SENDS) == (BAD, True, 3)
        assert t.sent == [READ] * 3

    def test_silence_throughout_is_told_apart_from_damage(self):
        assert transact_reread(_Scripted(), READ) == (b"", False, 3)
        assert transact_reread(_Scripted(b"", BAD), READ)[1] is True

    def test_a_general_poll_is_never_repeated_here(self):
        t = _Scripted(b"", b"\x00")
        assert transact_reread(t, GENERAL) == (b"", False, 1)

    def test_a_fourth_identical_poll_never_goes_out_in_a_row(self):
        """§3.2 defines three. A by-the-book machine has stopped answering
        that command by then; the address-0 poll restarts its count."""
        machine = ByTheBookMachine(knows_look_ahead=False)
        machine.print_ticket(VN, AMOUNT)
        port = _line(machine)
        assert transact_reread(port, LOOK) == (b"", False, 3)
        assert len(transact_reread(port, READ)[0]) == 35     # same command
        assert _sent(port) == [LOOK, LOOK, LOOK, ACK_POLL, READ]
        assert (port.acks_sent, port.resyncs_sent) == (0, 1)
        # without the guard that fourth 0x4D is met with silence
        bare = MockSASSerialPort(ByTheBookMachine(knows_look_ahead=False))
        bare.machine.print_ticket(VN, AMOUNT)
        transact_reread(bare, LOOK)
        assert bare.transact(READ) == b""

    def test_the_repeat_goes_out_with_no_ack_in_front(self):
        """The two halves of §3 must agree: a damaged answer gets the
        repeat (NACK), an intact one gets the ACK before the same command
        is used again."""
        machine = ByTheBookMachine()
        machine.print_ticket(VN, AMOUNT)
        machine.faults = [DAMAGE]
        port = _line(machine)
        resp, heard, sent = transact_reread(port, LOOK)
        assert sent == 2 and len(resp) == 35
        assert _sent(port) == [LOOK, LOOK]              # nothing between
        transact_reread(port, LOOK)                     # a NEW look
        assert _sent(port) == [LOOK, LOOK, ACK_POLL, LOOK]


# ---------------------------------------------------------------------------
# Item 1 — the 0x4D ticket record
# ---------------------------------------------------------------------------

class TestTicketRecordSurvivesADamagedAnswer:
    def test_clean_line_look_then_read(self, tmp_path):
        machine, port = _printed()
        store = _store(tmp_path)
        result = _capture(port, store)
        assert result.outcome is TicketCaptureOutcome.RECORDED
        assert (result.via, result.validation_number, result.amount_cents,
                result.validation_type) == ("4D", VN, AMOUNT, 0x00)
        assert store.get(VN)["state"] == ISSUED
        assert store.get(VN)["ticketNumber"] == 42
        assert _sent(port) == [GENERAL, LOOK, ACK_POLL, READ]
        # read, but only once the host's next poll says so
        assert len(machine.unread()) == 1
        port.transact(GENERAL)
        assert machine.unread() == []
        machine.fifteen_seconds_pass()
        assert machine.exceptions == []

    def test_the_old_sequence_loses_the_ticket_on_this_machine(self):
        """What the code did before, poll for poll: one 0x4D read, its
        answer damaged, then the 0x3D read. On a machine that keeps the
        document that second poll is the ACK."""
        machine, port = _printed()
        machine.faults = [DAMAGE]
        assert port.transact(READ)[-1] != READ[-1] or True   # arrives damaged
        port.transact(READ_3D)                               # ...and is ACKed
        assert machine.unread() == []            # the only copy is gone
        machine.fifteen_seconds_pass()
        assert machine.exceptions == []          # and 0x3D never comes back
        assert port.transact(READ)[2:-2] == bytes(31)

    def test_one_damaged_answer_no_longer_loses_it(self, tmp_path):
        machine, port = _printed()
        machine.faults = [None, DAMAGE]          # the look arrives, the read
        store = _store(tmp_path)                 # arrives damaged
        result = _capture(port, store)
        assert result.outcome is TicketCaptureOutcome.RECORDED
        assert store.get(VN)["amountCents"] == AMOUNT
        assert "look-ahead" not in result.detail     # the read itself got it
        # the read was repeated with nothing in between, and no 0x3D went out
        assert _sent(port) == [GENERAL, LOOK, ACK_POLL, READ, READ]
        assert READ_3D not in _sent(port)
        assert len(machine.unread()) == 1        # not ACKed yet
        port.transact(GENERAL)
        assert machine.unread() == []

    @pytest.mark.parametrize("fault", [DAMAGE, DROP, CUT])
    def test_every_kind_of_bad_answer_is_asked_for_again(self, tmp_path,
                                                         fault):
        machine, port = _printed()
        machine.faults = [None, fault]
        assert _capture(port, _store(tmp_path)).outcome \
            is TicketCaptureOutcome.RECORDED

    def test_three_bad_answers_use_the_look_ahead_copy(self, tmp_path):
        machine, port = _printed()
        machine.faults = [None, DAMAGE, DAMAGE]  # third send: final NACK
        store = _store(tmp_path)
        result = _capture(port, store)
        assert result.outcome is TicketCaptureOutcome.RECORDED
        assert "did not arrive in 3 tries" in result.detail
        assert "look-ahead (FF) copy" in result.detail
        assert store.get(VN)["amountCents"] == AMOUNT
        assert _sent(port).count(READ) == 3
        # by the book the machine still holds it and says so again...
        port.transact(GENERAL)
        assert len(machine.unread()) == 1
        machine.fifteen_seconds_pass()
        assert port.transact(ACK_POLL) == b"" and port.transact(GENERAL)[0] == 0x3D
        # ...and the second capture is the same ticket, not a second one
        again = _capture(port, store)
        assert again.outcome is TicketCaptureOutcome.DUPLICATE
        assert len(store.outstanding()) == 1
        port.transact(GENERAL)
        assert machine.unread() == []

    def test_a_damaged_look_alone_changes_nothing(self, tmp_path):
        machine, port = _printed()
        machine.faults = [DAMAGE, DAMAGE]        # the look never arrives
        result = _capture(port, _store(tmp_path))
        assert result.outcome is TicketCaptureOutcome.RECORDED
        assert "the look-ahead (FF) got no usable answer" in result.detail
        assert "copy" not in result.detail       # the read itself got it

    def test_nothing_intact_is_never_acknowledged_with_a_3d_read(self,
                                                                 tmp_path):
        machine, port = _printed()
        machine.faults = [DAMAGE] * 4            # look and read, both lost
        store = _store(tmp_path)
        result = _capture(port, store)
        assert result.outcome is TicketCaptureOutcome.TIMEOUT
        assert "not acknowledged" in result.detail
        assert READ_3D not in _sent(port)        # that would have been an ACK
        assert store.outstanding() == []
        port.transact(GENERAL)
        assert len(machine.unread()) == 1        # the machine still has it
        machine.fifteen_seconds_pass()
        port.transact(ACK_POLL)
        assert port.transact(GENERAL)[0] == 0x3D
        assert _capture(port, store).outcome is TicketCaptureOutcome.RECORDED

    def test_a_machine_that_marks_the_record_read_when_it_sends_it(
            self, tmp_path):
        """Not what §15.10 says, but a code comment had it as an open
        question for the Bluebird. The damaged read is gone for good on
        such a machine; the look-ahead copy is what saves the ticket."""
        machine, port = _printed(mark_read_on="send", resends_on_nack=False)
        machine.faults = [None, DAMAGE]
        store = _store(tmp_path)
        result = _capture(port, store)
        assert result.outcome is TicketCaptureOutcome.RECORDED
        assert "answered with an empty record" in result.detail
        assert "look-ahead (FF) copy" in result.detail
        assert store.get(VN)["amountCents"] == AMOUNT

    def test_a_machine_that_counts_the_look_as_the_read(self, tmp_path):
        machine, port = _printed(peek_marks_read=True)
        store = _store(tmp_path)
        result = _capture(port, store)
        assert result.outcome is TicketCaptureOutcome.RECORDED
        assert "look-ahead (FF) copy" in result.detail
        assert store.get(VN)["amountCents"] == AMOUNT

    def test_a_machine_that_does_not_know_the_look_ahead(self, tmp_path):
        machine, port = _printed(knows_look_ahead=False)
        result = _capture(port, _store(tmp_path))
        assert result.outcome is TicketCaptureOutcome.RECORDED
        assert "the look-ahead (FF) got no usable answer" in result.detail
        # three looks is the whole exchange; the count is restarted
        # before the same command is used for the read
        assert _sent(port) == [GENERAL, LOOK, LOOK, LOOK, ACK_POLL, READ]

    def test_an_empty_buffer_is_still_no_data(self, tmp_path):
        port = _line(ByTheBookMachine())
        result = _capture(port, _store(tmp_path))
        assert result.outcome is TicketCaptureOutcome.NO_DATA
        # the 0x3D read is tried too now: a standard-validation machine may
        # answer 0x4D with zeros (see the next test)
        assert _sent(port)[-1] == READ_3D
        assert "nothing to capture" in result.detail

    def test_standard_validation_that_answers_4d_with_zeros(self, tmp_path):
        """§15.10: such a machine "must either not respond to long poll 4D
        or return all zeros in the Validation Number field". The second
        kind never got the 0x3D read, so its tickets were never captured
        (audit item 20)."""
        machine = ByTheBookMachine(answers_4d_with_zeros=True)
        machine.print_ticket("00123456", 777)
        port = _line(machine)
        port.transact(GENERAL)
        store = _store(tmp_path)
        result = _capture(port, store)
        assert (result.outcome, result.via) == (TicketCaptureOutcome.RECORDED,
                                                "3D")
        assert store.get("0000000000123456")["amountCents"] == 777

    def test_standard_validation_still_falls_back_to_the_3d_read(self,
                                                                 tmp_path):
        """A machine that never answers 0x4D at all is the one case the
        0x3D read is for. Nothing was sent, so there is nothing to ACK."""
        machine = ByTheBookMachine(speaks_4d=False)
        machine.print_ticket("00123456", 777)
        port = _line(machine)
        port.transact(GENERAL)
        store = _store(tmp_path)
        result = _capture(port, store)
        assert (result.outcome, result.via) == (TicketCaptureOutcome.RECORDED,
                                                "3D")
        assert store.get("0000000000123456")["amountCents"] == 777
        assert _sent(port) == ([GENERAL] + [LOOK] * 3 + [ACK_POLL]
                               + [READ] * 3 + [READ_3D])


# ---------------------------------------------------------------------------
# Item 3 — what the record IS
# ---------------------------------------------------------------------------

TICKETS = CASH_TICKET_TYPES | PLAYABLE_ONLY_TICKET_TYPES
NOT_TICKETS = sorted(set(VALIDATION_TYPES) - TICKETS)


class TestOnlyATicketIsStored:
    def test_the_table_is_table_15_13c(self):
        assert sorted(VALIDATION_TYPES) == [
            0x00, 0x01, 0x02, 0x03, 0x04, 0x10, 0x20, 0x40, 0x60,
            0x80, 0x81, 0x82]
        assert CASH_TICKET_TYPES == {0x00, 0x02, 0x04}
        assert PLAYABLE_ONLY_TICKET_TYPES == {0x01, 0x03}
        # what is left is exactly the handpays and the ticket-in records
        assert NOT_TICKETS == [0x10, 0x20, 0x40, 0x60, 0x80, 0x81, 0x82]
        assert {validation_kind(t)[0] for t in CASH_TICKET_TYPES} == {"cash"}
        assert {validation_kind(t)[0]
                for t in PLAYABLE_ONLY_TICKET_TYPES} == {"playable"}
        assert {validation_kind(t)[0] for t in NOT_TICKETS} == {"other"}
        assert validation_kind(0x7F)[0] == "unknown"

    @pytest.mark.parametrize("vtype", sorted(CASH_TICKET_TYPES))
    def test_a_cash_ticket_is_stored(self, tmp_path, vtype):
        machine, port = _printed(validation_type=vtype)
        store = _store(tmp_path)
        result = _capture(port, store)
        assert result.outcome is TicketCaptureOutcome.RECORDED and result.ok
        assert result.validation_type == vtype
        assert result.detail == "ticket recorded via 4D"
        assert store.get(VN)["state"] == ISSUED
        assert store.get(VN)["extra"] == {"validationType": vtype}

    @pytest.mark.parametrize("vtype", sorted(PLAYABLE_ONLY_TICKET_TYPES))
    def test_a_playable_only_ticket_is_still_a_ticket(self, tmp_path, vtype):
        """It goes back into a machine like any other ticket, so refusing
        it would strand what the player is holding. Its type is kept with
        the stored record, for the day the hub's cash desk can flag it."""
        machine, port = _printed(validation_type=vtype)
        store = _store(tmp_path)
        result = _capture(port, store)
        assert result.outcome is TicketCaptureOutcome.RECORDED and result.ok
        assert result.validation_type == vtype
        assert "(playable only)" in result.detail
        assert store.get(VN)["state"] == ISSUED
        assert store.get(VN)["amountCents"] == AMOUNT
        assert store.get(VN)["extra"] == {"validationType": vtype}

    @pytest.mark.parametrize("vtype", NOT_TICKETS)
    def test_anything_else_in_the_buffer_is_not(self, tmp_path, vtype):
        machine, port = _printed(validation_type=vtype)
        store = _store(tmp_path)
        result = _capture(port, store)
        assert result.outcome is TicketCaptureOutcome.NOT_A_TICKET
        assert not result.ok and result.record is None
        assert (result.validation_number, result.amount_cents,
                result.validation_type) == (VN, AMOUNT, vtype)
        assert VALIDATION_TYPES[vtype] in result.detail
        assert store.get(VN) is None and store.outstanding() == []
        # the record was still READ, so the machine stops asking
        port.transact(GENERAL)
        assert machine.unread() == []

    def test_a_code_the_table_does_not_have_is_kept_as_a_ticket(self,
                                                                tmp_path):
        machine, port = _printed(validation_type=0x7F)
        store = _store(tmp_path)
        result = _capture(port, store)
        assert result.outcome is TicketCaptureOutcome.RECORDED
        assert "not in Table 15.13c" in result.detail
        assert store.get(VN)["state"] == ISSUED

    def test_the_look_ahead_copy_is_sorted_the_same_way(self, tmp_path):
        machine, port = _printed(validation_type=0x20)
        machine.faults = [None, DAMAGE, DAMAGE]
        store = _store(tmp_path)
        result = _capture(port, store)
        assert result.outcome is TicketCaptureOutcome.NOT_A_TICKET
        assert store.outstanding() == []

    def test_a_handpay_with_no_receipt_through_the_poller(self, tmp_path):
        """Exception 0x3E (handpay validated) used to end as an 'issued'
        ticket for the handpay amount on the hub."""
        machine = ByTheBookMachine(counts_idle_general_polls=False)
        machine.print_ticket(VN, 125000, validation_type=0x60,
                             ticket_number=None)
        assert machine.exceptions == [0x3E]
        port = _line(machine)
        store = _store(tmp_path)
        seen = []
        poller = SASPoller(port, address=1, meters=[])
        SASTITOHost(poller, store, sleep=NO_SLEEP,
                    on_ticket_issued=lambda a, r: seen.append(r))
        poller.poll_once()
        assert [r.outcome for r in seen] == [TicketCaptureOutcome.NOT_A_TICKET]
        assert store.outstanding() == [] and store.get(VN) is None
        assert machine.unread() == []
        assert poller.state.callback_errors == 0

    def test_the_satellite_log_and_tape_say_what_it_was(self, tmp_path):
        machine, port = _printed(validation_type=0x20)
        tape = collections.deque(maxlen=20)
        result = _capture(port, _store(tmp_path))
        sas_host.note_ticket_capture(1, result, tape)
        assert len(tape) == 1 and tape[0]["code"] == "0x4D"
        assert tape[0]["label"] == ("validation record, not a ticket: "
                                    "jackpot handpay, receipt printed")
        # a real ticket adds nothing to the tape here (its 0x3D is there)
        machine, port = _printed()
        sas_host.note_ticket_capture(1, _capture(port, _store(tmp_path)), tape)
        assert len(tape) == 1


# ---------------------------------------------------------------------------
# Item 1 — the 0x1B handpay record
# ---------------------------------------------------------------------------

def _handpay_rig():
    machine = ByTheBookMachine(counts_idle_general_polls=False)
    machine.handpay()
    port = _line(machine)
    got = []
    poller = SASPoller(port, address=1, meters=[],
                       on_handpay_info=lambda a, info: got.append(info))
    return machine, port, poller, got


class TestHandpayRecordSurvivesADamagedAnswer:
    def test_clean_line(self):
        machine, port, poller, got = _handpay_rig()
        poller.poll_once()
        assert len(got) == 1 and got[0].data == machine.handpays[0]["data"]
        assert machine.handpays[0]["reported"] is True
        assert _sent(port).count(READ_1B) == 1

    def test_the_old_sequence_loses_the_record_on_this_machine(self):
        machine, port, poller, got = _handpay_rig()
        machine.faults = [DAMAGE]
        port.transact(ACK_POLL)
        assert port.transact(GENERAL) == b"\x51"
        port.transact(READ_1B)                   # arrives damaged
        port.transact(ACK_POLL)                  # the drain loop's next poll
        assert machine.handpays[0]["reported"] is True   # gone, never read

    def test_one_damaged_answer_is_asked_for_again(self):
        machine, port, poller, got = _handpay_rig()
        machine.faults = [DAMAGE]
        poller.poll_once()
        assert len(got) == 1 and got[0].data == machine.handpays[0]["data"]
        sent = _sent(port)
        first = sent.index(READ_1B)
        assert sent[first:first + 3] == [READ_1B, READ_1B, ACK_POLL]
        assert machine.handpays[0]["reported"] is True

    def test_three_bad_answers_leave_it_in_the_machine_for_next_time(self):
        machine, port, poller, got = _handpay_rig()
        machine.faults = [DAMAGE, DAMAGE]        # third send: final NACK
        poller.poll_once()
        assert got == []
        assert _sent(port).count(READ_1B) == 3
        assert machine.handpays[0]["reported"] is False
        machine.fifteen_seconds_pass()           # exception 51 again
        poller.poll_once()
        assert len(got) == 1
        assert machine.handpays[0]["reported"] is True
