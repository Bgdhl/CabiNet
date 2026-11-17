"""
Lock, unlock and handpay reset report what the machine answered
(SAS 6.02 audit batch 6: items 7, 11, 23, 24, 41 and 42).

Against ByTheBookMachine, which answers these polls as sections 7.4, 7.9
and 14.2 define them:

  item 7   shutdown (01) and startup (02) were recorded as done whatever
           came back: a NACK and a busy answer read "ack", silence "sent".
  item 11  a machine may cash out its credits when it is shut down; nothing
           looked.
  item 23  a reset the machine answered "handpay was reset" was reported as
           a timeout unless exception 52 came within a second.
  item 24  the HANDPAY banner cleared only on exception 52: not on 55 (the
           player cancelled) nor when the machine said it is not in a
           handpay.
  item 41  a lone address byte counted as the answer to A8 and to 94.
  item 42  reset code 02 from 94 (not in a handpay) read "method refused".
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import sas_host
from core.sas_exceptions import lookup
from core.sas_handpay_reset import (
    CMD_REMOTE_HANDPAY_RESET, CMD_SET_HANDPAY_RESET_METHOD,
    HandpayResetOutcome, RESET_CODE_NO_HANDPAY, reset_handpay_to_credits)
from core.sas_implied_ack import ImpliedAckTransport
from core.sas_poller import SASPoller
from transport.serial.sas_serial import MockSASSerialPort

from by_the_book import ByTheBookMachine

NO_SLEEP = lambda s: None


def _line(machine):
    return ImpliedAckTransport(MockSASSerialPort(machine))


def _sent(port, command):
    return [bytes(f) for f, _ in port.sent_frames
            if len(f) > 1 and f[0] == 1 and f[1] == command]


def _locked_up():
    machine = ByTheBookMachine()
    machine.handpay()                    # $1,250.00, exception 51 waiting
    return machine, _line(machine)


# ---------------------------------------------------------------------------
# Item 23 — "handpay was reset" is the machine's word for it
# ---------------------------------------------------------------------------

class TestResetCodeZeroIsSuccess:
    def test_a_reset_without_exception_52_is_still_a_reset(self):
        """Exception 52 is held back until the handpay's 1B record has been
        read (§7.8.1). Nothing reads it here, so 52 never comes; the
        machine has still put the jackpot on the meter and said so."""
        machine, port = _locked_up()
        result = reset_handpay_to_credits(port, 1, sleep=NO_SLEEP)
        assert result.outcome is HandpayResetOutcome.RESET_OK
        assert result.ok is True
        assert result.step2_code == 0x00
        assert result.confirmed_by_exception is False
        assert "exception 0x52 not seen" in result.detail
        assert machine.in_handpay is False
        assert machine.credits == 125000

    def test_through_the_poller_the_record_is_read_and_52_arrives(self):
        """A 51 in the confirm window is answered with 1B, which is what
        lets the machine send 52. It used to be dispatched and left."""
        machine, port = _locked_up()
        got, seen = [], []
        poller = SASPoller(port, address=1, meters=[],
                           on_event=lambda a, c, n: seen.append(c),
                           on_handpay_info=lambda a, i: got.append(i))
        result = poller.reset_handpay_to_credits(sleep=NO_SLEEP)
        assert result.ok is True
        assert result.confirmed_by_exception is True
        assert len(got) == 1                         # the 1B record
        assert seen == [0x51]                        # 52 ended the wait
        assert machine.exceptions == []


# ---------------------------------------------------------------------------
# Item 41 — a lone address byte is not an answer to A8 or 94
# ---------------------------------------------------------------------------

class TestALoneAddressByteIsNotConsent:
    def test_a_chirp_in_the_window_does_not_send_the_reset(self):
        """A machine that cannot hear the host sends its own address every
        200 ms. Tables 14.2b and 7.9b define the answers as framed, with a
        code byte and a CRC."""
        port = _line(lambda f, w: b"\x01")
        result = reset_handpay_to_credits(port, 1, sleep=NO_SLEEP)
        assert result.outcome is HandpayResetOutcome.TIMEOUT
        assert result.step1_acked is False and result.step2_sent is False
        assert _sent(port, CMD_REMOTE_HANDPAY_RESET) == []

    def test_nor_does_it_count_as_the_reset_being_done(self):
        machine, _ = _locked_up()

        def line(f, w):
            if len(f) > 1 and f[1] == CMD_REMOTE_HANDPAY_RESET:
                return b"\x01"
            return machine(f, w)

        result = reset_handpay_to_credits(_line(line), 1, sleep=NO_SLEEP)
        assert result.outcome is HandpayResetOutcome.TIMEOUT
        assert result.step1_acked is True and result.step2_sent is True
        assert machine.in_handpay is True


# ---------------------------------------------------------------------------
# Item 42 — 94 can say "not in a handpay" too
# ---------------------------------------------------------------------------

class TestResetCodeTwo:
    def test_keyed_off_between_the_two_polls(self):
        machine, port = _locked_up()
        machine.key_off_after_a8 = True
        result = reset_handpay_to_credits(port, 1, sleep=NO_SLEEP)
        assert result.outcome is HandpayResetOutcome.NO_HANDPAY
        assert result.step2_code == RESET_CODE_NO_HANDPAY == 0x02
        assert "not in a handpay" in result.detail

    def test_code_one_is_still_a_refusal(self):
        machine, port = _locked_up()
        machine.reset_unable = True
        result = reset_handpay_to_credits(port, 1, sleep=NO_SLEEP)
        assert result.outcome is HandpayResetOutcome.METHOD_REFUSED
        assert result.step2_code == 0x01

    def test_a_machine_not_set_up_for_remote_reset_ignores_94(self):
        machine = ByTheBookMachine(remote_reset=False)
        machine.handpay()
        result = reset_handpay_to_credits(_line(machine), 1, sleep=NO_SLEEP)
        assert result.outcome is HandpayResetOutcome.TIMEOUT
        assert machine.in_handpay is True


# ---------------------------------------------------------------------------
# Item 24 — the HANDPAY banner
# ---------------------------------------------------------------------------

class TestTheHandpayBanner:
    def test_set_by_51_and_cleared_by_52(self):
        latch = sas_host.HandpayLatch()
        latch.on_exception(0x51)
        assert latch.pending is not None
        latch.on_exception(0x52)
        assert latch.pending is None

    def test_cleared_when_the_player_cancels(self):
        """51, then 55, never 52: the banner used to stay for good."""
        latch = sas_host.HandpayLatch()
        latch.on_exception(0x51)
        latch.on_exception(0x55)
        assert latch.pending is None
        assert lookup(0x55).event == "handpay_cancelled"

    def test_cleared_when_the_machine_says_it_is_not_in_a_handpay(self):
        latch = sas_host.HandpayLatch()
        latch.on_exception(0x51)
        latch.on_no_handpay()
        assert latch.pending is None

    def test_other_exceptions_leave_it(self):
        latch = sas_host.HandpayLatch()
        latch.on_exception(0x51)
        for code in (0x11, 0x3D, 0x7E):
            latch.on_exception(code)
        assert latch.pending is not None


def _run_stale_banner():
    """The whole program: a handpay is keyed off at the machine and its 52
    is lost. The hub's reset button then gets "not in a handpay"."""
    from test_sas_host_commands import _run_scripted
    box, state = {}, {"stage": 0}

    def factory():
        box["m"] = ByTheBookMachine()
        return box["m"]

    def script(i, reports):
        m = box["m"]
        if state["stage"] == 0 and any(r.get("online") for r in reports):
            state["stage"] = 1
            m.handpay()
        elif state["stage"] == 1 and reports[-1].get("pendingHandpay"):
            state["stage"] = 2
            m.in_handpay = False             # keyed off; the 52 never arrives
            m._reset_done = False
            return {"commands": [{"id": "hp-1", "type": "handpay_reset"}]}
        return {}

    return _run_scripted("stalebanner", factory, script, max_polls=300)


