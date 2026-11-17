"""
Implied-ACK guard: acknowledge a long poll's answer before sending the same
long poll command again (SAS 6.02 §3.1-3.2).

SAS has no ACK byte. A machine learns that its answer arrived from what the
host sends NEXT (§3.1, Table 3.1):

    answer to a long poll is ACKed by   a general poll, or a long poll with a
                                        DIFFERENT command byte, to the same
                                        machine; or any poll to another address

The same long poll command again says the opposite — "I did not get that,
send it again" (§3.2). A third in a row is the final NACK, which the machine
"may respond to". The spec counts by COMMAND BYTE, not by what the poll asks
for: that is why it defines 0xAF as a second code for the 0x6F meter poll,
"to allow a host to perform consecutive meter polls and still provide a
proper implied acknowledgement" (§7.21).

Why this exists (2026-11-16, a tester's Ainsworth A560 on a Pi 4): AFT
registration is 73/FF (read), 73/00 (initialize), 73/01 (register). Where the
machine echoes its asset number in the 73/FF answer, nothing goes out between
them, so the register poll is the THIRD 0x73 in a row. The A560 answered the
first two and never the third, on every attempt ("silence on register
(73/01)"), while it sat at registration status 00 with our key stored — the
initialize had landed. That is exactly what §3.2 allows. A WMS BB2 answers
all three, and usually gets a 0x7B asset read in between anyway, which is
why it had not shown. Not yet confirmed on the A560 itself.

The fix belongs under every caller, not inside the registration code: the
lock wait repeats 0x74, the transfer path repeats 0x72, the ticket and
census reads repeat their own. So this wraps the transport. Before a long
poll goes out, if the last thing sent was the same command to the same
machine AND its answer arrived intact, an address-0 poll goes out first —
the same one the poller already uses to ACK an exception (nothing answers
address 0, so it costs one byte and no wait).

A repeat is left alone when the previous answer did NOT arrive (nothing,
or a bad CRC). Then the repeat really is the implied NACK the spec
describes and the machine should send its answer again; ACKing there would
tell it to throw away something we never received. A busy response and a
NACK byte DID arrive, whole, and the poll that follows them is a new
attempt: those are ACKed first (poll_answered, v0.3.15).

Since 2026-11-17 (audit batch 4) the same guard keeps the rest of the
host-side link rules, for the same reason — they belong under every caller:

  GENERAL POLLS (§3.1-3.2, item 4). The rule above is not only for long
  polls. While a machine was idle we sent it the same general poll every
  200 ms with nothing between, about twenty in a row until the next meter
  sweep. The document calls the second a NACK and the third the final NACK,
  after which the machine "may respond ... but must not dispose of the
  volatile information". A general poll that was answered is now ACKed
  (the address-0 poll) before the next general poll to that machine; one
  that was NOT answered is repeated bare, which is the NACK that makes the
  machine send again.

  SYNC AFTER SILENCE (§3.3, item 27). A machine that hears no address byte
  for five seconds ignores polls to itself until another address is
  polled. When this host has sent nothing for SYNC_AFTER_SEC, the next
  frame to a machine is led by the address-0 poll.

  POLLING RATE (§2.3.3, item 8). "The host may not issue general polls or
  long polls to any single gaming machine at a rate faster than once per
  200 ms", not counting the machine's response time. With `poll_gap` set,
  a poll to a machine waits until that long after the end of the last
  exchange with it. Two cases need not wait, and do not: the same poll
  again after an answer that did not arrive correctly (§3.2), and a long
  poll that is the next poll after a priority exception (§2.2.1). A
  machine that says so in long poll A0 may be polled every 40 ms.
"""

import time

from .sas_protocol import sas_crc

# General poll to address 0. No machine has address 0, so nothing answers;
# every machine on the line takes it as a poll to another address.
ACK_POLL = bytes([0x80])


def long_poll_key(frame: bytes):
    """(address, command) when `frame` is a long poll to one machine, else
    None — a general poll (wake-up byte alone, bit 7 set), a broadcast to
    address 0, or too short to be either."""
    if len(frame) < 2 or frame[0] & 0x80 or frame[0] == 0:
        return None
    return frame[0], frame[1]


def poll_key(frame: bytes):
    """(address, command) for a long poll to one machine, (address, None)
    for a general poll to one machine, else None (address 0, or nothing)."""
    if len(frame) == 1 and frame[0] & 0x80 and frame[0] != 0x80:
        return frame[0] & 0x7F, None
    return long_poll_key(frame)


# §2.2.1: the priority exceptions. "The host does not need to wait for the
# specified polling rate time period to respond to a priority exception
# with a long poll."
PRIORITY_EXCEPTIONS = frozenset({
    0x57, 0x67, 0x68, 0x3F, 0x6A, 0x6B, 0x6F, 0x56, 0x3D, 0x3E, 0x69, 0x6C,
    0x6D, 0x51, 0x52, 0x8F, 0x70})

