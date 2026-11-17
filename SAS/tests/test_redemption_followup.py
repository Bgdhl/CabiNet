"""
A ticket redemption is followed to its end. SAS 6.02 audit of 2026-11-16,
items 5 (the exception 68 half), 19 and 37.

After the machine accepts a redemption (status 40) it stacks the ticket or
hands it back, raises exception 68, and "must reissue exception 68 every
fifteen seconds until the host polls for and ACKs the completion status.
... It is the responsibility of the host to properly complete a ticket
transaction" (§15.12). We waited about two seconds and gave up for good: a
slower stacker left the hub's ticket redeemPending for ever with the player
already credited, and a ticket handed back could then be used only at that
one machine.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from core.sas_implied_ack import ImpliedAckTransport
from core.sas_poller import SASPoller
from core.sas_protocol import sas_crc
from core.sas_ticket_store import ISSUED, REDEEM_PENDING, REDEEMED
from core.sas_tito_host import (
    CMD_REDEEM_TICKET, CMD_SEND_TICKET_VALIDATION_DATA, REDEEM_INTERROGATE,
    REDEMPTION_RETRY_SEC, TICKET_INSERTED_EXCEPTION,
    TICKET_TRANSFER_COMPLETE_EXCEPTION, RedemptionOutcome, SASTITOHost,
    redeem_status_kind)
from test_sas_tito_host import AMOUNT, VN16, TITOMachine, _frame, _store
from transport.serial.sas_serial import MockSASSerialPort

NO_SLEEP = lambda _t: None


class _Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t

    def tick(self, sec):
        self.t += sec


class SlowStacker(TITOMachine):
    """Accepts the redemption (status 40) and then takes its time. The
    ticket is stacked, or handed back, only when the test says so — after
    our host's two-second wait is over."""

    def __init__(self, announce=True, **kw):
        super().__init__(redeem_mode="pending", **kw)
        self.announce = announce
        self.interrogations = 0
        self._amount = 0

    def _complete_later(self, amount, vn):
        self._amount = amount              # nothing happens yet

    def stack(self):
        self.credited_cents += self._amount
        self.final_status = 0x00
        if self.announce:
            self.queue(TICKET_TRANSFER_COMPLETE_EXCEPTION)

    def hand_back(self, status=0x8B):      # validator failure
        self.returned_ticket = True
        self.final_status = status
        if self.announce:
            self.queue(TICKET_TRANSFER_COMPLETE_EXCEPTION)

    def _handle_71(self, frame):
        if frame[2:-2][1] == REDEEM_INTERROGATE:
            self.interrogations += 1
        return super()._handle_71(frame)


def _rig(tmp_path, machine):
    store = _store(tmp_path, (VN16, AMOUNT))
    port = ImpliedAckTransport(MockSASSerialPort(machine))
    poller = SASPoller(port, address=1, meters=[])
    seen = []
    host = SASTITOHost(poller, store, sleep=NO_SLEEP,
                       on_redemption=lambda a, r: seen.append(r))
    host.clock = _Clock()
    return store, port, poller, host, seen


def _insert(machine, poller):
    machine.queue(TICKET_INSERTED_EXCEPTION)
    poller.poll_once()


class TestStatusKinds:
    @pytest.mark.parametrize("status,kind", [
        (0x00, "redeemed"), (0x01, "redeemed"), (0x02, "redeemed"),
        (0x20, "waiting"), (0x40, "pending"),
        (0x80, "failed"), (0x81, "failed"), (0x88, "failed"), (0x8B, "failed"),
        (0xC0, "unknown"), (0xFF, "unknown"), (None, "unknown"),
        (0x60, "unknown"), (0xA0, "unknown"),
    ])
    def test_table_15_12d_by_its_top_three_bits(self, status, kind):
        assert redeem_status_kind(status) == kind


