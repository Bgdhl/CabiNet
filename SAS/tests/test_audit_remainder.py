"""
What was left of the SAS 6.02 audit after its seven batches. Each test
names its item.

  item 38 (the rest)  the machine is polled while the hub answers a ticket
                      call
  item 17             an in-house transfer carries a registration key of
                      all zeros and needs no registered machine; a machine
                      that refuses the zeros is sent its own key
"""

import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from core.hub_ticket_client import HubTicketAuthority
from core.sas_implied_ack import ImpliedAckTransport
from core.sas_poller import SASPoller
from core.sas_ticket_store import TicketStore
from core.sas_tito_host import (
    CMD_REDEEM_TICKET, CMD_SEND_TICKET_VALIDATION_DATA,
    REDEEM_AUTHORIZE_CASHABLE, RedemptionOutcome, SASTITOHost,
    SYSTEM_VALIDATION_REQUEST_EXCEPTION, TICKET_INSERTED_EXCEPTION,
    VALIDATION_APPROVE)
import sas_host
from modules.aft.aft_handler import (
    AFTHost, AFTOutcome, AFTStatus, AFTTransferRequest, KEY_MODE_MACHINE,
    KEY_MODE_ZERO, KeyModeStore, TRANSFER_TYPE_EGM_TO_HOST,
    TRANSFER_TYPE_HOST_TO_EGM, TRANSFER_TYPE_WIN_TO_HOST, ZERO_KEY)
from transport.serial.sas_serial import MockSASSerialPort

from by_the_book import ByTheBookAFTMachine
from test_sas_system_validation import SysValMachine
from test_sas_tito_host import AMOUNT, TITOMachine, VN16

NO_SLEEP = lambda _t: None
MINTED = "0100000000000042"


# ---------------------------------------------------------------------------
# Item 38, the rest — the machine is polled while the hub answers
#
# SAS 6.02 §4.3: "A gaming machine must consider the communications link to
# be down if it is not being actively polled by the host." With the link
# down a ticket in escrow is returned "immediately" (§15.11) and a cash-out
# proceeds "as though the system validation had been denied" (§15.8).
# ---------------------------------------------------------------------------

class _Hub:
    """Stands in for the HTTP connection to the hub. An op named in `hold`
    does not answer until `release` is set; an op named in `fail` raises."""

    def __init__(self, answers, hold=(), fail=None):
        self.answers = answers
        self.hold = set(hold)
        self.fail = fail or {}
        self.release = threading.Event()
        self.ops = []
        self.threads = []

    def post_json(self, body, op):
        self.ops.append(op)
        self.threads.append(threading.current_thread().name)
        if op in self.fail:
            raise self.fail[op]
        if op in self.hold:
            assert self.release.wait(5.0), "nothing let the hub answer"
        return 200, json.dumps(self.answers[op]).encode()


class _Watched:
    """A machine that notes how many general polls reach it between two
    long polls, and lets the hub answer once `after` of them are in."""

    def __init__(self, machine, release, after=3):
        self.machine = machine
        self.release = release
        self.after = after
        self.since_long = 0
        self.runs = []          # general polls counted before each long poll

    def __call__(self, frame, wakeup):
        if len(frame) == 1:
            if frame[0] & 0x7F == self.machine.address:
                self.since_long += 1
                if self.since_long >= self.after:
                    self.release.set()
        elif frame[0] == self.machine.address:
            self.runs.append((frame[1], self.since_long))
            self.since_long = 0
        return self.machine(frame, wakeup)


ANSWERS = {
    "authorize": {"ok": True, "authorized": True, "amountCents": AMOUNT,
                  "reason": ""},
    "commit": {"ok": True, "state": "redeemed"},
    "mint": {"ok": True, "validationNumber": MINTED, "systemId": 1,
             "seq": 42},
    "issued": {"ok": True},
    "sync": {"ok": True},
}


def _authority(tmp_path, hub, timeout=2.5):
    store = HubTicketAuthority(
        "http://hub:8081", "smib-test",
        TicketStore(str(tmp_path / "tickets.json")), timeout=timeout,
        journal_path=str(tmp_path / "journal.json"),
        start_sync_thread=False)
    store._http = hub
    store.wait_slice = 0.01     # the real 200 ms would only slow the tests
    return store


