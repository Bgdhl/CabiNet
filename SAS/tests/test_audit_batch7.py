"""
The remainder of the SAS 6.02 audit (batch 7): parsers, builders and the
lock cancel. Each test names its item.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from core.sas_implied_ack import ImpliedAckTransport
from core.sas_machine_info import (parse_enabled_game_numbers,
                                   parse_extended_validation_status)
from core.sas_meters import (TYPE_R_POLLS, build_enabled_features_poll,
                             build_meter_poll, parse_enabled_features,
                             parse_selected_meters_6f, parse_single_meter,
                             RESPONSE_DATA_PARSERS)
from core.sas_protocol import SASProtocol, sas_crc
from modules.aft.aft_handler import (
    AFTOutcome, AFTTransferRequest, LOCK_CODE_CANCEL, aft_transfer,
    build_aft_register_poll, build_aft_unregister_poll,
    parse_aft_register_response, REG_CODE_INITIALIZE, REG_CODE_UNREGISTER)
from transport.serial.sas_serial import MockSASSerialPort

from by_the_book import ByTheBookAFTMachine

NO_SLEEP = lambda s: None
KEY = bytes(range(20))
ASSET = bytes([0x92, 0x10, 0x00, 0x00])
POS = b"\x01\x00\x00\x00"


# ---------------------------------------------------------------------------
# Item 26 — a longer answer is read, not refused (§2.2.3)
# ---------------------------------------------------------------------------

class TestExtraBytesAreIgnored:
    def test_register_answer_with_a_field_appended(self):
        body = bytes([0x01]) + ASSET + KEY + POS + b"\xAA\xBB"
        reg = parse_aft_register_response(bytes([len(body)]) + body)
        assert (reg.status, reg.asset_number, reg.pos_id) == (0x01, ASSET, POS)

    def test_register_answer_cut_short_is_still_refused(self):
        body = bytes([0x01]) + ASSET + KEY
        with pytest.raises(ValueError):
            parse_aft_register_response(bytes([len(body)]) + body)

    def test_the_status_only_form_still_parses(self):
        assert parse_aft_register_response(bytes([0x01, 0x80])).status == 0x80

    def test_7b_with_a_field_appended(self):
        body = ASSET + b"\x00\x00" + bytes.fromhex("0180") \
            + bytes.fromhex("0030") + b"\x99"
        ev = parse_extended_validation_status(bytes([len(body)]) + body)
        assert (ev.cashable_expiration_days,
                ev.restricted_expiration_days) == (180, 30)

    def test_7b_cut_short_is_still_refused(self):
        with pytest.raises(ValueError):
            parse_extended_validation_status(bytes([9]) + bytes(9))

    def test_enabled_games_with_bytes_after_the_list(self):
        data = bytes([0x07, 0x02, 0x00, 0x01, 0x00, 0x12, 0xEE, 0xFF])
        assert parse_enabled_game_numbers(data) == [1, 12]

    def test_enabled_games_with_fewer_than_the_count_is_refused(self):
        with pytest.raises(ValueError):
            parse_enabled_game_numbers(bytes([0x03, 0x02, 0x00, 0x01]))


# ---------------------------------------------------------------------------
# Item 30 — A0 is not a bare poll; the jackpot meter has a parser
# ---------------------------------------------------------------------------

class TestEnabledFeaturesPoll:
    def test_a0_cannot_be_built_bare(self):
        assert 0xA0 not in TYPE_R_POLLS
        with pytest.raises(ValueError):
            build_meter_poll(1, 0xA0)

    def test_it_carries_a_game_number_and_a_crc(self):
        frame = build_enabled_features_poll(1)
        body = bytes([0x01, 0xA0, 0x00, 0x00])
        assert frame == body + sas_crc(body).to_bytes(2, "little")
        assert build_enabled_features_poll(1, 12)[2:4] == bytes([0x00, 0x12])

    def test_features3(self):
        f = parse_enabled_features(bytes([0, 0, 0, 0, 0x03, 0, 0, 0]))
        assert f.polling_40ms and f.multi_progressive_wins
        f = parse_enabled_features(bytes(8))
        assert not f.polling_40ms and not f.multi_progressive_wins

    def test_the_jackpot_meter(self):
        assert RESPONSE_DATA_PARSERS[0x14] is parse_single_meter
        assert build_meter_poll(1, 0x14) == bytes([0x01, 0x14])


# ---------------------------------------------------------------------------
# Item 31 — a meter the machine does not keep is not a zero
# ---------------------------------------------------------------------------

class TestExtendedMeters:
    def test_size_zero_is_none(self):
        body = bytes([0x00, 0x00,              # game 0000
                      0x24, 0x00, 0x04, 0x00, 0x01, 0x23, 0x45,
                      0x7F, 0x00, 0x00,        # not supported
                      0x0C, 0x00, 0x04, 0x00, 0x00, 0x00, 0x00])
        out = parse_selected_meters_6f(bytes([len(body)]) + body)
        assert out["meters"] == {0x0024: 12345, 0x007F: None, 0x000C: 0}


# ---------------------------------------------------------------------------
# Item 34 — a lock request whose answer was lost is cancelled
# ---------------------------------------------------------------------------

def _lock_frames(port):
    return [bytes(f) for f, _ in port.sent_frames
            if len(f) > 3 and f[1] == 0x74]


class TestALostLockAnswer:
    def test_the_request_is_cancelled(self):
        """Before: no answer meant no cancel, and a machine that had put
        the request on hold locked itself later with no transfer coming."""
        machine = ByTheBookAFTMachine()
        machine.deaf.add(0x74)
        port = ImpliedAckTransport(MockSASSerialPort(machine))
        result = aft_transfer(
            port, 1, AFTTransferRequest(transaction_id="LOSTLOCK",
                                        cashable_cents=500),
            ASSET, KEY, SASProtocol(), sleep=NO_SLEEP)
        assert result.outcome is AFTOutcome.LOCK_FAILED
        locks = _lock_frames(port)
        assert locks[0][2] == 0x00                 # the request
        assert locks[-1][2] == LOCK_CODE_CANCEL    # and its cancel
        assert machine.started == []               # no transfer went out


# ---------------------------------------------------------------------------
# Item 35 — unregister carries the code alone
# ---------------------------------------------------------------------------

class TestUnregister:
    def test_one_byte_body(self):
        body = bytes([0x01, 0x73, 0x01, 0x80])
        assert build_aft_unregister_poll(1) == \
            body + sas_crc(body).to_bytes(2, "little")

    def test_the_full_builder_refuses_it(self):
        with pytest.raises(ValueError):
            build_aft_register_poll(1, REG_CODE_UNREGISTER, ASSET, KEY, POS)
        assert build_aft_register_poll(1, REG_CODE_INITIALIZE, ASSET, KEY,
                                       POS)[2] == 0x1D


# ---------------------------------------------------------------------------
# Item 12 — the multi-denomination answer is checked before it is believed
# ---------------------------------------------------------------------------

def _b0(denom, base, data, address=1):
    body = bytes([denom, base]) + bytes(data)
    head = bytes([address, 0xB0, len(body)]) + body
    return head + sas_crc(head).to_bytes(2, "little")


class TestMultiDenomAnswer:
    P = SASProtocol()
    GAMES = bytes([0x03, 0x01, 0x00, 0x12])      # 0x56 payload: one game, 12

    def test_a_good_answer(self):
        dec = self.P.parse_multidenom_response(1, _b0(5, 0x56, self.GAMES),
                                               expect_denom=5)
        assert (dec["ok"], dec["outcome"], dec["denom"]) == (True, "data", 5)

    def test_a_bad_crc_is_not_acted_on(self):
        """Before: no CRC check at all on this answer."""
        good = _b0(5, 0x56, self.GAMES)
        bad = good[:-1] + bytes([good[-1] ^ 0xFF])
        dec = self.P.parse_multidenom_response(1, bad, expect_denom=5)
        assert (dec["ok"], dec["outcome"]) == (False, "malformed")

    def test_a_cut_short_answer_is_not_acted_on(self):
        dec = self.P.parse_multidenom_response(
            1, _b0(5, 0x56, self.GAMES)[:-3], expect_denom=5)
        assert (dec["ok"], dec["outcome"]) == (False, "malformed")

    def test_an_answer_for_another_denomination(self):
        """An answer the machine re-sent for the last denomination asked
        used to be credited to this one."""
        dec = self.P.parse_multidenom_response(1, _b0(4, 0x56, self.GAMES),
                                               expect_denom=5)
        assert (dec["ok"], dec["outcome"]) == (False, "wrong_denom")
        assert "0x04" in dec["errorText"] and "0x05" in dec["errorText"]

    def test_asking_for_the_default_accepts_what_it_resolves_to(self):
        dec = self.P.parse_multidenom_response(1, _b0(1, 0x56, self.GAMES),
                                               expect_denom=0)
        assert (dec["ok"], dec["denom"]) == (True, 1)

    def test_an_ack_for_the_right_denomination(self):
        dec = self.P.parse_multidenom_response(1, _b0(5, 0x09, b"\x01"),
                                               expect_denom=5)
        assert (dec["ok"], dec["outcome"]) == (True, "ack")


# ---------------------------------------------------------------------------
# Item 22 — a ticket's issue time is ours, not the machine's clock
# ---------------------------------------------------------------------------

import datetime

from core.sas_ticket_store import TicketStore
from core.sas_tito_host import capture_printed_ticket

from by_the_book import ByTheBookMachine


class TestTicketIssueTime:
    def test_a_machine_with_a_wrong_clock_does_not_backdate_the_ticket(
            self, tmp_path):
        """ByTheBookMachine stamps its records 11/16/2026 12:40:55. A real
        one after a RAM clear says something like 01/01/2000."""
        machine = ByTheBookMachine()
        machine.print_ticket("1234567890123456", 2500)
        port = ImpliedAckTransport(MockSASSerialPort(machine))
        store = TicketStore(path=str(tmp_path / "tickets.json"))
        before = datetime.datetime.now().replace(microsecond=0)
        result = capture_printed_ticket(port, 1, store, SASProtocol(),
                                        sleep=NO_SLEEP)
        rec = result.record
        issued = datetime.datetime.fromisoformat(rec["issuedAt"])
        assert before <= issued <= datetime.datetime.now()
        assert rec["extra"]["machineTime"] == "2026-11-16T12:40:55"
        assert rec["extra"]["validationType"] == 0


# ---------------------------------------------------------------------------
# Item 40 — the validation ID is the machine's own
# ---------------------------------------------------------------------------

from core.sas_tito_host import SASTITOHost, validation_machine_id
from core.sas_poller import SASPoller


class TestValidationMachineId:
    def test_three_bytes_never_zero_and_stable(self):
        a = validation_machine_id("smib-2831b6", 1)
        assert len(a) == 3 and any(a)
        assert a == validation_machine_id("smib-2831b6", 1)

    def test_two_machines_get_different_ids(self):
        ids = {validation_machine_id(s, n)
               for s in ("smib-2831b6", "smib-aaaaaa", "smib-bbbbbb")
               for n in (1, 2, 3)}
        assert len(ids) == 9

    def test_the_seed_sends_it(self, tmp_path):
        sent = []

        def machine(frame, wakeup):
            sent.append(bytes(frame))
            if len(frame) > 1 and frame[1] == 0x4C:
                body = bytes(frame[:8])
                return body + sas_crc(body).to_bytes(2, "little")
            return b"\x00" if len(frame) == 1 else b""

        mine = validation_machine_id("smib-2831b6", 1)
        tito = SASTITOHost(SASPoller(MockSASSerialPort(machine), address=1),
                           TicketStore(path=str(tmp_path / "t.json")),
                           auto_seed=False, sleep=NO_SLEEP, machine_id=mine)
        assert tito.seed_validation_id(force=True).ok
        seed = [f for f in sent if len(f) > 1 and f[1] == 0x4C][0]
        assert seed[2:5] == mine

    def test_a_zero_id_is_refused(self, tmp_path):
        with pytest.raises(ValueError):
            SASTITOHost(SASPoller(MockSASSerialPort(lambda f, w: b""),
                                  address=1),
                        TicketStore(path=str(tmp_path / "t.json")),
                        machine_id=bytes(3))


# ---------------------------------------------------------------------------
# Item 38 — one time limit for a whole call to the hub
# ---------------------------------------------------------------------------

from core.hub_ticket_client import KeepAliveHTTPClient


class _SlowConn:
    """An HTTP connection whose every step takes `step` seconds of the
    client's clock, and which records the socket timeouts it was given."""
    clock = None
    step = 1.0
    timeouts = []

    class _Sock:
        def settimeout(self, t):
            _SlowConn.timeouts.append(round(t, 3))

    def __init__(self, netloc, timeout=None):
        _SlowConn.timeouts.append(round(timeout, 3))
        self.sock = self._Sock()

    def _tick(self):
        _SlowConn.clock[0] += _SlowConn.step

    def request(self, *a, **k):
        self._tick()

    def getresponse(self):
        self._tick()

        class R:
            status, will_close = 200, False

            def read(s):
                self._tick()
                return b'{"ok": true}'
        return R()

    def close(self):
        pass


