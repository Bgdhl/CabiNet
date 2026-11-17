"""
Host cashout, SAS 6.02 section 8.7 (audit batch 5: items 6, 14 and 15).

"Cashout to host" is a setting on the machine. With it on, a press of CASH
OUT raises exception 6A (a win that has to be cashed out raises 6B) and the
host has eight seconds to answer with a long poll 72. These tests run
against ByTheBookAFTMachine, which keeps that section:

  item 15  a request we are not taking was left unanswered, so the machine
           waited out its eight seconds on every cash-out, and tilted if
           its mode was hard. It is now declined with a zero-amount 72.
  item 14  a 6B was answered like a 6A: type 80 for whatever was on the
           credit meter, which the machine must refuse with status 87.
  item 6   the arm read "available transfers" bit 1 as the host cashout
           enable, and never set the enable where the machine lets the
           host do so.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import sas_host
from core.sas_implied_ack import ImpliedAckTransport
from core.sas_poller import SASPoller
from modules.aft.aft_handler import (
    AFTHost, AFTOutcome, AFTTransferRequest, ALL_OF_IT_CENTS,
    HOST_CASHOUT_CONTROLLABLE, HOST_CASHOUT_ENABLED, HOST_CASHOUT_HARD,
    TRANSFER_TYPE_EGM_TO_HOST, TRANSFER_TYPE_HOST_TO_EGM,
    TRANSFER_TYPE_WIN_TO_HOST, build_aft_transfer_poll)
from transport.serial.sas_serial import MockSASSerialPort

from by_the_book import ByTheBookAFTMachine

NO_SLEEP = lambda s: None
KEY = bytes(range(20))
POS = b"\x01\x00\x00\x00"


def _rig(**machine_kw):
    machine_kw.setdefault("complete_in", 2)
    machine = ByTheBookAFTMachine(**machine_kw)
    port = ImpliedAckTransport(MockSASSerialPort(machine))
    requests = []
    poller = SASPoller(port, address=1, meters=[])
    host = AFTHost(poller, None, KEY, POS, sleep=NO_SLEEP,
                   on_cashout_request=lambda a, code: requests.append(code))
    return machine, port, poller, host, requests


def _transfers(port):
    """The 0x72 frames that START a transfer (not interrogations)."""
    return [bytes(f) for f, _ in port.sent_frames
            if len(f) > 4 and f[1] == 0x72 and f[3] != 0xFF]


def _answer(host, requests, armed, account="p1"):
    return sas_host.answer_cashout_request(host, requests[-1], armed, account)


# ---------------------------------------------------------------------------
# Item 15 — every request is answered
# ---------------------------------------------------------------------------

class TestARequestNobodyIsArmedFor:
    def test_the_machine_is_told_to_cash_out_its_own_way(self):
        """Before: nothing was sent, and the player stood through the
        machine's eight seconds on every cash-out."""
        machine, port, poller, host, requests = _rig(
            credits=2500, cashout_enabled=True)
        machine.press_cashout()
        poller.poll_once()
        assert requests == [0x6A]
        record = _answer(host, requests, armed=False)
        assert record is None                    # nothing for the hub
        assert machine.declined == 1
        assert machine.ticketed == [2500] and machine.credits == 0
        assert machine.cashout_pending is None
        sent = _transfers(port)
        assert len(sent) == 1 and sent[0][5] == TRANSFER_TYPE_EGM_TO_HOST
        assert sent[0][6:21] == bytes(15)        # every amount zero

    def test_the_transfer_cycle_is_closed_behind_it(self):
        machine, port, poller, host, requests = _rig(
            credits=2500, cashout_enabled=True)
        machine.press_cashout()
        poller.poll_once()
        _answer(host, requests, armed=False)
        poller.poll_once()
        assert machine.cycle is None
        assert host.journal.current is None

    def test_hard_mode_does_not_tilt(self):
        """Before: with no answer a hard-mode machine tilted and waited
        for an attendant."""
        machine, port, poller, host, requests = _rig(
            credits=2500, cashout_enabled=True, cashout_hard=True)
        machine.press_cashout()
        poller.poll_once()
        _answer(host, requests, armed=False)
        machine.eight_seconds_pass()
        assert machine.tilted is False
        assert machine.ticketed == [2500]

    def test_what_used_to_happen_is_what_the_machine_does_alone(self):
        """The pretend machine's own recovery, for the record."""
        machine = ByTheBookAFTMachine(credits=2500, cashout_enabled=True,
                                      cashout_hard=True)
        machine.press_cashout()
        machine.eight_seconds_pass()
        assert machine.tilted is True and machine.ticketed == []

    def test_a_win_nobody_is_armed_for_goes_to_the_meter(self):
        machine, port, poller, host, requests = _rig(cashout_enabled=True)
        machine.win(50000)
        poller.poll_once()
        assert requests == [0x6B]
        assert _answer(host, requests, armed=False) is None
        assert machine.wins_to_meter == [50000] and machine.credits == 50000
        assert _transfers(port)[0][5] == TRANSFER_TYPE_WIN_TO_HOST


