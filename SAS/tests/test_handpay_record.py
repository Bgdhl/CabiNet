"""
A handpay on a SAS machine: what it was for and how it ended (v0.3.28).

Until this the satellite collected the machine's handpay record (long
poll 1B, SAS 6.02 Table 7.8) without reading it, and told the hub only
that a handpay was pending. A machine with no G2S side had no amount on
the hub and nothing was booked when the win was paid by hand.

The records below are the ones a WMS Bluebird 2 gave on 2026-11-16 and
17, as the satellite logged them; the third is the $10,750.00 win, known
from the machine's own G2S side.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import sas_host
from sas_host import HandpayLatch, parse_handpay_record

PENDING, RESET, CANCELLED = 0x51, 0x52, 0x55


def record(hex_frame):
    """The record bytes of a logged 1B frame: after [addr][1B], before CRC."""
    return bytes.fromhex(hex_frame)[2:-2]


SEEN = {
    "progressive level 4": "011b0004000122000000000100000000000000000000c36a",
    "progressive level 2": "011b00020001929100000001000000000000000000006102",
    "the $10,750.00 win": "011b0000000107500000000100000000000000000000e0e3",
    "a $22,500.00 win": "011b000000022500000000010000000000000000000097b5",
}


class TestTheRecord:
    def test_a_win_is_in_accounting_units(self):
        r = parse_handpay_record(record(SEEN["the $10,750.00 win"]))
        assert r == {"group": 0, "level": 0, "amount": 1_075_000,
                     "partial": 0, "resetId": 1, "kind": "win",
                     "inCents": False}

    def test_a_progressive_is_in_cents(self):
        r = parse_handpay_record(record(SEEN["progressive level 4"]))
        assert (r["kind"], r["level"], r["amount"], r["inCents"]) == \
            ("progressive", 4, 1_220_000, True)
        r = parse_handpay_record(record(SEEN["progressive level 2"]))
        assert (r["level"], r["amount"]) == (2, 1_929_100)

    def test_the_other_levels(self):
        base = bytearray(record(SEEN["a $22,500.00 win"]))
        base[1] = 0x80
        assert parse_handpay_record(bytes(base))["kind"] == "cancelledCredits"
        base[1] = 0x40
        assert parse_handpay_record(bytes(base))["kind"] == "topWin"
        base[1], base[9] = 0x00, 0x00
        assert parse_handpay_record(bytes(base))["resetId"] == 0

    def test_partial_pay(self):
        base = bytearray(record(SEEN["a $22,500.00 win"]))
        base[7:9] = bytes.fromhex("1250")
        assert parse_handpay_record(bytes(base))["partial"] == 1250

    def test_nothing_to_read(self):
        assert parse_handpay_record(bytes(20)) is None      # queue drained
        assert parse_handpay_record(b"\x00\x00\x01") is None  # short
        assert parse_handpay_record(None) is None
        bad = bytearray(record(SEEN["a $22,500.00 win"]))
        bad[3] = 0xAB                                       # not BCD
        assert parse_handpay_record(bytes(bad)) is None


def lockup(latch, credits=500, seen="the $10,750.00 win"):
    latch.on_credits(credits)
    latch.on_exception(PENDING)
    latch.on_info(parse_handpay_record(record(SEEN[seen])))
    return latch


class TestHowItEnded:
    def test_pending_carries_the_record(self):
        latch = lockup(HandpayLatch(settle_sec=0))
        assert latch.pending["amount"] == 1_075_000
        assert latch.pending["kind"] == "win"
        assert latch.pending["creditsAtLockup"] == 500
        assert latch.last is None

    def test_the_record_seen_first_stands(self):
        latch = lockup(HandpayLatch(settle_sec=0))
        latch.on_exception(PENDING)          # the machine asks again
        latch.on_info(parse_handpay_record(record(SEEN["a $22,500.00 win"])))
        assert latch.pending["amount"] == 1_075_000

    def test_the_hubs_reset_puts_it_on_the_credit_meter(self):
        latch = lockup(HandpayLatch(settle_sec=0))
        latch.begin_reset()
        latch.on_exception(RESET)            # arrives inside the reset
        latch.end_reset()
        latch.on_reset_ok()
        assert latch.pending is None
        assert (latch.last["how"], latch.last["amount"]) == \
            ("credit", 1_075_000)
        assert latch.last["seq"] == 1        # reported once, not twice

    def test_the_hubs_reset_when_the_52_is_not_seen(self):
        latch = lockup(HandpayLatch(settle_sec=0))
        latch.begin_reset()
        latch.end_reset()
        latch.on_reset_ok()
        assert latch.last["how"] == "credit"

    def test_keyed_off_at_the_machine_is_paid_by_hand(self):
        latch = lockup(HandpayLatch(settle_sec=0), credits=500)
        latch.on_exception(RESET)
        assert latch.pending is None
        assert latch.last is None            # waits for a credit reading
        latch.on_credits(500)                # the meter did not move
        assert (latch.last["how"], latch.last["amount"]) == \
            ("hand", 1_075_000)

    def test_keyed_off_to_the_credit_meter_at_the_machine(self):
        latch = lockup(HandpayLatch(settle_sec=0), credits=500)
        latch.on_exception(RESET)
        latch.on_credits(500 + 1_075_000 - 300)   # less one bet since
        assert latch.last["how"] == "credit"

    def test_a_small_rise_is_not_the_handpay(self):
        latch = lockup(HandpayLatch(settle_sec=0), credits=500)
        latch.on_exception(RESET)
        latch.on_credits(500 + 2_000)        # a bill went in
        assert latch.last["how"] == "hand"

    def test_a_progressive_is_compared_in_credits(self):
        latch = lockup(HandpayLatch(denom_cents=lambda: 25, settle_sec=0), credits=0,
                       seen="progressive level 4")     # 1,220,000 cents
        latch.on_exception(RESET)
        latch.on_credits(1_220_000 // 25)
        assert latch.last["how"] == "credit"
        latch = lockup(HandpayLatch(denom_cents=lambda: None, settle_sec=0), credits=0,
                       seen="progressive level 4")
        latch.on_exception(RESET)
        latch.on_credits(1_220_000)          # no denomination to size it by:
        assert latch.last["how"] == "credit"  # any rise is the win
        latch = lockup(HandpayLatch(denom_cents=lambda: None, settle_sec=0), credits=0,
                       seen="progressive level 4")
        latch.on_exception(RESET)
        latch.on_credits(0)
        assert latch.last["how"] == "hand"

    def test_no_reading_before_the_lockup(self):
        latch = HandpayLatch(settle_sec=0)
        latch.on_exception(PENDING)
        latch.on_info(parse_handpay_record(record(SEEN["a $22,500.00 win"])))
        latch.on_exception(RESET)
        latch.on_credits(2_250_000)
        assert latch.last["how"] == "hand"   # nothing to compare with

    def test_cancelled_by_the_player(self):
        latch = lockup(HandpayLatch(settle_sec=0))
        latch.on_exception(CANCELLED)
        assert latch.pending is None and latch.last["how"] == "cancelled"

    def test_the_machine_says_it_is_in_no_handpay(self):
        latch = lockup(HandpayLatch(settle_sec=0))
        latch.on_no_handpay()
        assert latch.pending is None and latch.last["how"] == "gone"

    def test_each_one_is_numbered(self):
        latch = lockup(HandpayLatch(settle_sec=0))
        latch.on_exception(CANCELLED)
        lockup(latch)
        latch.on_exception(RESET)
        latch.on_credits(500)
        assert latch.last["seq"] == 2 and latch.last["how"] == "hand"

    def test_a_reset_with_nothing_pending_changes_nothing(self):
        latch = HandpayLatch(settle_sec=0)
        latch.on_exception(RESET)
        latch.on_reset_ok()
        latch.on_no_handpay()
        latch.on_credits(100)
        assert latch.pending is None and latch.last is None

    def test_a_bad_reading_does_not_settle_it(self):
        latch = lockup(HandpayLatch(settle_sec=0))
        latch.on_exception(RESET)
        latch.on_credits(None)
        assert latch.last is None
        latch.on_credits(500)
        assert latch.last["how"] == "hand"


WIN = 1_075_000


def keyed(credits_after, jackpot_after, kind=None, **kw):
    """A lockup with 500 credits and 7,000 on the jackpot meter, keyed off
    at the machine, then the two meters as read afterwards."""
    latch = HandpayLatch(settle_sec=0, **kw)
    latch.on_jackpot(7_000)
    lockup(latch, credits=500)
    if kind:
        latch.pending["kind"] = kind
    latch.on_exception(RESET)
    if credits_after is not None:
        latch.on_credits(credits_after)
    if jackpot_after is not None:
        latch.on_jackpot(jackpot_after)
    return latch


class TestWhichKey:
    """SAS 6.02 §14.2: "Jackpot handpay resets to the credit meter are
    accounted for by the gaming machine in the coin out meter, not the
    jackpot meter." Appendix G: Total Jackpot is "the cumulative sum of
    all credits paid by an attendant"."""

    def test_the_handpay_key_shows_on_the_jackpot_meter(self):
        latch = keyed(500, 7_000 + WIN)
        assert latch.pending is None and latch.last["how"] == "hand"

    def test_the_credits_key_shows_on_the_credit_meter_only(self):
        latch = keyed(500 + WIN, 7_000)
        assert latch.last["how"] == "credit"

    def test_neither_meter_moved_is_not_booked_as_anything(self):
        assert keyed(500, 7_000).last["how"] == "unknown"

    def test_both_moved_is_not_paid_by_hand(self):
        assert keyed(500 + WIN, 7_000 + WIN).last["how"] == "credit"

    def test_it_waits_for_both_readings(self):
        latch = keyed(500, None)
        assert latch.last is None            # the jackpot reading is due
        latch.on_jackpot(7_000 + WIN)
        assert latch.last["how"] == "hand"
        latch = keyed(None, 7_000 + WIN)
        assert latch.last is None            # and so is the credit one
        latch.on_credits(500)
        assert latch.last["how"] == "hand"

    def test_the_order_of_the_readings_does_not_matter(self):
        latch = HandpayLatch(settle_sec=0)
        latch.on_jackpot(7_000)
        lockup(latch, credits=500)
        latch.on_exception(RESET)
        latch.on_jackpot(7_000)              # jackpot first, unchanged
        latch.on_credits(500 + WIN)
        assert latch.last["how"] == "credit"

    def test_a_jackpot_reading_that_never_comes(self):
        now = [100.0]
        latch = keyed(500, None, clock=lambda: now[0])
        assert latch.last is None
        now[0] += HandpayLatch.JACKPOT_WAIT_SEC + 0.1
        latch.on_credits(500)                # judged on the credit meter
        assert latch.last["how"] == "hand"

    def test_a_machine_that_gives_no_jackpot_meter(self):
        latch = lockup(HandpayLatch(settle_sec=0), credits=500)
        latch.on_exception(RESET)
        latch.on_credits(500)                # nothing to wait for
        assert latch.last["how"] == "hand"

    def test_a_cash_out_paid_by_hand(self):
        # cancelled credits do not go on the jackpot meter (Appendix G)
        latch = keyed(0, 7_000, kind="cancelledCredits")
        assert latch.last["how"] == "hand"

    def test_readings_from_before_the_wait_is_over_are_not_used(self):
        now = [100.0]
        latch = HandpayLatch(clock=lambda: now[0])
        latch.on_jackpot(7_000)
        lockup(latch, credits=500)
        latch.on_exception(RESET)
        latch.on_credits(500)
        latch.on_jackpot(7_000)              # the meters have not caught up
        assert latch.last is None
        now[0] += HandpayLatch.KEY_OFF_SETTLE_SEC
        latch.on_credits(500)
        latch.on_jackpot(7_000 + WIN)
        assert latch.last["how"] == "hand"


    def test_one_key_off_does_not_colour_the_next(self):
        latch = keyed(500, 7_000 + WIN)                  # handpay key
        assert latch.last["how"] == "hand"
        lockup(latch, credits=500)                       # a second win
        latch.on_exception(RESET)
        latch.on_credits(500 + WIN)                      # credits key
        assert latch.last["seq"] == 1                    # jackpot is due
        latch.on_jackpot(7_000 + WIN)                    # unchanged since
        assert (latch.last["seq"], latch.last["how"]) == (2, "credit")


