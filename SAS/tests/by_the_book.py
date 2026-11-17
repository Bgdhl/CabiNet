"""
A pretend gaming machine that keeps SAS 6.02 to the letter.

Why it exists (2026-11-16): the machines on the bench forgive most of what
an audit of this stack against the document turned up. A WMS Bluebird 2
answers a command sent three times in a row, takes a poll 25 ms after the
last one, and so on, so a test against a machine modelled on it proves
nothing about a machine that does what the document says. This one does.
Every fix from that audit is shown failing here first.

It is not a test file (no test_ prefix); tests import it. It grows with each
batch of the audit. What it models so far:

  §3.1  implied ACK — the machine disposes of what it sent only when the
        host's NEXT poll is one Table 3.1 counts as an acknowledgement.
  §3.2  implied NACK — the identical poll again re-sends the last answer
        and disposes of nothing; a third gets no answer and the machine
        keeps what it was trying to deliver.
  §15.10 the validation buffer behind long poll 4D: function 00 returns the
        next unread record and marks it read on the ACK; FF returns the
        same record and marks nothing; 01-1F return a buffer position.
        Exception 3D (or 3E, for a handpay with no receipt) is raised while
        unread records remain.
  §7.8.1 the handpay queue behind long poll 1B: the oldest unreported entry
        is sent, and marked reported when the answer is acknowledged.
        Exception 51 is raised while unreported entries remain.

Line faults are injected on the machine's ANSWERS (`faults`), because that
is where a real one bites: the machine believes it answered.

`mark_read_on`, `peek_marks_read` and `resends_on_nack` turn on ways a real
machine might stray from §3.2 and §15.10 (marking a record read when it is
SENT, counting a look-ahead as the read, treating a repeated poll as a new
one), so a test can show the host copes with those too.

With a `clock` it also keeps time (audit batch 4):

  §2.3.3 polling rate — a poll that comes less than `min_gap` seconds after
        this machine's last exchange is ignored, as an over-polled machine
        ignores it. The two exemptions the document gives are kept: the
        same poll again as a NACK (§3.2), and a long poll that is the next
        poll after a priority exception (§2.2.1).
  §3.3  synchronisation — after five seconds with no address byte the
        machine ignores polls to itself until it sees another address
        polled (`must_resync`).
  §4.2  loop break — in that state it chirps its own address (`chirps`).

`general_faults` spoils the answers to general polls the same way `faults`
does for long polls (only DROP makes sense for a one-byte answer).

Lock, unlock and the handpay lockup (audit batch 6):

  §7.4  shutdown (01) and startup (02) are type S polls: the answer is ACK
        (the bare address) or NACK (address | 0x80), or the busy answer of
        §4.1 ([address][00]) when the machine cannot take it just now
        (`busy`, `nack`, `deaf` make it give each). §7.4.1 lets a machine
        cash out its credits when it is shut down
        (`cashes_out_on_shutdown`).
  §14.2 A8 enables a reset method for the pending handpay: code 00
        enabled, 01 unable, 02 not in a handpay.
  §7.9  94 resets the handpay: code 00 was reset, 01 unable, 02 not in a
        handpay. With the reset-to-credit method enabled the amount goes on
        the credit meter. A machine not configured for remote reset ignores
        94 (`remote_reset=False`).
  §7.8.1 exception 52 "is not sent until the corresponding 1B long poll has
        been received, responded to and acknowledged, and the associated
        handpay has been reset". Exception 55: the player cancelled the
        handpay request (player_cancels_handpay).

StrictMachine in test_sas_implied_ack.py is the older, narrower relative:
it models what a tester's Ainsworth A560 was seen to do with three 0x73
polls, which is not quite the document either.
"""

from core.sas_meters import int_to_bcd
from core.sas_protocol import sas_crc

# §2.2.1: the priority exceptions. A long poll that is the next poll after
# one of these need not wait for the polling rate.
PRIORITY_EXCEPTIONS = frozenset({
    0x57, 0x67, 0x68, 0x3F, 0x6A, 0x6B, 0x6F, 0x56, 0x3D, 0x3E, 0x69, 0x6C,
    0x6D, 0x51, 0x52, 0x8F, 0x70})

DAMAGE = "damage"        # the answer arrives with a bad CRC
DROP = "drop"            # the answer is sent and never arrives
CUT = "cut"              # the answer arrives short

