"""
The link rules of SAS 6.02, sections 2.3.3, 3 and 4.2 (audit batch 4).

Every test here runs against ByTheBookMachine, which keeps those sections to
the letter: it counts consecutive polls, ignores a poll that comes too soon,
and stops answering its own address after five seconds of silence. A WMS
Bluebird 2 forgives all of it, so only a machine like this one can show the
faults:

  item 4   the same general poll over and over while idle, with nothing
           between them that the document counts as an acknowledgement
  item 8   polls about 25 ms apart where the document allows 200 ms (40 ms
           on a machine that says it supports that)
  item 9   an exception byte equal to our address + 0x80 taken for a NACK
  item 25  the wait loops reporting 1F and chirps as events
  item 27  polls sent after a long silence with no sync poll before them
  item 28  the 20th drained exception left without its acknowledgement
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from core.sas_handpay_reset import _await_confirmation
from core.sas_implied_ack import ACK_POLL, ImpliedAckTransport
from core.sas_poller import SASPoller
from core.sas_protocol import SASProtocol
from core.sas_tito_host import _await_transfer_complete
from core.sas_wait import await_exception
from modules.aft.aft_handler import _await_aft_complete
from transport.serial.sas_serial import MockSASSerialPort

from by_the_book import DROP, ByTheBookMachine, frame

GENERAL = bytes([0x81])
PROTO = SASProtocol()
NO_SLEEP = lambda s: None


class FakeClock:
    """Time that moves only when something sleeps (or a test says so)."""

    def __init__(self, start=1000.0):
        self.t = start
        self.slept = []

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += seconds

    def pass_(self, seconds):
        self.t += seconds


def _line(machine, **kw):
    return ImpliedAckTransport(MockSASSerialPort(machine), **kw)


def _sent(port):
    return [bytes(f) for f, _ in port.sent_frames]


def _poller(port, **kw):
    seen = []
    kw.setdefault("meters", [])
    poller = SASPoller(port, address=kw.pop("address", 1),
                       on_event=lambda a, code, name: seen.append(code), **kw)
    return poller, seen


def _no_two_general_polls_in_a_row(machine):
    polls = [f for f in machine.received]
    general = bytes([0x80 | machine.address])
    return all(not (a == general and b == general)
               for a, b in zip(polls, polls[1:]))


# ---------------------------------------------------------------------------
# Item 4 — an idle machine is not sent NACK after NACK
# ---------------------------------------------------------------------------

class TestIdleGeneralPollsAreAcknowledged:
    def test_an_idle_machine_keeps_answering(self):
        """Before: this machine answered two idle polls, ignored the rest
        (a third consecutive poll is the final NACK), and was logged
        OFFLINE after five."""
        machine = ByTheBookMachine()
        poller, seen = _poller(_line(machine))
        for _ in range(40):
            poller.poll_once()
        assert poller.state.online is True
        assert poller.state.misses == 0
        assert poller.state.answers == 40
        assert _no_two_general_polls_in_a_row(machine)

    def test_an_exception_after_a_long_idle_spell_arrives_once(self):
        """Delivered on a third-or-later consecutive poll, an exception is
        kept by the machine (§3.2) and sent again after our ACK."""
        machine = ByTheBookMachine()
        poller, seen = _poller(_line(machine))
        for _ in range(25):
            poller.poll_once()
        machine.exceptions.append(0x11)
        for _ in range(10):
            poller.poll_once()
        assert seen == [0x11]
        assert machine.exceptions == []

    def test_a_lost_answer_is_asked_for_again_not_acknowledged(self):
        """The other half of §3: when nothing comes back, the SAME poll
        again is the NACK that makes the machine re-send. An address-0
        poll there would tell it to throw away an exception we never saw."""
        machine = ByTheBookMachine()
        port = _line(machine)
        poller, seen = _poller(port)
        for _ in range(3):
            poller.poll_once()
        machine.exceptions.append(0x11)
        machine.general_faults = [DROP]
        before = len(machine.received)
        poller.poll_once()                       # the answer is lost
        assert seen == []
        poller.poll_once()                       # asked for again
        assert seen == [0x11]
        assert machine.received[before:before + 3] == [
            ACK_POLL, GENERAL, GENERAL]
        poller.poll_once()
        assert seen == [0x11] and machine.exceptions == []

    def test_a_fourth_unanswered_poll_restarts_the_count(self):
        """Three is all §3.2 defines. A machine that missed three polls is
        not left counting a fourth, fifth, sixth NACK."""
        port = _line(lambda f, w: b"")
        for _ in range(7):
            port.transact(GENERAL)
        assert _sent(port) == [GENERAL, GENERAL, GENERAL, ACK_POLL,
                               GENERAL, GENERAL, GENERAL, ACK_POLL, GENERAL]
        assert port.resyncs_sent == 2 and port.acks_sent == 0

    def test_a_long_poll_between_two_general_polls_is_the_ack(self):
        """Table 3.1: a long poll to the same machine acknowledges a
        general poll's answer, so no address-0 poll is spent on it."""
        machine = ByTheBookMachine()
        port = _line(machine)
        port.transact(GENERAL)
        port.transact(bytes([0x01, 0x1B]))
        port.transact(GENERAL)
        assert _sent(port) == [GENERAL, bytes([0x01, 0x1B]), GENERAL]

    def test_a_damaged_long_poll_result_on_a_general_poll_is_not_acked(self):
        """A long-poll result can arrive in answer to a general poll (the
        ROM signature). It has a CRC, so a damaged one is asked for again."""
        good = frame(1, 0x21, bytes([0x34, 0x12]))
        answers = [good[:-1] + bytes([good[-1] ^ 0xFF]), good, b"\x00"]
        port = _line(lambda f, w: answers.pop(0) if f == GENERAL else b"")
        port.transact(GENERAL)
        port.transact(GENERAL)
        port.transact(GENERAL)
        assert _sent(port) == [GENERAL, GENERAL, ACK_POLL, GENERAL]