def _rig(tmp_path, machine, hub, after=3):
    """A poller, the ticket host and a hub-backed store wired as the
    satellite wires them."""
    watched = _Watched(machine, hub.release, after)
    port = MockSASSerialPort(watched)
    store = _authority(tmp_path, hub)
    events, redemptions, validations = [], [], []
    poller = SASPoller(port, address=1,
                       on_event=lambda a, c, n: events.append(c))
    tito = SASTITOHost(
        poller, store, sleep=NO_SLEEP,
        on_redemption=lambda a, r: redemptions.append(r),
        on_system_validation=lambda a, r: validations.append(r))
    store.while_waiting = tito.keep_polling
    return {"port": port, "store": store, "poller": poller, "tito": tito,
            "watched": watched, "events": events,
            "redemptions": redemptions, "validations": validations}


class TestTheMachineIsPolledWhileTheHubAnswers:
    def test_between_the_ticket_read_and_the_decision(self, tmp_path):
        """A ticket goes in, the satellite reads it (70) and asks the hub.
        The hub is slow. Until v0.3.17 nothing went to the machine until
        the answer came."""
        machine = TITOMachine(escrow={"vn": VN16, "amount": AMOUNT})
        machine.queue(TICKET_INSERTED_EXCEPTION)
        hub = _Hub(ANSWERS, hold={"authorize"})
        rig = _rig(tmp_path, machine, hub)
        rig["poller"].poll_once()

        assert [r.outcome for r in rig["redemptions"]] == \
            [RedemptionOutcome.REDEEMED]
        assert machine.credited_cents == AMOUNT
        # the long polls in order, and the general polls before each: at
        # least three reached the machine between the 70 and the 71
        runs = rig["watched"].runs[:2]
        assert [cmd for cmd, _n in runs] == \
            [CMD_SEND_TICKET_VALIDATION_DATA, CMD_REDEEM_TICKET]
        assert runs[1][1] >= 3
        # the hub was asked once for each, and never from the poll thread
        assert hub.ops == ["authorize", "commit"]
        assert all(name.startswith("hub-tito-call") for name in hub.threads)

    def test_between_the_cashout_read_and_the_validation_number(
            self, tmp_path):
        """The same for a cash-out: 57 is read, the hub is asked for a
        number, 58 delivers it."""
        machine = SysValMachine(cashout={"type": 0, "amount": 1000},
                                print_on_approve=False)
        machine.queue(SYSTEM_VALIDATION_REQUEST_EXCEPTION)
        hub = _Hub(ANSWERS, hold={"mint"})
        rig = _rig(tmp_path, machine, hub)
        rig["poller"].poll_once()

        assert len(rig["validations"]) == 1 and rig["validations"][0].ok
        assert rig["validations"][0].validation_number == MINTED
        assert machine.approvals[0][0] == VALIDATION_APPROVE
        runs = rig["watched"].runs[:2]
        assert [cmd for cmd, _n in runs] == [0x57, 0x58]
        assert runs[1][1] >= 3

    def test_a_quick_answer_sends_nothing_extra(self, tmp_path):
        """A hub on the same network answers inside one slice. The wire is
        then what it was: 70, 71, and no general poll between."""
        machine = TITOMachine(escrow={"vn": VN16, "amount": AMOUNT})
        machine.queue(TICKET_INSERTED_EXCEPTION)
        hub = _Hub(ANSWERS)
        rig = _rig(tmp_path, machine, hub)
        rig["store"].wait_slice = 2.0
        rig["poller"].poll_once()

        assert rig["redemptions"][0].outcome is RedemptionOutcome.REDEEMED
        assert rig["watched"].runs[:2] == [
            (CMD_SEND_TICKET_VALIDATION_DATA, 1), (CMD_REDEEM_TICKET, 0)]

    def test_what_the_machine_reports_meanwhile_is_passed_on_not_acted_on(
            self, tmp_path):
        """A door opens while the hub is thinking, and the machine repeats
        "ticket inserted". Both reach the listeners. Neither starts a second
        ticket cycle inside the first: 70 is sent once."""
        machine = TITOMachine(escrow={"vn": VN16, "amount": AMOUNT})
        machine.queue(TICKET_INSERTED_EXCEPTION)
        hub = _Hub(ANSWERS, hold={"authorize"})
        rig = _rig(tmp_path, machine, hub, after=4)
        # queued behind the 67 that starts the cycle
        machine.queue(0x11, TICKET_INSERTED_EXCEPTION)
        rig["poller"].poll_once()

        assert rig["events"][:3] == [TICKET_INSERTED_EXCEPTION, 0x11,
                                     TICKET_INSERTED_EXCEPTION]
        assert len(rig["redemptions"]) == 1
        assert rig["redemptions"][0].outcome is RedemptionOutcome.REDEEMED
        sent_70 = [f for f, _w in rig["port"].sent_frames
                   if len(f) == 2 and f[1] == CMD_SEND_TICKET_VALIDATION_DATA]
        assert len(sent_70) == 1
        authorizes = [f for f, _w in rig["port"].sent_frames
                      if len(f) > 3 and f[1] == CMD_REDEEM_TICKET
                      and f[3] == REDEEM_AUTHORIZE_CASHABLE]
        assert len(authorizes) == 1
        assert rig["tito"]._busy is False

    def test_a_manual_redeem_is_guarded_the_same_way(self, tmp_path):
        """redeem() called from a hub command does not go through the
        exception handler that sets the busy mark. keep_polling sets it for
        its own poll and puts it back."""
        machine = TITOMachine(escrow={"vn": VN16, "amount": AMOUNT})
        hub = _Hub(ANSWERS, hold={"authorize"})
        rig = _rig(tmp_path, machine, hub)
        machine.queue(TICKET_INSERTED_EXCEPTION)   # reported during the wait
        result = rig["tito"].redeem()

        assert result.outcome is RedemptionOutcome.REDEEMED
        assert len(rig["redemptions"]) == 1
        assert rig["tito"]._busy is False


