"""
Tests for the SAS poller engine (SMIB core loop) using a scripted mock
machine. No hardware, no real time.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.sas_protocol import SASProtocol, sas_crc
from core.sas_poller import SASPoller, MultiMachinePoller, exception_name
from transport.serial.sas_serial import MockSASSerialPort


class ScriptedMachine:
    """A pretend SAS machine with an exception FIFO and a credits meter, that
    models the real IMPLIED-ACK semantics: an exception stays 'pending' and is
    RE-SENT on a repeated general poll (implied NACK) until the host ACKs it
    with a long poll to this machine OR a poll to a different address. Without
    this fidelity the tests would pass against the wrong drain logic — the
    project's historical failure mode."""

    def __init__(self, address=1, credits=4200, require_sync=False):
        self.address = address
        self.fifo = []           # queued exception codes
        self.credits = credits
        self.proto = SASProtocol()
        self.pending = None      # exception sent but not yet ACKed
        self.require_sync = require_sync   # ignore self-polls until synced
        self.synced = not require_sync
        self.general_polls = 0   # total general polls to THIS address
        self.powered = True      # False = switched off: answers nothing

    def queue(self, *codes):
        self.fifo.extend(codes)

    def __call__(self, frame, wakeup):
        if not self.powered:
            return b""           # off: hears nothing, says nothing
        if not wakeup[0]:
            return b""           # not addressed with wake-up bit
        is_general = len(frame) == 1
        polled = (frame[0] & 0x7F) if is_general else frame[0]

        if is_general and polled == self.address:
            self.general_polls += 1
            if self.require_sync and not self.synced:
                return b""       # §3.3: ignore self-polls until we see another
            if self.pending is not None:
                return bytes([self.pending])      # implied NACK -> re-send
            self.pending = self.fifo.pop(0) if self.fifo else 0x00
            if self.pending == 0x00:
                self.pending = None
                return b"\x00"
            return bytes([self.pending])

        # any other poll (different address, incl. addr 0, or a long poll to
        # us) ACKs our pending exception and re-syncs us
        self.synced = True
        if self.pending is not None:
            self.pending = None
        if not is_general and frame[0] == self.address and frame[1] == 0x1A:
            body = bytes([self.address, 0x1A]) + self.proto._int_to_bcd(self.credits, 4)
            return body + sas_crc(body).to_bytes(2, "little")
        return b""               # poll to addr 0, or unsupported -> silence


def manual_clock():
    """A controllable monotonic clock."""
    t = {"now": 0.0}
    def clock():
        return t["now"]
    clock.advance = lambda dt: t.__setitem__("now", t["now"] + dt)
    return clock