class TestFollowedToItsEnd:
    def test_a_slow_stacker_used_to_leave_the_ticket_pending_for_ever(
            self, tmp_path):
        """The first verdict is unchanged: after the wait, UNCONFIRMED and
        the hub row held. What is new is that it is now remembered."""
        machine = SlowStacker(escrow={"vn": VN16, "amount": AMOUNT})
        store, port, poller, host, seen = _rig(tmp_path, machine)
        _insert(machine, poller)
        assert [r.outcome for r in seen] == [RedemptionOutcome.UNCONFIRMED]
        assert store.get(VN16)["state"] == REDEEM_PENDING
        assert host.open_redemption == {"vn": VN16, "amount": AMOUNT}

    def test_exception_68_brings_the_end(self, tmp_path):
        machine = SlowStacker(escrow={"vn": VN16, "amount": AMOUNT})
        store, port, poller, host, seen = _rig(tmp_path, machine)
        _insert(machine, poller)
        assert host.service_open_redemption() is None     # too soon, no 68
        machine.stack()
        poller.poll_once()                                # 68 off the FIFO
        assert host.transfer_complete_seen is True
        end = host.service_open_redemption()
        assert end.outcome is RedemptionOutcome.REDEEMED
        assert (end.validation_number, end.amount_cents) == (VN16, AMOUNT)
        assert "ended late" in end.detail
        assert store.get(VN16)["state"] == REDEEMED
        assert host.open_redemption is None
        assert [r.outcome for r in seen] == [RedemptionOutcome.UNCONFIRMED,
                                             RedemptionOutcome.REDEEMED]
        assert host.service_open_redemption() is None     # once

    def test_with_no_exception_the_wait_asks_anyway(self, tmp_path):
        machine = SlowStacker(announce=False,
                              escrow={"vn": VN16, "amount": AMOUNT})
        store, port, poller, host, seen = _rig(tmp_path, machine)
        _insert(machine, poller)
        machine.stack()
        host.clock.tick(REDEMPTION_RETRY_SEC[0] - 0.1)
        assert host.service_open_redemption() is None
        host.clock.tick(0.2)
        assert host.service_open_redemption().outcome \
            is RedemptionOutcome.REDEEMED
        assert store.get(VN16)["state"] == REDEEMED

    def test_while_the_ticket_is_still_moving_the_wait_grows(self, tmp_path):
        machine = SlowStacker(escrow={"vn": VN16, "amount": AMOUNT})
        store, port, poller, host, seen = _rig(tmp_path, machine)
        _insert(machine, poller)
        asked = []
        for _ in range(4):
            before = machine.interrogations
            for _ in range(60):
                if machine.interrogations != before:
                    break
                host.clock.tick(1.0)
                assert host.service_open_redemption() is None
            assert machine.interrogations != before, "it stopped asking"
            asked.append(host.clock())
        assert [round(b - a) for a, b in zip(asked, asked[1:])] == [5, 15, 15]
        assert store.get(VN16)["state"] == REDEEM_PENDING  # still held

    def test_a_ticket_handed_back_late_goes_back_to_issued(self, tmp_path):
        machine = SlowStacker(escrow={"vn": VN16, "amount": AMOUNT})
        store, port, poller, host, seen = _rig(tmp_path, machine)
        _insert(machine, poller)
        machine.hand_back(0x8B)
        poller.poll_once()
        end = host.service_open_redemption()
        assert end.outcome is RedemptionOutcome.MACHINE_REJECTED
        assert "0x8B" in end.detail
        assert store.get(VN16)["state"] == ISSUED          # usable again
        assert machine.credited_cents == 0

    def test_a_satellite_restarted_in_the_middle_still_closes_it(self,
                                                                 tmp_path):
        """A new process does not remember the ticket. The machine's answer
        names it."""
        machine = SlowStacker(escrow={"vn": VN16, "amount": AMOUNT})
        store, port, poller, host, seen = _rig(tmp_path, machine)
        _insert(machine, poller)
        machine.stack()
        port2 = ImpliedAckTransport(MockSASSerialPort(machine))
        poller2 = SASPoller(port2, address=1, meters=[])
        host2 = SASTITOHost(poller2, store, sleep=NO_SLEEP)
        assert host2.open_redemption is None
        poller2.poll_once()                                # hears the 68
        end = host2.service_open_redemption()
        assert end.outcome is RedemptionOutcome.REDEEMED
        assert end.validation_number == VN16
        assert store.get(VN16)["state"] == REDEEMED

    def test_the_next_ticket_is_not_read_before_the_last_one_is_closed(
            self, tmp_path):
        """"If the host sends long poll 70 without polling for the
        completion status of the previous ticket, that status will be
        lost." """
        machine = SlowStacker(announce=False,
                              escrow={"vn": VN16, "amount": AMOUNT})
        store, port, poller, host, seen = _rig(tmp_path, machine)
        _insert(machine, poller)
        machine.stack()
        frames_before = len(port.sent_frames)
        _insert(machine, poller)                           # the next ticket
        sent = [bytes(f) for f, _ in port.sent_frames[frames_before:]]
        first_70 = next(i for i, f in enumerate(sent)
                        if len(f) == 2 and f[1] == CMD_SEND_TICKET_VALIDATION_DATA)
        first_ff = next(i for i, f in enumerate(sent)
                        if len(f) > 3 and f[1] == CMD_REDEEM_TICKET
                        and f[3] == REDEEM_INTERROGATE)
        assert first_ff < first_70
        assert store.get(VN16)["state"] == REDEEMED