class TestOneDeadlineForTheWholeCall:
    def _client(self, timeout, step):
        client = KeepAliveHTTPClient("http://hub:8081/api/tito/", timeout)
        clock = [100.0]
        _SlowConn.clock, _SlowConn.step, _SlowConn.timeouts = clock, step, []
        client._conn_cls = _SlowConn
        client._clock = lambda: clock[0]
        return client, clock

    def test_each_step_gets_only_what_is_left(self):
        """Before: every step got the full 2.5 s again."""
        client, clock = self._client(2.5, 0.5)
        assert client.post_json(b"{}", "mint")[0] == 200
        assert _SlowConn.timeouts == [2.5, 2.5, 2.0, 1.5]

    def test_a_call_that_runs_past_the_limit_is_stopped(self):
        """Two slow steps use the time up; the third is not started. (A
        real socket would also have timed out inside the second step, on
        the shrunken timeout it was given.)"""
        client, clock = self._client(2.5, 1.5)
        with pytest.raises(OSError, match="ran past its 2.5 s limit"):
            client.post_json(b"{}", "mint")
        assert clock[0] - 100.0 == 3.0     # and not a third 1.5 s step
        assert _SlowConn.timeouts == [2.5, 2.5, 1.0]


# ---------------------------------------------------------------------------
# Items 32 and 33 — what the game enable commands say about themselves
# ---------------------------------------------------------------------------

