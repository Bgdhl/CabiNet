"""
SAS two-step handpay reset-to-credits.

SAS 6.02 sections 14.2 (long poll A8), 7.9 (long poll 94) and 7.8.1 (the
handpay queue and exceptions 51 and 52).

* THE TWO-STEP ORDER (AJ, 2026-07-01, from his own bench experience with
  real SAS machines: "SAS can reset to credit but it needs to send the byte
  prior to reset"). Step 1 enables the reset method (credit meter); step 2
  issues the reset. §7.9 says the same: "If the W2-G Reset To Credit Meter
  function has been enabled using long poll A8, long poll 94 will reset the
  handpay to the credit meter", and §14.2 that A8 "affects only the pending
  handpay". This module structurally enforces the order: step 2 is
  unreachable unless step 1 was answered with code 00.

* THE FRAMES (both type S, CRC-bearing even without data):

  step 1  [addr][A8][method][crc-lo][crc-hi]   enable a reset method
          method 00 = standard handpay, 01 = reset to the credit meter
          answer (Table 14.2b): [addr][A8][code][crc]
            00 = reset method enabled
            01 = unable to enable reset method
            02 = not currently in a handpay condition
          "If SAS is not enabled to control the jackpot handpay reset
          method, long poll A8 will be ignored."

  step 2  [addr][94][crc-lo][crc-hi]            remote handpay reset
          answer (Table 7.9b): [addr][94][code][crc]
            00 = handpay was reset
            01 = unable to reset the handpay
            02 = not currently in a handpay condition
          "If a gaming machine is not configured for remote handpay reset,
          it must ignore long poll 94."

  Both answers are framed, with a code byte and a CRC. A lone address byte
  is not an answer to either; it is what a loop-break chirp looks like
  (§4.2). Until v0.3.15 one was taken for "acknowledged" at both steps, so
  a machine that could not hear us "consented" to the reset.

  after   exception 52 "handpay was reset" follows, but §7.8.1 holds it
          back "until the corresponding 1B long poll has been received,
          responded to and acknowledged". So code 00 from 94 is the result;
          the 52 is watched for and reported, and a 51 that arrives in the
          watch is answered with 1B by the poller so the 52 can come.
          Until v0.3.15 a reset the machine had confirmed was reported as a
          timeout whenever 52 took longer than about a second.

Both polls were first used here on AJ's word before the document was to
hand; a WMS Bluebird 2 has since answered them as above ("reset acked and
confirmed by exception 0x52").

No automatic retries by design: this is an operator-triggered action (fob
tap / bridge command), a silent machine yields a typed TIMEOUT and the
caller may simply re-invoke (re-selecting the method is idempotent). Every
poll is paced at SAS_POLL_FLOOR — the 200 ms per-machine floor.
"""

import enum
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .sas_protocol import SASProtocol
from .sas_meters import SAS_POLL_FLOOR
from .sas_wait import await_exception

__all__ = [
    "CMD_SET_HANDPAY_RESET_METHOD", "CMD_REMOTE_HANDPAY_RESET",
    "RESET_METHOD_STANDARD", "RESET_METHOD_CREDIT_METER",
    "METHOD_ACK_ENABLED", "METHOD_ACK_UNABLE", "METHOD_ACK_NO_HANDPAY",
    "RESET_CODE_OK", "RESET_CODE_UNABLE", "RESET_CODE_NO_HANDPAY",
    "HANDPAY_PENDING_EXCEPTION", "HANDPAY_RESET_EXCEPTION",
    "CONFIRM_EXCEPTIONS",
    "HandpayResetOutcome", "HandpayResetResult",
    "build_reset_method_poll", "build_handpay_reset_poll",
    "reset_handpay_to_credits",
]

# --------------------------------------------------------------------------
# Constants — SAS 6.02 Tables 14.2a/b and 7.9a/b, Appendix A
# --------------------------------------------------------------------------

CMD_SET_HANDPAY_RESET_METHOD = 0xA8   # enable jackpot handpay reset method
CMD_REMOTE_HANDPAY_RESET = 0x94       # reset handpay

RESET_METHOD_STANDARD = 0x00          # attendant/standard handpay
RESET_METHOD_CREDIT_METER = 0x01      # pay the lockup to the credit meter

# 0xA8 answer codes (Table 14.2b)
METHOD_ACK_ENABLED = 0x00
METHOD_ACK_UNABLE = 0x01
METHOD_ACK_NO_HANDPAY = 0x02

# 0x94 answer codes (Table 7.9b)
RESET_CODE_OK = 0x00                  # handpay was reset
RESET_CODE_UNABLE = 0x01              # unable to reset the handpay
RESET_CODE_NO_HANDPAY = 0x02          # not currently in a handpay condition