class TestSASPoller:
    def test_online_on_first_answer(self):
        machine = ScriptedMachine(address=1)
        port = MockSASSerialPort(machine)
        online = []
        poller = SASPoller(port, address=1, on_online=lambda a: online.append(a))

        assert poller.state.online is False
        poller.poll_once()
        assert poller.state.online is True
        assert online == [1]

    def test_exception_dispatched_and_named(self):
        machine = ScriptedMachine(address=1)
        machine.queue(0x12, 0x8C)  # slot door closed, game selected
        port = MockSASSerialPort(machine)
        events = []
        poller = SASPoller(port, address=1,
                           on_event=lambda a, c, n: events.append((a, c, n)))

        poller.poll_once()  # pops 0x12, ACK, drains 0x8C, ACK, then 0x00
        codes = [c for _, c, _ in events]
        assert codes == [0x12, 0x8C]   # each dispatched exactly ONCE
        assert exception_name(0x8C) == "game selected"

    def test_a_run_of_the_own_address_code_is_reported_once(self):
        # addr|0x80 is an exception code like any other (Appendix A: 0x81 is
        # "hopper level low detected"), so it is reported. Until 2026-11-17
        # it was taken for the NACK of Table 7.4b and dropped (audit item
        # 9). What is kept: on 2026-07-10 a WMS BB2 at addr 1 sent 0x81 on
        # some twenty polls in a row, which put 21 lines on the floor UI.
        # The first of such a run is reported, the rest counted
        # (state.nacks), and the machine stays online.
        machine = ScriptedMachine(address=1)
        machine.queue(0x81, 0x81, 0x81)
        port = MockSASSerialPort(machine)
        events = []
        poller = SASPoller(port, address=1,
                           on_event=lambda a, c, n: events.append((a, c, n)))
        for _ in range(3):
            poller.poll_once()
        assert events == [(1, 0x81, "hopper level low detected")]
        assert poller.state.nacks == 2
        assert poller.state.online is True

    def test_nack_byte_is_address_relative(self):
        # The guard is addr|0x80, not a blanket 0x81: from a machine at
        # address 2 the byte 0x81 is NOT its NACK (0x82 would be) and must
        # still dispatch as a real exception.
        machine = ScriptedMachine(address=2)
        machine.queue(0x81)
        port = MockSASSerialPort(machine)
        events = []
        poller = SASPoller(port, address=2,
                           on_event=lambda a, c, n: events.append((a, c, n)))
        poller.poll_once()
        assert [c for _, c, _ in events] == [0x81]
        assert poller.state.nacks == 0

    def test_documented_exception_at_own_address_dispatched(self):
        # PORTABILITY (2026-07-21): 0x80|addr collides with a REAL documented
        # exception on a larger multidrop floor. A spec-compliant machine at
        # address 6 raising 0x86 "game out of service" (= 0x80|6) must have
        # that GAME event DISPATCHED, not swallowed as a busy/NACK chirp.
        machine = ScriptedMachine(address=6)
        machine.queue(0x86)                    # 0x80|6 — a documented exception
        port = MockSASSerialPort(machine)
        events = []
        poller = SASPoller(port, address=6,
                           on_event=lambda a, c, n: events.append((a, c, n)))
        poller.poll_once()
        assert [c for _, c, _ in events] == [0x86]
        assert exception_name(0x86) == "game out of service"
        assert poller.state.nacks == 0         # NOT counted as a chirp

    def test_attendant_menu_exit_at_address_3_is_reported(self):
        # At address 3 the byte 0x83 (= 0x80|3) is "display meters or
        # attendant menu has been exited" (Appendix A). It used to be
        # dropped as a NACK because the guide's table does not list it.
        # Reported once; the same byte on the very next poll is counted.
        machine = ScriptedMachine(address=3)
        machine.queue(0x83, 0x83)
        port = MockSASSerialPort(machine)
        events = []
        poller = SASPoller(port, address=3,
                           on_event=lambda a, c, n: events.append((a, c, n)))
        for _ in range(2):
            poller.poll_once()
        assert [c for _, c, _ in events] == [0x83]
        assert "attendant menu" in events[0][2]
        assert poller.state.nacks == 1

    def test_implied_ack_no_duplicate_events(self):
        """Against a machine that re-sends until ACKed (real semantics), the
        ACK-aware drain must dispatch each exception exactly once — a naive
        re-poll drain would loop on 0x12 forever."""
        machine = ScriptedMachine(address=1)
        machine.queue(0x11, 0x17, 0x8C)
        port = MockSASSerialPort(machine)
        events = []
        SASPoller(port, address=1,
                  on_event=lambda a, c, n: events.append(c)).poll_once()
        assert [c for c in events] == [0x11, 0x17, 0x8C]

    def test_sync_poll_before_first_real_poll(self):
        """A machine that enforces §3.3 (ignores self-polls until it sees a
        poll to another address) still gets read, because the poller leads
        with an address-0 poll."""
        machine = ScriptedMachine(address=1, require_sync=True)
        machine.queue(0x12)
        port = MockSASSerialPort(machine)
        events = []
        poller = SASPoller(port, address=1,
                           on_event=lambda a, c, n: events.append(c))
        poller.poll_once()
        assert poller.state.online is True   # the address-0 poll synced it
        assert 0x12 in events
        # and an address-0 (0x80) general poll was actually sent
        assert any(f == b"\x80" for f, _ in port.sent_frames)

    def test_machine_that_restarts_is_resynced_without_a_host_restart(self):
        """2026-11-15, BB2 + Zero 2 W: after the machine was switched off and
        on, its SAS link stayed dead until `systemctl restart cabinet-sas`.
        The poller sent its one re-sync poll the moment the machine went
        offline — to a machine that was off — and then polled address 1
        alone forever, which a machine that has just booted ignores (§3.3).
        Proven on the wire that day: 25 polls to address 1, no answer; one
        poll to address 0, and address 1 answered the next poll."""
        machine = ScriptedMachine(address=1, require_sync=True)
        port = MockSASSerialPort(machine)
        poller = SASPoller(port, address=1)
        poller.poll_once()
        assert poller.state.online is True
        machine.powered = False                 # switched off...
        for _ in range(60):                     # ...for a good while
            poller.poll_once()
        assert poller.state.online is False
        machine.powered, machine.synced = True, False   # finished booting
        poller.poll_once()
        assert poller.state.online is True      # no host restart needed

    def test_sync_poll_goes_out_with_every_poll_while_machine_is_silent(self):
        """The machine needs the address-0 poll AFTER it comes back, and the
        host cannot know when that is — so every poll to a silent machine is
        led by one. A healthy link pays nothing for it."""
        machine = ScriptedMachine(address=1, require_sync=True)
        machine.powered = False
        port = MockSASSerialPort(machine)
        poller = SASPoller(port, address=1)
        syncs = lambda: sum(1 for f, _ in port.sent_frames if f == b"\x80")
        for _ in range(10):
            poller.poll_once()
        assert syncs() == 10
        machine.powered = True
        poller.poll_once()                      # synced and answered
        assert poller.state.online is True
        before = syncs()
        for _ in range(10):
            poller.poll_once()
        assert syncs() == before                # none while it is answering

    # -- the loop-break chirp (SAS 6.02 §4.2) -------------------------------
    # A machine that hears no address byte for 5 s transmits its own address
    # every 200 ms. While we are polling, that means our polls are not
    # reaching it. It is not an answer, and it must never read as ONLINE.

    @staticmethod
    def _deaf(address=1, burst=1):
        """A machine that cannot hear us: every read window holds chirps."""
        return lambda frame, wakeup: bytes([address]) * burst

    def test_chirp_is_not_an_answer(self):
        port = MockSASSerialPort(self._deaf())
        events, online = [], []
        poller = SASPoller(port, address=1,
                           on_event=lambda a, c, n: events.append(c),
                           on_online=lambda a: online.append(a))
        for _ in range(20):
            assert poller.poll_once() is None      # same as silence
        assert poller.state.online is False
        assert online == [] and events == []       # no phantom "exception 0x01"
        assert poller.state.answers == 0
        assert poller.state.chirps == 20 and poller.state.chirp_run == 20

    def test_a_burst_of_chirps_is_not_a_long_poll_result(self):
        """Several chirps buffered together used to be parsed as a long-poll
        result and fail CRC ("expected 0101")."""
        port = MockSASSerialPort(self._deaf(burst=3))
        poller = SASPoller(port, address=1)
        poller.poll_once()
        assert poller.state.online is False
        assert poller.state.chirps == 1 and poller.state.poll_results == {}

    def test_machine_that_stops_hearing_us_goes_offline(self):
        """The transmit wire comes off mid-session: answers turn to chirps.
        Counted as answers they would hold the machine ONLINE forever."""
        machine = ScriptedMachine(address=1)
        hears = {"us": True}
        port = MockSASSerialPort(
            lambda f, w: machine(f, w) if hears["us"] else b"\x01")
        offline = []
        poller = SASPoller(port, address=1,
                           on_offline=lambda a: offline.append(a))
        poller.poll_once()
        assert poller.state.online is True
        hears["us"] = False
        for _ in range(5):
            poller.poll_once()
        assert poller.state.online is False and offline == [1]
        assert poller.state.chirp_run == 5
        hears["us"] = True                          # wire back on
        poller.poll_once()
        assert poller.state.online is True
        assert poller.state.chirp_run == 0          # a real answer ends the run
        assert poller.state.chirps == 5             # the total is kept

    def test_own_address_byte_that_is_a_real_exception_stays_an_exception(self):
        """At address 0x11 a lone 0x11 is "slot door opened", and cannot be
        told from a chirp — it stays an exception, as before. Two of them
        together can only be chirps."""
        machine = ScriptedMachine(address=0x11)
        machine.queue(0x11)
        port = MockSASSerialPort(machine)
        events = []
        poller = SASPoller(port, address=0x11,
                           on_event=lambda a, c, n: events.append(c))
        poller.poll_once()
        assert events == [0x11] and poller.state.chirps == 0
        port2 = MockSASSerialPort(self._deaf(address=0x11, burst=2))
        poller2 = SASPoller(port2, address=0x11)
        poller2.poll_once()
        assert poller2.state.chirps == 1 and poller2.state.online is False

    @pytest.mark.parametrize("address,meaning", [
        (0x51, "handpay is pending"), (0x52, "handpay was reset"),
        (0x57, "system validation request"), (0x20, "general tilt"),
        (0x7E, "game has started")])
    def test_any_table_a1_code_at_its_own_address_is_an_exception(
            self, address, meaning):
        """Audit item 44. A machine set to address 81 (0x51) answers a
        general poll with the one byte 0x51 for "handpay is pending". The
        chirp test used to ask the naming tables, which left most of Table
        A-1 out, so that byte was counted as a missed poll: no 1B read and
        no banner. It asks the whole table now (v0.3.13). Two such bytes
        together are still a chirp."""
        machine = ScriptedMachine(address=address)
        machine.queue(address)
        events = []
        poller = SASPoller(MockSASSerialPort(machine), address=address,
                           on_event=lambda a, c, n: events.append(c))
        poller.poll_once()
        assert events == [address], meaning
        assert poller.state.chirps == 0 and poller.state.online is True
        deaf = SASPoller(MockSASSerialPort(self._deaf(address=address,
                                                      burst=2)),
                         address=address)
        deaf.poll_once()
        assert deaf.state.chirps == 1 and deaf.state.online is False

    def test_callback_exception_does_not_kill_loop(self):
        machine = ScriptedMachine(address=1)
        machine.queue(0x12)
        port = MockSASSerialPort(machine)
        def boom(*a):
            raise RuntimeError("bad callback")
        poller = SASPoller(port, address=1, on_event=boom, on_online=boom)
        poller.poll_once()                   # must not raise
        assert poller.state.callback_errors >= 1
        assert poller.state.online is True

    def test_meter_sweep_reads_credits(self):
        machine = ScriptedMachine(address=1, credits=1234)
        port = MockSASSerialPort(machine)
        meters = []
        clock = manual_clock()
        poller = SASPoller(port, address=1, meter_interval=5.0, clock=clock,
                           on_meter=lambda a, c, v: meters.append((c, v)))

        poller.poll_once()                      # first sweep happens at t=0
        assert poller.state.meters.get("current_credits") == 1234
        assert (0x1A, 1234) in meters

    def test_meter_interval_throttles(self):
        machine = ScriptedMachine(address=1, credits=500)
        port = MockSASSerialPort(machine)
        meters = []
        clock = manual_clock()
        poller = SASPoller(port, address=1, meter_interval=5.0, clock=clock,
                           on_meter=lambda a, c, v: meters.append(v))

        poller.poll_once()           # sweep at t=0
        clock.advance(1.0)
        poller.poll_once()           # too soon, no sweep
        assert len(meters) == 1
        clock.advance(5.0)
        machine.credits = 999
        poller.poll_once()           # now due
        assert meters[-1] == 999

    def test_offline_after_consecutive_misses(self):
        machine = ScriptedMachine(address=1)
        port = MockSASSerialPort(machine)
        offline = []
        poller = SASPoller(port, address=1, offline_after=3,
                           on_offline=lambda a: offline.append(a))

        poller.poll_once()                       # online
        assert poller.state.online
        # Now make the machine deaf (wrong address responds to nobody)
        machine.address = 99
        for _ in range(3):
            poller.poll_once()
        assert poller.state.online is False
        assert offline == [1]

    def test_rom_signature_result_via_general_poll(self):
        """Guide §4.3 (p.11) / §3 (p.8): a pending 0x21 ROM signature result
        answers a LATER general poll as a full '[addr][21][sig][sig][crc]'
        frame, sent INSTEAD of an exception code — and the VGM erases it as
        soon as the next poll implies the ACK. The poller must capture it as
        a long-poll result, not dispatch resp[0] (the address byte) as a
        phantom exception 0x01."""
        body = bytes([1, 0x21, 0xAB, 0xCD])
        pending = [body + sas_crc(body).to_bytes(2, "little")]

        def machine(frame, wakeup):
            if not wakeup[0]:
                return b""
            if len(frame) == 1 and (frame[0] & 0x7F) == 1:
                return pending.pop(0) if pending else b"\x00"
            return b""                    # addr-0 polls, long polls: silence

        port = MockSASSerialPort(machine)
        events, results = [], []
        poller = SASPoller(port, address=1, meters=[],
                           on_event=lambda a, c, n: events.append(c),
                           on_poll_result=lambda a, c, v:
                               results.append((a, c, v)))
        code = poller.poll_once()
        assert results == [(1, 0x21, b"\xAB\xCD")]
        assert poller.state.poll_results[0x21] == b"\xAB\xCD"
        assert events == []               # no phantom exception dispatched
        assert code == 0x00               # nothing came off the event FIFO
        assert poller.state.online is True

    def test_corrupt_general_poll_frame_is_dropped(self):
        """A multi-byte general-poll answer whose CRC fails must be dropped,
        not recorded as a result or dispatched as an exception."""
        good = bytes([1, 0x21, 0xAB, 0xCD])
        bad = good + b"\x00\x00"          # wrong CRC
        pending = [bad]

        def machine(frame, wakeup):
            if len(frame) == 1 and (frame[0] & 0x7F) == 1:
                return pending.pop(0) if pending else b"\x00"
            return b""

        port = MockSASSerialPort(machine)
        events = []
        poller = SASPoller(port, address=1, meters=[],
                           on_event=lambda a, c, n: events.append(c))
        poller.poll_once()
        assert poller.state.poll_results == {}
        assert events == []

    def test_run_bounded(self):
        machine = ScriptedMachine(address=1)
        machine.queue(0x12)
        port = MockSASSerialPort(machine)
        poller = SASPoller(port, address=1)
        poller.run(interval=0, max_polls=10, sleep=lambda _: None)
        assert poller.state.polls >= 10
        assert poller.state.answers >= 10


class TestMultiMachine:
    def test_round_robin_two_machines(self):
        # Two machines share one transport; the mock routes by address.
        m1 = ScriptedMachine(address=1)
        m2 = ScriptedMachine(address=2)
        m2.queue(0x17)  # AC power applied on machine 2

        def router(frame, wakeup):
            r1 = m1(frame, wakeup)
            return r1 if r1 else m2(frame, wakeup)

        port = MockSASSerialPort(router)
        events = []
        mp = MultiMachinePoller(port, [1, 2],
                                on_event=lambda a, c, n: events.append((a, c)))
        mp.poll_round()
        assert mp.states[1].online and mp.states[2].online
        assert (2, 0x17) in events