def _run_game_commands(key, factory, commands):
    from test_sas_host_commands import _run_scripted
    served = {"done": False}

    def script(i, reports):
        if not served["done"] and any(
                isinstance(r.get("machineInfo"), dict) for r in reports):
            served["done"] = True
            return {"commands": commands}
        return {}

    return _run_scripted(key, factory, script, max_polls=300)


class TestGameEnableCommands:
    def test_a_disable_that_leaves_no_game_says_so(self):
        """Item 33: a bare 09 applies to every player denomination (Table
        16.1d). Nothing is refused; the result reports what is left."""
        from test_sas_host_commands import Lp09Machine
        run = _run_game_commands(
            "lastgame", lambda: Lp09Machine(address=1),
            [{"id": "off-1", "type": "sas_game_state", "game": 1,
              "enable": False}])
        r = run["by_id"]["off-1"]
        assert r["ok"] is True and r["enabledAfter"] == []
        assert "no game is enabled" in r["detail"]

    def test_an_enable_carries_the_list_and_no_warning(self):
        from test_sas_host_commands import Lp09Machine
        run = _run_game_commands(
            "addgame", lambda: Lp09Machine(address=1),
            [{"id": "on-2", "type": "sas_game_state", "game": 2,
              "enable": True}])
        r = run["by_id"]["on-2"]
        assert r["ok"] is True and r["enabledAfter"] == [1, 2]
        assert "detail" not in r

    def test_game_zero_behind_the_preamble_is_labelled_an_experiment(self):
        """Item 32: Table 7.6.1 gives long poll 09's game number as
        0001-9999. Game 0000 is still sent when asked, and says what it
        is."""
        from test_sas_host_commands import DenomMachine
        run = _run_game_commands(
            "gamezero", DenomMachine,
            [{"id": "zero-1", "type": "sas_denom_game_state", "game": 0,
              "enable": True, "denom": 1},
             {"id": "real-1", "type": "sas_denom_game_state", "game": 1,
              "enable": True, "denom": 1}])
        assert "outside the range" in run["by_id"]["zero-1"]["experiment"]
        assert "experiment" not in run["by_id"]["real-1"]
