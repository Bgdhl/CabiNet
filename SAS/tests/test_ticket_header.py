"""
Tests for the C2 ticket-header push: hub reply "ticketData" -> rev-gated
0x7C/0x7D wire write -> snapshot {"appliedRev", "detail"}.

SPEC ADJUDICATION UNDER TEST (SAS 6.02 full text, read 2026-07-09; golden
bytes below are hand-built straight from these tables):

* 0x7C Set Extended Ticket Data — §15.3 Table 15.3a:
      [addr][7C][len][code][dataLen][data]...[crc-lo][crc-hi]
  len = bytes following excluding CRC; elements repeat in any combination.
  Table 15.3c data codes: 00=Location (ASCII 40 max), 01=Address 1 (40),
  02=Address 2 (40), 10=Restricted ticket title (16), 20=Debit ticket
  title (16). Response (Table 15.3b): [addr][7C][flag][crc] — flag 01 =
  valid data received (ACK), 00 = invalid (NACK). "Data set using this
  long poll always takes precedence over any data set using long poll 7D."

* 0x7D Set Ticket Data — §15.4 Table 15.4a (POSITIONAL):
      [addr][7D][len 02-7E][hostID 2 binary][expiration 1 binary]
      [locLen 00-28][loc][a1Len][a1][a2Len][a2][crc]
  Host ID is LSB-first (§2: "All data exchanged in the binary format are
  sent least significant byte (LSB) first"); expiration 00 = never
  expires; a length byte 00 = "do not change"; trailing fields may be
  omitted entirely. Response (Table 15.4b) mirrors 15.3b. Max inner =
  2+1+3*(1+40) = 126 = 0x7E, exactly the table's ceiling.

* The normal CASH ticket title ("CASHOUT TICKET") is NOT host-settable
  anywhere in 6.02 — only the restricted (code 10) and debit (code 20)
  titles are. So the hub's titleCash must be SKIPPED (never mapped onto
  10/20 — those print on the wrong tickets), and the applier says so.

Tiers, mirroring test_sas_host_settings.py:
* UNIT — the builders' golden bytes, the reply-shape junk matrix, the
  TicketHeaderState rev/session gate, apply_ticket_header's 7C/7D
  decision table against a scripted transport, reporter pass-through.
* END-TO-END — offline defer, park defer, apply-on-resume, same-rev
  no-resend, a NACK that is not hammered, and snapshot shape in every
  state: sas_host.main() against a phased stub hub, every transition
  driven by what the satellite actually reports (never by sleeps).
* KEEPS TRYING (2026-11-16) — a header the machine did not take is tried
  again: on a growing wait, and at once when the machine says its setup
  changed. Unit tests on a pretend clock, a replay of the Bluebird 2 log
  that showed the gap, and two more sas_host.main() runs against a
  machine that is online but deaf to 0x7C/0x7D at first.
"""

import http.server
import json
import os
import signal
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import sas_host
from core.hub_ticket_client import HubTicketAuthority
from core.sas_protocol import SASProtocol, sas_crc
from core.sas_ticket_store import DEFAULT_SYSTEM_ID, TicketStore
from core.sas_tito_host import (
    CMD_SET_EXTENDED_TICKET_DATA, CMD_SET_TICKET_DATA,
    TICKET_DATA_LOCATION, TICKET_DATA_ADDRESS1, TICKET_DATA_ADDRESS2,
    TICKET_DATA_TITLE_RESTRICTED, TICKET_DATA_TITLE_DEBIT,
    build_set_extended_ticket_data, build_set_ticket_data,
    parse_ticket_data_flag,
)
from transport.serial.sas_serial import MockSASSerialPort


def _crc(body: bytes) -> bytes:
    return body + sas_crc(body).to_bytes(2, "little")


def _resp(address, command, code):
    return _crc(bytes([address, command, code]))


def _target(rev=1, prop="FUNCINO", line1="1 COLLECTOR WAY",
            line2="GAME ROOM NV", title=None, expire=0):
    """A parsed-shape target dict (what parse_ticket_data_reply emits)."""
    return {"propName": prop, "line1": line1, "line2": line2,
            "titleCash": title, "expireDays": expire, "rev": rev}


def _elements(frame):
    """Decode a 0x7C frame's (code, data) element stream for asserts."""
    inner = frame[3:-2]
    assert frame[2] == len(inner)
    out, i = [], 0
    while i < len(inner):
        code, n = inner[i], inner[i + 1]
        out.append((code, inner[i + 2:i + 2 + n]))
        i += 2 + n
    return out


# ---------------------------------------------------------------------------
# UNIT — builder golden bytes vs the spec adjudication
# ---------------------------------------------------------------------------

class TestBuildSetExtendedTicketData:
    def test_golden_full_header(self):
        """Table 15.3a/15.3c byte-for-byte: three (code, len, ASCII)
        elements, length = the element bytes, Kermit CRC LSB-first."""
        frame = build_set_extended_ticket_data(
            1, location="FUNCINO", address1="123 FAKE ST",
            address2="RENO NV")
        inner = (bytes([0x00, 7]) + b"FUNCINO"
                 + bytes([0x01, 11]) + b"123 FAKE ST"
                 + bytes([0x02, 7]) + b"RENO NV")
        assert frame == _crc(bytes([0x01, 0x7C, len(inner)]) + inner)
        assert len(inner) == 31

    def test_golden_single_field(self):
        frame = build_set_extended_ticket_data(3, location="FUNCINO")
        inner = bytes([0x00, 7]) + b"FUNCINO"
        assert frame == _crc(bytes([0x03, 0x7C, 9]) + inner)

    def test_titles_use_codes_10_and_20_with_16_char_clamp(self):
        """Table 15.3c: restricted=0x10, debit=0x20, ASCII 16 max."""
        frame = build_set_extended_ticket_data(
            1, restricted_title="PLAYABLE ONLY OR ELSE",  # 21 chars
            debit_title="DEBIT TICKET")
        els = _elements(frame)
        assert els == [
            (TICKET_DATA_TITLE_RESTRICTED, b"PLAYABLE ONLY OR"),   # 16
            (TICKET_DATA_TITLE_DEBIT, b"DEBIT TICKET"),
        ]

    def test_none_fields_are_omitted_entirely(self):
        frame = build_set_extended_ticket_data(1, address2="RENO NV")
        assert _elements(frame) == [(TICKET_DATA_ADDRESS2, b"RENO NV")]

    def test_40_char_clamp_and_ascii_scrub(self):
        frame = build_set_extended_ticket_data(1, location="B" * 45)
        assert _elements(frame) == [(TICKET_DATA_LOCATION, b"B" * 40)]
        frame = build_set_extended_ticket_data(1, location="CAFÉ\tX")
        assert _elements(frame) == [(TICKET_DATA_LOCATION, b"CAF??X")]

    def test_blank_line_is_spaces_not_empty(self):
        """Spec: 'To set a blank line ... set the ASCII text to one or
        more ASCII blanks (hex 20)' — spaces are legal; ''
        (revert-to-default vs do-not-change ambiguity) is refused."""
        frame = build_set_extended_ticket_data(1, location=" ")
        assert _elements(frame) == [(TICKET_DATA_LOCATION, b"\x20")]
        try:
            build_set_extended_ticket_data(1, location="")
            assert False, "empty '' must raise"
        except ValueError:
            pass

    def test_empty_string_and_all_none_raise(self):
        for kwargs in ({}, {"location": ""}, {"address1": ""}):
            try:
                build_set_extended_ticket_data(1, **kwargs)
                assert False, f"{kwargs} must raise"
            except ValueError:
                pass