HANDPAY_NO_RECEIPT_TYPES = (0x40, 0x60)


def frame(address, command, data=b""):
    body = bytes([address, command]) + bytes(data)
    return body + sas_crc(body).to_bytes(2, "little")


class ByTheBookMachine:
    def __init__(self, address=1, mark_read_on="ack", peek_marks_read=False,
                 knows_look_ahead=True, speaks_4d=True,
                 resends_on_nack=True, answers_4d_with_zeros=False,
                 clock=None, min_gap=0.0, remote_reset=True,
                 can_reset_to_credit=True, cashes_out_on_shutdown=False):
        self.address = address
        # lock/unlock and the handpay lockup
        self.shut_down = False
        self.cashes_out_on_shutdown = cashes_out_on_shutdown
        self.busy = {}               # command -> busy answers still to give
        self.nack = {}               # command -> NACKs still to give
        self.deaf = set()            # commands that get no answer at all
        self.credits = 0             # the credit meter, in cents
        self.ticketed = []           # amounts cashed out to a ticket
        self.in_handpay = False      # locked up in a handpay
        self.handpay_amount = 0
        self.reset_method = 0x00     # 00 standard, 01 to the credit meter
        self.remote_reset = remote_reset
        self.can_reset_to_credit = can_reset_to_credit
        self.reset_unable = False    # answer 94 with code 01
        self.key_off_after_a8 = False  # an attendant resets it in between
        self._reset_done = False     # reset, its exception 52 still owed
        self.resends_on_nack = resends_on_nack      # True per §3.2
        # timekeeping (only with a clock)
        self.clock = clock
        self.min_gap = min_gap       # §2.3.3: 0.2, or 0.04 where supported
        self.too_soon = []           # polls ignored for coming too early
        self.must_resync = False     # §3.3: waiting to see another address
        self.chirps = 0              # §4.2: read windows that held a chirp
        self._heard_at = None        # last address byte, any address
        self._polled_at = None       # end of our last exchange
        self._last_raw = None        # the last poll to us, byte for byte
        self._after_priority = False
        self.general_faults = []     # one entry per upcoming general answer
        # standard validation, the other way §15.10 allows: answer 0x4D,
        # with "all zeros in the Validation Number field"
        self.answers_4d_with_zeros = answers_4d_with_zeros
        self.mark_read_on = mark_read_on            # "ack" (§15.10) | "send"
        self.peek_marks_read = peek_marks_read      # False per §15.10
        self.knows_look_ahead = knows_look_ahead    # False: silent to FF
        self.speaks_4d = speaks_4d                  # False: standard validation
        self.records = []        # validation buffer, oldest first
        self.handpays = []       # handpay queue, oldest first
        self.exceptions = []     # FIFO of exception codes not yet delivered
        self.faults = []         # one entry per upcoming long-poll answer
        self.received = []       # every frame, in order
        self.acks = 0            # implied ACKs that disposed of something
        self._last = None        # the poll being counted: "general" | command
        self._run = 0            # how many times in a row it has come
        self._answer = b""       # what a NACK asks for again
        self._on_ack = None      # disposal that waits for the implied ACK

    # -- what happens AT the machine ----------------------------------------

    def print_ticket(self, vn, amount_cents, validation_type=0x00,
                     ticket_number=1):
        """A validation lands in the buffer and its exception is raised."""
        self.records.append({
            "type": validation_type, "vn": str(vn), "amount": amount_cents,
            "ticket_number": ticket_number, "read": False})
        self._raise_validation()

    def handpay(self, data=None):
        """A handpay lands in the queue and exception 51 is raised."""
        if data is None:
            # Table 7.8: group, level, amount, partial pay, reset ID, unused
            data = bytes([0x00, 0x40]) + int_to_bcd(125000, 5) \
                + int_to_bcd(0, 2) + bytes([0x00]) + bytes(10)
        self.handpays.append({"data": bytes(data), "reported": False})
        self.in_handpay = True
        self.handpay_amount = int(bytes(data)[2:7].hex())
        self.reset_method = 0x00
        if 0x51 not in self.exceptions:
            self.exceptions.append(0x51)

    def key_off(self):
        """An attendant resets the handpay at the machine (standard)."""
        self._reset_handpay(to_credit=False)

    def player_cancels_handpay(self):
        """The player cancels the handpay request: exception 55, no 52."""
        self.in_handpay = False
        self.exceptions.append(0x55)

    def _reset_handpay(self, to_credit):
        if not self.in_handpay:
            return
        self.in_handpay = False
        if to_credit:
            self.credits += self.handpay_amount
        self._reset_done = True
        self._owe_52()

    def _owe_52(self):
        """§7.8.1: 52 only once the handpay is reset AND its 1B record has
        been read and acknowledged."""
        if self._reset_done and all(h["reported"] for h in self.handpays):
            self._reset_done = False
            self.exceptions.append(0x52)

    def fifteen_seconds_pass(self):
        """The document's reissue timers: 3D/3E while unread records remain
        (§15.10), 51 while a handpay is unreported (§7.8.1)."""
        self._raise_validation()
        if any(not h["reported"] for h in self.handpays) \
                and 0x51 not in self.exceptions:
            self.exceptions.append(0x51)

    def unread(self):
        return [r for r in self.records if not r["read"]]

    def _raise_validation(self):
        unread = self.unread()
        if not unread:
            return
        code = 0x3E if unread[0]["type"] in HANDPAY_NO_RECEIPT_TYPES else 0x3D
        if code not in self.exceptions:
            self.exceptions.append(code)

    # -- the wire --------------------------------------------------------------

    def __call__(self, raw, wakeup):
        raw = bytes(raw)
        self.received.append(raw)
        if not wakeup[0]:
            return b""
        general = len(raw) == 1
        polled = raw[0] & 0x7F
        now = self.clock() if self.clock is not None else None
        if now is not None:
            # §3.3 / §4.2: five seconds with no address byte at all
            if self._heard_at is not None and now - self._heard_at >= 5.0:
                self.must_resync = True
                self._on_ack = None      # a lost sync is an implied NACK
            self._heard_at = now
        if polled != self.address:
            # another machine's poll, or address 0: Table 3.1's "poll to a
            # different gaming machine address" / global broadcast
            if self.must_resync:
                self.must_resync = False
            else:
                self._implied_ack()
            self._last, self._run = None, 0
            return b""
        if self.must_resync:
            self.chirps += 1
            return bytes([self.address])             # still chirping
        kind = "general" if general else raw[1]
        if now is not None and self.min_gap:
            exempt = (raw == self._last_raw            # §3.2: a NACK
                      or (not general and self._after_priority))
            if self._polled_at is not None and not exempt \
                    and now - self._polled_at < self.min_gap - 1e-9:
                self.too_soon.append(raw)
                return b""                           # over-polled: ignored
            self._polled_at = now
        self._last_raw = raw
        answer = self._count(raw, general, kind)
        self._after_priority = (general and len(answer) == 1
                                and answer[0] in PRIORITY_EXCEPTIONS)
        return answer

    def _count(self, raw, general, kind):
        if kind == self._last:
            self._run += 1
            if self._run == 2:
                if not self.resends_on_nack and not general:
                    self._answer = self._long(raw)   # strays: a new poll
                return self._spoil(self._answer, general)   # implied NACK
            # the final NACK: no answer, and nothing is disposed of
            self._on_ack = None
            return b""
        self._implied_ack()          # a different poll to us acknowledges
        self._last, self._run = kind, 1
        self._answer = self._general() if general else self._long(raw)
        return self._spoil(self._answer, general)

    def _implied_ack(self):
        if self._on_ack is not None:
            done, self._on_ack = self._on_ack, None
            done()
            self.acks += 1

    def _spoil(self, answer, general):
        faults = self.general_faults if general else self.faults
        if not answer or not faults:
            return answer
        fault = faults.pop(0)
        if fault == DROP:
            return b""
        if fault == CUT:
            return answer[:len(answer) // 2]
        if fault == DAMAGE:
            return answer[:-1] + bytes([answer[-1] ^ 0xFF])
        return answer

    # -- general poll ----------------------------------------------------------

    def _general(self):
        if not self.exceptions:
            return b"\x00"
        code = self.exceptions[0]
        self._on_ack = lambda: self.exceptions.remove(code) \
            if code in self.exceptions else None
        return bytes([code])

    # -- long polls ------------------------------------------------------------

    def _long(self, raw):
        command = raw[1]
        if command in self.deaf:
            return b""
        if self.busy.get(command):
            self.busy[command] -= 1
            return bytes([self.address, 0x00])       # §4.1: busy
        if command == 0x1B and len(raw) == 2:
            return self._send_handpay_info()
        if command == 0x1A and len(raw) == 2:
            return frame(self.address, 0x1A, int_to_bcd(self.credits, 4))
        if command in (0x01, 0x02, 0x94, 0xA8):
            if sas_crc(raw[:-2]).to_bytes(2, "little") != raw[-2:]:
                return b""
            if self.nack.get(command):
                self.nack[command] -= 1
                return bytes([self.address | 0x80])  # Table 7.4b: NACK
            if command in (0x01, 0x02):
                return self._shutdown(command == 0x01)
            if command == 0xA8:
                return frame(self.address, 0xA8, bytes([self._method(raw[2])]))
            return self._remote_reset()
        if command == 0x3D and len(raw) == 2:
            # standard validation read; a machine in enhanced validation
            # answers with a zero validation field
            standard = not self.speaks_4d or self.answers_4d_with_zeros
            if not standard or not self.records:
                return frame(self.address, 0x3D, bytes(9))
            r = self.records[-1]
            return frame(self.address, 0x3D, int_to_bcd(int(r["vn"]), 4)
                         + int_to_bcd(r["amount"], 5))
        if len(raw) < 4 or sas_crc(raw[:-2]).to_bytes(2, "little") != raw[-2:]:
            return b""                               # bad CRC: ignored
        if command == 0x4D:
            return self._send_validation(raw[2])
        return b""                                   # not supported: ignored

    def _shutdown(self, off):
        self.shut_down = off
        if off and self.cashes_out_on_shutdown and self.credits:
            self.ticketed.append(self.credits)       # §7.4.1 allows this
            self.credits = 0
        return bytes([self.address])                 # Table 7.4b: ACK

    def _method(self, method):
        """§14.2, Table 14.2b."""
        if not self.in_handpay:
            return 0x02
        if method == 0x01 and not self.can_reset_to_credit:
            return 0x01
        self.reset_method = method
        if self.key_off_after_a8:
            self.key_off()
        return 0x00

    def _remote_reset(self):
        """§7.9, Table 7.9b."""
        if not self.remote_reset:
            return b""                               # "must ignore long poll 94"
        if not self.in_handpay:
            return frame(self.address, 0x94, bytes([0x02]))
        if self.reset_unable:
            return frame(self.address, 0x94, bytes([0x01]))
        self._reset_handpay(to_credit=self.reset_method == 0x01)
        return frame(self.address, 0x94, bytes([0x00]))

    def _record_bytes(self, r, index):
        return (bytes([r["type"], index])
                + bytes.fromhex("11162026") + bytes.fromhex("124055")
                + int_to_bcd(int(r["vn"]), 8) + int_to_bcd(r["amount"], 5)
                + (0xFFFF if r["ticket_number"] is None
                   else r["ticket_number"]).to_bytes(2, "little")
                + b"\x00" + bytes.fromhex("00000180") + b"\x00\x00")

    def _send_validation(self, function):
        if not self.speaks_4d:
            return b""
        if self.answers_4d_with_zeros:
            return frame(self.address, 0x4D, bytes(31))
        if function == 0xFF and not self.knows_look_ahead:
            return b""
        if function in (0x00, 0xFF):
            unread = self.unread()
            if not unread:
                return frame(self.address, 0x4D, bytes(31))
            r = unread[0]
            marks = function == 0x00 or self.peek_marks_read
            if marks:
                def mark(r=r):
                    r["read"] = True
                if self.mark_read_on == "send":
                    mark()
                else:
                    self._on_ack = mark
            return frame(self.address, 0x4D,
                         self._record_bytes(r, self.records.index(r) + 1))
        if 1 <= function <= len(self.records):
            return frame(self.address, 0x4D,
                         self._record_bytes(self.records[function - 1],
                                            function))
        return frame(self.address, 0x4D, bytes(31))

    def _send_handpay_info(self):
        waiting = [h for h in self.handpays if not h["reported"]]
        if not waiting:
            return frame(self.address, 0x1B, bytes(20))
        entry = waiting[0]

        def reported(entry=entry):
            entry["reported"] = True
            self._owe_52()
        self._on_ack = reported
        return frame(self.address, 0x1B, entry["data"])


# ---------------------------------------------------------------------------
# §8 — Advanced Funds Transfer (audit batch 2)
# ---------------------------------------------------------------------------

class ByTheBookAFTMachine(ByTheBookMachine):
    """The transfer cycle of SAS 6.02 §8.3, kept to the letter.

    A cycle begins with an initiating long poll 72 and ends only "when the
    host retrieves the final transfer and transaction receipt completion
    status codes" — read with 72/FF AND acknowledged. Until then this
    machine:
      * raises exception 69 when the transfer completes and again every
        fifteen seconds (a priority exception, not in the queue);
      * answers any other initiating 72 with the short C0 form, "and omit
        the remaining fields";
      * never enters the locked state (§8.2).
    Cancelling is "always at the discretion of the gaming machine": this
    one declines unless honours_cancel. With no current or previous
    transfer, an interrogation gets the short form: position 00, status FF,
    receipt FF.

    The transfer stays PENDING until the test calls complete(), or until
    `complete_after` general polls have come in after a declined cancel —
    which is exactly when our host has given up on it.

    Host cashout, §8.7 (audit batch 5). The host cashout status byte of the
    0x74 answer (and the flags byte of every 0x72 answer) says whether the
    host may control the enable (`cashout_controllable`), whether cashout
    to host is enabled now (`cashout_enabled`) and whether the mode is hard
    (`cashout_hard`). While enabled:
      * press_cashout() makes a cashout from the credit meter pending and
        raises exception 6A; win(amount) makes a win pending and raises 6B.
        Both are priority exceptions, raised again every 800 ms
        (eight_hundred_ms_pass) for as long as the cashout is pending.
      * the host answers with a 0x72 of the matching type (80 for 6A, 90
        for 6B). Amounts of all 9s take the whole cashout; amounts of zero
        tell the machine to cash out by other means, which it does at once
        (a ticket, or the win paid to the credit meter).
      * any other kind of transfer meanwhile is answered 87 and the
        pending cashout is not aborted.
      * with no answer in eight seconds (eight_seconds_pass) the machine
        runs its failure recovery: soft mode cashes out the other way,
        hard mode tilts and waits for an attendant.
    While NOT enabled, press_cashout() prints a ticket straight away and no
    6A is ever raised.
    A successful transfer whose flags have bit 0 set changes the enable to
    bit 1 and the mode to bit 2, if the machine lets the host control it.
    A transfer with a total of zero is accepted for that purpose alone."""

    def __init__(self, credits=0, honours_cancel=False, complete_after=None,
                 hears_initiate=True, restricted=0, complete_in=None,
                 cashout_controllable=False, cashout_enabled=False,
                 cashout_hard=False, **kw):
        super().__init__(**kw)
        self.cashout_controllable = cashout_controllable
        self.cashout_enabled = cashout_enabled
        self.cashout_hard = cashout_hard
        self.cashout_pending = None  # {"kind": "credits"|"win", "amount"}
        self.ticketed = []           # amounts cashed out to a ticket
        self.wins_to_meter = []      # wins paid to the credit meter instead
        self.tilted = False          # hard mode: cashout-to-host failure
        self.declined = 0            # zero-amount answers to 6A/6B
        self.refused_87 = 0          # other transfers tried meanwhile
        self._6x_due = False
        self.asset = bytes([0x92, 0x10, 0x00, 0x00])
        self.key = bytes(range(20))
        self.pos = b"\x01\x00\x00\x00"
        self.credits = credits               # cashable
        self.restricted = restricted         # restricted promotional
        # complete the transfer this many general polls after it starts,
        # i.e. inside the host's normal wait (None: only when told to)
        self.complete_in = complete_in
        self.honours_cancel = honours_cancel
        self.complete_after = complete_after
        self.hears_initiate = hears_initiate    # False: the 0x72 is lost
        self.locked = False
        self.cycle = None            # the open transfer cycle
        self.last = None             # the most recent closed one
        self.started = []            # txn ids that began a cycle
        self.refused_while_open = 0  # initiating 72s answered C0
        self.locks_refused = 0
        self.cancels_declined = 0
        self._countdown = None
        self._69_due = False

    # -- what happens AT the machine ----------------------------------------

    def complete(self):
        """The pending transfer happens: credits move, exception 69."""
        c = self.cycle
        if c is None or c["status"] != 0x40:
            return
        if c["type"] == 0x90:
            pass                                    # a win: never on the meter
        else:
            sign = -1 if c["type"] == 0x80 else 1   # 80: machine -> host
            self.credits += sign * c["cents"]
            self.restricted += sign * c.get("restricted", 0)
        if c.get("answers") is not None:            # it answered a 6A/6B
            self.cashout_pending = None
            self._6x_due = False
            self.tilted = False
        self._apply_enable(c.get("flags", 0))
        c["status"] = 0x00
        self._69_due = True

    def _apply_enable(self, flags):
        """§8.7: the requested enable takes effect "only when a transfer has
        successfully completed", and the machine may override it."""
        if flags & 0x01 and self.cashout_controllable:
            self.cashout_enabled = bool(flags & 0x02)
            self.cashout_hard = bool(flags & 0x04)

    def fifteen_seconds_pass(self):
        super().fifteen_seconds_pass()
        if self.cycle is not None and self.cycle["status"] != 0x40:
            self._69_due = True

    # -- host cashout (§8.7) --------------------------------------------------

    def host_cashout_status(self):
        return ((0x01 if self.cashout_controllable else 0)
                | (0x02 if self.cashout_enabled else 0)
                | (0x04 if self.cashout_enabled and self.cashout_hard else 0))

    def press_cashout(self):
        """The player presses CASH OUT with credits on the meter."""
        if self.credits + self.restricted <= 0:
            return
        if self.cashout_enabled:
            self.cashout_pending = {"kind": "credits"}
            self._6x_due = True
        else:
            self._other_means({"kind": "credits"})

    def win(self, amount):
        """A win that has to be cashed out rather than paid to the meter."""
        if self.cashout_enabled:
            self.cashout_pending = {"kind": "win", "amount": amount}
            self._6x_due = True
        else:
            self._other_means({"kind": "win", "amount": amount})

    def eight_hundred_ms_pass(self):
        if self.cashout_pending is not None and self.cycle is None:
            self._6x_due = True

    def eight_seconds_pass(self):
        """No transfer and no decline from the host: failure recovery."""
        if self.cashout_pending is None or self.cycle is not None:
            return
        if self.cashout_hard:
            self.tilted = True               # waits for an attendant
        else:
            self._other_means(self.cashout_pending)

    def _other_means(self, pending):
        if pending["kind"] == "credits":
            self.ticketed.append(self.credits + self.restricted)
            self.credits = self.restricted = 0
        else:
            self.wins_to_meter.append(pending["amount"])
            self.credits += pending["amount"]
        self.cashout_pending = None
        self._6x_due = False

    def _close_cycle(self):
        self.last, self.cycle = self.cycle, None
        self._69_due = False
        self.locked = False

    # -- the wire --------------------------------------------------------------

    def _general(self):
        if self._countdown is not None:
            self._countdown -= 1
            if self._countdown <= 0:
                self._countdown = None
                self.complete()
        if self._69_due:
            def delivered():
                self._69_due = False
            self._on_ack = delivered
            return b"\x69"
        if self._6x_due and self.cashout_pending is not None:
            def asked():
                self._6x_due = False         # until the 800 ms timer
            self._on_ack = asked
            return (b"\x6a" if self.cashout_pending["kind"] == "credits"
                    else b"\x6b")
        return super()._general()

    def _framed(self, command, body):
        return frame(self.address, command, bytes([len(body)]) + bytes(body))

    def _long(self, raw):
        command = raw[1]
        if command not in (0x72, 0x73, 0x74):
            return super()._long(raw)
        if len(raw) < 4 or sas_crc(raw[:-2]).to_bytes(2, "little") != raw[-2:]:
            return b""
        if command == 0x73:
            return self._framed(0x73, bytes([0x01]) + self.asset + self.key
                                + self.pos)
        if command == 0x74:
            return self._lock(raw[2])
        return self._transfer(raw[3:-2])

    def _lock(self, code):
        if code == 0x00:                            # request
            if self.cycle is not None:
                self.locks_refused += 1
                self.locked = False
            else:
                self.locked = True
        elif code == 0x80:                          # cancel
            self.locked = False
        status = 0x00 if self.locked else 0xFF
        avail = 0x03 | (0x08 if self.cashout_pending is not None
                        and self.cashout_pending["kind"] == "win" else 0)
        return self._framed(0x74, self.asset
                            + bytes([status, avail,
                                     self.host_cashout_status(), 0xFF, 0x0A])
                            + int_to_bcd(self.credits, 5)
                            + int_to_bcd(self.restricted, 5) + bytes(5)
                            + int_to_bcd(1000000, 5))

    def _status(self, t):
        txn = t["txn"].encode("ascii")
        live = t["status"] in (0x00, 0x40)
        return self._framed(0x72, bytes([0x00, t["status"], 0xFF, t["type"]])
                            + int_to_bcd(t["cents"] if live else 0, 5)
                            + int_to_bcd(t.get("restricted", 0)
                                         if live else 0, 5)
                            + bytes(5) + bytes([self.host_cashout_status()])
                            + self.asset + bytes([len(txn)]) + txn)

    def _report(self, t):
        """Answer with a transfer's status; a FINAL status of the open
        cycle ends that cycle once the host acknowledges this answer."""
        if t is self.cycle and t["status"] != 0x40:
            self._on_ack = self._close_cycle
        return self._status(t)

    def _transfer(self, d):
        code = d[0]
        current = self.cycle or self.last
        if code == 0xFF:                            # interrogate
            if current is None:
                return self._framed(0x72, bytes([0x00, 0xFF, 0xFF]))
            return self._report(current)
        if code == 0x80:                            # cancel
            if current is None:
                return self._framed(0x72, bytes([0x00, 0xFF, 0xFF]))
            if self.cycle is not None and self.cycle["status"] == 0x40:
                if self.honours_cancel:
                    self.cycle["status"] = 0x80
                else:
                    self.cancels_declined += 1
                    self._countdown = self.complete_after
            return self._report(current)
        # an initiating poll
        if not self.hears_initiate:
            return b""
        if self.cycle is not None:
            self.refused_while_open += 1
            return self._framed(0x72, bytes([0x00, 0xC0]))
        txn_len = d[43]
        ALL = 9999999999
        cash, restr, nonr = (int(d[3:8].hex()), int(d[8:13].hex()),
                             int(d[13:18].hex()))
        self.cycle = {"txn": d[44:44 + txn_len].decode("ascii"),
                      "type": d[2], "cents": cash, "restricted": restr,
                      "status": 0x40, "flags": d[18]}
        self.started.append(self.cycle["txn"])
        pending = self.cashout_pending
        if pending is not None:
            wanted = 0x80 if pending["kind"] == "credits" else 0x90
            if d[2] != wanted:
                # §8.7: "the gaming machine will respond with status code
                # 87 ... This must not cause the cashout in progress to be
                # aborted."
                self.refused_87 += 1
                return self._final(0x87)
            if cash + restr + nonr == 0:
                self.declined += 1           # cash out by other means
                self._other_means(pending)
                self.tilted = False
                return self._final(0x00)
            if pending["kind"] == "credits":
                take_c = self.credits if cash == ALL else cash
                take_r = self.restricted if restr == ALL else restr
                if take_c > self.credits or take_r > self.restricted:
                    return self._final(0x84)
            else:
                take_c = pending["amount"] if cash == ALL else cash
                take_r = 0
                if take_c > pending["amount"]:
                    return self._final(0x84)
            self.cycle.update(cents=take_c, restricted=take_r,
                              answers=pending["kind"])
        elif d[2] == 0x90:
            return self._final(0x8B)         # no won credits available
        elif cash + restr + nonr == 0:
            # §8.7: a transfer "for the sole purpose of setting the Host
            # Cashout Enable state"
            self._apply_enable(d[18])
            return self._final(0x00)
        elif ALL in (cash, restr, nonr):
            return self._final(0x83)         # not a valid amount
        if self.complete_in is not None:
            self._countdown = self.complete_in
        return self._status(self.cycle)

    def _final(self, status):
        """The transfer just asked for ends at once with this status."""
        c = self.cycle
        c["status"] = status
        if status != 0x00:
            c["cents"] = c["restricted"] = 0
        return self._report(c)
