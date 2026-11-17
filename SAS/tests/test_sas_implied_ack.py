"""
Tests for the implied-ACK guard (core/sas_implied_ack.py).

The failure it fixes came from a tester's Ainsworth A560 (2026-11-16): AFT
registration sends 73/FF, 73/00, 73/01 with nothing in between, and the
machine never answered the third. StrictMachine below keeps the SAS 6.02
§3.2 poll counter that explains it, so the failure is reproduced here with
the exact journal line the tester sent, and then shown fixed.

That model is built from the spec and from what the A560 was seen to do. It
is not a capture from an A560: these tests prove the host now follows
§3.1-3.2, not that the A560 registers. Only the machine can show that.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import sas_host
from core.sas_implied_ack import (ACK_POLL, ImpliedAckTransport,
                                  answer_intact, long_poll_key)
from core.sas_poller import SASPoller
from core.sas_protocol import sas_crc
from modules.aft.aft_handler import (AFT_CMD_LOCK_STATUS, AFT_CMD_REGISTER,
                                     AFTHost, AFTOutcome, REG_CODE_INITIALIZE,
                                     REG_CODE_READ_CURRENT, REG_CODE_REGISTER,
                                     aft_transfer)
from transport.serial.sas_serial import MockSASSerialPort

from test_aft_handler import (AFTMachine, ASSET, KEY, NO_SLEEP, POS,
                              _request)


class StrictMachine:
    """A pretend machine that keeps the §3.2 count of consecutive polls.

    A long poll command it has just answered may come once more without an
    implied ACK — it takes that as a new poll, as the A560 did with 73/00.
    The THIRD in a row gets no answer and is not acted on (§3.2: the machine
    "may respond" to a final NACK). A general poll, a different long poll
    command, or anything sent to another address is the implied ACK that
    starts the count again (§3.1, Table 3.1)."""

    def __init__(self, inner, address=1):
        self.inner = inner
        self.address = address
        self.last_command = None
        self.run = 0
        self.ignored = []

    def __call__(self, frame, wakeup):
        key = long_poll_key(frame)
        if key is None or key[0] != self.address:
            self.last_command, self.run = None, 0
            return self.inner(frame, wakeup)
        if key[1] == self.last_command:
            self.run += 1
        else:
            self.last_command, self.run = key[1], 1
        if self.run >= 3:
            self.ignored.append(frame)
            return b""
        return self.inner(frame, wakeup)


def _frames(port):
    return [f for f, _ in port.sent_frames]


def _poll(address, command, *data):
    body = bytes([address, command, *data])
    return body + sas_crc(body).to_bytes(2, "little")


def _host(port):
    return AFTHost(SASPoller(port, address=1), asset_number=None,
                   registration_key=KEY, pos_id=POS, sleep=NO_SLEEP)


class TestTheA560Failure:
    def test_three_73s_in_a_row_fail_the_way_the_bundle_shows(self):
        machine = AFTMachine(registered=False)      # 73/FF echoes its asset
        port = MockSASSerialPort(StrictMachine(machine))
        ok, reg, detail = _host(port).auto_register()
        assert not ok
        assert detail == "silence on register (73/01)"   # the journal line
        assert machine.registered is False
        # nothing but 0x73 went out: read, initialize, register
        assert [(f[1], f[3]) for f in _frames(port)] == [
            (AFT_CMD_REGISTER, REG_CODE_READ_CURRENT),
            (AFT_CMD_REGISTER, REG_CODE_INITIALIZE),
            (AFT_CMD_REGISTER, REG_CODE_REGISTER)]

    def test_the_guard_registers_the_same_machine(self):
        machine = AFTMachine(registered=False)
        strict = StrictMachine(machine)
        port = ImpliedAckTransport(MockSASSerialPort(strict))
        ok, reg, detail = _host(port).auto_register()
        assert ok and reg.registered and detail == "registered"
        assert machine.registered is True
        assert strict.ignored == []
        sent = _frames(port)
        assert [f[3] if len(f) > 1 else "ack" for f in sent] == [
            REG_CODE_READ_CURRENT, "ack",
            REG_CODE_INITIALIZE, "ack",
            REG_CODE_REGISTER]
        assert sent[1] == sent[3] == ACK_POLL
        assert port.acks_sent == 2

    def test_a_lenient_machine_still_registers_through_the_guard(self):
        # the BB2 shape: no asset in the 73/FF answer, so a 0x7B read goes
        # out between the read and the initialize
        machine = AFTMachine(registered=False, asset=b"\x00" * 4)
        machine.answer_7b = True
        seen = {}

        def lenient(frame, wakeup):
            if len(frame) > 1 and frame[1] == 0x7B:
                seen["7b"] = True
                machine.asset = ASSET           # the operator asset, via 7B
            return machine(frame, wakeup)

        port = ImpliedAckTransport(MockSASSerialPort(lenient))
        ok, reg, _ = _host(port).auto_register()
        assert ok and reg.registered and seen.get("7b")
        # only initialize -> register is a repeat here, so one ACK
        assert port.acks_sent == 1

    def test_lock_wait_repeats_0x74_and_needs_the_guard_too(self):
        # request -> pending, interrogate -> pending, interrogate -> locked:
        # three 0x74 in a row
        unguarded = AFTMachine(lock_mode="pending")
        bare = MockSASSerialPort(StrictMachine(unguarded))
        result = aft_transfer(bare, 1, _request(700), ASSET, KEY,
                              sleep=NO_SLEEP)
        assert not result.ok
        assert unguarded.credited_cents == 0        # no money moved

        machine = AFTMachine(lock_mode="pending")
        strict = StrictMachine(machine)
        port = ImpliedAckTransport(MockSASSerialPort(strict))
        result = aft_transfer(port, 1, _request(700), ASSET, KEY,
                              sleep=NO_SLEEP)
        assert result.outcome is AFTOutcome.COMPLETED
        assert machine.credited_cents == 700
        assert strict.ignored == []
        locks = [i for i, f in enumerate(_frames(port))
                 if len(f) > 1 and f[1] == AFT_CMD_LOCK_STATUS]
        assert len(locks) >= 3
        assert all(b - a >= 2 for a, b in zip(locks, locks[1:]))


class _Scripted:
    """Answers each long poll from a list, in order; records what was sent."""

    def __init__(self, *answers):
        self.answers = list(answers)

    def __call__(self, frame, wakeup):
        if len(frame) == 1:
            return b"" if frame == ACK_POLL else b"\x00"
        return self.answers.pop(0) if self.answers else b""


GOOD_1F = _poll(1, 0x1F, 0x41, 0x47)
GOOD_2F = _poll(1, 0x2F, 0x00)
ASK_1F = _poll(1, 0x1F)
ASK_2F = _poll(1, 0x2F)


def _guarded(*answers):
    return ImpliedAckTransport(MockSASSerialPort(_Scripted(*answers)))


class TestWhenTheGuardAcks:
    def test_same_command_after_an_intact_answer_is_acked_first(self):
        port = _guarded(GOOD_1F, GOOD_1F)
        assert port.transact(ASK_1F) == GOOD_1F
        assert port.transact(ASK_1F) == GOOD_1F
        assert _frames(port) == [ASK_1F, ACK_POLL, ASK_1F]

    def test_every_repeat_is_acked_not_only_the_first(self):
        port = _guarded(GOOD_1F, GOOD_1F, GOOD_1F, GOOD_1F)
        for _ in range(4):
            port.transact(ASK_1F)
        assert _frames(port) == [ASK_1F, ACK_POLL, ASK_1F, ACK_POLL,
                                 ASK_1F, ACK_POLL, ASK_1F]
        assert port.acks_sent == 3

    def test_a_different_command_is_already_the_ack(self):
        port = _guarded(GOOD_1F, GOOD_2F, GOOD_1F)
        port.transact(ASK_1F)
        port.transact(ASK_2F)
        port.transact(ASK_1F)
        assert _frames(port) == [ASK_1F, ASK_2F, ASK_1F]
        assert port.acks_sent == 0

    def test_a_general_poll_in_between_is_already_the_ack(self):
        port = _guarded(GOOD_1F, GOOD_1F)
        port.transact(ASK_1F)
        port.transact(bytes([0x81]))                # general poll, address 1
        port.transact(ASK_1F)
        assert _frames(port) == [ASK_1F, bytes([0x81]), ASK_1F]

    def test_the_pollers_own_ack_poll_counts(self):
        port = _guarded(GOOD_1F, GOOD_1F)
        port.transact(ASK_1F)
        SASPoller(port, address=1)._ack_poll()      # goes out via send_frame
        port.transact(ASK_1F)
        assert _frames(port) == [ASK_1F, ACK_POLL, ASK_1F]
        assert port.acks_sent == 0                  # none added by the guard

    def test_same_command_to_another_address_is_not_a_repeat(self):
        port = _guarded(GOOD_1F, b"", GOOD_1F)
        port.transact(ASK_1F)
        port.transact(_poll(2, 0x1F))
        port.transact(ASK_1F)
        assert ACK_POLL not in _frames(port)

    def test_a_one_byte_ack_counts_as_an_answer(self):
        # a type S poll is answered with the machine's address alone
        port = _guarded(b"\x01", b"\x01")
        port.transact(_poll(1, 0x7C, 0x00))
        port.transact(_poll(1, 0x7C, 0x01))
        assert _frames(port)[1] == ACK_POLL


class TestWhenTheRepeatIsARealNack:
    """Nothing usable came back, so the repeat must reach the machine
    un-ACKed: that is how it is asked to send the answer again (§3.2)."""

    @pytest.mark.parametrize("lost", [
        b"",                                        # silence
        GOOD_1F[:-1] + bytes([GOOD_1F[-1] ^ 0xFF]),  # damaged CRC
        GOOD_1F[:5],                                # cut short
        GOOD_2F,                                    # an answer to another poll
    ])
    def test_no_ack_before_the_retry(self, lost):
        port = _guarded(lost, GOOD_1F)
        port.transact(ASK_1F)
        assert port.transact(ASK_1F) == GOOD_1F
        assert _frames(port) == [ASK_1F, ASK_1F]
        assert port.acks_sent == 0

    @pytest.mark.parametrize("whole", [
        bytes([0x81]),                              # NACK: address | 0x80
        bytes([0x01, 0x00]),                        # busy (§4.1)
    ])
    def test_a_busy_or_a_nack_is_acknowledged_before_the_retry(self, whole):
        """Both arrived whole and carry nothing the machine must keep. The
        poll after them is a NEW attempt; sent with nothing between it is
        an implied NACK, and a machine that counts re-sends its busy and
        then ignores the third (v0.3.15)."""
        port = _guarded(whole, GOOD_1F)
        port.transact(ASK_1F)
        assert port.transact(ASK_1F) == GOOD_1F
        assert _frames(port) == [ASK_1F, ACK_POLL, ASK_1F]
        assert port.acks_sent == 1

    def test_three_tries_at_a_lost_answer_stay_three_bare_polls(self):
        port = _guarded(b"", b"", b"")
        for _ in range(3):
            port.transact(ASK_1F)
        assert _frames(port) == [ASK_1F] * 3


class TestTheHelpers:
    def test_long_poll_key(self):
        assert long_poll_key(ASK_1F) == (1, 0x1F)
        assert long_poll_key(bytes([0x81])) is None     # general poll
        assert long_poll_key(ACK_POLL) is None
        assert long_poll_key(_poll(0, 0x7F, 0x00)) is None   # broadcast
        assert long_poll_key(b"") is None

    def test_answer_intact(self):
        assert answer_intact(GOOD_1F, 1, 0x1F)
        assert answer_intact(b"\x01", 1, 0x7C)
        assert not answer_intact(GOOD_1F, 2, 0x1F)
        assert not answer_intact(GOOD_1F, 1, 0x2F)
        assert not answer_intact(b"", 1, 0x1F)
        assert not answer_intact(b"\x81", 1, 0x1F)
        assert not answer_intact(b"\x01\x00", 1, 0x1F)

    def test_a_broadcast_is_never_tracked(self):
        port = _guarded()
        frame = _poll(0, 0x7F, 0x00)
        port.transact(frame)
        port.transact(frame)
        assert _frames(port) == [frame, frame]


class TestTheWrapperIsTransparent:
    def test_everything_else_reaches_the_wrapped_port(self):
        inner = MockSASSerialPort(_Scripted())
        port = ImpliedAckTransport(inner)
        assert port.sent_frames is inner.sent_frames
        assert port.listen(0.01) == b""
        port.close()

    def test_a_port_with_transact_alone_still_gets_its_ack(self):
        class TransactOnly:
            def __init__(self):
                self.sent = []

            def transact(self, frame, max_bytes=256):
                self.sent.append(frame)
                return GOOD_1F if len(frame) > 1 else b""

        inner = TransactOnly()
        port = ImpliedAckTransport(inner)
        port.transact(ASK_1F)
        port.transact(ASK_1F)
        port.send_frame(ACK_POLL)
        assert inner.sent == [ASK_1F, ACK_POLL, ASK_1F, ACK_POLL]


class TestTheSatelliteUsesIt:
    def test_main_hands_the_poller_a_guarded_port(self, monkeypatch):
        inner = MockSASSerialPort(_Scripted())
        seen = {}

        class Stop(Exception):
            pass

        def capture(transport, **kwargs):
            seen["transport"] = transport
            raise Stop

        monkeypatch.setattr(sas_host, "open_port",
                            lambda path, mock, address, protocol: inner)
        monkeypatch.setattr(sas_host, "SASPoller", capture)
        monkeypatch.setattr(sys, "argv", ["sas_host.py", "--mock"])
        with pytest.raises(Stop):
            sas_host.main()
        assert isinstance(seen["transport"], ImpliedAckTransport)
        assert seen["transport"].sent_frames is inner.sent_frames