# ---------------------------------------------------------------------------
# Items 4 and 25 — the three wait loops
# ---------------------------------------------------------------------------

WAIT_LOOPS = [
    pytest.param(_await_confirmation, 0x52, id="handpay reset"),
    pytest.param(_await_transfer_complete, 0x68, id="ticket redemption"),
    pytest.param(_await_aft_complete, 0x69, id="AFT transfer"),
]


class TestWaitLoops:
    @pytest.mark.parametrize("loop,wanted", WAIT_LOOPS)
    def test_a_strict_machine_answers_all_the_way_to_the_event(self, loop,
                                                               wanted):
        """Before: the loop sent up to ten bare general polls, and a strict
        machine stopped answering after the second."""
        machine = ByTheBookMachine()
        port = _line(machine)
        polls = {"n": 0}

        def sleep(_):
            polls["n"] += 1
            if polls["n"] == 8:
                machine.exceptions.append(wanted)

        assert loop(port, 1, PROTO, 0.2, sleep, 10, None, None) is True
        assert machine.exceptions == []           # and it was acknowledged
        assert _no_two_general_polls_in_a_row(machine)

    @pytest.mark.parametrize("loop,wanted", WAIT_LOOPS)
    def test_no_activity_and_chirps_are_not_events(self, loop, wanted):
        """1F is "no activity, waiting for player" (§13.2 asks hosts to
        treat it as 00); a lone address byte is a loop-break chirp."""
        answers = [b"\x1f", b"\x01", b"\x1f", b"\x11", b"\x1f", b"\x00"]
        port = _line(lambda f, w: (answers.pop(0) if answers else b"\x00")
                     if f == GENERAL else b"")
        events = []
        assert loop(port, 1, PROTO, 0.2, NO_SLEEP, 6, events.append,
                    None) is False
        assert events == [0x11]

    def test_other_exceptions_are_handed_up_and_acknowledged(self):
        machine = ByTheBookMachine()
        machine.exceptions += [0x11, 0x12, 0x69]
        events = []
        assert await_exception(_line(machine), 1, PROTO, (0x69,), 0.2,
                               NO_SLEEP, 10, events.append) is True
        assert events == [0x11, 0x12]
        assert machine.exceptions == []

    def test_a_long_poll_result_is_handed_up(self):
        result = frame(1, 0x21, bytes([0x34, 0x12]))
        answers = [result, b"\x69"]
        frames = []
        port = _line(lambda f, w: (answers.pop(0) if answers else b"\x00")
                     if f == GENERAL else b"")
        assert await_exception(port, 1, PROTO, (0x69,), 0.2, NO_SLEEP, 5,
                               None, frames.append) is True
        assert frames == [result]