def test_the_meter_sweep_feeds_the_latch():
    seen = []
    latch = HandpayLatch(settle_sec=0)
    feed = sas_host.feed_handpay_latch(
        lambda *a: seen.append(a), latch)
    feed(1, 0x1A, 500)
    feed(1, 0x0F, {"cancelled_credits": 1, "coin_in": 2, "coin_out": 3,
                   "total_drop": 4, "jackpot": 7_000, "games_played": 5})
    feed(1, 0x46, 0)
    assert len(seen) == 3                    # the ingest still gets all
    latch.on_exception(PENDING)
    assert latch.pending["creditsAtLockup"] == 500
    assert latch.pending["jackpotAtLockup"] == 7_000


class TestTheWait:
    """A key-off at the machine is judged on a credit reading taken a
    few seconds after the reset, not on one from the moment of it."""

    def test_a_reading_at_the_moment_of_the_reset_is_not_used(self):
        now = [100.0]
        latch = lockup(HandpayLatch(clock=lambda: now[0]), credits=500)
        latch.on_exception(RESET)            # the credits key was turned
        latch.on_credits(500)                # the meter has not caught up
        assert latch.last is None            # so: not "paid by hand" yet
        now[0] += HandpayLatch.KEY_OFF_SETTLE_SEC - 0.1
        latch.on_credits(500)
        assert latch.last is None
        now[0] += 0.2
        latch.on_credits(500 + 1_075_000)    # and now it has
        assert latch.last["how"] == "credit"

    def test_the_handpay_key_is_by_hand_once_the_wait_is_over(self):
        now = [100.0]
        latch = lockup(HandpayLatch(clock=lambda: now[0]), credits=500)
        latch.on_exception(RESET)
        now[0] += HandpayLatch.KEY_OFF_SETTLE_SEC
        latch.on_credits(500)
        assert latch.last["how"] == "hand"

    def test_the_hubs_reset_does_not_wait(self):
        now = [100.0]
        latch = lockup(HandpayLatch(clock=lambda: now[0]))
        latch.begin_reset()
        latch.on_exception(RESET)
        latch.end_reset()
        assert latch.last["how"] == "credit"


def test_the_report_carries_both():
    assert "lastHandpay" in Path(sas_host.__file__).read_text()