class TestTheCallItself:
    def test_a_hub_that_is_down_falls_back_as_before(self, tmp_path):
        """No answer is a refusal for a redemption: the ticket goes back."""
        hub = _Hub(ANSWERS, fail={"authorize": OSError("connection refused")})
        store = _authority(tmp_path, hub)
        polls = []
        store.while_waiting = lambda: polls.append(1)
        out = store.authorize_redemption(1, VN16, AMOUNT)
        assert out["authorized"] is False
        assert out["reason"] == "hub unreachable — ticket returned"
        assert polls == []

    def test_a_socket_timeout_is_the_calls_failure_not_more_waiting(
            self, tmp_path):
        """A socket timeout is a TimeoutError, and so is "the wait ran out"
        in concurrent.futures. Taken for the second, the poll thread would
        keep waiting on a call that has already failed."""
        hub = _Hub(ANSWERS, fail={"mint": TimeoutError("timed out")})
        store = _authority(tmp_path, hub)
        polls = []
        store.while_waiting = lambda: polls.append(1)
        out = store.mint_validation_number(1000, 1)
        # the local fallback mint: its own system id, never the hub's 01
        assert out["system_id"] == store.fallback_sid
        assert hub.ops == ["mint"] and polls == []

    def test_a_refusal_from_the_hub_comes_through(self, tmp_path):
        hub = _Hub(dict(ANSWERS, authorize={
            "ok": True, "authorized": False, "reason": "already redeemed"}),
            hold={"authorize"})
        store = _authority(tmp_path, hub)
        polls = []

        def hook():
            polls.append(1)
            if len(polls) == 2:
                hub.release.set()
        store.while_waiting = hook
        out = store.authorize_redemption(1, VN16, AMOUNT)
        assert out == {"authorized": False, "amount_cents": 0,
                       "reason": "already redeemed", "retry": False,
                       "restricted": False}
        assert len(polls) == 2

    def test_a_call_that_never_comes_back_is_given_up(self, tmp_path):
        """The call's own limit is on its sockets. Past that limit and a
        margin, the poll thread stops waiting and takes the fallback."""
        hub = _Hub(ANSWERS, hold={"authorize"})
        store = _authority(tmp_path, hub, timeout=0.05)
        store.wait_grace = 0.05
        polls = []
        store.while_waiting = lambda: polls.append(1)
        out = store.authorize_redemption(1, VN16, AMOUNT)
        assert out["authorized"] is False
        assert out["reason"] == "hub unreachable — ticket returned"
        assert polls                     # the machine was polled meanwhile
        # the stuck call ends; the next one is answered
        hub.release.set()
        out = store.authorize_redemption(1, VN16, AMOUNT)
        assert out["authorized"] is True

    def test_a_fault_while_polling_does_not_lose_the_hubs_answer(
            self, tmp_path):
        hub = _Hub(ANSWERS, hold={"authorize"})
        store = _authority(tmp_path, hub)
        calls = []

        def hook():
            calls.append(1)
            if len(calls) == 2:
                hub.release.set()
            raise RuntimeError("serial port fell over")
        store.while_waiting = hook
        out = store.authorize_redemption(1, VN16, AMOUNT)
        assert out["authorized"] is True and len(calls) >= 2

    def test_without_the_hook_the_call_is_made_in_place(self, tmp_path):
        """Tests, a bench run, anything that has no machine to poll."""
        hub = _Hub(ANSWERS)
        store = _authority(tmp_path, hub)
        assert store.while_waiting is None
        assert store.authorize_redemption(1, VN16, AMOUNT)["authorized"]
        assert hub.threads == [threading.current_thread().name]

    @pytest.mark.parametrize("op", ["mint", "issued", "authorize", "commit"])
    def test_all_four_poll_thread_calls_take_this_road(self, tmp_path, op):
        hub = _Hub(ANSWERS)
        store = _authority(tmp_path, hub)
        store.while_waiting = lambda: None
        if op == "mint":
            store.mint_validation_number(1000, 1)
        elif op == "issued":
            store.record_issued(VN16, AMOUNT, 1)
        elif op == "authorize":
            store.authorize_redemption(1, VN16, AMOUNT)
        else:
            store.close_redemption(1, VN16, redeemed=True)
        assert hub.ops == [op]
        assert hub.threads[0].startswith("hub-tito-call")

    def test_the_sync_thread_does_not_poll_the_machine(self, tmp_path):
        """sync_now runs on its own thread. It must never touch the serial
        port, so its calls do not go through the hook."""
        hub = _Hub(ANSWERS)
        store = _authority(tmp_path, hub)
        polls = []
        store.while_waiting = lambda: polls.append(1)
        store.wait_slice = 0.0          # any wait at all would call the hook
        assert store.sync_now() is True
        assert hub.ops == ["sync"] and polls == []
        assert hub.threads == [threading.current_thread().name]