class TestAnswersThatSayNothing:
    @pytest.mark.parametrize("status", [0xC0, 0xFF])
    def test_c0_and_ff_no_longer_release_the_hold(self, tmp_path, status):
        """C0 is "not compatible with current redemption cycle (ignored)",
        FF "no validation information". Neither says the ticket was turned
        away, so the hub's hold stays."""
        class Odd(TITOMachine):
            def _handle_71(self, frame):
                return _frame(self.address, CMD_REDEEM_TICKET,
                              bytes([0x01, status]))

        machine = Odd(escrow={"vn": VN16, "amount": AMOUNT})
        store, port, poller, host, seen = _rig(tmp_path, machine)
        _insert(machine, poller)
        assert [r.outcome for r in seen] == [RedemptionOutcome.UNCONFIRMED]
        assert store.get(VN16)["state"] == REDEEM_PENDING

    def test_a_real_refusal_still_does(self, tmp_path):
        machine = TITOMachine(escrow={"vn": VN16, "amount": AMOUNT},
                              redeem_mode="reject")
        store, port, poller, host, seen = _rig(tmp_path, machine)
        _insert(machine, poller)
        assert [r.outcome for r in seen] == [
            RedemptionOutcome.MACHINE_REJECTED]
        assert store.get(VN16)["state"] == ISSUED


class TestAskedAgain:
    def test_a_damaged_escrow_read_is_asked_for_again(self, tmp_path):
        machine = TITOMachine(escrow={"vn": VN16, "amount": AMOUNT})
        spoiled = {"done": False}

        def line(frame, wakeup):
            answer = machine(frame, wakeup)
            if len(frame) == 2 and frame[1] == CMD_SEND_TICKET_VALIDATION_DATA \
                    and not spoiled["done"]:
                spoiled["done"] = True
                return answer[:-1] + bytes([answer[-1] ^ 0xFF])
            return answer

        store = _store(tmp_path, (VN16, AMOUNT))
        port = ImpliedAckTransport(MockSASSerialPort(line))
        poller = SASPoller(port, address=1, meters=[])
        seen = []
        SASTITOHost(poller, store, sleep=NO_SLEEP,
                    on_redemption=lambda a, r: seen.append(r))
        machine.queue(TICKET_INSERTED_EXCEPTION)
        poller.poll_once()
        assert [r.outcome for r in seen] == [RedemptionOutcome.REDEEMED]
        assert store.get(VN16)["state"] == REDEEMED

    def test_a_machine_still_waiting_for_its_71_is_sent_it_again(self,
                                                                 tmp_path):
        """Status 20 at the interrogation: our authorisation never arrived.
        It used to end UNCONFIRMED with the ticket sitting in escrow."""
        class DeafOnce(TITOMachine):
            heard = 0

            def _handle_71(self, frame):
                code = frame[2:-2][1]
                if code == REDEEM_INTERROGATE and self.final_status is None:
                    body = bytes([0x01, 0x20])
                    return _frame(self.address, CMD_REDEEM_TICKET, body)
                if code != REDEEM_INTERROGATE:
                    self.heard += 1
                    if self.heard <= 3:            # the first three are lost
                        return b""
                return super()._handle_71(frame)

        machine = DeafOnce(escrow={"vn": VN16, "amount": AMOUNT})
        store, port, poller, host, seen = _rig(tmp_path, machine)
        _insert(machine, poller)
        assert [r.outcome for r in seen] == [RedemptionOutcome.REDEEMED]
        assert "second authorisation" in seen[0].detail
        assert machine.credited_cents == AMOUNT
        assert store.get(VN16)["state"] == REDEEMED