class TestBuildSetTicketData:
    def test_golden_full_positional_layout(self):
        """Table 15.4a byte-for-byte: hostID LSB-first (§2 binary rule),
        expiration, then [len][text] x3."""
        frame = build_set_ticket_data(
            1, host_id=0x0203, expiration_days=5, location="FUNCINO",
            address1="123 FAKE ST", address2="RENO NV")
        inner = (b"\x03\x02"            # host ID 0x0203 LSB-first
                 + b"\x05"              # 5-day expiration
                 + bytes([7]) + b"FUNCINO"
                 + bytes([11]) + b"123 FAKE ST"
                 + bytes([7]) + b"RENO NV")
        assert frame == _crc(bytes([0x01, 0x7D, len(inner)]) + inner)

    def test_mid_stream_none_is_len_00_do_not_change(self):
        frame = build_set_ticket_data(
            1, host_id=1, expiration_days=0, location="FUNCINO",
            address1=None, address2="RENO NV")
        inner = (b"\x01\x00" + b"\x00"
                 + bytes([7]) + b"FUNCINO"
                 + b"\x00"                        # address1: do not change
                 + bytes([7]) + b"RENO NV")
        assert frame == _crc(bytes([0x01, 0x7D, len(inner)]) + inner)

    def test_trailing_none_fields_are_dropped(self):
        frame = build_set_ticket_data(1, host_id=1, expiration_days=0,
                                      location="X")
        inner = b"\x01\x00" + b"\x00" + bytes([1]) + b"X"
        assert frame == _crc(bytes([0x01, 0x7D, 0x05]) + inner)

    def test_max_frame_hits_the_tables_7e_ceiling_exactly(self):
        """Cross-check of the adjudication: 2+1+3*(1+40) = 126 = 0x7E,
        the exact upper bound Table 15.4a gives for the length byte."""
        frame = build_set_ticket_data(1, host_id=1, expiration_days=0,
                                      location="A" * 40,
                                      address1="B" * 40,
                                      address2="C" * 40)
        assert frame[2] == 0x7E

    def test_validation_errors(self):
        cases = [
            dict(host_id=-1, location="X"),
            dict(host_id=0x10000, location="X"),
            dict(expiration_days=-1, location="X"),
            dict(expiration_days=256, location="X"),
            dict(),                                # no text at all
            dict(location=""),                     # ambiguous empty
        ]
        for kwargs in cases:
            try:
                build_set_ticket_data(1, **kwargs)
                assert False, f"{kwargs} must raise"
            except ValueError:
                pass

    def test_allow_no_text_len_03_expiration_only_form(self):
        """Table 15.4a's length floor is 02; the len-03 form = host ID +
        expiration with every text slot untouched — the expiration-only
        write used after a 0x7C carried the text."""
        frame = build_set_ticket_data(1, host_id=0x0203, expiration_days=30,
                                      allow_no_text=True)
        inner = b"\x03\x02" + b"\x1e"
        assert frame == _crc(bytes([0x01, 0x7D, 0x03]) + inner)

    def test_allow_no_text_still_validates_ranges(self):
        try:
            build_set_ticket_data(1, expiration_days=999, allow_no_text=True)
            assert False, "must raise"
        except ValueError:
            pass


class TestParseTicketDataFlag:
    def test_ack_and_nack(self):
        assert parse_ticket_data_flag(b"\x01") is True
        assert parse_ticket_data_flag(b"\x00") is False

    def test_structural_lies_raise(self):
        for junk in (b"", b"\x02", b"\xff", b"\x01\x00", b"\x00\x00"):
            try:
                parse_ticket_data_flag(junk)
                assert False, f"{junk!r} must raise"
            except ValueError:
                pass


# ---------------------------------------------------------------------------
# UNIT — reply-shape junk matrix (the C2 exact-shape guard)
# ---------------------------------------------------------------------------

class TestParseTicketDataReply:
    GOOD = {"propName": "FUNCINO", "line1": "1 COLLECTOR WAY",
            "line2": "GAME ROOM NV", "titleCash": "FUN MONEY", "rev": 3}

    def test_valid_full_shape(self):
        out = sas_host.parse_ticket_data_reply(dict(self.GOOD))
        assert out == {"propName": "FUNCINO", "line1": "1 COLLECTOR WAY",
                       "line2": "GAME ROOM NV", "titleCash": "FUN MONEY",
                       "expireDays": 0, "rev": 3}

    def test_expire_days_absent_none_or_valid(self):
        """Absent/null = old hub -> 0; a legal 0..255 int passes through."""
        assert sas_host.parse_ticket_data_reply(
            dict(self.GOOD))["expireDays"] == 0
        assert sas_host.parse_ticket_data_reply(
            dict(self.GOOD, expireDays=None))["expireDays"] == 0
        for v in (0, 1, 30, 255):
            assert sas_host.parse_ticket_data_reply(
                dict(self.GOOD, expireDays=v))["expireDays"] == v

    def test_expire_days_junk_rejects_the_whole_dict(self):
        """A PRESENT junk expireDays is a malformed push — partial trust
        is how wrong bytes reach a one-shot wire."""
        for v in (True, False, "30", 3.5, -1, 256, [], {}):
            assert sas_host.parse_ticket_data_reply(
                dict(self.GOOD, expireDays=v)) is None

    def test_title_null_absent_or_blank_all_mean_no_title(self):
        for variant in (None, "", "   "):
            d = dict(self.GOOD, titleCash=variant)
            assert sas_host.parse_ticket_data_reply(d)["titleCash"] is None
        d = dict(self.GOOD)
        del d["titleCash"]
        assert sas_host.parse_ticket_data_reply(d)["titleCash"] is None

    def test_strings_stripped_and_clamped_to_64(self):
        d = dict(self.GOOD, propName="  FUNCINO  ", line1="A" * 100)
        out = sas_host.parse_ticket_data_reply(d)
        assert out["propName"] == "FUNCINO"
        assert out["line1"] == "A" * 64

    def test_rev_zero_and_blank_lines_are_legal(self):
        d = dict(self.GOOD, line1="", line2="", rev=0, titleCash=None)
        out = sas_host.parse_ticket_data_reply(d)
        assert out["rev"] == 0 and out["line1"] == "" and out["line2"] == ""

    def test_junk_matrix_returns_none(self):
        junk = [
            None, 5, "x", [], True,                     # not a dict
            {},                                          # everything absent
            dict(self.GOOD, propName=""),                # unset prop
            dict(self.GOOD, propName="   "),
            dict(self.GOOD, propName=7),
            dict(self.GOOD, propName=None),
            dict(self.GOOD, line1=None),                 # contract: str
            dict(self.GOOD, line1=5),
            dict(self.GOOD, line2=None),
            dict(self.GOOD, titleCash=5),
            dict(self.GOOD, titleCash=[]),
            dict(self.GOOD, titleCash=True),
            dict(self.GOOD, rev=None),
            dict(self.GOOD, rev=True),                   # bool is not a rev
            dict(self.GOOD, rev="3"),
            dict(self.GOOD, rev=-1),
            dict(self.GOOD, rev=1.5),
        ]
        d = dict(self.GOOD)
        del d["line1"]
        junk.append(d)
        d = dict(self.GOOD)
        del d["rev"]
        junk.append(d)
        for j in junk:
            assert sas_host.parse_ticket_data_reply(j) is None, j


# ---------------------------------------------------------------------------
# UNIT — TicketHeaderState rev/session gating
# ---------------------------------------------------------------------------