# ---------------------------------------------------------------------------
# Item 17 — an in-house transfer does not need a registered machine
#
# SAS 6.02 §8.1: "The registration process allows a debit system to
# associate a gaming machine with a POS terminal. Registration should not be
# used for in-house and bonus transactions. These transactions should use a
# registration key of all zeros." Table 8.3a: registration key, "0 =
# registration not required". Status 88: "Gaming machine not registered
# (required for debit transfers)".
#
# Until v0.3.17 every transfer was preceded by a registration and carried
# the key the machine was registered with; a machine that could not be
# registered got no transfer. A registered WMS Bluebird 2 took a push with
# the zeros on 2026-11-17.
# ---------------------------------------------------------------------------

RIG_KEY = b"CasinoNet-AFT-rig001"
POS = b"\x01\x00\x00\x00"


def _aft(key_mode=None, **machine_kw):
    machine_kw.setdefault("complete_in", 2)
    machine = ByTheBookAFTMachine(**machine_kw)
    port = ImpliedAckTransport(MockSASSerialPort(machine))
    poller = SASPoller(port, address=1, meters=[])
    host = AFTHost(poller, None, RIG_KEY, POS, sleep=NO_SLEEP,
                   key_mode=key_mode)
    return machine, port, host


def _long_polls(port):
    """(command, code) of every long poll sent: the code is the byte that
    says which form of 72, 73 or 74 it is (74 has no length byte before
    it)."""
    return [(f[1], f[2] if f[1] == 0x74 else f[3])
            for f, _w in port.sent_frames if len(f) > 3]