# ---------------------------------------------------------------------------
# Item 28 — more exceptions waiting than one drain takes
# ---------------------------------------------------------------------------

class TestDrain:
    def test_twenty_five_exceptions_each_arrive_once(self):
        """Before: the drain took 20 and stopped without acknowledging the
        last, which the machine then sent again."""
        machine = ByTheBookMachine()
        codes = [0x11, 0x12, 0x13, 0x14, 0x15] * 5
        machine.exceptions += codes
        poller, seen = _poller(_line(machine))
        for _ in range(4):
            poller.poll_once()
        assert seen == codes
        assert machine.exceptions == []

    def test_the_poller_acknowledges_its_last_drained_exception(self):
        """The same, with no guard under it: the poller alone."""
        machine = ByTheBookMachine()
        codes = [0x11, 0x12] * 11
        machine.exceptions += codes
        poller, seen = _poller(MockSASSerialPort(machine))
        poller.poll_once()
        assert seen == codes[:21]
        assert machine.exceptions == codes[21:]


# ---------------------------------------------------------------------------
# Item 9 — address + 0x80 is an exception code like any other
# ---------------------------------------------------------------------------

class TestAddressPlus80:
    def test_attendant_menu_at_address_2_is_reported(self):
        """0x82 "display meters or attendant menu has been entered" was
        swallowed at address 2 as a NACK, and never acknowledged."""
        machine = ByTheBookMachine(address=2)
        machine.exceptions += [0x82, 0x83]
        named = []
        poller = SASPoller(_line(machine), address=2, meters=[],
                           on_event=lambda a, c, n: named.append((c, n)))
        poller.poll_once()
        assert [c for c, _ in named] == [0x82, 0x83]
        assert "attendant menu" in named[0][1]
        assert machine.exceptions == []

    def test_a_run_of_the_same_byte_is_reported_once(self):
        """Seen once on a Bluebird 2 at address 1 (2026-07-10): 0x81 on
        some twenty polls in a row, then two seconds of silence. One
        report, the rest counted."""
        answers = [b"\x81"] * 12 + [b"\x00"] * 3 + [b"\x81", b"\x00"]
        port = _line(lambda f, w: (answers.pop(0) if answers else b"\x00")
                     if f == GENERAL else b"")
        poller, seen = _poller(port)
        for _ in range(6):
            poller.poll_once()
        assert seen == [0x81, 0x81]
        assert poller.state.nacks == 11


# ---------------------------------------------------------------------------
# Item 27 — after a long silence the machine needs the sync poll first
# ---------------------------------------------------------------------------