class TestTicketHeaderStateGating:
    def _fed(self, rev=1):
        st = sas_host.TicketHeaderState()
        st.on_reply(self.reply(rev))
        return st

    @staticmethod
    def reply(rev):
        return {"propName": "FUNCINO", "line1": "", "line2": "",
                "titleCash": None, "rev": rev}

    def test_initial_state_nothing_due_snapshot_shape(self):
        st = sas_host.TicketHeaderState()
        assert st.due() is None
        snap = st.snapshot()
        assert snap["appliedRev"] is None
        assert isinstance(snap["detail"], str) and snap["detail"]

    def test_junk_reply_never_arms(self):
        st = sas_host.TicketHeaderState()
        for junk in (None, "x", {"rev": 1}, {"propName": "", "line1": "",
                                             "line2": "", "rev": 1}):
            st.on_reply(junk)
        assert st.target is None and st.due() is None

    def test_valid_reply_arms_and_success_disarms(self):
        st = self._fed(rev=1)
        due = st.due()
        assert due is not None and due["rev"] == 1
        st.mark_attempted(1)
        st.record_result(1, True, "applied")
        assert st.due() is None
        assert st.snapshot() == {"appliedRev": 1, "detail": "applied"}

    def test_same_rev_never_resends_within_one_online_session(self):
        """The hub echoes the same rev every REPORT_SEC forever — after one
        applied rev the gate stays closed for every echo THAT SESSION."""
        st = self._fed(rev=1)
        st.mark_attempted(1)
        st.record_result(1, True, "applied")
        for _ in range(5):
            st.on_reply(self.reply(1))
            assert st.due() is None

    def test_rejoin_re_applies_even_an_already_applied_rev(self):
        """A REJOIN must re-assert the header, same rev or not.

        Nothing on the wire announces a RAM clear, and a RAM-cleared cabinet
        comes back carrying FACTORY ticket text while the hub still believes
        its header is live. This used to assert `due() is None` after
        on_online() — that assertion WAS the bug (AJ, 2026-07-26: "my ram
        cleared machine is back to the generic machine default"). One
        idempotent re-apply per online session is the price."""
        st = self._fed(rev=1)
        st.mark_attempted(1)
        st.record_result(1, True, "applied")
        assert st.due() is None              # settled in this session
        st.on_online()                       # rejoin
        due = st.due()
        assert due is not None and due["rev"] == 1
        # ...and it is still ONE attempt per session, never a hot loop.
        st.mark_attempted(1)
        assert st.due() is None

    def test_failure_is_not_retried_at_once_and_a_rev_bump_rearms(self):
        st = self._fed(rev=2)
        st.mark_attempted(2)
        st.record_result(2, False, "silence on both")
        for _ in range(5):
            st.on_reply(self.reply(2))
            assert st.due() is None          # no hot loop
        assert st.snapshot()["appliedRev"] is None
        st.on_reply(self.reply(3))           # rev bump re-arms
        assert st.due()["rev"] == 3

    def test_rejoin_rearms_exactly_one_attempt_for_a_failed_rev(self):
        st = self._fed(rev=2)
        st.mark_attempted(2)
        st.record_result(2, False, "silence")
        assert st.due() is None
        st.on_online()                       # new online session
        assert st.due()["rev"] == 2
        st.mark_attempted(2)
        assert st.due() is None              # and only one

    def test_mark_attempted_runs_before_the_wire(self):
        """A raising apply must not re-run: attempted is burned even if
        record_result never happens."""
        st = self._fed(rev=1)
        st.mark_attempted(1)
        assert st.due() is None


# ---------------------------------------------------------------------------
# UNIT — a header the machine did not take is tried again (2026-11-16)
# ---------------------------------------------------------------------------

class _Clock:
    """A clock the test moves by hand."""

    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def tick(self, sec):
        self.t += sec


def _reply(rev):
    return {"propName": "FUNCINO", "line1": "", "line2": "",
            "titleCash": None, "rev": rev}


def _state(rev=2):
    clock = _Clock()
    st = sas_host.TicketHeaderState(clock=clock)
    st.on_reply(_reply(rev))
    return st, clock


def _fail(st, rev=2, detail="silence on both"):
    assert st.due() is not None
    st.mark_attempted(rev)
    st.record_result(rev, False, detail)


class TestTicketHeaderKeepsTrying:
    def test_a_failed_attempt_is_due_again_after_its_wait(self):
        st, clock = _state()
        _fail(st)
        first = sas_host.TICKET_HEADER_RETRY_SEC[0]
        assert st.retry_in() == first
        clock.tick(first - 0.01)
        assert st.due() is None
        clock.tick(0.02)
        assert st.due()["rev"] == 2

    def test_the_wait_grows_then_holds_at_the_last_value(self):
        st, clock = _state()
        waits = []
        for _ in range(len(sas_host.TICKET_HEADER_RETRY_SEC) + 3):
            _fail(st)
            waits.append(st.retry_in())
            clock.tick(st.retry_in() + 0.01)
        table = list(sas_host.TICKET_HEADER_RETRY_SEC)
        assert waits == table + [table[-1]] * 3
        assert table == sorted(table) and table[0] >= 1.0   # never a hot loop
        assert table[-1] <= 60.0     # a ready machine waits a minute at most

    def test_it_never_gives_up_while_the_header_is_missing(self):
        st, clock = _state()
        for _ in range(500):
            _fail(st)
            clock.tick(st.retry_in() + 0.01)
        assert st.due()["rev"] == 2

    def test_success_ends_the_retries(self):
        st, clock = _state()
        _fail(st)
        clock.tick(60)
        st.mark_attempted(2)
        st.record_result(2, True, "applied")
        clock.tick(3600)
        assert st.due() is None and st.retry_in() is None
        assert st.failures == 0 and st.snapshot()["appliedRev"] == 2

    def test_a_rejoin_starts_the_wait_over(self):
        st, clock = _state()
        for _ in range(4):
            _fail(st)
            clock.tick(st.retry_in() + 0.01)
        st.on_online()
        _fail(st)                            # due at once in the new session
        assert st.retry_in() == sas_host.TICKET_HEADER_RETRY_SEC[0]

    def test_an_attempt_that_never_reports_back_stays_burned(self):
        """mark_attempted runs before the wire. If the apply blew up past
        record_result there must be no wait left over to re-run it."""
        st, clock = _state()
        _fail(st)
        clock.tick(60)
        st.mark_attempted(2)                 # ...and no record_result
        clock.tick(3600)
        assert st.due() is None

    def test_a_lost_event_sends_an_applied_header_again(self):
        st, clock = _state()
        st.mark_attempted(2)
        st.record_result(2, True, "applied")
        st.on_machine_event(lost=True)
        assert st.snapshot()["appliedRev"] is None   # no longer vouched for
        assert st.due() is None                      # ...but let it settle
        clock.tick(sas_host.TICKET_HEADER_SETTLE_SEC + 0.01)
        assert st.due()["rev"] == 2
        st.mark_attempted(2)
        st.record_result(2, True, "applied")
        clock.tick(3600)
        assert st.due() is None                      # once, not for ever

    def test_a_plain_event_leaves_an_applied_header_alone(self):
        st, clock = _state()
        st.mark_attempted(2)
        st.record_result(2, True, "applied")
        st.on_machine_event()
        clock.tick(3600)
        assert st.due() is None and st.snapshot()["appliedRev"] == 2

    def test_an_event_cuts_a_long_wait_short(self):
        st, clock = _state()
        for _ in range(6):
            _fail(st)
            clock.tick(st.retry_in() + 0.01)
        _fail(st)
        assert st.retry_in() == sas_host.TICKET_HEADER_RETRY_SEC[-1]
        st.on_machine_event()
        assert st.retry_in() == sas_host.TICKET_HEADER_SETTLE_SEC
        clock.tick(sas_host.TICKET_HEADER_SETTLE_SEC + 0.01)
        _fail(st)                            # due, and the wait starts over
        assert st.retry_in() == sas_host.TICKET_HEADER_RETRY_SEC[0]

    def test_a_burst_of_events_is_one_attempt(self):
        st, clock = _state()
        _fail(st)
        st.on_machine_event(lost=True)       # 0x7A, 0x3F, the seed: 0.1 s
        for _ in range(2):                   # apart, as on a RAM clear
            clock.tick(0.1)
            st.on_machine_event(lost=True)
        assert st.due() is None
        clock.tick(sas_host.TICKET_HEADER_SETTLE_SEC)
        assert st.due()["rev"] == 2
        st.mark_attempted(2)
        st.record_result(2, True, "applied")
        clock.tick(3600)
        assert st.due() is None

    def test_a_repeating_event_cannot_hold_the_attempt_off(self):
        """SAS 6.02 §15: 0x3F comes at power-up "and every fifteen seconds
        until configured". Whatever the rhythm, an event only ever brings
        the next attempt forward."""
        for every in (0.2, 1.0, 15.0):
            st, clock = _state()
            _fail(st)
            attempts, t = 0, 0.0
            while t < 120.0:
                st.on_machine_event(lost=True)
                for _ in range(int(every / 0.1)):
                    clock.tick(0.1)
                    t += 0.1
                    if st.due() is not None:
                        _fail(st)
                        attempts += 1
            assert attempts >= 120 / 60, (every, attempts)
            # ...and never faster than the settle time allows
            assert attempts <= 120 / sas_host.TICKET_HEADER_SETTLE_SEC + 1

    def test_tries_count_from_when_the_header_was_last_on(self):
        st, clock = _state()
        _fail(st)
        clock.tick(60)
        _fail(st)
        st.on_machine_event(lost=True)       # the wait starts over...
        assert st.failures == 0 and st.tries == 2    # ...the count does not
        clock.tick(sas_host.TICKET_HEADER_SETTLE_SEC + 0.01)
        st.mark_attempted(2)
        assert st.tries == 3
        st.record_result(2, True, "applied")
        assert st.tries == 0

    def test_an_event_before_any_header_arrives_changes_nothing(self):
        clock = _Clock()
        st = sas_host.TicketHeaderState(clock=clock)
        st.on_machine_event(lost=True)
        clock.tick(60)
        assert st.due() is None
        st.on_reply(_reply(1))
        assert st.due()["rev"] == 1

    def test_the_events_that_count_exist_and_exclude_game_selected(self):
        """The names must be real SASExceptionInfo events, or the hook is
        dead code. A player picking a game is not one of them."""
        from core.sas_exceptions import lookup
        named = {lookup(code).event for code in (0x7A, 0x3F, 0x3C)}
        assert named == set(sas_host.TICKET_HEADER_LOST_EVENTS)
        assert lookup(0x8C).event not in sas_host.TICKET_HEADER_LOST_EVENTS