class TestAnArmedRequestWeCannotTake:
    def test_an_empty_meter_is_declined_and_reported(self):
        machine, port, poller, host, requests = _rig(
            credits=0, cashout_enabled=True)
        machine.cashout_pending = {"kind": "credits"}   # the press itself
        machine._6x_due = True
        poller.poll_once()
        record = _answer(host, requests, armed=True)
        assert (record["ok"], record["outcome"], record["amountCents"]) == (
            False, "no_credits", 0)
        assert "told to cash out another way" in record["detail"]
        assert machine.declined == 1

    def test_an_unregistered_machine_cannot_even_be_declined(self):
        """With no asset number no 72 can be built. The machine falls back
        by itself, as it always did."""
        machine, port, poller, host, requests = _rig(
            credits=2500, cashout_enabled=True)
        host.auto_register = lambda: (False, None, "no asset number")
        machine.press_cashout()
        poller.poll_once()
        record = _answer(host, requests, armed=True)
        assert (record["ok"], record["outcome"]) == (False, "asset_unknown")
        assert _transfers(port) == []
        assert _answer(host, requests, armed=False) is None


# ---------------------------------------------------------------------------
# The armed 6A, as before
# ---------------------------------------------------------------------------

class TestAnArmedCashOut:
    def test_the_credits_go_to_the_wallet(self):
        machine, port, poller, host, requests = _rig(
            credits=2500, restricted=1500, cashout_enabled=True)
        machine.press_cashout()
        poller.poll_once()
        record = _answer(host, requests, armed=True)
        assert (record["type"], record["ok"], record["outcome"],
                record["amountCents"], record["restrictedCents"],
                record["accountId"]) == (
            "aft_cashout", True, "completed", 2500, 1500, "p1")
        assert (machine.credits, machine.restricted) == (0, 0)
        assert machine.ticketed == [] and machine.declined == 0
        assert machine.cashout_pending is None


# ---------------------------------------------------------------------------
# Item 14 — a win is a different transfer
# ---------------------------------------------------------------------------