class TestTheWholeProgramClearsAStaleBanner:
    def test_the_command_says_no_handpay(self):
        r = _run_stale_banner()["by_id"]["hp-1"]
        assert (r["ok"], r["outcome"]) == (False, "no_handpay")

    def test_and_the_banner_goes(self):
        reports = _run_stale_banner()["reports"]
        assert any(r.get("pendingHandpay") for r in reports)
        assert reports[-1].get("pendingHandpay") is None


# ---------------------------------------------------------------------------
# Items 7 and 11 — lock and unlock
# ---------------------------------------------------------------------------

def _run_lock(key, commands, prepare=None):
    """The whole program: hub lock/unlock commands against one machine."""
    from test_sas_host_commands import _run_scripted
    box, served = {}, {"done": False}

    def factory():
        box["m"] = ByTheBookMachine()
        if prepare is not None:
            prepare(box["m"])
        return box["m"]

    def script(i, reports):
        if not served["done"] and any(
                isinstance(r.get("machineInfo"), dict) or r.get("polls", 0) > 20
                for r in reports):
            served["done"] = True
            return {"commands": commands}
        return {}

    return _run_scripted(key, factory, script, max_polls=260)


LOCK = [{"id": "lock-1", "type": "sas_disable"}]


class TestLockAndUnlock:
    def test_an_acknowledged_lock_is_a_lock(self):
        run = _run_lock("lockack", LOCK + [{"id": "unlock-1",
                                           "type": "sas_enable"}])
        lock, unlock = run["by_id"]["lock-1"], run["by_id"]["unlock-1"]
        assert (lock["ok"], lock["outcome"], lock["locked"],
                lock["result"]) == (True, "ack", True, "ack")
        assert (unlock["ok"], unlock["locked"]) == (True, False)
        assert run["machine"].shut_down is False
        assert lock["raw"] == "01"

    def test_a_refused_lock_is_not_recorded_as_one(self):
        """Before: any answer at all read "ack" and the machine was shown
        locked, including this one (address | 0x80, a NACK)."""
        run = _run_lock("locknack", LOCK,
                        lambda m: m.nack.update({0x01: 9}))
        r = run["by_id"]["lock-1"]
        assert (r["ok"], r["outcome"], r["result"]) == (False, "nack", "nack")
        assert "locked" not in r
        assert r["raw"] == "81"
        assert run["machine"].shut_down is False

    def test_a_busy_machine_is_asked_again(self):
        """§4.1: on a busy answer the host puts the poll back for later.
        Before, the two-byte busy answer read "ack"."""
        run = _run_lock("lockbusy", LOCK,
                        lambda m: m.busy.update({0x01: 2}))
        r = run["by_id"]["lock-1"]
        assert (r["ok"], r["outcome"], r["locked"]) == (True, "ack", True)
        assert r["tries"] == 3
        assert run["machine"].shut_down is True

    def test_a_machine_that_stays_busy_is_reported_busy(self):
        run = _run_lock("lockbusy9", LOCK,
                        lambda m: m.busy.update({0x01: 9}))
        r = run["by_id"]["lock-1"]
        assert (r["ok"], r["outcome"]) == (False, "busy")
        assert "locked" not in r and r["raw"] == "01 00"

    def test_silence_is_not_a_lock(self):
        """Before: "sent", and recorded as locked."""
        run = _run_lock("lockdeaf", LOCK, lambda m: m.deaf.add(0x01))
        r = run["by_id"]["lock-1"]
        assert (r["ok"], r["outcome"]) == (False, "silent")
        assert "locked" not in r
        assert run["machine"].shut_down is False

    def test_credits_cashed_out_by_the_shutdown_are_reported(self):
        """§7.4.1: a machine "can either automatically cash out any
        accumulated credits or allow the user to cash them out"."""
        def prepare(m):
            m.credits = 2500
            m.cashes_out_on_shutdown = True
        run = _run_lock("lockcashout", LOCK, prepare)
        r = run["by_id"]["lock-1"]
        assert (r["ok"], r["locked"]) == (True, True)
        assert (r["creditsBefore"], r["creditsAfter"]) == (2500, 0)
        assert "cashed out" in r["detail"]
        assert run["machine"].ticketed == [2500]

    def test_credits_left_on_the_meter_are_reported_too(self):
        run = _run_lock("lockkeeps", LOCK,
                        lambda m: setattr(m, "credits", 2500))
        r = run["by_id"]["lock-1"]
        assert (r["creditsBefore"], r["creditsAfter"]) == (2500, 2500)
        assert "cashed out" not in (r.get("detail") or "")


class TestTheAnswerToATypeSPoll:
    @pytest.mark.parametrize("reply,kind", [
        (b"\x01", "ack"), (b"\x81", "nack"), (b"\x01\x00", "busy"),
        (b"", "silent"), (b"\x02", "silent"), (b"\x01\x01\x01", "silent")])
    def test_each(self, reply, kind):
        assert sas_host.classify_type_s(reply, 1) == kind