class TestSyncAfterSilence:
    def test_the_first_poll_after_five_quiet_seconds_is_led_by_a_sync(self):
        """Before: after SAS was switched back on, the AFT status polls in
        the same pass went to a machine still ignoring its own address,
        and the hub was told it was not registered."""
        clock = FakeClock()
        machine = ByTheBookMachine(clock=clock)
        port = _line(machine, clock=clock, sleep=clock.sleep)
        port.transact(GENERAL)
        clock.pass_(6.0)
        answer = port.transact(bytes([0x01, 0x1B]))
        assert answer == frame(1, 0x1B, bytes(20))
        assert machine.chirps == 0
        assert _sent(port) == [GENERAL, ACK_POLL, bytes([0x01, 0x1B])]
        assert port.syncs_sent == 1

    def test_a_short_pause_costs_nothing(self):
        clock = FakeClock()
        machine = ByTheBookMachine(clock=clock)
        port = _line(machine, clock=clock, sleep=clock.sleep)
        port.transact(GENERAL)
        clock.pass_(2.0)
        port.transact(bytes([0x01, 0x1B]))
        assert _sent(port) == [GENERAL, bytes([0x01, 0x1B])]
        assert port.syncs_sent == 0

    def test_the_poller_recovers_after_a_stall(self):
        clock = FakeClock()
        machine = ByTheBookMachine(clock=clock)
        poller, seen = _poller(_line(machine, clock=clock,
                                     sleep=clock.sleep), clock=clock)
        poller.poll_once()
        clock.pass_(30.0)                        # the poll thread was held
        machine.exceptions.append(0x11)
        poller.poll_once()
        assert seen == [0x11]
        assert poller.state.misses == 0


# ---------------------------------------------------------------------------
# Item 8 — the polling rate
# ---------------------------------------------------------------------------

def _paced(gap=0.2, **machine_kw):
    clock = FakeClock()
    machine = ByTheBookMachine(clock=clock, min_gap=gap, **machine_kw)
    port = _line(machine, clock=clock, sleep=clock.sleep, poll_gap=gap)
    return clock, machine, port


class TestPollingRate:
    def test_a_strict_machine_hears_every_drained_exception(self):
        """Before: the drain polled again about 25 ms after each answer,
        and a machine that keeps the 200 ms rate ignored those polls."""
        clock, machine, port = _paced()
        codes = [0x11, 0x12, 0x13, 0x14, 0x15]
        machine.exceptions += codes
        poller, seen = _poller(port, clock=clock)
        start = clock()
        poller.poll_once()
        assert seen == codes
        assert machine.too_soon == []
        # five more polls after the first, 200 ms apart
        assert clock() - start == pytest.approx(1.0)

    def test_long_polls_in_a_row_are_spaced(self):
        clock, machine, port = _paced()
        start = clock()
        for _ in range(3):
            assert port.transact(bytes([0x01, 0x1B])) != b""
        assert machine.too_soon == []
        assert clock() - start == pytest.approx(0.4)

    def test_time_already_spent_is_not_spent_again(self):
        """A caller that slept 200 ms itself is not made to wait twice."""
        clock, machine, port = _paced()
        port.transact(GENERAL)
        clock.pass_(0.25)
        port.transact(GENERAL)
        assert clock.slept == []

    def test_forty_milliseconds_where_the_machine_supports_it(self):
        clock, machine, port = _paced(gap=0.04)
        start = clock()
        for _ in range(3):
            port.transact(bytes([0x01, 0x1B]))
        assert machine.too_soon == []
        assert clock() - start == pytest.approx(0.08)

    def test_the_rate_can_be_changed_once_the_machine_says(self):
        clock, machine, port = _paced()
        machine.min_gap = 0.04
        port.poll_gap = 0.04
        port.transact(GENERAL)
        port.transact(bytes([0x01, 0x1B]))
        assert clock.slept == [pytest.approx(0.04)]

    def test_a_repeat_after_a_damaged_answer_does_not_wait(self):
        """§3.2: "the host does not need to wait for the specified polling
        rate time period to re-issue the poll"."""
        clock, machine, port = _paced()
        machine.handpay()
        machine.faults = ["damage"]
        port.transact(GENERAL)                   # 0x51
        port.transact(bytes([0x01, 0x1B]))       # damaged
        slept = list(clock.slept)
        answer = port.transact(bytes([0x01, 0x1B]))
        assert clock.slept == slept              # no wait for the repeat
        assert answer == frame(1, 0x1B, machine.handpays[0]["data"])
        assert machine.too_soon == []

    def test_the_long_poll_after_a_priority_exception_does_not_wait(self):
        """§2.2.1: "the host does not need to wait for the specified
        polling rate time period to respond to a priority exception with a
        long poll"."""
        clock, machine, port = _paced()
        machine.handpay()
        assert port.transact(GENERAL) == b"\x51"
        port.transact(bytes([0x01, 0x1B]))
        assert clock.slept == []
        assert machine.too_soon == []
        port.transact(GENERAL)                   # the one after it waits
        assert clock.slept == [pytest.approx(0.2)]

    def test_an_ordinary_exception_gives_no_such_leave(self):
        clock, machine, port = _paced()
        machine.exceptions.append(0x11)
        port.transact(GENERAL)
        port.transact(bytes([0x01, 0x1B]))
        assert clock.slept == [pytest.approx(0.2)]

    def test_polls_to_address_zero_are_not_held_and_do_not_count(self):
        """The rate is per machine. A poll to address 0 neither waits for
        it nor restarts it."""
        clock, machine, port = _paced()
        port.transact(GENERAL)
        clock.pass_(0.15)
        port.send_frame(ACK_POLL)
        assert clock.slept == []
        port.transact(GENERAL)                   # 0.05 s is all that is left
        assert clock.slept == [pytest.approx(0.05)]
        assert machine.too_soon == []

    def test_with_no_rate_set_nothing_ever_waits(self):
        clock = FakeClock()
        port = _line(ByTheBookMachine(), clock=clock, sleep=clock.sleep)
        for _ in range(5):
            port.transact(GENERAL)
            port.transact(bytes([0x01, 0x1B]))
        assert clock.slept == []

    def test_the_whole_idle_loop_at_the_document_rate(self):
        """Poll, drain, and a handpay read, against a machine that keeps
        every rule in this file at once."""
        clock, machine, port = _paced()
        got = []
        poller, seen = _poller(port, clock=clock,
                               on_handpay_info=lambda a, i: got.append(i))
        for n in range(30):
            if n == 10:
                machine.exceptions += [0x11, 0x12]
            if n == 20:
                machine.handpay()
            poller.poll_once()
            clock.sleep(0.2)
        assert seen == [0x11, 0x12, 0x51]
        assert len(got) == 1
        assert machine.too_soon == []
        assert poller.state.misses == 0
        assert _no_two_general_polls_in_a_row(machine)