class TestAWinToTheHost:
    def test_type_90_for_the_whole_win(self):
        """Before: type 80 for the credit meter. This machine answers that
        with 87, the win stays pending, and after eight seconds it is paid
        some other way."""
        machine, port, poller, host, requests = _rig(
            credits=700, cashout_enabled=True)
        machine.win(50000)
        poller.poll_once()
        record = _answer(host, requests, armed=True)
        assert (record["ok"], record["outcome"], record["amountCents"],
                record.get("win")) == (True, "completed", 50000, True)
        assert machine.refused_87 == 0
        assert machine.credits == 700            # the meter is not touched
        assert machine.wins_to_meter == []
        sent = _transfers(port)[0]
        assert sent[3] == 0x01                   # partial transfer allowed
        assert sent[5] == TRANSFER_TYPE_WIN_TO_HOST
        assert sent[6:11] == bytes.fromhex("9999999999")

    def test_the_old_answer_is_refused_by_a_strict_machine(self):
        machine, port, poller, host, requests = _rig(
            credits=700, cashout_enabled=True)
        machine.win(50000)
        poller.poll_once()
        host.auto_register()
        result = host.transfer(
            AFTTransferRequest(transaction_id="OLDWAY01",
                               transfer_type=TRANSFER_TYPE_EGM_TO_HOST,
                               cashable_cents=700), require_lock=False)
        assert result.outcome is AFTOutcome.REFUSED
        assert machine.refused_87 == 1
        assert machine.cashout_pending is not None      # not aborted


# ---------------------------------------------------------------------------
# Repeats of the same request
# ---------------------------------------------------------------------------

class TestTheMachineRepeatsItsRequest:
    def test_repeats_during_the_answer_are_not_a_second_request(self):
        """The whole program: one press, one transfer, one record."""
        from test_sas_host_commands import _run_scripted
        box = {}

        def factory():
            box["m"] = ByTheBookAFTMachine(
                credits=2500, cashout_enabled=True, complete_in=3)
            return box["m"]

        state = {"pressed": False}

        def script(i, reports):
            if not state["pressed"] and any(r.get("online") for r in reports):
                state["pressed"] = True
                box["m"].press_cashout()
            return {}

        run = _run_scripted("unarmedpress", factory, script, max_polls=300)
        machine = run["machine"]
        assert machine.ticketed == [2500]
        assert machine.declined == 1
        assert len(machine.started) == 1
        assert not [r for r in run["records"]
                    if r.get("type") == "aft_cashout"]


# ---------------------------------------------------------------------------
# Item 6 — arming reads the right byte, and sets the enable where it may
# ---------------------------------------------------------------------------