# §3.3 gives the machine five seconds; lead with the sync poll a little
# before that so a host that has been quiet for 4.9 s is not caught out.
SYNC_AFTER_SEC = 4.0


def answer_intact(resp: bytes, address: int, command) -> bool:
    """True when `resp` is a complete answer to that poll.

    Long poll: the lone address byte a type S poll is ACKed with, or a
    frame that echoes the address and command and passes its CRC. A NACK
    (address | 0x80), a busy ([address][00]), silence and anything damaged
    are all False.

    General poll (command None): any single byte (an exception code has no
    check on it), or a long-poll result delivered on a general poll (the
    ROM signature), which does have a CRC."""
    if not resp:
        return False
    if command is None:
        if len(resp) == 1:
            return True
        return (len(resp) >= 4 and resp[0] == address
                and sas_crc(resp[:-2]) == int.from_bytes(resp[-2:], "little"))
    if len(resp) == 1:
        return resp[0] == address
    if len(resp) < 4 or resp[0] != address or resp[1] != command:
        return False
    return sas_crc(resp[:-2]) == int.from_bytes(resp[-2:], "little")


def is_busy(resp: bytes, address: int) -> bool:
    """The gaming machine busy response of §4.1: the address followed by a
    00 command code. "Upon receiving a gaming machine busy response, the
    host aborts the long poll attempt and reinserts the long poll into its
    transmit queue for transmission at a later time." """
    return len(resp or b"") == 2 and resp[0] == address and resp[1] == 0x00


def poll_answered(resp: bytes, address: int, command) -> bool:
    """True when the machine gave a COMPLETE answer of any kind, so that
    the same poll sent again is a new attempt and not a request to repeat:
    an intact answer (answer_intact), the busy response, or the NACK byte
    of Table 7.4b (address | 0x80).

    Busy and NACK carry nothing the machine has to keep, and both arrived
    whole. The host's next move is to send the poll AGAIN, later (§4.1) or
    at once (a NACK means the poll did not validate). Sent with nothing
    between, that second poll is by §3.2 an implied NACK: a machine that
    counts re-sends its busy or its NACK without looking at the poll, and
    ignores the third. So these are acknowledged before the retry like any
    other answer. Until v0.3.15 they were not, and a retry after busy
    could not succeed against a machine that keeps the count."""
    if answer_intact(resp, address, command):
        return True
    if command is None or not resp:
        return False
    return is_busy(resp, address) or (len(resp) == 1
                                      and resp[0] == (address | 0x80))


NACK_SENDS = 3    # §3.2: the poll, the implied NACK, and the final NACK


def transact_reread(transport, frame: bytes, sends: int = NACK_SENDS):
    """Send a long poll and, while its answer does not arrive correctly,
    send the IDENTICAL poll again at once. Returns (resp, heard, sent):
    the answer taken (the last one when none was intact), whether any byte
    at all came back on any send, and how many times the poll went out.

    Why (2026-11-16, the SAS 6.02 audit, item 1). §3.2: "If the host does
    not receive the gaming machine's response correctly, it repeats the
    general or long poll" — that repeat is the implied NACK, and it need
    not wait for the polling rate. Our callers did not repeat. They sent a
    DIFFERENT poll next, which Table 3.1 defines as the implied ACK, so a
    machine that had answered with a record it deletes on ACK (the 0x4D
    ticket record, §15.10; the 0x1B handpay record, §7.8.1) threw away the
    one copy of something we never received.

    "Correctly" is answer_intact — the same test ImpliedAckTransport uses
    to decide an answer needs an ACK — so the two halves of §3 can never
    disagree: an answer is either acknowledged or asked for again. A
    well-formed answer the caller cannot USE (a length it does not know) is
    not asked for again; it would be the same the second time.

    After the third send the document has the machine keep what it was
    trying to deliver, so a caller that gets nothing usable simply stops;
    the machine raises its exception again."""
    key = long_poll_key(frame)
    resp, heard = b"", False
    for sent in range(1, max(1, sends) + 1):
        resp = transport.transact(frame)
        heard = heard or bool(resp)
        # A busy machine is not asked again here (§4.1: the host "aborts
        # the long poll attempt" and comes back later); that is the
        # caller's "later".
        if key is None or answer_intact(resp, *key) \
                or is_busy(resp, key[0]):
            break
    return resp, heard, sent