def _request(cents=500, pull=False):
    _request.n += 1
    return AFTTransferRequest(
        transaction_id=f"TEST{_request.n:06d}",
        transfer_type=(TRANSFER_TYPE_EGM_TO_HOST if pull
                       else TRANSFER_TYPE_HOST_TO_EGM),
        cashable_cents=cents)


_request.n = 0


def _push(host, cents=500, pull=False):
    ok, _reg, detail = host.prepare_transfer()
    assert ok, detail
    return host.transfer(_request(cents, pull)), detail


class TestATransferCarriesAKeyOfAllZeros:
    def test_to_a_registered_machine(self):
        """What the Bluebird was seen to take. One 73/FF for the asset
        number, the lock, the transfer. No second 73: with no key there is
        no registration to check."""
        machine, port, host = _aft(credits=0)
        result, detail = _push(host)
        assert result.outcome is AFTOutcome.COMPLETED
        assert detail == "registration key of all zeros"
        assert _long_polls(port)[:3] == [
            (0x73, 0xFF), (0x74, 0x00), (0x72, 0x00)]
        assert machine.keys_seen == [ZERO_KEY]
        assert machine.credits == 500
        assert host.key_sent == "zero" and host.key_mode.mode == KEY_MODE_ZERO

    def test_a_pull(self):
        machine, port, host = _aft(credits=2500)
        result, _detail = _push(host, cents=2500, pull=True)
        assert result.outcome is AFTOutcome.COMPLETED
        assert machine.credits == 0 and machine.keys_seen == [ZERO_KEY]

    def test_to_a_machine_that_is_not_registered_and_nothing_registers_it(
            self):
        """Before: registered first, every time. After a RAM clear that is
        three polls of 73 the document says an in-house transfer does not
        need."""
        machine, port, host = _aft(credits=0, registered=False)
        result, _detail = _push(host)
        assert result.outcome is AFTOutcome.COMPLETED
        assert machine.credits == 500 and machine.keys_seen == [ZERO_KEY]
        assert machine.registered is False
        assert not [1 for c, code in _long_polls(port)
                    if c == 0x73 and code != 0xFF]

    def test_registering_a_machine_does_not_change_what_its_transfers_carry(
            self):
        """The hub asks for a registration when a machine joins
        (aft_register, which is auto_register). The machine is registered
        with the rig key, so a key is there if it ever wants one, and its
        transfers go on carrying the zeros."""
        machine, port, host = _aft(credits=0, registered=False)
        ok, _reg, detail = host.auto_register()
        assert ok and detail == "registered"
        assert machine.registered and machine.key == RIG_KEY
        result, detail = _push(host)
        assert result.outcome is AFTOutcome.COMPLETED
        assert detail == "registration key of all zeros"
        assert machine.keys_seen == [ZERO_KEY]
        assert host.key_mode.mode == KEY_MODE_ZERO

    def test_to_a_machine_that_cannot_be_registered(self):
        """Before: "cannot push — registration refused", and nothing was
        sent."""
        machine, port, host = _aft(credits=0, registered=False,
                                   registers=False)
        result, _detail = _push(host)
        assert result.outcome is AFTOutcome.COMPLETED
        assert machine.credits == 500 and machine.keys_seen == [ZERO_KEY]

    def test_to_a_machine_that_never_answers_73(self):
        """It may have no registration at all. Its asset number comes from
        the 74 answer."""
        machine, port, host = _aft(credits=0, registered=False)
        machine.deaf.add(0x73)
        result, _detail = _push(host)
        assert result.outcome is AFTOutcome.COMPLETED
        assert machine.credits == 500 and machine.keys_seen == [ZERO_KEY]

    def test_no_asset_number_is_still_a_refusal(self):
        """The machine checks the asset number on every 72 (status 93), so
        with none there is nothing to send."""
        machine, port, host = _aft(credits=0, registered=False)
        machine.asset = bytes(4)
        ok, _reg, detail = host.prepare_transfer()
        assert (ok, detail) == (False, "machine reports no asset number")
        assert not [1 for c, _code in _long_polls(port) if c == 0x72]

    def test_a_refusal_for_another_reason_stays_the_machines_answer(self):
        """A win cash-out with no win waiting is refused 8B whatever the
        key. On a registered machine the key is tried once, the machine
        refuses again, and that is the result: the zeros stay in use and
        nothing is remembered."""
        machine, port, host = _aft(credits=100)
        ok, _reg, _detail = host.prepare_transfer()
        assert ok
        result = host.transfer(AFTTransferRequest(
            transaction_id="WIN0000001",
            transfer_type=TRANSFER_TYPE_WIN_TO_HOST, cashable_cents=100))
        assert result.outcome is AFTOutcome.REFUSED
        assert result.final.status == AFTStatus.NO_WON_CREDITS_AVAILABLE
        assert machine.keys_seen == [ZERO_KEY, machine.key]
        assert host.key_mode.mode == KEY_MODE_ZERO
        assert result.detail.endswith(
            "(refused with a registration key of all zeros as well, status "
            "0x8B)")
        # and the next transfer starts with the zeros again
        result, detail = _push(host)
        assert result.outcome is AFTOutcome.COMPLETED
        assert detail == "registration key of all zeros"
        assert machine.keys_seen[-1] == ZERO_KEY

    def test_on_an_unregistered_machine_such_a_refusal_is_not_retried(self):
        """There is no key to offer, and registering a machine because it
        refused a transfer for some other reason is not ours to do."""
        machine, port, host = _aft(credits=100, registered=False)
        ok, _reg, _detail = host.prepare_transfer()
        assert ok
        result = host.transfer(AFTTransferRequest(
            transaction_id="WIN0000002",
            transfer_type=TRANSFER_TYPE_WIN_TO_HOST, cashable_cents=100))
        assert result.outcome is AFTOutcome.REFUSED
        assert machine.keys_seen == [ZERO_KEY]
        assert machine.registered is False
        assert not [1 for c, code in _long_polls(port)
                    if c == 0x73 and code != 0xFF]