class TestTheMachinesOwnWordOnItsTicketSettings:
    """SAS has no poll that reads the header text back. The expiry travels
    with it, and long poll 7B reports it at any time (audit item 39, and
    AJ 2026-11-16: "The machine should tell us what its ticket header is
    set as")."""

    def _applied(self, baseline=180):
        st, clock = _state()
        st.mark_attempted(2)
        st.record_result(2, True, "applied")
        st.expiry_baseline = baseline
        return st, clock

    def test_the_same_expiry_changes_nothing(self):
        st, clock = self._applied()
        for _ in range(5):
            assert st.on_expiry_report(180) is False
        clock.tick(3600)
        assert st.due() is None and st.snapshot()["appliedRev"] == 2

    def test_a_changed_expiry_sends_the_header_again(self):
        st, clock = self._applied()
        assert st.on_expiry_report(30) is True       # back to its default
        assert st.snapshot()["appliedRev"] is None
        clock.tick(sas_host.TICKET_HEADER_SETTLE_SEC + 0.01)
        assert st.due()["rev"] == 2
        # once: nothing more until the header has gone on again
        assert st.on_expiry_report(30) is False

    def test_it_compares_with_the_machines_own_report_not_the_hubs_value(
            self):
        """A machine that clamps the value (asked for 180, reports 99) must
        not be sent the header on every read for ever."""
        st, clock = self._applied(baseline=99)
        for _ in range(10):
            assert st.on_expiry_report(99) is False
        assert st.due() is None

    def test_no_baseline_or_no_answer_means_no_opinion(self):
        st, clock = self._applied(baseline=None)
        assert st.on_expiry_report(30) is False
        st, clock = self._applied()
        assert st.on_expiry_report(None) is False

    def test_a_failed_apply_forgets_the_baseline(self):
        st, clock = self._applied()
        st.on_machine_event(lost=True)
        clock.tick(sas_host.TICKET_HEADER_SETTLE_SEC + 0.01)
        st.mark_attempted(2)
        st.record_result(2, False, "silence")
        assert st.expiry_baseline is None
        assert st.on_expiry_report(30) is False


# The Bluebird 2's own log, 2026-11-16, in seconds after it came online at
# 12:33:53.762. The header attempt two seconds in met silence; the first
# ticket was printed 422 s in with no header from us on the machine.
_BB2_EVENTS = (
    (0.07, "meters_reset"),                    # 0x7A with the online edge
    (37.07, "validation_id_not_configured"),   # 0x3F
    (37.12, "seeded"),                         # our 0x4C seed was taken
    (127.99, "config_changed"),                # 0x3C, operator at the menu
    (142.72, "config_changed"),
    (146.44, "config_changed"),
)
_BB2_FIRST_TICKET = 422.12


def _replay_bluebird(ready_at, until=_BB2_FIRST_TICKET, step=0.05):
    """Drive the state the way the poll loop does, against a machine that
    takes ticket data only from `ready_at` (None = never). Returns the
    attempt times and the time the header went on (or None)."""
    st, clock = _state()
    t0 = clock()
    st.on_online()
    events = list(_BB2_EVENTS)
    attempts, applied = [], None
    while clock() - t0 <= until:
        now = clock() - t0
        while events and events[0][0] <= now:
            _, name = events.pop(0)
            if name == "seeded" or name in sas_host.TICKET_HEADER_LOST_EVENTS:
                st.on_machine_event(lost=True)
        due = st.due()
        if due is not None:
            st.mark_attempted(due["rev"])
            ok = ready_at is not None and now >= ready_at
            st.record_result(due["rev"], ok, "applied" if ok else "silence")
            attempts.append(round(now, 2))
            if ok and applied is None:
                applied = now
        clock.tick(step)
    return attempts, applied


class TestBluebirdRamClearReplay:
    def test_a_deaf_machine_gets_more_than_the_one_attempt(self):
        """The log shows one try, silence, and nothing more for the
        rest of the session. The same timeline now gets many."""
        attempts, applied = _replay_bluebird(ready_at=None)
        assert applied is None
        assert len(attempts) > 5

    def test_header_is_on_within_two_seconds_of_the_validation_exception(
            self):
        """If the machine takes ticket data once it has a validation ID —
        the likeliest reading of the log, and what SAS 6.02 §15 describes —
        the header is on at the latest 2 s after its 0x3F (sooner if a
        timed attempt happens to fall first), six minutes before the first
        ticket."""
        attempts, applied = _replay_bluebird(ready_at=37.2)
        assert applied is not None
        assert 37.2 <= applied <= 37.07 + sas_host.TICKET_HEADER_SETTLE_SEC + 0.5
        assert applied < _BB2_FIRST_TICKET - 360

    def test_with_the_timed_attempts_out_of_the_way_the_event_does_it(self):
        """The same replay with the waits made far longer than the log, so
        the 0x3F and the seed are the only thing that can bring it."""
        saved = sas_host.TICKET_HEADER_RETRY_SEC
        sas_host.TICKET_HEADER_RETRY_SEC = (3600.0,)
        try:
            attempts, applied = _replay_bluebird(ready_at=37.2)
        finally:
            sas_host.TICKET_HEADER_RETRY_SEC = saved
        assert applied is not None
        assert abs(applied - (37.07 + sas_host.TICKET_HEADER_SETTLE_SEC)) < 0.2

    def test_whenever_the_machine_is_ready_the_header_follows_in_a_minute(
            self):
        """We do not know when the Bluebird would have answered. Whatever
        the moment, the header is on within the longest wait."""
        longest = sas_host.TICKET_HEADER_RETRY_SEC[-1]
        for ready_at in (0.5, 3.0, 20.0, 37.2, 90.0, 130.0, 147.0, 200.0,
                         300.0, 360.0):
            attempts, applied = _replay_bluebird(ready_at=ready_at)
            assert applied is not None, ready_at
            assert applied - ready_at <= longest + 0.1, (ready_at, applied)
            assert applied < _BB2_FIRST_TICKET, ready_at

    def test_a_machine_that_never_answers_is_not_hammered(self):
        attempts, applied = _replay_bluebird(ready_at=None, until=3600.0,
                                             step=0.25)
        assert applied is None
        gaps = [b - a for a, b in zip(attempts, attempts[1:])]
        assert min(gaps) >= sas_host.TICKET_HEADER_SETTLE_SEC - 0.3
        assert len(attempts) <= 3600 / 60 + 15     # about one a minute