class TestArming:
    def test_a_machine_with_it_on_needs_nothing(self):
        machine, port, poller, host, requests = _rig(cashout_enabled=True)
        status = host.set_host_cashout(True)
        assert status.host_cashout_status & HOST_CASHOUT_ENABLED
        assert _transfers(port) == []

    def test_a_host_controlled_machine_is_switched_on(self):
        """The setting to use: HOST CONTROLLED. The button prints a ticket
        until a player arms it."""
        machine, port, poller, host, requests = _rig(
            credits=2500, cashout_controllable=True)
        machine.press_cashout()                  # nobody armed: a ticket,
        poller.poll_once()                       # and the host is not asked
        assert machine.ticketed == [2500] and requests == []
        host.auto_register()
        status = host.set_host_cashout(True)
        assert machine.cashout_enabled is True
        assert status.host_cashout_status == (HOST_CASHOUT_CONTROLLABLE
                                              | HOST_CASHOUT_ENABLED)
        sent = _transfers(port)
        assert len(sent) == 1
        assert sent[0][5] == TRANSFER_TYPE_HOST_TO_EGM
        assert sent[0][6:21] == bytes(15)        # a total of zero
        assert sent[0][21] == 0x03               # set the enable, to on, soft

    def test_and_switched_off_again_on_disarm(self):
        machine, port, poller, host, requests = _rig(
            cashout_controllable=True)
        host.auto_register()
        host.set_host_cashout(True)
        poller.poll_once()
        status = host.set_host_cashout(False)
        assert machine.cashout_enabled is False
        assert status.host_cashout_status == HOST_CASHOUT_CONTROLLABLE
        assert _transfers(port)[-1][21] == 0x01  # set the enable, to off

    def test_the_whole_round_on_a_host_controlled_machine(self):
        """Arm, press, the credits go to the wallet; disarm, press, a
        ticket."""
        machine, port, poller, host, requests = _rig(
            credits=2500, cashout_controllable=True)
        host.auto_register()
        host.set_host_cashout(True)
        poller.poll_once()
        machine.press_cashout()
        poller.poll_once()
        assert requests == [0x6A]
        record = _answer(host, requests, armed=True)
        assert (record["ok"], record["amountCents"]) == (True, 2500)
        assert machine.ticketed == []
        poller.poll_once()
        host.set_host_cashout(False)
        poller.poll_once()
        machine.credits = 900
        machine.press_cashout()
        poller.poll_once()
        assert machine.ticketed == [900]
        assert requests == [0x6A]                # the host was not asked

    def test_one_left_on_by_a_satellite_that_restarted_is_switched_off(self):
        """A new AFTHost knows nothing of an earlier arm. The machine says
        host controlled and on, so a disarm switches it off all the same."""
        machine, port, poller, host, requests = _rig(
            cashout_controllable=True, cashout_enabled=True)
        host.auto_register()
        host.set_host_cashout(False)
        assert machine.cashout_enabled is False

    def test_a_machine_the_operator_fixed_on_is_left_on(self):
        machine, port, poller, host, requests = _rig(cashout_enabled=True)
        status = host.set_host_cashout(False)
        assert status.host_cashout_status == HOST_CASHOUT_ENABLED
        assert _transfers(port) == []
        assert machine.cashout_enabled is True

    def test_a_machine_with_it_off_and_not_ours_to_set(self):
        """This is the machine the old gate got wrong: "transfer from
        gaming machine OK" is set, cashout to host is not."""
        machine, port, poller, host, requests = _rig()
        status = host.set_host_cashout(True)
        assert status.available_transfers & 0x02
        assert status.host_cashout_status == 0
        assert _transfers(port) == []

    def test_the_words_for_each(self):
        assert sas_host.describe_host_cashout(0x00) == \
            "off, set at the machine"
        assert sas_host.describe_host_cashout(0x01) == \
            "off, host controlled"
        assert sas_host.describe_host_cashout(0x02) == \
            "ON, soft, set at the machine"
        assert sas_host.describe_host_cashout(0x07) == \
            "ON, hard, host controlled"
        assert sas_host.describe_host_cashout(None) == "unknown"


def _run_arm(key, **machine_kw):
    """The whole program: the hub sends one arm command."""
    from test_sas_host_commands import _run_scripted
    served = {"done": False}

    def script(i, reports):
        if not served["done"] and any(
                isinstance(r.get("aft"), dict)
                and r["aft"].get("hostCashoutStatus") is not None
                for r in reports):
            served["done"] = True
            return {"commands": [{"id": "arm-1",
                                  "type": "aft_set_host_cashout",
                                  "enable": True, "accountId": "p1"}]}
        return {}

    return _run_scripted(key, lambda: ByTheBookAFTMachine(**machine_kw),
                         script, max_polls=250)


class TestTheArmCommand:
    def test_the_status_byte_rides_the_report(self):
        run = _run_arm("armoff")
        bytes_seen = {r["aft"].get("hostCashoutStatus")
                      for r in run["reports"] if isinstance(r.get("aft"), dict)}
        assert 0 in bytes_seen

    def test_an_arm_the_machine_cannot_honour_says_so(self):
        """Before: "armed", and the button printed a ticket."""
        r = _run_arm("armoff")["by_id"]["arm-1"]
        assert (r["ok"], r["armed"], r["outcome"]) == (
            False, False, "host_cashout_off")
        assert r["hostCashoutStatus"] == 0
        assert "keep printing a ticket" in r["detail"]
        assert "host controlled" in r["detail"]

    def test_an_arm_on_a_controllable_machine_switches_it_on(self):
        run = _run_arm("armctl", cashout_controllable=True)
        r = run["by_id"]["arm-1"]
        assert (r["ok"], r["armed"], r["outcome"]) == (True, True, "armed")
        assert r["hostCashoutStatus"] == 0x03
        assert run["machine"].cashout_enabled is True

    def test_an_arm_on_an_enabled_machine(self):
        r = _run_arm("armon", cashout_enabled=True)["by_id"]["arm-1"]
        assert (r["ok"], r["armed"], r["hostCashoutStatus"]) == (
            True, True, 0x02)


