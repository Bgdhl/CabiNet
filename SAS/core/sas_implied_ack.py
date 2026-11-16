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

A repeat is left alone when the previous answer did NOT arrive intact
(nothing, a bad CRC, a NACK, a busy). Then the repeat really is the implied
NACK the spec describes and the machine should send its answer again;
ACKing there would tell it to throw away something we never received.
"""

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


def answer_intact(resp: bytes, address: int, command: int) -> bool:
    """True when `resp` is a complete answer to that long poll: the lone
    address byte a type S poll is ACKed with, or a frame that echoes the
    address and command and passes its CRC. A NACK (address | 0x80), a busy
    ([address][00]), silence and anything damaged are all False."""
    if not resp:
        return False
    if len(resp) == 1:
        return resp[0] == address
    if len(resp) < 4 or resp[0] != address or resp[1] != command:
        return False
    return sas_crc(resp[:-2]) == int.from_bytes(resp[-2:], "little")


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
        if key is None or answer_intact(resp, *key):
            break
    return resp, heard, sent


class ImpliedAckTransport:
    """A SAS transport that never repeats an answered long poll un-ACKed.

    Wraps any transport with transact() (and, when it has one, send_frame()).
    Everything else — close(), listen(), a mock's sent_frames — passes
    straight through to the wrapped transport."""

    def __init__(self, inner):
        self._inner = inner
        # the long poll whose intact answer is still waiting for its ACK
        self._answered = None
        self.acks_sent = 0
        # the long poll sent last, and how many times in a row it has gone
        # out with no intact answer and nothing else in between
        self._run_key = None
        self._run = 0
        self.resyncs_sent = 0

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
        """ACK first when `frame` repeats the long poll just answered.
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
        key = long_poll_key(frame)
        if key is not None and key == self._answered:
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
        return key

    def send_frame(self, frame: bytes) -> None:
        self._before(frame)
        self._send_only(frame)

    def transact(self, frame: bytes, *args, **kwargs) -> bytes:
        key = self._before(frame)
        resp = self._inner.transact(frame, *args, **kwargs)
        if key is not None and answer_intact(resp, *key):
            self._answered = key
        return resp