# ---------------------------------------------------------------------------
# UNIT — apply_ticket_header decision table (scripted transport)
# ---------------------------------------------------------------------------

class ScriptedTransport:
    def __init__(self, replies):
        self.replies = list(replies)
        self.frames = []

    def transact(self, frame, max_bytes=256):
        self.frames.append(bytes(frame))
        return self.replies.pop(0) if self.replies else b""


class TestApplyTicketHeader:
    def _apply(self, replies, target=None, **kw):
        tp = ScriptedTransport(replies)
        sleeps = []
        ok, detail = sas_host.apply_ticket_header(
            tp, 1, target or _target(), protocol=SASProtocol(),
            sleep=sleeps.append, **kw)
        return ok, detail, tp, sleeps

    def test_7c_ack_then_expiration_only_7d(self):
        """The text rides 0x7C; a 7C ACK is followed by one text-less 7D
        for the expiration, and then by long poll 7B, which SAS 6.02 §15.2
        gives for it (audit item 39). A machine silent to 7B changes
        nothing."""
        ok, detail, tp, sleeps = self._apply(
            [_resp(1, 0x7C, 0x01), _resp(1, 0x7D, 0x01)])
        assert ok is True and "0x7C" in detail
        assert "expiration 0d via 0x7D (ACK)" in detail
        assert "0x7B" not in detail
        assert len(tp.frames) == 3
        assert tp.frames[0] == build_set_extended_ticket_data(
            1, location="FUNCINO", address1="1 COLLECTOR WAY",
            address2="GAME ROOM NV")
        assert tp.frames[1] == build_set_ticket_data(
            1, host_id=DEFAULT_SYSTEM_ID, expiration_days=0,
            allow_no_text=True)
        # 7B: mask 0000, states 0000, cashable 9999 (never), restricted 0000
        assert tp.frames[2] == _crc(bytes([0x01, 0x7B, 0x08, 0, 0, 0, 0,
                                           0x99, 0x99, 0x00, 0x00]))
        assert sleeps == [sas_host.SAS_POLL_FLOOR] * 2

    def test_the_expiry_also_goes_by_7b_and_the_answer_is_kept(self):
        """The 7B answer carries the expiry the machine will print. It is
        handed back as the baseline for the later check."""
        answer_7b = _crc(bytes([0x01, 0x7B, 0x0A, 0xE8, 0x03, 0, 0, 0, 0,
                                0x01, 0x80, 0x00, 0x30]))
        seen = {}
        ok, detail, tp, _ = self._apply(
            [_resp(1, 0x7C, 0x01), _resp(1, 0x7D, 0x01), answer_7b],
            target=_target(expire=180), report=seen)
        assert ok is True
        assert "expiration 180d via 0x7D (ACK) and 0x7B (machine reports " \
               "180d)" in detail
        assert seen == {"expiry": 180}
        assert tp.frames[2] == _crc(bytes([0x01, 0x7B, 0x08, 0, 0, 0, 0,
                                           0x01, 0x80, 0x00, 0x00]))

    def test_expiration_7d_nack_or_silence_never_fails_the_header(self):
        """The expiration write is advisory: a machine that refuses or
        ignores the text-less 7D keeps its ACKed header (ok=True) with an
        honest detail."""
        ok, detail, _, _ = self._apply(
            [_resp(1, 0x7C, 0x01), _resp(1, 0x7D, 0x00)],
            target=_target(expire=30))
        assert ok is True and "30d 0x7D NACK" in detail
        ok, detail, _, _ = self._apply(
            [_resp(1, 0x7C, 0x01), b""], target=_target(expire=30))
        assert ok is True and "30d 0x7D silent" in detail

    def test_expiration_days_ride_both_7d_forms(self):
        """expireDays reaches the wire byte in the after-7C form AND the
        7C-silent fallback form."""
        _, _, tp, _ = self._apply(
            [_resp(1, 0x7C, 0x01), _resp(1, 0x7D, 0x01)],
            target=_target(expire=45))
        assert tp.frames[1] == build_set_ticket_data(
            1, host_id=DEFAULT_SYSTEM_ID, expiration_days=45,
            allow_no_text=True)
        _, _, tp, _ = self._apply(
            [b"", _resp(1, 0x7D, 0x01)], target=_target(expire=45))
        assert tp.frames[1] == build_set_ticket_data(
            1, host_id=DEFAULT_SYSTEM_ID, expiration_days=45,
            location="FUNCINO", address1="1 COLLECTOR WAY",
            address2="GAME ROOM NV")

    def test_7c_nack_never_escalates_to_7d(self):
        ok, detail, tp, _ = self._apply([_resp(1, 0x7C, 0x00)])
        assert ok is False and "NACK" in detail
        assert len(tp.frames) == 1              # the machine refused; stop

    def test_7c_silence_falls_back_to_7d_ack_with_pacing(self):
        ok, detail, tp, sleeps = self._apply([b"", _resp(1, 0x7D, 0x01)])
        assert ok is True and "0x7D" in detail
        assert "expiration 0d" in detail
        assert [f[1] for f in tp.frames] == [0x7C, 0x7D]
        assert tp.frames[1] == build_set_ticket_data(
            1, host_id=DEFAULT_SYSTEM_ID, expiration_days=0,
            location="FUNCINO", address1="1 COLLECTOR WAY",
            address2="GAME ROOM NV")
        assert sleeps == [sas_host.SAS_POLL_FLOOR]

    def test_both_silent_is_an_honest_failure(self):
        ok, detail, tp, _ = self._apply([b"", b""])
        assert ok is False and "silence on both" in detail
        assert [f[1] for f in tp.frames] == [0x7C, 0x7D]

    def test_corrupt_foreign_and_malformed_replies_are_silence(self):
        """The classify rule: bad CRC, a foreign address, a wrong command
        echo, or an out-of-range flag byte never count as an ACK — each
        falls through to the 7D fallback."""
        good = _resp(1, 0x7C, 0x01)
        corrupt = good[:-1] + bytes([good[-1] ^ 0xFF])
        for first in (corrupt, _resp(2, 0x7C, 0x01), _resp(1, 0x7E, 0x01),
                      _resp(1, 0x7C, 0x07), _crc(bytes([1, 0x7C, 1, 0]))):
            ok, detail, tp, _ = self._apply([first, b""])
            assert ok is False, first.hex()
            assert [f[1] for f in tp.frames] == [0x7C, 0x7D]

    def test_7d_nack_reported(self):
        ok, detail, tp, _ = self._apply([b"", _resp(1, 0x7D, 0x00)])
        assert ok is False and "0x7D NACK" in detail

    def test_title_cash_is_skipped_and_said_so(self):
        """titleCash has NO SAS mapping — the frame must carry only codes
        00/01/02 (never 10/20) and the verdict must admit the skip."""
        ok, detail, tp, _ = self._apply(
            [_resp(1, 0x7C, 0x01)], target=_target(title="FUN MONEY"))
        assert ok is True
        assert "titleCash skipped" in detail
        codes = [c for c, _ in _elements(tp.frames[0])]
        assert codes == [TICKET_DATA_LOCATION, TICKET_DATA_ADDRESS1,
                         TICKET_DATA_ADDRESS2]

    def test_blank_lines_are_pushed_as_a_single_space(self):
        """A blank line in Options means PRINT NOTHING THERE, so it must
        reach the wire — omitting it leaves the EGM's factory placeholder
        ("YOUR CITY, STATE ZIP") printing on real tickets.

        The value is a single SPACE, never "": per §15.3 a zero-length 7C
        element means 'revert to default', which would re-arm the very
        placeholder this is meant to kill."""
        ok, _, tp, _ = self._apply(
            [_resp(1, 0x7C, 0x01)], target=_target(line1="", line2=""))
        assert ok is True
        assert _elements(tp.frames[0]) == [
            (TICKET_DATA_LOCATION, b"FUNCINO"),
            (TICKET_DATA_ADDRESS1, b" "),
            (TICKET_DATA_ADDRESS2, b" "),
        ]