# General-poll exceptions (Appendix A). Named in sas_exceptions'
# BENCH_EXCEPTIONS; kept here as constants for the flows that key on them.
HANDPAY_PENDING_EXCEPTION = 0x51
HANDPAY_RESET_EXCEPTION = 0x52
CONFIRM_EXCEPTIONS = frozenset({HANDPAY_RESET_EXCEPTION})

_PROTO = SASProtocol()                # stateless; safe to share


class HandpayResetOutcome(enum.Enum):
    """Typed result of a reset-to-credits attempt."""
    RESET_OK = "reset_ok"             # 94 answered 00: handpay was reset
    METHOD_REFUSED = "method_refused"  # machine refused step 1 (or step 2)
    NO_HANDPAY = "no_handpay"         # machine says it is not in a handpay
    TIMEOUT = "timeout"               # no usable answer to A8 or to 94


@dataclass(frozen=True)
class HandpayResetResult:
    outcome: HandpayResetOutcome
    step1_acked: bool                 # method selection acknowledged
    step2_sent: bool                  # reset poll actually left the host
    step2_code: Optional[int]         # 0x94 response code byte, if one came
    confirmed_by_exception: bool      # exception 52 seen in the watch after
    detail: str

    @property
    def ok(self) -> bool:
        return self.outcome is HandpayResetOutcome.RESET_OK


# --------------------------------------------------------------------------
# Frame builders
# --------------------------------------------------------------------------

def build_reset_method_poll(address: int,
                            method: int = RESET_METHOD_CREDIT_METER) -> bytes:
    """Step 1 — enable a handpay reset method: [addr][A8][method][crc]
    (SAS 6.02 Table 14.2a)."""
    if method not in (RESET_METHOD_STANDARD, RESET_METHOD_CREDIT_METER):
        raise ValueError(f"unknown handpay reset method 0x{method:02X}")
    return _PROTO.build_packet(address, CMD_SET_HANDPAY_RESET_METHOD,
                               bytes([method]))


def build_handpay_reset_poll(address: int) -> bytes:
    """Step 2 — remote handpay reset: [addr][94][crc]. Type S: CRC even with
    no data (SAS 6.02 Table 7.9a)."""
    return _PROTO.build_packet(address, CMD_REMOTE_HANDPAY_RESET)


# --------------------------------------------------------------------------
# Response classification
# --------------------------------------------------------------------------

def _classify(resp: bytes, address: int, command: int,
              proto: SASProtocol) -> tuple:
    """Classify a step response -> (kind, code, detail).

    kind: 'ack' | 'refused' | 'no_handpay' | 'silence'
    code: the response's status byte, when the machine sent a full frame.

    Only the framed answer of Tables 14.2b / 7.9b with code 00 is consent.
    A lone address byte is NOT: neither table has it, and it is what a
    machine that cannot hear the host sends unasked (§4.2). A lone
    address|0x80 is the NACK of Table 7.4b (the poll did not arrive
    intact). A bad CRC, or a frame from the wrong address or command, is
    silence, never consent to fire a reset."""
    if not resp:
        return "silence", None, "no response"
    if len(resp) == 1:
        v = resp[0]
        if v == address:
            return "silence", None, ("a lone address byte, which is not an "
                                     "answer to this poll (a loop-break "
                                     "chirp?)")
        if v == (address | 0x80):
            return "refused", None, "address|0x80 NACK"
        return "silence", None, f"unclassifiable lone byte 0x{v:02X}"
    if len(resp) == 2 and resp[1] == 0x00:
        return "silence", None, "machine busy ([addr][00])"
    packet = proto.parse_packet(resp)
    if packet is None or packet.address != address \
            or packet.command != command or len(packet.data) < 1:
        return "silence", None, "corrupt/foreign frame dropped"
    code = packet.data[0]
    if command == CMD_SET_HANDPAY_RESET_METHOD:
        if code == METHOD_ACK_ENABLED:
            return "ack", code, "method enabled (ack 0x00)"
        if code == METHOD_ACK_NO_HANDPAY:
            return "no_handpay", code, "not in a handpay condition (ack 0x02)"
        return "refused", code, f"method refused (ack 0x{code:02X})"
    # CMD_REMOTE_HANDPAY_RESET
    if code == RESET_CODE_OK:
        return "ack", code, "handpay was reset (code 0x00)"
    if code == RESET_CODE_NO_HANDPAY:
        return "no_handpay", code, ("not in a handpay condition "
                                    "(code 0x02)")
    return "refused", code, f"reset refused (code 0x{code:02X})"


# --------------------------------------------------------------------------
# The two-step sequence
# --------------------------------------------------------------------------

