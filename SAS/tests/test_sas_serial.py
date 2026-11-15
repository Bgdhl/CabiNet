"""
Tests for the SAS 9-bit wakeup serial transport (mock-based — real-hardware
validation happens on the Pi against a live machine).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.sas_protocol import SASProtocol, sas_crc
from transport.serial.sas_serial import MockSASSerialPort


class TestWakeupFraming:
    def setup_method(self):
        self.protocol = SASProtocol()

    def test_wakeup_set_on_first_byte_only(self):
        """The 9th bit must be set on byte 0 and clear on all others."""
        port = MockSASSerialPort()
        frame = self.protocol.build_packet(0x01, 0x21, b"\x00\x00")
        port.send_frame(frame)

        sent, wakeup = port.sent_frames[0]
        assert sent == frame
        assert wakeup[0] is True
        assert all(w is False for w in wakeup[1:])

    def test_general_poll_is_one_wakeup_byte(self):
        port = MockSASSerialPort()
        port.send_frame(self.protocol.build_general_poll(0x01))

        sent, wakeup = port.sent_frames[0]
        assert sent == b"\x81"
        assert wakeup == [True]

    def test_transact_round_trip(self):
        """A mock machine at address 1 answers a current-credits short poll."""
        protocol = self.protocol

        def machine(frame, wakeup):
            # Machine logic: only respond if addressed with wake-up bit set
            if not wakeup[0]:
                return b""
            if frame == b"\x01\x1A":  # current credits short poll
                body = b"\x01\x1A" + b"\x00\x00\x12\x34"
                return body + sas_crc(body).to_bytes(2, "little")
            return b""

        port = MockSASSerialPort(machine)
        resp = port.transact(protocol.build_short_poll(0x01, 0x1A))

        parsed = protocol.parse_packet(resp)
        assert parsed is not None
        assert parsed.address == 0x01
        assert parsed.command == 0x1A
        assert protocol._bcd_to_int(parsed.data) == 1234

    def test_machine_ignores_frame_without_wakeup(self):
        """A frame whose first byte lacks the wake-up bit gets silence —
        this is exactly what the old (parity-less) implementation caused."""
        def machine(frame, wakeup):
            return b"\x00" if wakeup[0] else b""

        port = MockSASSerialPort(machine)
        # Simulate the old bug: pretend no wakeup flag on any byte
        port.sent_frames.append((b"\x81", [False]))
        assert machine(b"\x81", [False]) == b""
        # And the correct path answers
        assert port.transact(b"\x81") == b"\x00"


class BurstySerial:
    """A serial port that hands bytes up the way a Pi 4's PL011 does: in
    FIFO-sized bursts with a pause between them, on a reply the machine sent
    without a gap. Virtual clock — no sleeping, fully deterministic."""

    def __init__(self, reply: bytes, burst: int = 16, pause: float = 0.009):
        self.timeout = 0.050
        self.now = 0.0
        self.arrivals = [(0.002 + i * pause, reply[o:o + burst])
                         for i, o in enumerate(range(0, len(reply), burst))]
        self.buf = b""

    def _deliver(self):
        while self.arrivals and self.arrivals[0][0] <= self.now:
            self.buf += self.arrivals.pop(0)[1]

    @property
    def in_waiting(self):
        self._deliver()
        return len(self.buf)

    def read(self, n=1):
        self._deliver()
        if not self.buf:
            if self.arrivals and self.arrivals[0][0] - self.now <= self.timeout:
                self.now = self.arrivals[0][0]
                self._deliver()
            else:
                self.now += self.timeout
                return b""
        out, self.buf = self.buf[:n], self.buf[n:]
        return out


def _port_reading(reply, gap_timeout=None):
    """A SASSerialPort wired to a BurstySerial, skipping the real open()."""
    from transport.serial.sas_serial import SASSerialPort
    import inspect
    port = SASSerialPort.__new__(SASSerialPort)
    sig = inspect.signature(SASSerialPort.__init__).parameters
    port.first_byte_timeout = sig["first_byte_timeout"].default
    port.gap_timeout = (sig["gap_timeout"].default if gap_timeout is None
                        else gap_timeout)
    port.ser = BurstySerial(reply)
    port._last_timeout = port.ser.timeout
    return port


class TestBurstDelivery:
    """GitHub issues #5 and #6 (2026-11): on a Raspberry Pi 4 every reply
    longer than 16 bytes was cut in two and both halves failed CRC. Tickets
    stayed 'minted' (the 35-byte 0x4D print record never read) and AFT never
    registered (the 0x73 reply never read), while short meter replies looked
    healthy. The read gap was the spec's 5 ms wire limit; userspace sees
    16-byte bursts ~9 ms apart. Confirmed on a Pi 4B + WMS BB2: the same
    0x4D read went from 0 of 12 good with a 6 ms gap to all good at 25 ms."""

    # the shape of a real 0x4D ticket record: addr, cmd, 31 data bytes, crc
    BODY = bytes([0x01, 0x4D]) + bytes(range(31))
    REPLY = BODY + sas_crc(BODY).to_bytes(2, "little")

    def test_long_reply_survives_16_byte_bursts(self):
        assert len(self.REPLY) == 35
        got = _port_reading(self.REPLY).read_response()
        assert got == self.REPLY
        packet = SASProtocol().parse_packet(got)
        assert packet is not None and packet.command == 0x4D

    def test_the_old_6ms_gap_cut_it_at_16_bytes(self):
        # documents the failure, so nobody "restores the spec value"
        got = _port_reading(self.REPLY, gap_timeout=0.006).read_response()
        assert len(got) == 16

    def test_default_gap_outlasts_a_fifo_burst_pause(self):
        port = _port_reading(self.REPLY)
        assert port.gap_timeout >= 0.020

    def test_short_reply_and_silence_still_end_cleanly(self):
        assert _port_reading(b"\x01\x1a\x00\x00\x12\x34").read_response() \
            == b"\x01\x1a\x00\x00\x12\x34"
        assert _port_reading(b"").read_response() == b""

    def test_a_very_long_reply_in_many_bursts(self):
        reply = bytes(range(200))
        assert _port_reading(reply).read_response() == reply