# ---------------------------------------------------------------------------
# UNIT — HubReporter._take_commands "ticketData" pass-through
# ---------------------------------------------------------------------------

class TestTakeCommandsTicketData:
    def _reporter(self, state):
        return sas_host.HubReporter("http://127.0.0.1:9", "unit", "(mock)",
                                    1, None, None, None, None,
                                    ticket_header=state)

    def test_raw_value_reaches_the_state(self):
        st = sas_host.TicketHeaderState()
        rep = self._reporter(st)
        rep._take_commands(json.dumps({"ticketData": {
            "propName": "FUNCINO", "line1": "", "line2": "",
            "titleCash": None, "rev": 4}}).encode())
        assert st.target is not None and st.target["rev"] == 4

    def test_absent_key_and_junk_change_nothing(self):
        st = sas_host.TicketHeaderState()
        rep = self._reporter(st)
        rep._take_commands(b'{"ok": true, "commands": []}')
        rep._take_commands(b'{"ticketData": "junk"}')
        rep._take_commands(b'{"ticketData": {"rev": true}}')
        rep._take_commands(b"not json")
        assert st.target is None

    def test_raising_state_is_swallowed_commands_still_parse(self):
        class Boom:
            def on_reply(self, value):
                raise RuntimeError("scripted ingest blow-up")

        rep = self._reporter(Boom())
        rep._take_commands(json.dumps(
            {"ticketData": {"propName": "X"},
             "commands": [{"id": "c1", "type": "t"}]}).encode())
        assert [c["id"] for c in rep.pending_commands] == ["c1"]

    def test_no_state_wired_is_fine(self):
        rep = sas_host.HubReporter("http://127.0.0.1:9", "unit", "(mock)",
                                   1, None, None, None, None)
        rep._take_commands(b'{"ticketData": {"propName": "X"}}')
        assert not rep.pending_commands


# ---------------------------------------------------------------------------
# END-TO-END — offline defer / park defer / apply / no-resend / NACK
# ---------------------------------------------------------------------------

def _td(rev):
    return {"propName": "FUNCINO", "line1": "1 COLLECTOR WAY",
            "line2": "GAME ROOM NV", "titleCash": "FUN MONEY", "rev": rev}


class TicketHeaderMachine:
    """Boots DARK (answers nothing -> the satellite stays offline), then
    answers general polls with 'no activity' and 0x7C/0x7D with the
    Table 15.3b/15.4b flag response (scripted ACK/NACK). Everything else
    — meter type-R reads, AFT polls — is silence, like a machine that
    never learned them."""

    def __init__(self, address=1):
        self.address = address
        self.dark = True
        self.ack = True
        self.ticket_frames = []     # (cmd, frame) for CRC-valid 7C/7D

    def __call__(self, frame, wakeup):
        if self.dark or not wakeup[0]:
            return b""
        if len(frame) == 1:                       # general poll
            if (frame[0] & 0x7F) != self.address:
                return b""
            return b"\x00"
        if frame[0] != self.address or len(frame) < 4:
            return b""
        body, crc = frame[:-2], frame[-2:]
        if sas_crc(body).to_bytes(2, "little") != crc:
            return b""                            # type-R reads: silence
        cmd = frame[1]
        if cmd in (CMD_SET_EXTENDED_TICKET_DATA, CMD_SET_TICKET_DATA):
            self.ticket_frames.append((cmd, bytes(frame)))
            return _resp(self.address, cmd, 0x01 if self.ack else 0x00)
        return b""


class _PhasedHub(http.server.HTTPServer):
    """Reply state machine over the satellite's own reports:

      offline  -> machine dark, serve rev 1: the satellite must NOT try
                  the wire (frames sample == 0), then wake the machine;
      apply    -> serve rev 1 until a report shows appliedRev 1;
      soak     -> 4 more rev-1 echoes: the frame count must FREEZE;
      park_arm -> sasEnabled false, NO ticketData, until the polls
                  counter freezes (the poll thread is provably parked);
      park_td  -> still parked, serve rev 2 for 4 reports: no frames;
      resume   -> sasEnabled true + rev 2 until appliedRev 2, then flip
                  the machine to NACK;
      nack     -> serve rev 3 until one attempt hits the wire;
      nacksoak -> 4 more rev-3 echoes: exactly that ONE attempt, then
                  done (SIGTERM main())."""

    def setup_state(self, machine):
        self.lock = threading.Lock()
        self.machine = machine
        self.reports = []                # [(phase, report, frames), ...]
        self.phase = "offline"
        self.offline_seen = 0
        self.park_polls = None
        self.park_frozen = 0
        self.park_td_seen = 0
        self.soak_left = 4
        self.nacksoak_left = 4
        self.samples = {}
        self.park_applied = "unset"
        self.final_td = None
        self.signaled = False
        self.t0 = time.monotonic()

    def reply_for(self, rep):
        td = rep.get("ticketData") or {}
        frames = len(self.machine.ticket_frames)
        if time.monotonic() - self.t0 > 20 and self.phase != "done":
            self.phase = "done"           # watchdog: never hang the suite
        if self.phase == "offline":
            if rep.get("online") is False and "ticketData" in rep:
                self.offline_seen += 1
            if self.offline_seen >= 4:
                self.samples["after_offline"] = frames
                self.machine.dark = False
                self.phase = "apply"
            else:
                return {"ok": True, "sasEnabled": True, "ticketData": _td(1)}
        if self.phase == "apply":
            if td.get("appliedRev") == 1:
                self.samples["after_apply1"] = frames
                self.phase = "soak"
            else:
                return {"ok": True, "sasEnabled": True, "ticketData": _td(1)}
        if self.phase == "soak":
            if self.soak_left > 0:
                self.soak_left -= 1
                return {"ok": True, "sasEnabled": True, "ticketData": _td(1)}
            self.samples["after_soak"] = frames
            self.phase = "park_arm"
        if self.phase == "park_arm":
            if rep.get("sasEnabled") is False:
                if rep.get("polls") == self.park_polls:
                    self.park_frozen += 1
                self.park_polls = rep.get("polls")
            if self.park_frozen >= 2:
                self.samples["park_engaged"] = frames
                self.phase = "park_td"
            else:
                return {"ok": True, "sasEnabled": False}
        if self.phase == "park_td":
            if self.park_td_seen >= 4:
                self.samples["after_park"] = frames
                self.park_applied = td.get("appliedRev")
                self.phase = "resume"
            else:
                self.park_td_seen += 1
                return {"ok": True, "sasEnabled": False,
                        "ticketData": _td(2)}
        if self.phase == "resume":
            if td.get("appliedRev") == 2:
                self.samples["after_apply2"] = frames
                self.machine.ack = False
                self.phase = "nack"
            else:
                return {"ok": True, "sasEnabled": True, "ticketData": _td(2)}
        if self.phase == "nack":
            if frames > self.samples["after_apply2"]:
                self.phase = "nacksoak"
            else:
                return {"ok": True, "sasEnabled": True, "ticketData": _td(3)}
        if self.phase == "nacksoak":
            if self.nacksoak_left > 0:
                self.nacksoak_left -= 1
                return {"ok": True, "sasEnabled": True, "ticketData": _td(3)}
            self.samples["after_nacksoak"] = frames
            self.final_td = td
            self.phase = "done"
        if not self.signaled:
            self.signaled = True
            os.kill(os.getpid(), signal.SIGTERM)
        return {"ok": True}