class TestAMachineThatRefusesTheZeros:
    """Not what the document describes. If one exists, it answers 88 or 89,
    nothing moves, and it is sent the same transfer again with its key."""

    def test_registered_it_gets_the_transfer_with_its_own_key(self):
        machine, port, host = _aft(credits=0, needs_registration=True)
        result, _detail = _push(host)
        assert result.outcome is AFTOutcome.COMPLETED
        assert machine.credits == 500          # once, not twice
        assert machine.keys_seen == [ZERO_KEY, machine.key]
        assert result.detail == (
            "the machine refused the transfer with a registration key of "
            "all zeros (status 0x89); sent again with the machine's own "
            "key: full transfer successful")
        # two cycles on the machine, the second under a new id, and the
        # result names the one that moved the credits
        assert len(machine.started) == 2
        assert machine.started[0] != machine.started[1]
        assert result.request.transaction_id == machine.started[1]
        assert host.key_sent == "machine"

    def test_one_that_refuses_them_in_a_way_of_its_own(self):
        """How the older Bally and Konami games that are said to fail on
        the zeros answer is not known. Whatever failure code a registered
        machine gives, its key is tried, and when the key is what made the
        transfer go through the machine is sent its key from then on."""
        machine, port, host = _aft(credits=0, zero_key_status=0x82)
        result, _detail = _push(host)
        assert result.outcome is AFTOutcome.COMPLETED
        assert machine.credits == 500
        assert machine.keys_seen == [ZERO_KEY, machine.key]
        assert "status 0x82" in result.detail
        assert "sent again with the machine's own key" in result.detail
        assert result.request.transaction_id == machine.started[1]
        assert (host.key_mode.mode, host.key_mode.status) == (
            KEY_MODE_MACHINE, "0x82")
        result, detail = _push(host)
        assert detail == "already registered" and machine.credits == 1000
        assert machine.keys_seen == [ZERO_KEY, machine.key, machine.key]

    def test_it_is_remembered_and_the_next_transfer_takes_the_old_road(self):
        """Poll for poll what every transfer was until v0.3.17: 73/FF to
        prepare, 73/FF at the start of the transfer, the lock, the
        transfer with the machine's key. No zeros a second time."""
        machine, port, host = _aft(credits=0, needs_registration=True)
        _push(host)
        assert host.key_mode.mode == KEY_MODE_MACHINE
        assert host.key_mode.status == "0x89"
        before = len(_long_polls(port))
        result, detail = _push(host)
        assert result.outcome is AFTOutcome.COMPLETED
        assert detail == "already registered"
        assert _long_polls(port)[before:before + 4] == [
            (0x73, 0xFF), (0x73, 0xFF), (0x74, 0x00), (0x72, 0x00)]
        assert machine.keys_seen == [ZERO_KEY, machine.key, machine.key]
        assert machine.credits == 1000

    def test_not_registered_it_is_registered_and_then_sent_the_transfer(
            self):
        machine, port, host = _aft(credits=0, registered=False,
                                   needs_registration=True)
        result, _detail = _push(host)
        assert result.outcome is AFTOutcome.COMPLETED
        assert "status 0x88" in result.detail
        assert machine.registered and machine.key == RIG_KEY
        assert machine.keys_seen == [ZERO_KEY, RIG_KEY]
        assert machine.credits == 500

    def test_one_that_cannot_be_registered_either_is_refused(self):
        """Where every unregistered machine stood before. The result is the
        machine's refusal, with why nothing more could be done."""
        machine, port, host = _aft(credits=0, registered=False,
                                   registers=False, needs_registration=True)
        result, _detail = _push(host)
        assert result.outcome is AFTOutcome.REFUSED
        assert result.final.status == AFTStatus.GAMING_MACHINE_NOT_REGISTERED
        assert result.detail.startswith(
            "the machine refused the transfer with a registration key of "
            "all zeros (status 0x88) and could not be registered: "
            "registration refused")
        assert machine.credits == 0 and machine.keys_seen == [ZERO_KEY]
        # and from now on it is prepared the old way, which says so before
        # anything is sent
        ok, _reg, detail = host.prepare_transfer()
        assert ok is False and "registration refused" in detail

    def test_a_refusal_of_its_own_key_is_not_sent_again(self):
        """The second chance is for the zeros only. A machine in the old
        mode that answers 89 to its own key (the key changed under us) is
        refused once and that is the result."""
        machine, port, host = _aft(credits=0, needs_registration=True)
        _push(host)                                  # now in the old mode
        ok, _reg, _detail = host.prepare_transfer()
        assert ok and host.key_sent == "machine"
        machine.key = bytes(reversed(machine.key))   # no longer what we hold
        started = len(machine.started)
        result = host.transfer(_request())
        assert result.outcome is AFTOutcome.REFUSED
        assert result.final.status == \
            AFTStatus.REGISTRATION_KEY_DOES_NOT_MATCH
        assert len(machine.started) == started + 1
        assert "sent again" not in result.detail

    def test_a_pull_is_sent_again_too(self):
        machine, port, host = _aft(credits=2500, needs_registration=True)
        result, _detail = _push(host, cents=2500, pull=True)
        assert result.outcome is AFTOutcome.COMPLETED
        assert machine.credits == 0
        assert machine.keys_seen == [ZERO_KEY, machine.key]

    def test_a_cash_out_request_is_answered_inside_the_one_call(self):
        """The player pressed CASH OUT and the machine is waiting (eight
        seconds, §8.7). The refusal, the second transfer and the credit to
        the wallet all happen in the one answer, and the record carries the
        id of the transfer that moved the credits."""
        machine, port, host = _aft(credits=2500, needs_registration=True,
                                   cashout_enabled=True)
        requests = []
        host.on_cashout_request = lambda a, code: requests.append(code)
        machine.press_cashout()
        host.poller.poll_once()
        assert requests == [0x6A]
        record = sas_host.answer_cashout_request(host, 0x6A, True, "p1")
        assert (record["ok"], record["outcome"], record["amountCents"]) == (
            True, "completed", 2500)
        assert record["keySent"] == "machine"
        assert record["txnId"] == machine.started[-1]
        assert machine.credits == 0 and machine.ticketed == []


