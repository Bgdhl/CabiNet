"""
Tests for the typed exception-code table (core/sas_exceptions.py) and its
wiring into the poller's event path.

Authority: IGT SAS Protocol 6.02 Appendix A, Table A-1, for every code;
the Montana DOJ SAS Implementation Guide v1.5.0 §3.1 (p.9) and §4.6.2 for
the subset kept in EXCEPTIONS. Every Table A-1 code must have a name and no
other byte may: a fabricated code here would be a lie about the wire.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.sas_exceptions import (
    APPENDIX_A_CODES, APPENDIX_A_EXCEPTIONS, BENCH_EXCEPTIONS,
    BILL_DENOM_CENTS, EXCEPTIONS, EXCEPTION_NAMES, ExceptionCategory,
    SASExceptionInfo, exception_name, lookup,
)
from core.sas_poller import SASPoller
from transport.serial.sas_serial import MockSASSerialPort


# The guide §3.1 table, transcribed code-for-code (plus 3E from §4.6.2).
GUIDE_31_CODES = {
    0x00, 0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17, 0x18, 0x19, 0x1A,
    0x1B, 0x1C, 0x1D, 0x1E, 0x27, 0x28, 0x29, 0x2B, 0x3C, 0x3D, 0x3F,
    0x47, 0x48, 0x49, 0x4A, 0x60, 0x61, 0x70, 0x7A, 0x86, 0x8C,
}


class TestTableCompleteness:
    def test_exactly_the_cited_codes(self):
        """Every §3.1 code present; nothing invented beyond §3.1 + 0x3E +
        0x1F (spec §12.6 'no activity and waiting for player' — live-proven
        streaming from the BB2 in attract mode, 2026-07-08)."""
        assert set(EXCEPTIONS) == GUIDE_31_CODES | {0x3E, 0x1F}

    def test_no_fabricated_handpay_or_big_bill_codes(self):
        """The handpay-pending and $50/$100-bill codes are NOT in the guide,
        so they stay out of the guide's table. Table A-1 has them, and they
        are named in the other two."""
        for uncited in (0x51, 0x52, 0x4B, 0x4C):
            assert uncited not in EXCEPTIONS
            assert lookup(uncited).category is not ExceptionCategory.UNKNOWN

    def test_backcompat_name_map_matches(self):
        assert EXCEPTION_NAMES[0x8C] == "game selected"
        assert EXCEPTION_NAMES[0x11] == "slot door opened"
        assert set(EXCEPTION_NAMES) == set(EXCEPTIONS)


class TestCategoriesAndEvents:
    def test_door_codes(self):
        for code in (0x11, 0x13, 0x15, 0x19, 0x1D):
            info = lookup(code)
            assert info.category is ExceptionCategory.DOOR
            assert info.event == "door_open"
        for code in (0x12, 0x14, 0x16, 0x1A, 0x1E):
            info = lookup(code)
            assert info.category is ExceptionCategory.DOOR
            assert info.event == "door_closed"

    def test_power_codes(self):
        assert lookup(0x17).event == "power_on"
        assert lookup(0x18).event == "power_lost"
        assert lookup(0x18).category is ExceptionCategory.POWER

    def test_bill_denominations(self):
        """Table A-1: 47=$1, 48=$5, 49=$10, 4A=$20, 4B=$50, 4C=$100, 4D=$2,
        4E=$500, 50=$200. 4F is "bill accepted" with no denomination: the
        code for a bill with none of its own, and for every bill in real
        time event mode."""
        assert BILL_DENOM_CENTS == {
            0x47: 100, 0x48: 500, 0x49: 1000, 0x4A: 2000, 0x4B: 5000,
            0x4C: 10000, 0x4D: 200, 0x4E: 50000, 0x50: 20000}
        for code in range(0x47, 0x51):
            assert lookup(code).event == "bill_inserted"
            assert lookup(code).category is ExceptionCategory.BILL
        info = lookup(0x49)
        assert info.denom_cents == 1000
        assert lookup(0x4F).denom_cents is None
        assert lookup(0x4F).name == "bill accepted"
        # bill_rejected carries NO denomination (guide doesn't give one)
        assert lookup(0x2B).denom_cents is None

    def test_handpay_validated_from_4_6_2(self):
        info = lookup(0x3E)
        assert info.category is ExceptionCategory.HANDPAY
        assert info.event == "handpay_validated"

    def test_ticket_and_game_codes(self):
        assert lookup(0x3D).event == "ticket_printed"
        assert lookup(0x3F).category is ExceptionCategory.TICKET
        assert lookup(0x8C).event == "game_selected"
        assert lookup(0x86).category is ExceptionCategory.GAME

    def test_unknown_code_is_visible_not_fatal(self):
        info = lookup(0xE9)
        assert info.category is ExceptionCategory.UNKNOWN
        assert info.event == "unknown_exception"
        assert info.code == 0xE9
        assert exception_name(0xE9) == "exception 0xE9"


class TestAppendixA:
    """SAS 6.02 Table A-1 against the naming tables (audit item 29)."""

    def test_every_code_in_the_table_has_a_name(self):
        """The log and the hub's event list showed "exception 0x7E" for
        every game start, and so on for 51 codes."""
        named = set(EXCEPTIONS) | set(BENCH_EXCEPTIONS) \
            | set(APPENDIX_A_EXCEPTIONS)
        assert named == set(APPENDIX_A_CODES)
        assert len(APPENDIX_A_CODES) == 114
        for code in APPENDIX_A_CODES:
            info = lookup(code)
            assert not info.name.startswith("exception 0x"), hex(code)
            assert info.category is not ExceptionCategory.UNKNOWN, hex(code)
            assert info.event != "unknown_exception", hex(code)

    def test_no_code_is_named_twice(self):
        assert not set(EXCEPTIONS) & set(BENCH_EXCEPTIONS)
        assert not set(EXCEPTIONS) & set(APPENDIX_A_EXCEPTIONS)
        assert not set(BENCH_EXCEPTIONS) & set(APPENDIX_A_EXCEPTIONS)

    def test_a_byte_the_table_does_not_define_stays_unknown(self):
        """The gaps in Table A-1 (26, 2F, 30, 58-5F, 62-65, 73, 7D, 8D,
        90-97 and everything from 9C up) are not exceptions."""
        for code in (0x01, 0x10, 0x26, 0x2F, 0x30, 0x58, 0x5F, 0x62, 0x65,
                     0x73, 0x7D, 0x8D, 0x90, 0x97, 0x9C, 0xFF):
            assert code not in APPENDIX_A_CODES
            assert lookup(code).event == "unknown_exception", hex(code)

    def test_names_fit_the_hubs_event_label(self):
        """The hub keeps 48 characters of an event label. Two names ran
        over while they carried a "(VERIFY_ON_BENCH)" tag, which Table A-1
        makes untrue for every code that had it."""
        for code in APPENDIX_A_CODES:
            name = lookup(code).name
            assert len(name) <= 48, (hex(code), name)
            assert "VERIFY" not in name and "BENCH" not in name, hex(code)

    def test_game_start_and_end(self):
        assert exception_name(0x7E) == "game started"
        assert exception_name(0x7F) == "game ended"
        assert lookup(0x7E).category is ExceptionCategory.GAME

    def test_a_sample_of_the_names_against_the_table(self):
        want = {
            0x20: "general tilt", 0x23: "hopper empty detected",
            0x2C: "counterfeit bill detected",
            0x3B: "low backup battery detected",
            0x46: "reel mechanism disconnected",
            0x53: "no progressive information for 5 seconds",
            0x56: "SAS progressive level hit",
            0x57: "system validation request",
            0x66: "cash out button pressed", 0x67: "ticket inserted",
            0x68: "ticket transfer complete", 0x69: "AFT transfer complete",
            0x6A: "AFT request for host cashout",
            0x6B: "AFT request for host to cash out win",
            0x6C: "AFT request to register",
            0x6D: "AFT registration acknowledged",
            0x6E: "AFT registration cancelled",
            0x74: "printer paper low", 0x78: "printer carriage jammed",
            0x7B: "bill validator period totals reset",
            0x7C: "legacy bonus pay or multiplied jackpot",
        }
        for code, name in want.items():
            assert exception_name(code) == name, hex(code)


class _OneShotMachine:
    """Minimal implied-ACK machine: serves each queued exception once (re-
    sends until ACKed by a poll to another address), then 0x00."""

    def __init__(self, address, codes):
        self.address = address
        self.fifo = list(codes)
        self.pending = None

    def __call__(self, frame, wakeup):
        is_general = len(frame) == 1
        polled = (frame[0] & 0x7F) if is_general else frame[0]
        if is_general and polled == self.address:
            if self.pending is None:
                self.pending = self.fifo.pop(0) if self.fifo else None
            return bytes([self.pending]) if self.pending else b"\x00"
        self.pending = None          # any other poll = implied ACK
        return b""


class TestPollerTypedEvents:
    def test_typed_callback_receives_category_and_denom(self):
        machine = _OneShotMachine(1, [0x11, 0x48, 0x8C])
        port = MockSASSerialPort(machine)
        typed = []
        legacy = []
        poller = SASPoller(
            port, address=1,
            on_event=lambda a, c, n: legacy.append((a, c, n)),
            on_typed_event=lambda a, i: typed.append((a, i)))
        poller.poll_once()

        assert [i.event for _, i in typed] == \
            ["door_open", "bill_inserted", "game_selected"]
        assert typed[1][1].denom_cents == 500          # $5 bill
        assert all(isinstance(i, SASExceptionInfo) for _, i in typed)
        # legacy callback still fires in lockstep with identical codes
        assert [c for _, c, _ in legacy] == [0x11, 0x48, 0x8C]
        assert legacy[0][2] == "slot door opened"

    def test_typed_callback_isolated_like_legacy(self):
        machine = _OneShotMachine(1, [0x12])
        port = MockSASSerialPort(machine)

        def boom(*a):
            raise RuntimeError("typed cb blew up")

        poller = SASPoller(port, address=1, on_typed_event=boom)
        poller.poll_once()               # must not raise
        assert poller.state.callback_errors >= 1

    def test_unknown_code_flows_through_typed_path(self):
        machine = _OneShotMachine(1, [0xE9])
        port = MockSASSerialPort(machine)
        typed = []
        SASPoller(port, address=1,
                  on_typed_event=lambda a, i: typed.append(i)).poll_once()
        assert typed[0].category is ExceptionCategory.UNKNOWN
        assert typed[0].code == 0xE9