class _HubHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        srv = self.server
        if self.path != "/api/sas/report":
            self.send_response(404)
            self.end_headers()
            return
        try:
            rep = json.loads(body)
        except ValueError:
            rep = {}
        with srv.lock:
            srv.reports.append((srv.phase, rep,
                                len(srv.machine.ticket_frames)))
            reply = srv.reply_for(rep)
        payload = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):              # keep pytest output clean
        pass


_CACHE = {}


def _drive_main(httpd, port, smib_id, patches=None, backstop_sec=25.0):
    """Run sas_host.main() against a stub hub and a mock serial port until
    the hub SIGTERMs it. `patches` are extra sas_host module values for the
    run. Returns the hub's reports."""
    tmp = tempfile.mkdtemp(prefix="sas_ticket_header_test_")
    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()

    captured = []
    _RBase = sas_host.HubReporter

    class _CapturingReporter(_RBase):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            captured.append(self)

    patches = dict(patches or {})
    saved_signals = {s: signal.getsignal(s)
                     for s in (signal.SIGTERM, signal.SIGINT)}
    saved = {n: getattr(sas_host, n) for n in
             ("REPORT_SEC", "HubReporter", "open_port",
              "TicketStore", "HubTicketAuthority") + tuple(patches)}
    saved_argv = sys.argv
    backstop = threading.Timer(backstop_sec,
                               lambda: os.kill(os.getpid(), signal.SIGTERM))
    backstop.daemon = True
    try:
        sas_host.REPORT_SEC = 0.05
        sas_host.HubReporter = _CapturingReporter
        sas_host.open_port = lambda path, mock, address, protocol: port
        sas_host.TicketStore = lambda: TicketStore(
            path=os.path.join(tmp, "tickets.json"))
        sas_host.HubTicketAuthority = lambda hub, smib, local, **kw: \
            HubTicketAuthority(hub, smib, local,
                               journal_path=os.path.join(tmp, "journal.json"),
                               start_sync_thread=False, **kw)
        for name, value in patches.items():
            setattr(sas_host, name, value)
        sys.argv = ["sas_host.py", "--mock", "--interval", "0.002",
                    "--hub", f"http://127.0.0.1:{httpd.server_address[1]}",
                    "--smib-id", smib_id]
        backstop.start()
        sas_host.main()
    finally:
        backstop.cancel()
        sys.argv = saved_argv
        for name, value in saved.items():
            setattr(sas_host, name, value)
        for sig, handler in saved_signals.items():
            signal.signal(sig, handler)
        if captured:
            captured[0].stop = True
        time.sleep(0.12)                       # let a final report land
        httpd.shutdown()
        server_thread.join(timeout=2)

    with httpd.lock:
        return list(httpd.reports)


def _run_ticket_channel():
    """Run sas_host.main() once against the phased hub and memoize; every
    e2e test asserts on this single run (~2 s)."""
    if _CACHE:
        return _CACHE

    machine = TicketHeaderMachine(address=1)
    port = MockSASSerialPort(machine)
    httpd = _PhasedHub(("127.0.0.1", 0), _HubHandler)
    httpd.setup_state(machine)
    reports = _drive_main(httpd, port, "pytest-ticket-smib")
    _CACHE.update(machine=machine, hub=httpd, reports=reports,
                  samples=dict(httpd.samples))
    return _CACHE


class TestTicketHeaderEndToEnd:
    def test_run_completed_all_phases(self):
        run = _run_ticket_channel()
        assert run["hub"].phase == "done"
        for key in ("after_offline", "after_apply1", "after_soak",
                    "park_engaged", "after_park", "after_apply2",
                    "after_nacksoak"):
            assert key in run["samples"], key

    def test_offline_defers_no_wire_attempt(self):
        """Four reports rode with a pending rev while the machine was
        dark — zero 0x7C/0x7D frames reached it (C2: machine-online
        only)."""
        run = _run_ticket_channel()
        assert run["samples"]["after_offline"] == 0
        offline = [rep for ph, rep, _ in run["reports"]
                   if ph == "offline"]
        assert len(offline) >= 4
        assert all(rep["ticketData"]["appliedRev"] is None
                   for rep in offline)

    def test_apply_on_first_online_session(self):
        run = _run_ticket_channel()
        # a successful apply is now TWO frames: 0x7C text + the text-less
        # 0x7D expiration write
        assert run["samples"]["after_apply1"] == 2
        cmd, frame = run["machine"].ticket_frames[0]
        assert cmd == CMD_SET_EXTENDED_TICKET_DATA
        assert b"FUNCINO" in frame
        cmd2, frame2 = run["machine"].ticket_frames[1]
        assert cmd2 == CMD_SET_TICKET_DATA
        assert frame2[2] == 0x03                  # len-03 expiration-only

    def test_same_rev_never_resends(self):
        """Rev 1 kept echoing for 4+ reports after the apply — the frame
        count froze at the apply's two frames."""
        run = _run_ticket_channel()
        assert run["samples"]["after_soak"] == \
            run["samples"]["after_apply1"] == 2

    def test_park_defers_pending_rev(self):
        """Rev 2 arrived while the poll thread was provably parked (polls
        counter frozen): no frames moved, appliedRev stayed 1; the apply
        happened only after resume."""
        run = _run_ticket_channel()
        assert run["samples"]["park_engaged"] == 2
        assert run["samples"]["after_park"] == 2
        assert run["hub"].park_applied == 1
        assert run["samples"]["after_apply2"] == 4

    def test_nack_gets_exactly_one_attempt_and_an_honest_verdict(self):
        """The machine NACKed rev 3: one 0x7C attempt (no 0x7D of any kind
        — a NACK is understanding, not silence), appliedRev pinned at 2,
        and the snapshot detail says NACK. One attempt across the four
        reports that follow: the next is seconds away, not the next poll."""
        run = _run_ticket_channel()
        assert run["samples"]["after_nacksoak"] == \
            run["samples"]["after_apply2"] + 1 == 5
        # frames alternate 7C,7D per ACKed apply; the NACKed rev adds ONE
        # bare 7C at the tail
        cmds = [cmd for cmd, _ in run["machine"].ticket_frames]
        assert cmds == [CMD_SET_EXTENDED_TICKET_DATA, CMD_SET_TICKET_DATA,
                        CMD_SET_EXTENDED_TICKET_DATA, CMD_SET_TICKET_DATA,
                        CMD_SET_EXTENDED_TICKET_DATA]
        final = run["hub"].final_td
        assert final["appliedRev"] == 2
        assert "NACK" in final["detail"]

    def test_snapshot_shape_stable_in_every_state(self):
        """C2/C4: every report — offline, live, parked — carries
        ticketData as {"appliedRev": int|None, "detail": str}."""
        reports = [rep for _, rep, _ in _run_ticket_channel()["reports"]]
        assert reports
        for rep in reports:
            td = rep["ticketData"]
            assert set(td) == {"appliedRev", "detail"}
            assert td["appliedRev"] is None or isinstance(
                td["appliedRev"], int)
            assert isinstance(td["detail"], str)