# ---------------------------------------------------------------------------
# The zero-amount request itself
# ---------------------------------------------------------------------------

class TestZeroAmountRequests:
    def test_a_zero_total_is_still_refused_by_default(self):
        with pytest.raises(ValueError, match="must be positive"):
            AFTTransferRequest(transaction_id="ZERO0001")

    def test_and_allowed_when_meant(self):
        req = AFTTransferRequest(transaction_id="ZERO0002", zero_ok=True)
        frame = build_aft_transfer_poll(1, req, b"\x01\x00\x00\x00", KEY)
        assert frame[6:21] == bytes(15)

    def test_all_nines_fits_the_field(self):
        req = AFTTransferRequest(transaction_id="NINES001",
                                 transfer_type=TRANSFER_TYPE_WIN_TO_HOST,
                                 cashable_cents=ALL_OF_IT_CENTS)
        frame = build_aft_transfer_poll(1, req, b"\x01\x00\x00\x00", KEY)
        assert frame[6:11] == bytes.fromhex("9999999999")


# ---------------------------------------------------------------------------
# Item 36 — why a registration was refused
# ---------------------------------------------------------------------------

from core.sas_protocol import SASProtocol, sas_crc
from modules.aft.aft_handler import aft_register, registration_refusal


class RegistrationMachine(ByTheBookAFTMachine):
    """Registration as SAS 6.02 §8.1 has it: 73/00 with the right asset
    number gives status 00, then 73/01 gives 01; a wrong asset number, or
    a poll out of sequence, gives 80."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.reg_status = 0x80

    def _long(self, raw):
        if raw[1] != 0x73:
            return super()._long(raw)
        if sas_crc(raw[:-2]).to_bytes(2, "little") != raw[-2:]:
            return b""
        code = raw[3]
        if code == 0xFF:
            pass
        elif code == 0x80 or raw[4:8] != self.asset:
            self.reg_status = 0x80
        elif code == 0x00:
            self.reg_status = 0x00
        elif code == 0x01:
            self.reg_status = 0x01 if self.reg_status == 0x00 else 0x80
        return self._framed(0x73, bytes([self.reg_status]) + self.asset
                            + self.key + self.pos)


class TestWhyARegistrationWasRefused:
    @staticmethod
    def _register(asset):
        machine = RegistrationMachine()
        port = ImpliedAckTransport(MockSASSerialPort(machine))
        return machine, aft_register(port, 1, asset, KEY, POS, SASProtocol(),
                                     sleep=NO_SLEEP)

    def test_the_right_asset_number_registers(self):
        machine, (ok, reg, detail) = self._register(
            bytes([0x92, 0x10, 0x00, 0x00]))
        assert ok is True and detail == "registered"
        assert machine.reg_status == 0x01

    def test_the_wrong_asset_number_is_named_as_the_cause(self):
        """Before: "possibly awaiting operator acknowledgment at the
        machine", which a machine cannot do unless the host asks for it."""
        machine, (ok, reg, detail) = self._register(b"\x01\x00\x00\x00")
        assert ok is False and reg.status == 0x80
        assert "did not accept the asset number" in detail
        assert "answered the initialize (73/00) with status 0x80" in detail
        assert "operator" not in detail

    def test_the_other_statuses(self):
        assert "waiting for an operator to acknowledge" in \
            registration_refusal(0x40, 0x00)
        assert registration_refusal(0x00, 0x00) == (
            "registration refused (status 0x00): the machine started the "
            "cycle and did not complete it")
        assert registration_refusal(0x80, None).endswith(
            "rejected the order of the polls")
