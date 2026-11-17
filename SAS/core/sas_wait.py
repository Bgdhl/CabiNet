"""
Waiting on general polls for one exception, the same way everywhere.

Three flows wait for the machine to say it has finished something: a
handpay reset (exception 52), a ticket redemption (68) and an AFT transfer
(69). Each had its own copy of the same loop, and the copies had drifted
from the poller they sit beside (SAS 6.02 audit, items 4 and 25):

  * they skipped only exception 00. Exception 1F, "no activity and waiting
    for player", was handed up as an event; §13.2 asks hosts to treat it as
    00, and a Bluebird 2 sends it all the time while idle. So was a lone
    byte equal to our own address, which is the loop-break chirp of §4.2
    and not an answer at all.
  * they acknowledged only the exceptions they handed up, so an idle wait
    was the same general poll up to ten times in a row. That part is now
    kept by the transport (core/sas_implied_ack.py): an answered general
    poll is acknowledged before the next one, whoever sends it.

This module is the one classifier and the one loop.
"""

from .sas_exceptions import APPENDIX_A_CODES

# Idle general-poll answers (§13.2): 00 = no activity, 1F = no activity and
# waiting for player. Both mean "nothing to report" — neither is an event.
IDLE_EXCEPTIONS = frozenset({0x00, 0x1F})


def is_chirp(resp: bytes, address: int) -> bool:
    """Is this the machine's loop-break chirp rather than an answer?

    §4.2: a machine that has not received any address byte for five seconds
    transmits its OWN ADDRESS every 200 ms until it hears one. One byte
    equal to our address is a chirp unless that value is also an exception
    code in Table A-1 (the receive side cannot see the wakeup bit that
    marks a chirp, so at those addresses, 0x11 and up, it stays an
    exception). Two or more bytes that are ALL our address are always a
    chirp: no exception is longer than one byte and no long-poll result
    looks like that."""
    if not resp or any(b != address for b in resp):
        return False
    return len(resp) > 1 or address not in APPENDIX_A_CODES


def await_exception(transport, address, proto, wanted, pace, sleep, polls,
                    on_exception=None, on_frame=None) -> bool:
    """General-poll the machine up to `polls` times, `pace` seconds apart,
    until it reports one of the exception codes in `wanted`. True when it
    did.

    Any other exception that comes off the queue meanwhile goes to
    `on_exception(code)`; a long-poll result delivered on a general poll
    (the ROM signature) goes to `on_frame(resp)`. Silence, no-activity
    answers and chirps are waited through.

    Every exception taken here is acknowledged with an address-0 poll at
    once, so the machine does not send it again to whoever polls next."""
    for _ in range(polls):
        sleep(pace)
        resp = transport.transact(proto.build_general_poll(address))
        if not resp or is_chirp(resp, address):
            continue
        if len(resp) > 1:
            if on_frame is not None:
                on_frame(resp)
            continue
        code = resp[0]
        if code in IDLE_EXCEPTIONS:
            continue
        transport.transact(proto.build_general_poll(0))   # implied ACK
        if code in wanted:
            return True
        if on_exception is not None:
            on_exception(code)
    return False