# ---------------------------------------------------------------------------
# END-TO-END — a machine that is online but deaf to ticket data at first
# ---------------------------------------------------------------------------

class DeafAtFirstMachine:
    """A RAM-cleared machine before it is set up: on the link from the
    first poll and answering general polls, but silent to 0x7C/0x7D until
    `ready`. It NEVER drops off the link — the case the one-attempt rule
    had no answer for. `raise_next` is an exception code handed out on the
    next general poll (0x3C = operator changed configuration, 0x3F =
    validation ID not configured). It takes the 0x4C validation seed the
    satellite sends after a 0x3F, echoing it as §4.6.1 says."""

    def __init__(self, address=1):
        self.address = address
        self.ready = False
        self.raise_next = None
        self.seeds = 0              # 0x4C validation seeds taken
        self.deaf_frames = []       # 7C/7D that met silence
        self.ticket_frames = []     # 7C/7D that were answered

    def __call__(self, frame, wakeup):
        if not wakeup[0]:
            return b""
        if len(frame) == 1:                       # general poll
            if (frame[0] & 0x7F) != self.address:
                return b""
            if self.raise_next is not None:
                code, self.raise_next = self.raise_next, None
                return bytes([code])
            return b"\x00"
        if frame[0] != self.address or len(frame) < 4:
            return b""
        body, crc = frame[:-2], frame[-2:]
        if sas_crc(body).to_bytes(2, "little") != crc:
            return b""
        cmd = frame[1]
        if cmd in (CMD_SET_EXTENDED_TICKET_DATA, CMD_SET_TICKET_DATA):
            if not self.ready:
                self.deaf_frames.append(cmd)
                return b""
            self.ticket_frames.append((cmd, bytes(frame)))
            return _resp(self.address, cmd, 0x01)
        if cmd == 0x4C:                           # the validation seed
            self.seeds += 1
            return bytes(frame)                   # echo: ID + sequence
        return b""


class _DeafHub(http.server.HTTPServer):
    """deaf  -> serve rev 1 until a report carries the silence verdict;
                then make the machine ready and have it raise 0x3C
                ("event" mode) or 0x3F ("seed" mode);
       wait  -> serve rev 1 until a report shows appliedRev 1, then done."""

    def setup_state(self, machine, mode):
        self.lock = threading.Lock()
        self.machine = machine
        self.mode = mode
        self.reports = []
        self.phase = "deaf"
        self.samples = {}
        self.signaled = False
        self.t0 = time.monotonic()

    def reply_for(self, rep):
        td = rep.get("ticketData") or {}
        if time.monotonic() - self.t0 > 15 and self.phase != "done":
            self.phase = "done"                   # never hang the suite
        if self.phase == "deaf":
            if "silence on both" in (td.get("detail") or ""):
                self.samples["deaf_before"] = len(self.machine.deaf_frames)
                self.samples["ready_at"] = time.monotonic()
                self.machine.ready = True
                if self.mode == "event":
                    self.machine.raise_next = 0x3C
                elif self.mode == "seed":
                    self.machine.raise_next = 0x3F
                self.phase = "wait"
            return {"ok": True, "sasEnabled": True, "ticketData": _td(1)}
        if self.phase == "wait":
            if td.get("appliedRev") == 1:
                self.samples["applied_after"] = (time.monotonic()
                                                 - self.samples["ready_at"])
                self.samples["final_td"] = td
                self.phase = "done"
            else:
                return {"ok": True, "sasEnabled": True, "ticketData": _td(1)}
        if not self.signaled:
            self.signaled = True
            os.kill(os.getpid(), signal.SIGTERM)
        return {"ok": True}


_DEAF_CACHE = {}


def _run_deaf_machine(mode):
    """One sas_host.main() run per mode, memoized.

    "event": the wait after a failure is 30 s, far longer than the run, so
             only the machine's 0x3C can explain a second attempt.
    "seed":  the same 30 s wait, the machine raises 0x3F, and the list of
             exception names that re-arm the header is EMPTY — so only the
             hook on the validation seed can explain it. This is the path a
             RAM clear takes, and it cannot be tried on a real machine
             without one.
    "timer": the machine raises nothing, so only the wait can."""
    if mode in _DEAF_CACHE:
        return _DEAF_CACHE[mode]
    machine = DeafAtFirstMachine(address=1)
    port = MockSASSerialPort(machine)
    httpd = _DeafHub(("127.0.0.1", 0), _HubHandler)
    httpd.setup_state(machine, mode)
    patches = {"event": {"TICKET_HEADER_RETRY_SEC": (30.0,),
                         "TICKET_HEADER_SETTLE_SEC": 0.2,
                         "MACHINE_INFO_SETTLE_SEC": 0.15},
               "seed": {"TICKET_HEADER_RETRY_SEC": (30.0,),
                        "TICKET_HEADER_SETTLE_SEC": 0.2,
                        "TICKET_HEADER_LOST_EVENTS": ()},
               "timer": {"TICKET_HEADER_RETRY_SEC": (0.3, 0.3)}}[mode]
    reports = _drive_main(httpd, port, f"pytest-deaf-{mode}", patches,
                          backstop_sec=20.0)
    _DEAF_CACHE[mode] = dict(machine=machine, hub=httpd, reports=reports,
                             samples=dict(httpd.samples))
    return _DEAF_CACHE[mode]


class TestDeafAtFirstMachineEndToEnd:
    def _check_common(self, run):
        assert run["hub"].phase == "done"
        assert "applied_after" in run["samples"], run["samples"]
        # the header went on, as one 0x7C + one 0x7D, once
        assert [cmd for cmd, _ in run["machine"].ticket_frames] == [
            CMD_SET_EXTENDED_TICKET_DATA, CMD_SET_TICKET_DATA]
        assert b"FUNCINO" in run["machine"].ticket_frames[0][1]
        assert "applied" in run["samples"]["final_td"]["detail"]
        # ...and the machine never left the link to make that happen
        online = [rep.get("online") for _, rep, _ in run["reports"]]
        first_on = online.index(True)
        assert all(online[first_on:]), online

    def test_the_machines_own_event_brings_the_header(self):
        run = _run_deaf_machine("event")
        self._check_common(run)
        # one deaf attempt (7C then the 7D fallback) before the event, and
        # with a 30 s wait nothing but the 0x3C can have caused the next
        assert run["samples"]["deaf_before"] == 2
        assert run["machine"].deaf_frames == [
            CMD_SET_EXTENDED_TICKET_DATA, CMD_SET_TICKET_DATA]
        assert run["samples"]["applied_after"] < 5.0

    def test_the_validation_seed_after_a_ram_clear_brings_the_header(self):
        run = _run_deaf_machine("seed")
        self._check_common(run)
        assert run["machine"].seeds == 1              # 0x3F -> our 0x4C
        assert run["samples"]["deaf_before"] == 2
        assert run["machine"].deaf_frames == [
            CMD_SET_EXTENDED_TICKET_DATA, CMD_SET_TICKET_DATA]
        assert run["samples"]["applied_after"] < 5.0

    def test_with_no_event_at_all_the_wait_brings_it(self):
        run = _run_deaf_machine("timer")
        self._check_common(run)
        assert run["machine"].raise_next is None      # nothing was raised
        assert len(run["machine"].deaf_frames) >= 2
        assert run["samples"]["applied_after"] < 5.0

    def test_the_verdict_no_longer_promises_a_rejoin(self):
        run = _run_deaf_machine("timer")
        deaf = [rep["ticketData"]["detail"] for ph, rep, _ in run["reports"]
                if "silence on both" in rep["ticketData"]["detail"]]
        assert deaf
        assert all("keeps trying" in d and "rejoin" not in d for d in deaf)