def reset_handpay_to_credits(transport, address: int,
                             protocol: Optional[SASProtocol] = None,
                             method: int = RESET_METHOD_CREDIT_METER,
                             pace: float = SAS_POLL_FLOOR,
                             sleep: Callable[[float], None] = time.sleep,
                             confirm_polls: int = 5,
                             on_exception: Optional[Callable[[int], None]] = None,
                             on_frame: Optional[Callable[[bytes], None]] = None
                             ) -> HandpayResetResult:
    """Run the two-step reset-to-credits sequence against one machine.

      1. enable the reset method (0xA8, method byte) — REQUIRE code 00;
      2. only then issue the remote handpay reset (0x94). Code 00, "handpay
         was reset", is success;
      3. watch the general poll for exception 52 "handpay was reset" and
         say whether it came. It does not decide the result: §7.8.1 holds
         52 back until the handpay's 1B record has been read.

    THE ORDER IS THE FEATURE (AJ bench fact): step 2 is never sent unless
    step 1 was acked — silence, a NACK, a busy, or a corrupt frame on step 1
    all end the sequence with a typed result and NO reset on the wire.

    Pacing: `sleep(pace)` (default SAS_POLL_FLOOR, the 200 ms per-machine
    floor) runs between consecutive polls. Inject a no-op sleep only in mock
    tests. No automatic retries — see the module docstring.

    on_exception(code): non-confirmation exceptions drained during the
    confirm window (door events etc.) are handed here so a wrapping poller
    can dispatch them normally instead of losing them.
    on_frame(resp): a multi-byte pending long-poll result (guide §3 p.8,
    e.g. a ROM signature) arriving during the confirm window is handed here
    raw — SASPoller routes it through its _handle_long_poll_result.

    Long polls double as the implied ACK for a prior general-poll exception
    (guide §3), so running this inside a poll loop is protocol-safe.
    """
    proto = protocol or _PROTO

    # -- step 1: select the reset method — REQUIRE ack ----------------------
    resp = transport.transact(build_reset_method_poll(address, method))
    kind, code, detail = _classify(resp, address,
                                   CMD_SET_HANDPAY_RESET_METHOD, proto)
    if kind == "no_handpay":
        return HandpayResetResult(HandpayResetOutcome.NO_HANDPAY,
                                  False, False, None, False,
                                  f"step 1: {detail}")
    if kind == "refused":
        return HandpayResetResult(HandpayResetOutcome.METHOD_REFUSED,
                                  False, False, None, False,
                                  f"step 1: {detail}")
    if kind != "ack":
        return HandpayResetResult(HandpayResetOutcome.TIMEOUT,
                                  False, False, None, False,
                                  f"step 1: {detail}; reset NOT sent")

    # -- step 2: the reset — reachable ONLY through the acked branch --------
    sleep(pace)
    resp = transport.transact(build_handpay_reset_poll(address))
    kind, code, detail = _classify(resp, address,
                                   CMD_REMOTE_HANDPAY_RESET, proto)
    if kind == "no_handpay":
        # keyed off at the machine between the two polls (Table 7.9b)
        return HandpayResetResult(HandpayResetOutcome.NO_HANDPAY,
                                  True, True, code, False,
                                  f"step 2: {detail}")
    if kind == "refused":
        return HandpayResetResult(HandpayResetOutcome.METHOD_REFUSED,
                                  True, True, code, False,
                                  f"step 2: {detail}")
    if kind != "ack":
        return HandpayResetResult(HandpayResetOutcome.TIMEOUT,
                                  True, True, code, False,
                                  f"step 2: {detail}")

    # -- the machine has said "handpay was reset". Watch for exception 52. --
    confirmed = _await_confirmation(transport, address, proto, pace, sleep,
                                    confirm_polls, on_exception, on_frame)
    if confirmed:
        return HandpayResetResult(HandpayResetOutcome.RESET_OK,
                                  True, True, code, True,
                                  "reset acked and confirmed by exception "
                                  f"0x{HANDPAY_RESET_EXCEPTION:02X}")
    return HandpayResetResult(HandpayResetOutcome.RESET_OK,
                              True, True, code, False,
                              "the machine answered that the handpay was "
                              "reset (code 0x00); exception "
                              f"0x{HANDPAY_RESET_EXCEPTION:02X} not seen "
                              f"within {confirm_polls} polls, it follows "
                              "once the handpay's record has been read")


def _await_confirmation(transport, address, proto, pace, sleep,
                        confirm_polls, on_exception, on_frame) -> bool:
    """General-poll (paced) until exception 52, handpay was reset. The loop
    itself is core/sas_wait.await_exception, shared with the ticket and
    AFT waits."""
    return await_exception(transport, address, proto, CONFIRM_EXCEPTIONS,
                           pace, sleep, confirm_polls, on_exception, on_frame)