class TestWhatIsRememberedAboutAMachine:
    def test_it_survives_a_restart(self, tmp_path):
        path = str(tmp_path / "aft_key_mode.json")
        machine, port, host = _aft(key_mode=KeyModeStore(path), credits=0,
                                   needs_registration=True)
        _push(host)
        again = KeyModeStore(path)
        assert (again.mode, again.status) == (KEY_MODE_MACHINE, "0x89")
        assert again.since
        # a new satellite process on the same machine: no zeros this time
        machine2, port2, host2 = _aft(key_mode=again, credits=0,
                                      needs_registration=True)
        result, _detail = _push(host2)
        assert result.outcome is AFTOutcome.COMPLETED
        assert machine2.keys_seen == [machine2.key]

    def test_no_file_or_a_bad_one_means_the_zeros(self, tmp_path):
        assert KeyModeStore(str(tmp_path / "none.json")).mode == KEY_MODE_ZERO
        bad = tmp_path / "bad.json"
        bad.write_text("{not json")
        assert KeyModeStore(str(bad)).mode == KEY_MODE_ZERO
        odd = tmp_path / "odd.json"
        odd.write_text('{"mode": "something else"}')
        assert KeyModeStore(str(odd)).mode == KEY_MODE_ZERO

    def test_a_machine_that_takes_the_zeros_leaves_no_file(self, tmp_path):
        path = tmp_path / "aft_key_mode.json"
        machine, port, host = _aft(key_mode=KeyModeStore(str(path)),
                                   credits=0)
        _push(host)
        assert not path.exists()


