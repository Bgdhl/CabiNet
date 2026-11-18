"""
A cash-out pull the machine cannot make at the moment it is asked.

The hub's aft_cashout_pull is a player's tap on CASH OUT TO WALLET. A
machine in the middle of a game does not lock for a transfer just then, and
the pull used to end there: "game lock never confirmed", a second after the
tap. The owner, 2026-11-18: "why doesnt the hub hold it for up to 15
seconds and retry when available", then "im not sure i want it as a hold...
but a retry queue.. it shouldnt be paused".

Two layers are checked here:

  * PullRetry on its own, with a clock the test moves: what is kept, when
    it is due, what ends it, what a dropped or replaced pull is told.
  * the whole satellite program (sas_host.main) against a by-the-book
    machine that will not lock for a while: the hub hears ONE result, the
    credits move once the machine is free, and a disarm that says the
    player left stops the pull before it can take the next player's
    credits.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import sas_host
from sas_host import PullRetry
from by_the_book import ByTheBookAFTMachine


# ---------------------------------------------------------------------------
# PullRetry alone
# ---------------------------------------------------------------------------

class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t

    def pass_(self, sec):
        self.t += sec


def _busy(cmd_id="copull1"):
    return {"id": cmd_id, "type": "aft_cashout", "ok": False,
            "outcome": "lock_failed", "amountCents": 0,
            "detail": "game lock never confirmed; transfer NOT sent",
            "result": "lock_failed"}


def _done(cmd_id="copull1", cents=2500):
    return {"id": cmd_id, "type": "aft_cashout", "ok": True,
            "outcome": "completed", "amountCents": cents,
            "detail": "full transfer successful", "result": "ack"}


def _cmd(cmd_id="copull1", retry=15):
    cmd = {"id": cmd_id, "type": "aft_cashout_pull", "accountId": "p1"}
    if retry is not None:
        cmd["retrySec"] = retry
    return cmd


class TestHowLongACommandAsksFor:
    def test_no_retrysec_is_no_retry(self):
        assert PullRetry.window(_cmd(retry=None)) == 0.0

    def test_the_seconds_asked_for(self):
        assert PullRetry.window(_cmd(retry=15)) == 15.0
        assert PullRetry.window(_cmd(retry=2.5)) == 2.5

    def test_never_longer_than_the_ceiling(self):
        assert PullRetry.window(_cmd(retry=3600)) == sas_host.PULL_RETRY_MAX_SEC

    def test_nonsense_is_no_retry(self):
        for bad in (0, -5, True, "15", None, [15], float("nan")):
            assert PullRetry.window(_cmd(retry=bad)) == 0.0, bad


class TestWhatCountsAsNotNow:
    def test_nothing_was_sent(self):
        for outcome in ("lock_failed", "timeout", "unresolved_earlier"):
            assert PullRetry.not_now({"ok": False, "outcome": outcome})

    def test_the_machine_said_unable_at_this_time(self):
        assert PullRetry.not_now({"ok": False, "outcome": "refused",
                                  "transferStatus": 0x87})

    def test_any_other_refusal_is_an_answer(self):
        for status in (0x82, 0x84, 0x88, 0x89, 0x93, None):
            assert not PullRetry.not_now({"ok": False, "outcome": "refused",
                                          "transferStatus": status})

    def test_real_answers_are_not_retried(self):
        for outcome in ("no_credits", "over_ceiling", "not_registered",
                        "asset_unknown", "machine_offline", "sas_disabled",
                        "rolled_back", "error"):
            assert not PullRetry.not_now({"ok": False, "outcome": outcome})

    def test_a_transfer_that_may_have_happened_is_never_sent_again(self):
        assert not PullRetry.not_now({"ok": False, "outcome": "unconfirmed"})

    def test_success_is_not_retried(self):
        assert not PullRetry.not_now(_done())


class TestOneTryAsBefore:
    def test_a_command_without_retrysec_gets_its_answer_at_once(self):
        clock = Clock()
        q = PullRetry(clock=clock)
        cmd = _cmd(retry=None)
        assert q.start(cmd) is None
        assert q.finish(cmd, _busy()) == _busy()      # untouched
        assert q.waiting is None and q.due() is None

    def test_a_first_try_that_works_is_reported_untouched(self):
        q = PullRetry(clock=Clock())
        cmd = _cmd()
        q.start(cmd)
        assert q.finish(cmd, _done()) == _done()
        assert q.waiting is None


class TestKeptAndTriedAgain:
    def _waiting(self):
        clock = Clock()
        q = PullRetry(gap=1.0, clock=clock)
        cmd = _cmd()
        q.start(cmd)
        clock.pass_(1.5)                    # the try itself took a while
        assert q.finish(cmd, _busy()) is None
        return q, cmd, clock

    def test_a_busy_machine_is_not_reported(self):
        q, cmd, clock = self._waiting()
        assert q.waiting is cmd

    def test_it_is_not_due_before_the_gap(self):
        q, cmd, clock = self._waiting()
        assert q.due() is None
        clock.pass_(0.9)
        assert q.due() is None

    def test_it_is_due_after_the_gap(self):
        q, cmd, clock = self._waiting()
        clock.pass_(1.0)
        assert q.due() is cmd

    def test_while_being_tried_it_is_not_due_again(self):
        q, cmd, clock = self._waiting()
        clock.pass_(1.0)
        assert q.start(q.due()) is None
        assert q.due() is None and q.waiting is None

    def test_the_try_that_works_is_the_one_result(self):
        q, cmd, clock = self._waiting()
        clock.pass_(1.0)
        q.start(cmd)
        clock.pass_(1.5)
        result = q.finish(cmd, _done())
        assert (result["ok"], result["outcome"], result["amountCents"]) == (
            True, "completed", 2500)
        assert result["tries"] == 2
        assert result["waitedSec"] == 4.0
        assert "try 2" in result["detail"]
        assert result["detail"].startswith("full transfer successful")
        assert q.waiting is None and q.due() is None

    def test_a_real_answer_ends_the_wait(self):
        q, cmd, clock = self._waiting()
        clock.pass_(1.0)
        q.start(cmd)
        empty = dict(_busy(), outcome="no_credits",
                     detail="machine reports no cashable credits")
        result = q.finish(cmd, empty)
        assert (result["outcome"], result["tries"]) == ("no_credits", 2)
        assert q.waiting is None

    def test_it_stops_when_the_time_asked_for_is_up(self):
        clock = Clock()
        q = PullRetry(gap=1.0, clock=clock)
        cmd = _cmd(retry=15)
        tries = 0
        result = None
        q.start(cmd)
        while True:
            tries += 1
            clock.pass_(1.5)                # each try takes 1.5 s
            result = q.finish(cmd, _busy())
            if result is not None:
                break
            clock.pass_(1.0)
            assert q.start(q.due()) is None
        # tries began at 0, 2.5, 5, 7.5, 10, 12.5 and 15 s; the one that
        # ended at 16.5 could not be followed by another inside 15 s
        assert tries == 7
        assert (result["ok"], result["outcome"]) == (False, "lock_failed")
        assert result["tries"] == 7 and result["waitedSec"] == 16.5
        assert q.waiting is None and q.due() is None

    def test_no_try_begins_after_the_time_asked_for(self):
        clock = Clock()
        q = PullRetry(gap=1.0, clock=clock)
        cmd = _cmd(retry=3)
        q.start(cmd)
        clock.pass_(2.5)                    # ended at 2.5; next would be 3.5
        result = q.finish(cmd, _busy())
        assert result is not None and "tries" not in result


class TestGivingWay:
    def _waiting(self):
        clock = Clock()
        q = PullRetry(gap=1.0, clock=clock)
        cmd = _cmd("copull1")
        q.start(cmd)
        clock.pass_(1.5)
        q.finish(cmd, _busy("copull1"))
        return q, clock

    def test_a_drop_answers_the_waiting_pull_as_cancelled(self):
        q, clock = self._waiting()
        clock.pass_(2.0)
        gone = q.drop("the player left")
        assert (gone["id"], gone["type"], gone["ok"], gone["outcome"],
                gone["amountCents"], gone["result"]) == (
            "copull1", "aft_cashout", False, "cancelled", 0, "cancelled")
        assert gone["tries"] == 1 and gone["waitedSec"] == 3.5
        assert "the player left" in gone["detail"]
        assert "game lock never confirmed" in gone["detail"]

    def test_after_a_drop_nothing_is_waiting_or_due(self):
        q, clock = self._waiting()
        q.drop("the player left")
        clock.pass_(60)
        assert q.waiting is None and q.due() is None

    def test_a_drop_with_nothing_waiting_says_nothing(self):
        assert PullRetry(clock=Clock()).drop("the player left") is None
        q, clock = self._waiting()
        q.drop("x")
        assert q.drop("x") is None

    def test_a_different_pull_takes_the_place(self):
        q, clock = self._waiting()
        newer = _cmd("copull2")
        gone = q.start(newer)
        assert (gone["id"], gone["outcome"], gone["ok"]) == (
            "copull1", "cancelled", False)
        clock.pass_(1.5)
        assert q.finish(newer, _busy("copull2")) is None
        assert q.waiting is newer

    def test_a_pull_without_retry_still_takes_the_place(self):
        q, clock = self._waiting()
        once = _cmd("sweep1", retry=None)
        assert q.start(once)["id"] == "copull1"
        assert q.finish(once, _busy("sweep1")) == _busy("sweep1")
        assert q.waiting is None

    def test_a_result_for_a_pull_never_started_passes_through(self):
        q = PullRetry(clock=Clock())
        assert q.finish(_cmd("stray"), _busy("stray")) == _busy("stray")


# ---------------------------------------------------------------------------
# The whole satellite program
# ---------------------------------------------------------------------------

class BusyMachine(ByTheBookAFTMachine):
    """A machine in the middle of a game: a lock request is answered
    "lock pending" (40) and stays pending until the request is cancelled
    (SAS 6.02 §8.2: "If the game is playable but the requested transfer
    availability condition is not met, the gaming machine may stay in the
    lock pending state indefinitely"). `in_game` is how many lock REQUESTS it
    answers that way before the game ends (None: for ever);
    `lock_requests` counts them."""

    def __init__(self, in_game=None, **kw):
        super().__init__(**kw)
        self.in_game = in_game
        self.lock_requests = 0
        self.lock_cancels = 0
        self._pending = False

    def _lock(self, code):
        if code == 0x00:
            self.lock_requests += 1
            if self.in_game is None or self.in_game > 0:
                if self.in_game is not None:
                    self.in_game -= 1
                self._pending = True
        elif code == 0x80:
            self.lock_cancels += 1
            self._pending = False
        if not self._pending:
            return super()._lock(code)
        answer = bytearray(super()._lock(0xFF))
        # the lock status is the byte after address, command, length and
        # the four of the asset number
        answer[7] = 0x40
        body = bytes(answer[:-2])
        return body + sas_host_crc(body)


def sas_host_crc(body):
    from core.sas_protocol import sas_crc
    return sas_crc(body).to_bytes(2, "little")


_RUNS = {}


def _run(key, machine_factory, script, max_polls=400, gap=0.05):
    """sas_host.main() against a scripted hub (test_sas_host_commands), with
    the transfer's own pacing and the retry gap shrunk so a run of several
    tries takes well under a second each."""
    if key in _RUNS:
        return _RUNS[key]
    from test_sas_host_commands import _run_scripted
    real_host = sas_host.AFTHost
    saved_gap = sas_host.PULL_RETRY_GAP_SEC
    sas_host.AFTHost = lambda *a, **k: real_host(*a, pace=0.001, **k)
    sas_host.PULL_RETRY_GAP_SEC = gap
    try:
        run = _run_scripted(f"pullretry-{key}", machine_factory, script,
                            max_polls=max_polls)
    finally:
        sas_host.AFTHost = real_host
        sas_host.PULL_RETRY_GAP_SEC = saved_gap
    _RUNS[key] = run
    return run


def _once_online(commands):
    """A hub script: hand `commands` over in the first reply after the
    machine is seen online."""
    served = {"done": False}

    def script(i, reports):
        if not served["done"] and any(r.get("online") for r in reports):
            served["done"] = True
            return {"commands": commands}
        return {}
    return script


def _results_for(run, cmd_id):
    """Every distinct result the hub was sent for this command id."""
    seen = []
    for rep in run["reports"]:
        for r in rep.get("commandResults") or []:
            if r.get("id") == cmd_id and r not in seen:
                seen.append(r)
    return seen


def _busy_then_free():
    return _run(
        "busy-then-free",
        lambda: BusyMachine(in_game=3, credits=2500, complete_in=2),
        _once_online([{"id": "copull1", "type": "aft_cashout_pull",
                       "accountId": "p1", "retrySec": 15}]))


class TestABusyMachineIsTriedAgain:
    def test_the_credits_move_once_the_game_has_ended(self):
        run = _busy_then_free()
        assert run["machine"].credits == 0

    def test_the_hub_hears_one_result_and_it_is_the_good_one(self):
        run = _busy_then_free()
        results = _results_for(run, "copull1")
        assert len(results) == 1, results
        r = results[0]
        assert (r["type"], r["ok"], r["outcome"], r["amountCents"],
                r["accountId"]) == ("aft_cashout", True, "completed", 2500,
                                    "p1")

    def test_it_says_how_many_tries_it_took(self):
        r = _busy_then_free()["by_id"]["copull1"]
        assert r["tries"] == 4 and "try 4" in r["detail"]
        assert isinstance(r["waitedSec"], float)

    def test_each_busy_try_asked_for_the_lock_and_let_it_go(self):
        machine = _busy_then_free()["machine"]
        # three tries found it busy and cancelled their request; the
        # fourth got the lock (aft_transfer asks once per try)
        assert machine.lock_requests == 4
        assert machine.lock_cancels == 3

    def test_only_one_transfer_was_ever_started(self):
        assert len(_busy_then_free()["machine"].started) == 1


def _busy_no_retry():
    return _run(
        "busy-no-retry",
        lambda: BusyMachine(in_game=3, credits=2500, complete_in=2),
        _once_online([{"id": "sweep1", "type": "aft_cashout_pull",
                       "accountId": "house"}]))


class TestACommandThatDoesNotAskIsTriedOnce:
    def test_the_answer_is_the_busy_one(self):
        run = _busy_no_retry()
        results = _results_for(run, "sweep1")
        assert len(results) == 1
        r = results[0]
        assert (r["ok"], r["outcome"], r["amountCents"]) == (
            False, "lock_failed", 0)
        assert "tries" not in r

    def test_the_machine_keeps_its_credits_and_was_asked_once(self):
        machine = _busy_no_retry()["machine"]
        assert machine.credits == 2500
        assert machine.lock_requests == 1 and not machine.started


def _busy_for_ever():
    return _run(
        "busy-for-ever",
        lambda: BusyMachine(in_game=None, credits=2500),
        _once_online([{"id": "copull2", "type": "aft_cashout_pull",
                       "accountId": "p1", "retrySec": 0.6}]),
        max_polls=500)


class TestTheTimeAskedForRunsOut:
    def test_the_hub_hears_one_result_and_it_is_the_busy_one(self):
        run = _busy_for_ever()
        results = _results_for(run, "copull2")
        assert len(results) == 1, results
        r = results[0]
        assert (r["ok"], r["outcome"], r["amountCents"]) == (
            False, "lock_failed", 0)

    def test_it_was_tried_more_than_once_and_then_left_alone(self):
        run = _busy_for_ever()
        r = run["by_id"]["copull2"]
        assert r["tries"] >= 2 and r["waitedSec"] <= 2.0
        machine = run["machine"]
        assert machine.lock_requests == r["tries"]
        assert machine.credits == 2500 and not machine.started
        # every request it left pending was cancelled on the way out
        assert machine.lock_cancels == machine.lock_requests


def _left(drop):
    """A pull waiting on a busy machine when the hub disarms. The game
    ends two replies after the disarm: whatever is still waiting then
    would go through."""
    box = {}

    def factory():
        box["m"] = BusyMachine(in_game=None, credits=2500, complete_in=2)
        return box["m"]

    state = {"pull": None, "disarm": None}

    def script(i, reports):
        if state["pull"] is None:
            if any(r.get("online") for r in reports):
                state["pull"] = i
                return {"commands": [{"id": "copull3",
                                      "type": "aft_cashout_pull",
                                      "accountId": "p1", "retrySec": 15}]}
            return {}
        if state["disarm"] is None and box["m"].lock_requests >= 2:
            state["disarm"] = i
            cmd = {"id": "codis1", "type": "aft_set_host_cashout",
                   "enable": False}
            if drop:
                cmd["dropPull"] = True
            return {"commands": [cmd]}
        if state["disarm"] is not None and i >= state["disarm"] + 2:
            box["m"].in_game = 0               # the game ends
        return {}

    return _run("left-drop" if drop else "left-keep", factory, script,
                max_polls=600)


class TestThePlayerLeftWhileItWaited:
    def test_the_waiting_pull_is_answered_cancelled(self):
        run = _left(drop=True)
        results = _results_for(run, "copull3")
        assert len(results) == 1, results
        r = results[0]
        assert (r["type"], r["ok"], r["outcome"], r["amountCents"]) == (
            "aft_cashout", False, "cancelled", 0)
        assert "the player left" in r["detail"]

    def test_nothing_is_pulled_once_the_machine_is_free(self):
        machine = _left(drop=True)["machine"]
        assert machine.in_game == 0            # the game did end in the run
        assert machine.credits == 2500 and not machine.started

    def test_the_disarm_itself_is_still_answered(self):
        r = _left(drop=True)["by_id"]["codis1"]
        assert (r["ok"], r["outcome"]) == (True, "disarmed")


class TestTheButtonSwitchedOffIsNotThePlayerLeaving:
    """A disarm without dropPull is the player turning off where the
    machine's own CASH OUT button sends credits. The cash-out they asked
    for from the screen is still wanted."""

    def test_the_pull_goes_through_when_the_machine_is_free(self):
        run = _left(drop=False)
        assert run["machine"].credits == 0
        results = _results_for(run, "copull3")
        assert len(results) == 1, results
        assert (results[0]["ok"], results[0]["outcome"],
                results[0]["amountCents"]) == (True, "completed", 2500)


class DeafOnce(ByTheBookAFTMachine):
    """Loses the answer to the first 0x74 status read it is sent after
    `armed` is set (one corrupted frame)."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.armed = False
        self.lost = 0

    def _lock(self, code):
        if self.armed and code == 0xFF and not self.lost:
            self.lost += 1
            return b""
        return super()._lock(code)


def _lost_status_read(retry):
    box = {}

    def factory():
        box["m"] = DeafOnce(credits=2500, complete_in=2)
        return box["m"]

    served = {"done": False}

    def script(i, reports):
        if not served["done"] and any(r.get("online") for r in reports):
            served["done"] = True
            box["m"].armed = True
            cmd = {"id": "copull4", "type": "aft_cashout_pull",
                   "accountId": "p1"}
            if retry:
                cmd["retrySec"] = 15
            return {"commands": [cmd]}
        return {}

    return _run("lost-read-retry" if retry else "lost-read-once", factory,
                script)


class TestNoAnswerIsNotNoCredits:
    def test_one_lost_frame_does_not_say_the_machine_is_empty(self):
        run = _lost_status_read(retry=False)
        r = run["by_id"]["copull4"]
        assert run["machine"].lost == 1
        assert (r["ok"], r["outcome"]) == (False, "timeout")
        assert run["machine"].credits == 2500

    def test_with_retry_the_next_try_brings_the_credits(self):
        run = _lost_status_read(retry=True)
        r = run["by_id"]["copull4"]
        assert run["machine"].lost == 1
        assert (r["ok"], r["outcome"], r["amountCents"], r["tries"]) == (
            True, "completed", 2500, 2)
        assert run["machine"].credits == 0


class UnableMachine(ByTheBookAFTMachine):
    """Locks at once, then answers the transfer itself with status 87,
    "unable to perform transfers at this time", for the first `unable`
    transfers it is sent."""

    def __init__(self, unable=0, **kw):
        super().__init__(**kw)
        self.unable = unable
        self.said_87 = 0

    def _key_refusal(self, d):
        if self.unable > 0:
            self.unable -= 1
            self.said_87 += 1
            return 0x87
        return super()._key_refusal(d)


def _unable(retry):
    cmd = {"id": "copull5", "type": "aft_cashout_pull", "accountId": "p1"}
    if retry:
        cmd["retrySec"] = 15
    return _run("unable-retry" if retry else "unable-once",
                lambda: UnableMachine(unable=4, credits=2500,
                                      complete_in=2),
                _once_online([cmd]))


class TestUnableAtThisTime:
    """Status 87 is from the document (SAS 6.02 Table 8.3e); no machine on
    the floor has answered a pull with it."""

    def test_without_retry_it_is_the_answer(self):
        run = _unable(retry=False)
        r = run["by_id"]["copull5"]
        assert (r["ok"], r["outcome"], r["transferStatus"]) == (
            False, "refused", 0x87)
        assert run["machine"].credits == 2500

    def test_with_retry_a_later_try_brings_the_credits(self):
        run = _unable(retry=True)
        results = _results_for(run, "copull5")
        assert len(results) == 1, results
        r = results[0]
        assert (r["ok"], r["outcome"], r["amountCents"]) == (
            True, "completed", 2500)
        assert r["tries"] >= 2
        assert run["machine"].said_87 == 4 and run["machine"].credits == 0