# ---------------------------------------------------------------------------
# Item 10 — a chirp must not be read as "done"
# ---------------------------------------------------------------------------

import sas_host
from core.sas_poller import MachineState
from test_sas_host_commands import _run_scripted

DEAF_COMMANDS = [
    {"id": "deaf-bonus", "type": "legacy_bonus", "credits": 25},
    {"id": "deaf-game", "type": "sas_game_state", "game": 1, "enable": False},
]


def _run_deaf():
    """A machine that cannot hear the Pi: every read window holds its
    loop-break chirp, a lone address byte. That byte is also what a type S
    long poll (a bonus award, a game enable) is ACKed with."""
    served = {"n": 0}

    def script(i, reports):
        if len(reports) >= 3 and served["n"] < 1:
            served["n"] += 1
            return {"commands": DEAF_COMMANDS}
        return {}

    return _run_scripted("deaf", lambda: (lambda frame, wakeup: b"\x01"),
                         script, max_polls=200)


class TestCommandsToAMachineThatCannotHear:
    def test_a_chirp_is_not_taken_for_an_award(self):
        """Before: about one such command in four read "ack", so a bonus
        was reported awarded on a machine that never heard it."""
        run = _run_deaf()
        for cmd_id in ("deaf-bonus", "deaf-game"):
            rec = run["by_id"][cmd_id]
            assert rec["ok"] is False
            assert rec["outcome"] == "machine_offline"
            assert rec["result"] == "rejected: machine not answering"
            assert "not hearing the Pi" in rec["detail"]

    def test_nothing_was_sent_for_them(self):
        run = _run_deaf()
        sent = [bytes(f) for f, _ in run["port"].sent_frames]
        assert all(f in (b"\x80", b"\x81") for f in sent)

    def test_the_reason_given(self):
        st = MachineState(address=1)
        assert sas_host.link_down_reason(st) == \
            "the machine is not answering polls"
        st.online = True
        assert sas_host.link_down_reason(st) is None
        st.chirp_run = 3
        assert "loop-break chirp" in sas_host.link_down_reason(st)