def _run_push(key, **machine_kw):
    from test_sas_host_commands import _run_scripted
    served = {"done": False}

    def script(i, reports):
        if not served["done"] and any(
                isinstance(r.get("aft"), dict) for r in reports):
            served["done"] = True
            return {"commands": [{"id": "push-1", "type": "aft_transfer",
                                  "cents": 700, "accountId": "p1"}]}
        return {}

    return _run_scripted(
        key, lambda: ByTheBookAFTMachine(credits=0, complete_in=2,
                                         **machine_kw),
        script, max_polls=300)


def _key_modes(run):
    return [r["aft"].get("keyMode") for r in run["reports"]
            if isinstance(r.get("aft"), dict)]


class TestTheWholeSatellite:
    def test_a_push_to_a_machine_that_cannot_be_registered(self):
        run = _run_push("unregpush", registered=False, registers=False)
        record = run["by_id"]["push-1"]
        assert (record["ok"], record["outcome"], record["keySent"]) == (
            True, "completed", "zero")
        assert run["machine"].credits == 700
        assert run["machine"].keys_seen[-1] == ZERO_KEY
        # the report says what the machine is, and which key it is sent
        assert any(isinstance(r.get("aft"), dict)
                   and r["aft"].get("registered") is False
                   for r in run["reports"])
        assert set(_key_modes(run)) == {"zero"}

    def test_a_push_to_a_machine_that_refuses_the_zeros(self):
        run = _run_push("minds", needs_registration=True)
        record = run["by_id"]["push-1"]
        machine = run["machine"]
        assert (record["ok"], record["outcome"], record["keySent"]) == (
            True, "completed", "machine")
        assert record["amountCents"] == 700 and machine.credits == 700
        assert "with a registration key of all zeros" in record["detail"]
        # the hub books the push under the id of the transfer that moved
        # the credits, not the one that was refused
        assert record["txnId"] == machine.started[-1]
        assert machine.keys_seen == [ZERO_KEY, machine.key]
        # the status block that carries keyMode runs every 30 seconds, so
        # this short run only sees the mode it started in
        assert _key_modes(run)[0] == "zero"
