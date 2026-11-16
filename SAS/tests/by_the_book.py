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

`counts_idle_general_polls=False` relaxes ONE rule, for tests that run the
poller's idle loop: the poller still sends the same general poll over and
over with no acknowledgement between (audit item 4, not fixed yet), which
this machine would answer twice and then ignore. Relaxed, a repeated
general poll re-sends an exception that is waiting for its ACK and
otherwise counts as a new poll. Remove the switch when item 4 is fixed.
StrictMachine in test_sas_implied_ack.py is the older, narrower relative:
it models what a tester's Ainsworth A560 was seen to do with three 0x73
polls, which is not quite the document either.
"""

from core.sas_meters import int_to_bcd
from core.sas_protocol import sas_crc

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
                 counts_idle_general_polls=True, resends_on_nack=True):
        self.address = address
        self.counts_idle_general_polls = counts_idle_general_polls
        self.resends_on_nack = resends_on_nack      # True per §3.2
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
        if 0x51 not in self.exceptions:
            self.exceptions.append(0x51)

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
        if polled != self.address:
            # another machine's poll, or address 0: Table 3.1's "poll to a
            # different gaming machine address" / global broadcast
            self._implied_ack()
            self._last, self._run = None, 0
            return b""
        kind = "general" if general else raw[1]
        if general and kind == self._last \
                and not self.counts_idle_general_polls:
            if self._on_ack is None:                 # nothing waiting: new poll
                self._answer = self._general()
            return self._answer
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
        if general or not answer or not self.faults:
            return answer
        fault = self.faults.pop(0)
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
        if command == 0x1B and len(raw) == 2:
            return self._send_handpay_info()
        if command == 0x3D and len(raw) == 2:
            # standard validation read; a machine in enhanced validation
            # answers with a zero validation field
            if self.speaks_4d or not self.records:
                return frame(self.address, 0x3D, bytes(9))
            r = self.records[-1]
            return frame(self.address, 0x3D, int_to_bcd(int(r["vn"]), 4)
                         + int_to_bcd(r["amount"], 5))
        if len(raw) < 4 or sas_crc(raw[:-2]).to_bytes(2, "little") != raw[-2:]:
            return b""                               # bad CRC: ignored
        if command == 0x4D:
            return self._send_validation(raw[2])
        return b""                                   # not supported: ignored

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
        self._on_ack = reported
        return frame(self.address, 0x1B, entry["data"])