# ---------------------------------------------------------------------------
# Item 8 — the rate comes from the machine's own word
# ---------------------------------------------------------------------------

from core.sas_meters import parse_enabled_features


class TestTheMachineSaysHowFastItMayBePolled:
    @staticmethod
    def _a0(features1=0x00, features3=0x00):
        return bytes([0x00, 0x00, features1, 0x00, features3, 0, 0, 0])

    def test_features3_bit_0(self):
        f = parse_enabled_features(self._a0(features3=0x01))
        assert f.polling_40ms is True and f.supports_fast_polling is True

    def test_secure_enhanced_validation_implies_it(self):
        """§15: such machines "must also support a 40 ms polling rate",
        and an older one is "not guaranteed to set this bit"."""
        f = parse_enabled_features(self._a0(features1=0x40))
        assert f.validation_style == "enhanced"
        assert f.polling_40ms is False and f.supports_fast_polling is True

    def test_system_validation_implies_it(self):
        f = parse_enabled_features(self._a0(features1=0x20))
        assert f.validation_style == "system"
        assert f.supports_fast_polling is True

    def test_a_machine_that_says_nothing_stays_at_200(self):
        f = parse_enabled_features(self._a0())
        assert f.supports_fast_polling is False


from test_sas_host_commands import Lp09Machine


class _TicketingMachine:
    """Lp09Machine, plus an A0 answer that says secure enhanced
    validation (Features1 bits 6-5 = 10)."""

    def __init__(self):
        self.inner = Lp09Machine(address=1)

    def __call__(self, raw, wakeup):
        if len(raw) > 1 and raw[0] == 1 and raw[1] == 0xA0:
            return frame(1, 0xA0, bytes([0, 0, 0x40, 0, 0, 0, 0, 0]))
        return self.inner(raw, wakeup)


def _rates(run):
    return [rep["machineInfo"].get("pollRateMs") for rep in run["reports"]
            if isinstance(rep.get("machineInfo"), dict)]


class TestTheHostTakesTheRateFromTheCensus:
    def test_a_ticketing_machine_is_polled_at_40_ms(self):
        run = _run_scripted("rate40", _TicketingMachine,
                            lambda i, reports: {}, max_polls=120)
        assert _rates(run) and set(_rates(run)) == {40}

    def test_any_other_machine_stays_at_200_ms(self):
        run = _run_scripted("rate200", lambda: Lp09Machine(address=1),
                            lambda i, reports: {}, max_polls=120)
        assert _rates(run) and set(_rates(run)) == {200}


# ---------------------------------------------------------------------------
# Item 29 (the link half) — a chirp or an exception, by the document's list
# ---------------------------------------------------------------------------

from core.sas_exceptions import APPENDIX_A_CODES
from core.sas_wait import is_chirp


class TestChirpOrException:
    def test_a_low_address_is_never_an_exception_code(self):
        for address in range(1, 0x11):
            assert is_chirp(bytes([address]), address) is True

    def test_general_tilt_at_address_0x20_is_an_exception(self):
        """0x20 is "general tilt" in Table A-1. The naming table has no
        entry for it, so it used to be taken for a chirp at address 0x20
        and the machine marked not answering."""
        assert 0x20 in APPENDIX_A_CODES
        assert is_chirp(b"\x20", 0x20) is False

    def test_a_value_the_document_does_not_define_is_a_chirp(self):
        assert 0x26 not in APPENDIX_A_CODES
        assert is_chirp(b"\x26", 0x26) is True

    def test_several_address_bytes_are_always_a_chirp(self):
        assert is_chirp(b"\x20\x20", 0x20) is True