class ImpliedAckTransport:
    """A SAS transport that keeps the host's side of the link rules: it
    never repeats an answered poll un-ACKed, leads with a sync poll after a
    silence, and (with `poll_gap`) holds each machine to its polling rate.
    See the module docstring.

    Wraps any transport with transact() (and, when it has one, send_frame()).
    Everything else — close(), listen(), a mock's sent_frames — passes
    straight through to the wrapped transport.

    `poll_gap` is seconds between polls to one machine; 0 (the default)
    paces nothing, which is what tests on a pretend machine want. It may be
    changed at any time (sas_host lowers it to 40 ms when the machine
    reports that rate)."""

    def __init__(self, inner, poll_gap: float = 0.0,
                 sync_after: float = SYNC_AFTER_SEC,
                 clock=time.monotonic, sleep=time.sleep):
        self._inner = inner
        self.poll_gap = poll_gap
        self.sync_after = sync_after
        self._clock = clock
        self._sleep = sleep
        # the poll whose intact answer is still waiting for its ACK
        self._answered = None
        self.acks_sent = 0
        # the poll sent last, and how many times in a row it has gone out
        # with no intact answer and nothing else in between
        self._run_key = None
        self._run = 0
        self.resyncs_sent = 0
        # when anything was last sent (None: nothing yet — the poller sends
        # the first sync poll itself)
        self._last_tx = None
        self.syncs_sent = 0
        # per machine: when the last exchange with it ended
        self._last_end = {}
        self.waited = 0.0
        # the last frame sent to a machine, and whether its answer arrived
        # intact — the same frame again after a bad answer is a NACK
        self._last_frame = None
        self._last_intact = True
        # the machine whose last general poll brought a priority exception
        self._priority_from = None

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def _send_only(self, frame: bytes) -> None:
        send = getattr(self._inner, "send_frame", None)
        if send is None:
            # a transport with transact() alone (bench and replay shims)
            self._inner.transact(frame)
        else:
            send(frame)

    def _before(self, frame: bytes):
        """ACK first when `frame` repeats the poll just answered.
        Whatever goes out next settles the question either way, so the
        marker is always cleared.

        The same address-0 poll also goes out before a FOURTH identical
        long poll in a row. Three is all §3.2 defines — the poll, the NACK,
        the final NACK — and a machine counts by command byte, so a caller
        that gives up on one read and starts another with the same command
        (0x4D look-ahead, then the 0x4D read) would otherwise be talking to
        a machine that has stopped answering that command. After a final
        NACK the machine keeps what it was trying to deliver, so this
        acknowledges nothing; it restarts the count (§3.3)."""
        key = poll_key(frame)
        if key is not None:
            self._hold_to_rate(frame, key)
        now = self._clock()
        if key is not None and self._last_tx is not None \
                and now - self._last_tx >= self.sync_after:
            # §3.3: after this long with nothing sent, the machine may be
            # ignoring its own address. A lost sync "must be considered an
            # implied NACK", so this acknowledges nothing.
            self._send_only(ACK_POLL)
            self.syncs_sent += 1
            self._run = 0
        elif key is not None and key == self._answered:
            self._send_only(ACK_POLL)
            self.acks_sent += 1
            self._run = 0
        elif key is not None and key == self._run_key \
                and self._run >= NACK_SENDS:
            self._send_only(ACK_POLL)
            self.resyncs_sent += 1
            self._run = 0
        if key is not None and key == self._run_key:
            self._run += 1
        else:
            self._run_key, self._run = key, (0 if key is None else 1)
        self._answered = None
        self._last_tx = self._clock()
        return key

    def _hold_to_rate(self, frame: bytes, key) -> None:
        """Wait out what is left of the polling rate for this machine
        (§2.3.3), unless this poll is one of the two that need not wait."""
        if self.poll_gap <= 0:
            return
        address = key[0]
        ended = self._last_end.get(address)
        if ended is None:
            return
        if frame == self._last_frame and not self._last_intact:
            return                      # §3.2: the NACK need not wait
        if key[1] is not None and self._priority_from == address:
            return                      # §2.2.1: answering a priority exception
        wait = ended + self.poll_gap - self._clock()
        if wait > 0:
            self.waited += wait
            self._sleep(wait)

    def _after(self, frame: bytes, key, resp) -> None:
        if key is None:
            return
        address, command = key
        self._last_end[address] = self._clock()
        self._last_frame = bytes(frame)
        self._last_intact = resp is not None and poll_answered(resp, *key)
        if command is None and resp and len(resp) == 1 \
                and resp[0] in PRIORITY_EXCEPTIONS:
            self._priority_from = address
        else:
            self._priority_from = None

    def send_frame(self, frame: bytes) -> None:
        key = self._before(frame)
        self._send_only(frame)
        self._after(frame, key, None)

    def transact(self, frame: bytes, *args, **kwargs) -> bytes:
        key = self._before(frame)
        resp = self._inner.transact(frame, *args, **kwargs)
        if key is not None and poll_answered(resp, *key):
            self._answered = key
        self._after(frame, key, resp)
        return resp
