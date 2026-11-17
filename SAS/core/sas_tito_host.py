"""
Host-side TITO — ticket-out capture and ticket-in redemption for the SAS host.

Built on the core/sas_handpay_reset.py template: pure transport-driven
functions returning typed results, a poller-integration wrapper
(SASTITOHost), callbacks, and MockSASSerialPort tests
(tests/test_sas_tito_host.py). CabiNet is the HOST — it polls, records and
adjudicates tickets; it never answers polls (the old EGM-side core/tito.py
was retired to attic/sas-20260707/ for exactly that role confusion).

PROVENANCE — two very different tiers in this module:

* GUIDE-CITED (Montana DOJ SAS Implementation Guide v1.5.0, on disk):
  - exception 0x3D "cash out ticket printed" + 0x3E "hand pay validated"
    (§3.1 / §4.6.2) trigger the ticket-out read;
  - 0x4D Send Enhanced Validation Information (§4.6.2): the 33-byte record
    with date/time, 16-digit validation number, amount in cents, sequential
    ticket number — built/parsed by core/sas_meters;
  - 0x4C Set Enhanced Validation ID (§4.6.1): 3-byte VGM validation ID +
    3-byte starting sequence, echoed back; ignored (silence) if the VGM is
    not configured for enhanced validation;
  - 0x3D Send Cash Out Ticket Information (§4.6.3): the standard-validation
    fallback read (validation field is all zeros when the machine is
    configured enhanced/system).

* VERIFY_ON_BENCH (the ticket-IN redemption cycle): the Montana guide
  documents NO redemption long polls — every 0x70/0x71 constant and frame
  layout below is a believed value from the full SAS spec's ticket-
  redemption chapter (not on disk), in the same spirit as the believed
  0xA8/0x94 handpay polls. The believed cycle:

      EGM queues exception 0x67 "ticket inserted"
   -> host polls 0x70 (type R) for the escrowed ticket's data
      response: [addr][70][len][status][amount 5-BCD cents][parsing code]
                [validation data ...][crc]
   -> host adjudicates against the TicketStore
   -> host sends 0x71 authorize (transfer code 0x00 cashable, the ISSUED
      amount, validation data echoed) or deny (transfer code 0x80, amount 0)
      response mirrors the 0x70 data shape with a MACHINE STATUS byte
   -> if the machine answers 'pending' (0x40) the host awaits exception
      0x68 "ticket transfer complete", then interrogates 0x71 (transfer
      code 0xFF, no other data) for the final status
   -> final status 0x00 = redeemed (store: redeemPending -> redeemed);
      status >= 0x80 = machine rejected/returned the ticket (store: reset
      to issued).

  Every one of those bytes needs bench adjudication on a real TITO-capable
  machine (BB2E on a SAS title). The mock tests prove self-consistency and
  the store's state machine, not interop. Record bench results in
  COMPATIBILITY.md and fix the constants HERE if disproven.

Safety posture mirrors the handpay module: a bad CRC, a foreign address, or
a malformed frame is treated as SILENCE (never as consent to authorize or to
mark a ticket redeemed), and an authorized-but-unconfirmed redemption leaves
the store redeemPending with an honest typed result — a retry from the same
machine re-draws the identical authorization (TicketStore idempotency).
"""

import enum
import time
from dataclasses import dataclass, replace
from typing import Callable, Optional

from .sas_protocol import SASProtocol
import zlib

from .sas_implied_ack import transact_reread
from .sas_wait import await_exception
from .sas_meters import (
    SAS_POLL_FLOOR, bcd_to_int, int_to_bcd,
    build_enhanced_validation_info_poll, build_set_validation_id_poll,
    build_meter_poll, parse_enhanced_validation_info,
    parse_cashout_ticket_info, ValidationRecord, CashoutTicketInfo,
    RECEIVE_VALIDATION_NUMBER, PendingCashout,
    build_send_pending_cashout_poll, parse_pending_cashout,
    build_receive_validation_number_poll,
)
from .sas_ticket_store import (
    TicketStore, DEFAULT_SYSTEM_ID, DEFAULT_MAX_TICKET_CENTS,
    ValidationMintError,
)

__all__ = [
    "TICKET_PRINTED_EXCEPTION", "HANDPAY_VALIDATED_EXCEPTION",
    "VALIDATION_ID_NOT_CONFIGURED_EXCEPTION",
    "TICKET_INSERTED_EXCEPTION", "TICKET_TRANSFER_COMPLETE_EXCEPTION",
    "CMD_SEND_TICKET_VALIDATION_DATA", "CMD_REDEEM_TICKET",
    "REDEEM_AUTHORIZE_CASHABLE", "REDEEM_AUTHORIZE_RESTRICTED",
    "REDEEM_REJECT", "REDEEM_INTERROGATE",
    "REDEEM_STATUS_REDEEMED", "REDEEM_STATUS_PENDING",
    "REDEEM_STATUS_REJECTED", "PARSING_CODE_BCD", "redeem_status_kind",
    "REDEMPTION_RETRY_SEC", "read_redemption_status",
    "TicketInData", "parse_ticket_in_data",
    "build_ticket_data_poll", "build_redeem_ticket_poll",
    "build_redeem_interrogate_poll",
    "CMD_SET_EXTENDED_TICKET_DATA", "CMD_SET_TICKET_DATA",
    "TICKET_DATA_LOCATION", "TICKET_DATA_ADDRESS1", "TICKET_DATA_ADDRESS2",
    "TICKET_DATA_TITLE_RESTRICTED", "TICKET_DATA_TITLE_DEBIT",
    "TICKET_TEXT_MAX", "TICKET_TITLE_MAX",
    "build_set_extended_ticket_data", "build_set_ticket_data",
    "parse_ticket_data_flag",
    "TicketCaptureOutcome", "TicketCaptureResult", "capture_printed_ticket",
    "RedemptionOutcome", "TicketRedemptionResult", "redeem_inserted_ticket",
    "ValidationIdOutcome", "ValidationIdResult", "configure_validation_id",
    "read_validation_record", "SASTITOHost",
    "VALIDATION_TYPES", "CASH_TICKET_TYPES", "PLAYABLE_ONLY_TICKET_TYPES",
    "VALIDATION_LOOK_AHEAD", "validation_kind",
    "SYSTEM_VALIDATION_REQUEST_EXCEPTION", "CASHOUT_BUTTON_EXCEPTION",
    "VALIDATION_APPROVE", "VALIDATION_REJECT", "DEFAULT_PAYLOAD_STYLE",
    "build_validation_payload", "SystemValidationOutcome",
    "SystemValidationResult", "service_system_validation",
]

# --------------------------------------------------------------------------
# Guide-cited trigger exceptions (sas_exceptions.EXCEPTIONS, §3.1 / §4.6.2)
# --------------------------------------------------------------------------
TICKET_PRINTED_EXCEPTION = 0x3D       # guide §3.1: cash out ticket printed
HANDPAY_VALIDATED_EXCEPTION = 0x3E    # guide §4.6.2: hand pay validated
# §4.6.1: an enhanced-validation EGM raises this until the host assigns its
# validation ID (0x4C). It fires ONLY while unseeded, so auto-seeding off it is
# collision-safe — a mid-life EGM (already seeded) never emits it, so we can
# never reset a live validation sequence and reuse barcodes.
VALIDATION_ID_NOT_CONFIGURED_EXCEPTION = 0x3F

# --------------------------------------------------------------------------
# System-validation ticket-out (SAS 6.02 §15.8, cross-verified 2026-07-08 —
# NOT in the on-disk Montana guide; see the memory reference
# reference_cabinet_sas_system_validation). CabiNet is the HOST that mints
# the validation number in real time when a SYSTEM-mode EGM raises 0x57.
# --------------------------------------------------------------------------
SYSTEM_VALIDATION_REQUEST_EXCEPTION = 0x57   # EGM: "system validation request"
CASHOUT_BUTTON_EXCEPTION = 0x66              # informational precursor (no-op)

# 0x58 validation-ID byte (host -> EGM)
VALIDATION_APPROVE = 0x01             # approve: print the cash-out ticket
VALIDATION_REJECT = 0x00             # reject: EGM drops the cash-out to handpay
CASHOUT_TYPE_NOT_WAITING = 0x80      # Table 15.7b, in the long poll 57 answer
VALIDATION_STATUS_NOT_IN_CASHOUT = 0x80   # Table 15.8b, the long poll 58
VALIDATION_STATUS_IMPROPER = 0x81         # answer's status byte

# 0x58 8-byte payload style. "bcd_number" (default) = the minted validation
# number as packed BCD, big-endian (SPEC-CORRECT: SAS 6.02 §15.8 + saspy, and
# it round-trips into the 0x4D record's 8-BCD validation-number field).
# "type_amount" = the ArduinoTITO echo [cashout type][00][00][5B amount], which
# also works on real cabinets (a system EGM may only require ID==0x01). Flip to
# capture raw bytes at the bench; default stays spec-correct.
PAYLOAD_STYLE_BCD = "bcd_number"
PAYLOAD_STYLE_TYPE_AMOUNT = "type_amount"
DEFAULT_PAYLOAD_STYLE = PAYLOAD_STYLE_BCD

# --------------------------------------------------------------------------
# VERIFY_ON_BENCH — believed full-SAS-spec values, uncited on disk.
# (These two exception codes live in sas_exceptions.BENCH_EXCEPTIONS so the
# poller's typed-event path names them; the command/status bytes live only
# here, like the handpay module's 0xA8/0x94.)
# --------------------------------------------------------------------------
TICKET_INSERTED_EXCEPTION = 0x67          # VERIFY_ON_BENCH
TICKET_TRANSFER_COMPLETE_EXCEPTION = 0x68  # VERIFY_ON_BENCH

CMD_SEND_TICKET_VALIDATION_DATA = 0x70    # VERIFY_ON_BENCH: believed type R
CMD_REDEEM_TICKET = 0x71                  # VERIFY_ON_BENCH

# 0x71 transfer-code byte (host -> EGM)          VERIFY_ON_BENCH, all three
REDEEM_AUTHORIZE_CASHABLE = 0x00      # authorize: credit as cashable
REDEEM_AUTHORIZE_RESTRICTED = 0x01    # authorize: a valid restricted
#                                       promotional ticket (Table 15.12c)
REDEEM_REJECT = 0x80                  # deny: return the ticket
REDEEM_INTERROGATE = 0xFF             # status interrogate (no other data)

# 0x71/0x70 machine-status byte (EGM -> host)    VERIFY_ON_BENCH, all three
REDEEM_STATUS_REDEEMED = 0x00         # ticket credited
REDEEM_STATUS_PENDING = 0x40          # redemption in progress
REDEEM_STATUS_REJECTED = 0x80         # 100xxxxx: the failed family
REDEEM_STATUS_WAITING = 0x20          # still waiting for long poll 71

REDEMPTION_RETRY_SEC = (2.0, 5.0, 15.0)   # wait before asking a machine
#                                       again about a redemption with no
#                                       final status; the last repeats.
#                                       Exception 68 asks at once.


def redeem_status_kind(status: Optional[int]) -> str:
    """What a long poll 71 machine status MEANS, by its top three bits —
    SAS 6.02 Table 15.12d: "3 MSbits can be used to determine category of
    status code".

      redeemed  000xxxxx  00 cashable, 01 restricted, 02 nonrestricted
      waiting   001xxxxx  20: the machine has not had its long poll 71
      pending   010xxxxx  40: accepted, the ticket is not stacked yet
      failed    100xxxxx  80-8B: the ticket was NOT redeemed
      unknown   anything else — C0 ("not compatible with current
                redemption cycle (ignored)"), FF (no validation
                information), no answer at all.

    Only `failed` puts a ticket back to issued, and only `redeemed` spends
    it. This used to be "anything from 0x80 up is a rejection", which sent
    C0 and FF — answers that say NOTHING about the ticket — down the
    rejection path and released the hub's hold on a ticket the machine may
    have been stacking (audit item 37)."""
    if status is None:
        return "unknown"
    top = status & 0xE0
    if top == 0x00:
        return "redeemed"
    if top == 0x20:
        return "waiting"
    if top == 0x40:
        return "pending"
    if top == 0x80:
        return "failed"
    return "unknown"


PARSING_CODE_BCD = 0x00               # VERIFY_ON_BENCH: validation data is
                                      # packed BCD digits

_PROTO = SASProtocol()                # stateless; safe to share


# --------------------------------------------------------------------------
# Frame builders + parser for the believed 0x70/0x71 layouts
# --------------------------------------------------------------------------

def build_ticket_data_poll(address: int) -> bytes:
    """0x70 — read the escrowed (inserted) ticket's data. VERIFY_ON_BENCH:
    believed to be a bare type R poll [addr][70] with no CRC, like the
    guide's '01 3D' read."""
    return _PROTO.build_short_poll(address, CMD_SEND_TICKET_VALIDATION_DATA)


# --------------------------------------------------------------------------
# Ticket-header configuration — SET ticket data. SPEC-CITED (full SAS 6.02
# text, adjudicated 2026-07-09), a stronger tier than the VERIFY_ON_BENCH
# guesses above: every byte below comes from Tables 15.3a/15.3b/15.3c
# (long poll 7C, §15.3) and 15.4a/15.4b (long poll 7D, §15.4).
#
# Which poll to use: §15.4's closing note — "Hosts utilizing extended
# validation support will likely use long polls 7B and 7C instead of long
# poll 7D. Data sent using 7B and 7C always takes precedence over data sent
# using long poll 7D." Our machines run secure-enhanced/system validation
# (the 0x4C/0x57 flows in this module), so 0x7C is the PRIMARY write —
# it also avoids 0x7D's positional side effects (a 7D that carries any
# text MUST also carry the 2-byte host ID and the 1-byte expiration).
#
# Field map (Table 15.3c): code 00 = Location (ASCII, 40 max) — the
# casino/property name line; 01 = Address 1 (street, 40 max); 02 =
# Address 2 (city/state/zip, 40 max); 10 = RESTRICTED ticket title
# (16 max, default "PLAYABLE ONLY"); 20 = DEBIT ticket title (16 max,
# default "DEBIT TICKET"). NOTE: the normal CASH ticket title ("CASHOUT
# TICKET") is NOT host-settable anywhere in SAS 6.02 — §15.3 offers only
# the restricted and debit titles, so a hub "titleCash" cannot be applied
# over SAS (never map it onto code 10/20: those print on the WRONG
# tickets).
#
# Response to either poll as a type S (Tables 15.3b/15.4b):
# [addr][cmd][flag][crc] — flag 01 = the machine's nonvolatile ticket-data
# status flag, set TRUE on valid data (an ACK) / 00 = set FALSE on invalid
# data (a NACK). Machines never answer the type G broadcast form (addr 0).
# --------------------------------------------------------------------------
CMD_SET_EXTENDED_TICKET_DATA = 0x7C   # §15.3 Table 15.3a
CMD_SET_TICKET_DATA = 0x7D            # §15.4 Table 15.4a

# Table 15.3c data codes
TICKET_DATA_LOCATION = 0x00
TICKET_DATA_ADDRESS1 = 0x01
TICKET_DATA_ADDRESS2 = 0x02
TICKET_DATA_TITLE_RESTRICTED = 0x10
TICKET_DATA_TITLE_DEBIT = 0x20

TICKET_TEXT_MAX = 40      # location/address lines (length fields 00-28)
TICKET_TITLE_MAX = 16     # restricted/debit titles


def _ticket_ascii(text, limit: int, field: str) -> bytes:
    """One ticket text field -> spec-legal ASCII bytes: printable ASCII
    only (0x20-0x7E; anything else becomes '?'), truncated to the field's
    spec limit. Empty is REFUSED — a zero-length element means 'revert to
    default' (7C) or 'do not change' (7D), two different machine behaviors
    an empty string must never silently pick between (pass None to leave a
    field untouched, ' ' to print a blank line)."""
    s = "".join(ch if " " <= ch <= "~" else "?" for ch in str(text))
    if not s:
        raise ValueError(f"{field}: empty text is ambiguous — pass None "
                         "(leave unchanged) or ' ' (blank line)")
    return s[:limit].encode("ascii")


def build_set_extended_ticket_data(address: int, location=None,
                                   address1=None, address2=None,
                                   restricted_title=None,
                                   debit_title=None) -> bytes:
    """0x7C Set Extended Ticket Data — SPEC-CITED (6.02 §15.3, Table
    15.3a): [addr][7C][len][code][dataLen][data]...[crc], len = bytes
    following excluding the CRC, each element a Table 15.3c (code, length,
    ASCII data) triple in any combination. None = element omitted (the
    machine value is untouched); at least one field is required (the
    zero-element length-00 form is the interrogate, a different poll).
    Text is clamped to printable ASCII and the per-field spec limit
    (40 for location/addresses, 16 for the titles)."""
    fields = [
        (TICKET_DATA_LOCATION, location, TICKET_TEXT_MAX, "location"),
        (TICKET_DATA_ADDRESS1, address1, TICKET_TEXT_MAX, "address1"),
        (TICKET_DATA_ADDRESS2, address2, TICKET_TEXT_MAX, "address2"),
        (TICKET_DATA_TITLE_RESTRICTED, restricted_title, TICKET_TITLE_MAX,
         "restricted_title"),
        (TICKET_DATA_TITLE_DEBIT, debit_title, TICKET_TITLE_MAX,
         "debit_title"),
    ]
    inner = b""
    for code, text, limit, name in fields:
        if text is None:
            continue
        data = _ticket_ascii(text, limit, name)
        inner += bytes([code, len(data)]) + data
    if not inner:
        raise ValueError("set extended ticket data needs at least one field")
    body = bytes([address, CMD_SET_EXTENDED_TICKET_DATA, len(inner)]) + inner
    return _crc(body)


def build_set_ticket_data(address: int, host_id: int = DEFAULT_SYSTEM_ID,
                          expiration_days: int = 0, location=None,
                          address1=None, address2=None,
                          allow_no_text: bool = False) -> bytes:
    """0x7D Set Ticket Data — SPEC-CITED (6.02 §15.4, Table 15.4a):
    [addr][7D][len 02-7E][host ID 2 binary][expiration 1 binary]
    [locLen][loc][addr1Len][addr1][addr2Len][addr2][crc]. Fields are
    POSITIONAL: any text requires the host ID and expiration slots, and a
    mid-stream None rides as length 00 = 'do not change' (Table 15.4a
    note); trailing Nones are dropped entirely. The 2-byte host ID is
    LSB-first (§2's global rule: 'All data exchanged in the binary format
    are sent least significant byte (LSB) first'). expiration_days 00 =
    tickets never expire (the collector-right default — 15.4a defines no
    'do not change' for this slot, so sending 7D always sets it).

    allow_no_text=True permits the text-less len-03 form — host ID +
    expiration only, every text slot untouched (Table 15.4a's length
    floor is 02, so 03 is legal on the wire). That is the expiration-only
    write the hub uses AFTER a 0x7C carried the text (7C text takes §15.4
    precedence; 7C has no expiration slot, so 7D is the only way to set
    it). Default False keeps the original all-None accident guard for
    text pushes. Max frame cross-check: 2+1+3*(1+40) = 126 = 0x7E,
    exactly the table's length ceiling."""
    if not 0 <= host_id <= 0xFFFF:
        raise ValueError(f"host_id must be 0..65535, got {host_id}")
    if not 0 <= expiration_days <= 0xFF:
        raise ValueError(f"expiration_days must be 0..255 (00 = never "
                         f"expires), got {expiration_days}")
    texts = [(location, "location"), (address1, "address1"),
             (address2, "address2")]
    if all(text is None for text, _ in texts) and not allow_no_text:
        raise ValueError("set ticket data needs at least one text field "
                         "(pass allow_no_text=True for the expiration-only "
                         "form)")
    while texts and texts[-1][0] is None:
        texts.pop()                       # trailing fields: omit entirely
    inner = host_id.to_bytes(2, "little") + bytes([expiration_days])
    for text, name in texts:
        if text is None:
            inner += b"\x00"              # mid-stream: 00 = do not change
        else:
            data = _ticket_ascii(text, TICKET_TEXT_MAX, name)
            inner += bytes([len(data)]) + data
    body = bytes([address, CMD_SET_TICKET_DATA, len(inner)]) + inner
    return _crc(body)


def parse_ticket_data_flag(data: bytes) -> bool:
    """Parse SASPacket.data for the 7C/7D set-ticket-data response
    (Tables 15.3b/15.4b): exactly one ticket-data status flag byte.
    True = 01 (valid data received — the ACK), False = 00 (invalid data —
    the NACK). Anything else is a structural lie -> ValueError (callers
    treat it as silence, never as consent)."""
    if len(data) != 1:
        raise ValueError(f"ticket data status must be 1 byte, got "
                         f"{len(data)}")
    if data[0] not in (0x00, 0x01):
        raise ValueError(f"ticket data status flag must be 00/01, got "
                         f"0x{data[0]:02X}")
    return data[0] == 0x01


def _canonical_vn(validation_number: str) -> str:
    """Canonical TicketStore key: the 16-digit zero-padded form. The same
    piece of paper surfaces at different wire widths (8-digit standard 3D
    reads at capture vs the 0x70 escrow read's 8-byte field at redemption),
    so un-normalized keys would make a 3D-captured ticket unredeemable."""
    return validation_number.rjust(16, "0")


def _validation_to_bcd(validation_number: str) -> bytes:
    """Digit string -> packed BCD, zero-padded on the left to 8 bytes
    (16 digits — the enhanced/system width the 4D record uses)."""
    vn = validation_number.rjust(16, "0")
    if len(vn) > 16 or not vn.isdigit():
        raise ValueError(f"validation number must be <=16 digits: "
                         f"{validation_number!r}")
    return int_to_bcd(int(vn), 8)


def build_redeem_ticket_poll(address: int, transfer_code: int,
                             amount_cents: int,
                             validation: bytes) -> bytes:
    """0x71 authorize/deny — BENCH-PROVEN layout (WMS BB2, 2026-07-08):
    [addr][71][len=0x10][transfer code][amount 5-BCD cents][parsing code 00]
    [validation data — the raw field echoed from the 0x70 read][crc].
    len = the bytes after it (= 7 + len(validation); 0x10/16 for the BB2's
    9-byte field). The EGM matches the echoed validation against its escrow,
    so it MUST be TicketInData.validation_raw byte-for-byte — the machine's
    1-byte validation-type prefix included.

    History: the earlier saspy-shaped 21-byte frame (an 8-byte re-encoded
    validation + a 4-byte restricted-expiration + a 2-byte pool-id) drew a
    0x81 reject on the bench. The machine's OWN 0x71 response proved the true
    shape — 16-byte body, no expiration/pool, 9-byte validation echoed. saspy
    (sas.py:1264-1275) uses the longer form; the BB2 wants the ArduinoTITO
    form. Restricted/promo redemptions (with a real pool/expiry) are untested
    and out of scope here — cashable only."""
    if transfer_code not in (REDEEM_AUTHORIZE_CASHABLE,
                             REDEEM_AUTHORIZE_RESTRICTED, REDEEM_REJECT):
        raise ValueError(f"unknown redeem transfer code 0x{transfer_code:02X}")
    if not validation:
        raise ValueError("redeem needs the raw validation field to echo")
    inner = (bytes([transfer_code]) + int_to_bcd(amount_cents, 5)
             + bytes([PARSING_CODE_BCD]) + bytes(validation))
    body = bytes([address, CMD_REDEEM_TICKET, len(inner)]) + inner
    return _crc(body)


def build_redeem_interrogate_poll(address: int) -> bytes:
    """0x71 interrogate — VERIFY_ON_BENCH: [addr][71][len=01][FF][crc]."""
    return _crc(bytes([address, CMD_REDEEM_TICKET, 1, REDEEM_INTERROGATE]))


def _crc(body: bytes) -> bytes:
    from .sas_protocol import sas_crc
    return body + sas_crc(body).to_bytes(2, "little")


@dataclass(frozen=True)
class TicketInData:
    """One parsed 0x70/0x71 response record (VERIFY_ON_BENCH layout).
    validation_number is the zero-padded digit string; amount is CENTS."""
    status: int
    amount_cents: Optional[int]
    parsing_code: Optional[int]
    validation_number: Optional[str]
    # The raw validation-data field verbatim (BENCH 2026-07-08: the WMS BB2
    # sends a 9-byte field = a 1-byte validation-type prefix + the 8-byte BCD
    # number). The 0x71 redeem MUST echo this field byte-for-byte, so keep it.
    validation_raw: bytes = b""


def parse_ticket_in_data(data: bytes) -> TicketInData:
    """Parse SASPacket.data for the believed 0x70/0x71 response shape:
    [len][status][amount 5-BCD][parsing code][validation data ...].
    A short record ([len=01][status]) is legal — an interrogate with nothing
    in escrow carries only the status byte. Raises ValueError on structural
    lies (length byte disagreeing with the frame)."""
    if len(data) < 2:
        raise ValueError("ticket data response too short")
    declared = data[0]
    if declared != len(data) - 1:
        raise ValueError(f"length byte {declared} != {len(data) - 1}")
    status = data[1]
    if len(data) < 8:
        return TicketInData(status, None, None, None)
    amount = bcd_to_int(data[2:7])
    parsing = data[7]
    val = data[8:]
    # The 0x70 validation-data field carries the 8-byte BCD validation number,
    # but the EGM may prepend a validation-type/length byte. BENCH-PROVEN
    # 2026-07-08 (WMS BB2): a ticket minted as 0100000000000001 read back as
    # val = 01 01 00 00 00 00 00 00 01 (a leading 0x01 + the 8-byte number).
    # Take the TRAILING 8 bytes as the number so a machine-added prefix does
    # not corrupt the ledger lookup (saspy sas.py:~1264 uses an 8-byte BCD
    # validation field on the 0x71 echo, so 8 bytes is the number width).
    val_number = val[-8:] if len(val) >= 8 else val
    validation_number = val_number.hex() if val_number else None
    return TicketInData(status, amount, parsing, validation_number, bytes(val))


# --------------------------------------------------------------------------
# Response classification (shared safety rule: corrupt/foreign = silence)
# --------------------------------------------------------------------------

def _classify_frame(resp: bytes, address: int, command: int,
                    proto: SASProtocol):
    """Return the SASPacket for a well-formed [addr][cmd][data][crc] frame
    from the RIGHT machine and command, else None. Bad CRC, foreign address,
    wrong command, busy, or a lone byte are all 'silence' here — never
    consent (the handpay module's rule)."""
    if not resp or len(resp) < 4:
        return None
    packet = proto.parse_packet(resp)
    if packet is None or packet.address != address \
            or packet.command != command:
        return None
    return packet


# --------------------------------------------------------------------------
# 0x4C — configure the VGM validation ID (guide §4.6.1)
# --------------------------------------------------------------------------

class ValidationIdOutcome(enum.Enum):
    ACCEPTED = "accepted"             # VGM echoed an ID/sequence
    IGNORED = "ignored"               # silence — per the guide, a VGM not
                                      # configured for enhanced validation
                                      # ignores this poll entirely


@dataclass(frozen=True)
class ValidationIdResult:
    outcome: ValidationIdOutcome
    vgm_id: Optional[bytes]           # echoed 3-byte ID (raw wire order)
    sequence: Optional[bytes]         # echoed 3-byte current sequence
    detail: str

    @property
    def ok(self) -> bool:
        return self.outcome is ValidationIdOutcome.ACCEPTED


def configure_validation_id(transport, address: int,
                            validation_id: bytes = b"\x00\x00\x00",
                            sequence: bytes = b"\x00\x00\x00",
                            protocol: Optional[SASProtocol] = None
                            ) -> ValidationIdResult:
    """Set (or, with an all-zero ID, read) the VGM validation ID + starting
    sequence — guide §4.6.1 (0x4C). The response echoes the ID and CURRENT
    sequence. Byte order of the 3-byte binary fields is a documented guide
    gap (see build_set_validation_id_poll) — both are handled as opaque
    bytes. Silence is a typed IGNORED, which the guide defines as 'not
    configured for enhanced validation', not an error."""
    proto = protocol or _PROTO
    resp = transport.transact(
        build_set_validation_id_poll(address, validation_id, sequence))
    packet = _classify_frame(resp, address, 0x4C, proto)
    if packet is None or len(packet.data) != 6:
        return ValidationIdResult(
            ValidationIdOutcome.IGNORED, None, None,
            "no 4C echo — VGM not configured for enhanced validation "
            "(guide §4.6.1) or poll unsupported")
    return ValidationIdResult(
        ValidationIdOutcome.ACCEPTED, packet.data[0:3], packet.data[3:6],
        "VGM echoed validation ID/sequence")


# --------------------------------------------------------------------------
# Ticket-out: exception 0x3D -> read validation info -> record
# --------------------------------------------------------------------------

VALIDATION_LOOK_AHEAD = 0xFF   # 0x4D function code: look, do not mark as read

# SAS 6.02 Table 15.13c — what a 0x4D validation record IS. The machine keeps
# handpays, receipts and redeemed tickets in the same buffer as the tickets
# it prints, and raises the same exception (0x3D, or 0x3E for a handpay with
# no receipt) for all of them: "The host should use the data from long poll
# 4D to determine the validation type" (§15).
VALIDATION_TYPES = {
    0x00: "cashable ticket",
    0x01: "restricted promotional ticket",
    0x02: "cashable ticket from an AFT transfer",
    0x03: "restricted ticket from an AFT transfer",
    0x04: "debit ticket from an AFT transfer",
    0x10: "cancelled-credit handpay, receipt printed",
    0x20: "jackpot handpay, receipt printed",
    0x40: "cancelled-credit handpay, no receipt",
    0x60: "jackpot handpay, no receipt",
    0x80: "cashable ticket redeemed",
    0x81: "restricted promotional ticket redeemed",
    0x82: "nonrestricted promotional ticket redeemed",
}
CASH_TICKET_TYPES = frozenset({0x00, 0x02, 0x04})
PLAYABLE_ONLY_TICKET_TYPES = frozenset({0x01, 0x03})


def validation_kind(validation_type: int):
    """('cash' | 'playable' | 'other' | 'unknown', label) for a Table
    15.13c type code. The question it answers is "is this a ticket in a
    player's hand?"

    'cash'      a ticket worth cash (a debit ticket is the player's own
                money, so it counts).
    'playable'  a restricted, promotional ticket: "PLAYABLE ONLY". It IS a
                ticket and goes back into a machine like any other, so it is
                stored. What a system does differently is flag it at the
                cash desk (AJ, 2026-11-16, on the SMIB system he ran before:
                "they act the same in reality but in the cash desk part ...
                they would be flagged as playable only"). The hub cannot
                flag it yet; the type is kept with the stored record so it
                can.
    'other'     a real record that is NOT a ticket: a handpay ("the receipt
                is not the cash out, the handpay is the cash out", §15), or
                the record of a ticket that was redeemed. Never stored.
    'unknown'   a code the table does not have. Kept as a ticket, as every
                record was before 2026-11-16, because turning away a real
                ticket strands a player's money."""
    label = VALIDATION_TYPES.get(validation_type)
    if label is None:
        return "unknown", f"type {validation_type:02X}, not in Table 15.13c"
    if validation_type in CASH_TICKET_TYPES:
        return "cash", label
    if validation_type in PLAYABLE_ONLY_TICKET_TYPES:
        return "playable", label
    return "other", label


def _read_4d(transport, address: int, function_code: int, proto):
    """One 0x4D read, asked for again when its answer arrives damaged
    (§3.2, see transact_reread). Returns (record | None, heard, sent)."""
    resp, heard, sent = transact_reread(
        transport, build_enhanced_validation_info_poll(address, function_code))
    packet = _classify_frame(resp, address, 0x4D, proto)
    if packet is None:
        return None, heard, sent
    try:
        return parse_enhanced_validation_info(packet.data), heard, sent
    except ValueError:
        return None, heard, sent


def read_validation_record(transport, address: int,
                           function_code: int = 0x00,
                           protocol: Optional[SASProtocol] = None
                           ) -> Optional[ValidationRecord]:
    """One 0x4D read (SAS 6.02 §15.10). Returns None on silence or a
    malformed frame, after asking again up to twice.

    What the function code does to the machine's buffer, from the document
    (this used to be marked VERIFY_ON_BENCH):
      00     the next UNREAD record; the machine marks it read once it gets
             an implied ACK, and stops raising 0x3D/0x3E for it;
      FF     the same record, looked at WITHOUT marking it read;
      01-1F  the record at that buffer position, read state unchanged.
    All fields are zero when there is nothing to return."""
    rec, _heard, _sent = _read_4d(transport, address, function_code,
                                  protocol or _PROTO)
    return rec


def _usable(rec) -> bool:
    return bool(rec is not None and rec.validation_number.strip("0")
                and rec.amount_cents)


class TicketCaptureOutcome(enum.Enum):
    RECORDED = "recorded"             # new ticket persisted
    DUPLICATE = "duplicate"           # idempotent re-read; store unchanged
    NO_DATA = "no_data"               # machine answered but had no record
    TIMEOUT = "timeout"               # silence on both reads
    NOT_A_TICKET = "not_a_ticket"     # a real record that is not a cash
    #                                   ticket (handpay, restricted, redeemed)


@dataclass(frozen=True)
class TicketCaptureResult:
    outcome: TicketCaptureOutcome
    validation_number: Optional[str]
    amount_cents: Optional[int]
    via: Optional[str]                # '4D' (enhanced) or '3D' (standard)
    record: Optional[dict]            # the store record, when persisted
    detail: str
    validation_type: Optional[int] = None   # Table 15.13c code (0x4D only)

    @property
    def ok(self) -> bool:
        return self.outcome in (TicketCaptureOutcome.RECORDED,
                                TicketCaptureOutcome.DUPLICATE)


def capture_printed_ticket(transport, address: int, store: TicketStore,
                           protocol: Optional[SASProtocol] = None,
                           pace: float = SAS_POLL_FLOOR,
                           sleep: Callable[[float], None] = time.sleep
                           ) -> TicketCaptureResult:
    """The ticket-OUT read, run when exception 0x3D (cash out ticket
    printed) — or 0x3E (hand pay validated) — comes off the FIFO:

      1. 0x4D (SAS 6.02 §15.10, secure enhanced and system validation): the
         full record with its type, the 16-digit validation number, amount
         in cents and ticket number;
      2. fallback 0x3D read (standard validation), ONLY for a machine that
         never answered 0x4D at all: 8-digit validation number + amount.

    ⭐ THE 0x4D READ, AND WHY IT IS TWO POLLS (2026-11-16, audit items 1, 3).

    Function 00 hands over the next unread record and the machine marks it
    read on our implied ACK. It is the only copy we will be offered: after
    that the machine stops raising 0x3D for it. So:

      a. FF first — the same record, looked at WITHOUT marking it read. It
         is kept in memory, not stored.
      b. 00 — the read that counts. If its answer arrives damaged it is
         asked for again (the implied NACK), never followed by another poll,
         which would have been the ACK that makes the machine drop it.
      c. If the 00 read still brings nothing usable, the copy from (a) is
         what gets stored. That covers a machine that marks the record read
         when it SENDS it, and one that counts our look as the read.

    Before this, one damaged 35-byte answer lost the ticket: the 0x3D poll
    that followed acknowledged it, and the hub then refused the paper at
    every machine as an unknown number.

    A record is stored as a ticket only when its type says it is one
    (validation_kind). A handpay raises the same exceptions and sits in the
    same buffer; stored as a ticket, its receipt would have been paid a
    second time.

    The winning record is persisted via TicketStore.record_issued, which is
    idempotent — a duplicate capture (retried exception, re-read) returns
    DUPLICATE and mutates nothing. Long polls double as the implied ACK for
    the triggering exception (§3), so calling this from the poller's
    dispatch path is protocol-safe."""
    proto = protocol or _PROTO

    # -- 1: enhanced record ----------------------------------------------------
    peek, peek_heard, _ = _read_4d(transport, address, VALIDATION_LOOK_AHEAD,
                                   proto)
    sleep(pace)
    rec, heard, sent = _read_4d(transport, address, 0x00, proto)
    if _usable(rec):
        # The note says what the look-ahead did whenever it was not simply
        # the same record, so a machine's log shows whether the safety copy
        # exists on that machine.
        note = ""
        if not _usable(peek):
            note = (" · the look-ahead (FF) "
                    + ("came back empty" if peek is not None
                       else "got no usable answer"))
        elif _canonical_vn(peek.validation_number) \
                != _canonical_vn(rec.validation_number):
            note = (" · the look-ahead (FF) showed a different record ("
                    f"{_canonical_vn(peek.validation_number)}) and was "
                    "not used")
        return _persist_4d(store, rec, address, note)
    if _usable(peek):
        why = ("answered with an empty record" if rec is not None
               else f"did not arrive in {sent} tries")
        return _persist_4d(store, peek, address,
                           f" · the 0x4D read {why}; taken from the "
                           "look-ahead (FF) copy")
    # A parsed-but-zeroed record. On a machine in enhanced or system
    # validation that is an empty buffer. But a STANDARD-validation machine
    # may answer 0x4D too: it "must either not respond to long poll 4D or
    # return all zeros in the Validation Number field" (§15.10). Both go on
    # to the 0x3D read, which tells them apart (audit item 20).
    zeroed = rec is not None
    if not zeroed and (heard or peek_heard):
        # The machine speaks 0x4D and its answer did not arrive. No 0x3D
        # read: that is for standard validation, and a different long poll
        # here would have been the ACK. The machine keeps the record unread
        # after a final NACK and raises its exception again.
        return TicketCaptureResult(
            TicketCaptureOutcome.TIMEOUT, None, None, "4D", None,
            f"0x4D answered but not intact in {sent} tries — not "
            "acknowledged, so the machine still holds the record and "
            "raises its exception again")

    # -- 2: standard validation: 0x4D never answered, or answered zeros -------
    sleep(pace)
    resp = transport.transact(build_meter_poll(address, 0x3D))
    packet = _classify_frame(resp, address, 0x3D, proto)
    if packet is None:
        if zeroed:
            return TicketCaptureResult(
                TicketCaptureOutcome.NO_DATA, None, None, "4D", None,
                "4D answered with a zeroed record — validation buffer empty")
        return TicketCaptureResult(
            TicketCaptureOutcome.TIMEOUT, None, None, None, None,
            "silence on both 4D and 3D reads — ticket data unrecoverable "
            "this cycle")
    try:
        info: CashoutTicketInfo = parse_cashout_ticket_info(packet.data)
    except ValueError as e:
        return TicketCaptureResult(
            TicketCaptureOutcome.TIMEOUT, None, None, "3D", None,
            f"malformed 3D response ({e}) — treated as silence")
    if not info.validation_number.strip("0"):
        return TicketCaptureResult(
            TicketCaptureOutcome.NO_DATA, None, info.amount_cents, "3D", None,
            ("4D answered with a zeroed record and the 3D validation field "
             "is all zeros — nothing to capture") if zeroed else
            "3D validation field all zeros (machine is enhanced/system-"
            "configured, guide §4.6.3 note) and 4D did not answer")
    if not info.amount_cents:
        return TicketCaptureResult(
            TicketCaptureOutcome.NO_DATA, info.validation_number, None, "3D",
            None, "3D record carries no amount")
    return _persist(store, info.validation_number, info.amount_cents,
                    address, "3D")


def validation_machine_id(smib_id: str, address: int) -> bytes:
    """A validation ID for long poll 4C that is this machine's own: the
    3 wire bytes (binary, least significant first) of a number worked out
    from the satellite's id and the machine's SAS address. Stable, so a
    re-seed after a RAM clear gives the machine the same ID; never zero,
    which long poll 4C reads as "tell me the current ID"."""
    n = zlib.crc32(f"{smib_id}/{int(address)}".encode("utf-8")) & 0xFFFFFF
    return (n or 1).to_bytes(3, "little")


def _persist_4d(store, rec, address, note="") -> TicketCaptureResult:
    """Store a 0x4D record — if its type says it is a ticket."""
    kind, label = validation_kind(rec.validation_type)
    if kind == "other":
        return TicketCaptureResult(
            TicketCaptureOutcome.NOT_A_TICKET,
            _canonical_vn(rec.validation_number), rec.amount_cents, "4D",
            None, f"{label} — not a ticket, not stored{note}",
            validation_type=rec.validation_type)
    if kind == "playable":
        note = f" · {label}{note}"
    elif kind == "unknown":
        note = f" · {label}; stored as a cash ticket{note}"
    # The issue time is OURS (the store stamps it), not the date in the
    # record. That date is the machine's own clock, which nothing here sets
    # or checks, and which is wrong after a RAM clear or with a dead clock
    # battery. The hub works ticket expiry from the issue time: a ticket
    # stamped with a date years back counts as long expired and is removed
    # at the next reap, leaving fresh paper the hub does not know (SAS 6.02
    # audit, item 22). The machine's date is kept beside it for the record.
    extra = {"validationType": rec.validation_type}
    if rec.when:
        extra["machineTime"] = rec.when.isoformat()
    out = _persist(store, rec.validation_number, rec.amount_cents, address,
                   "4D", ticket_number=rec.ticket_number_le, extra=extra)
    return replace(out, detail=out.detail + note,
                   validation_type=rec.validation_type)


def _persist(store, vn, amount_cents, address, via,
             ticket_number=None, issued_at=None,
             extra=None) -> TicketCaptureResult:
    vn = _canonical_vn(vn)
    out = store.record_issued(vn, amount_cents, address,
                              ticket_number=ticket_number, source=f"egm_{via}",
                              issued_at=issued_at, extra=extra)
    if out["duplicate"]:
        return TicketCaptureResult(
            TicketCaptureOutcome.DUPLICATE, vn, amount_cents, via,
            out["record"], "already recorded — idempotent re-capture")
    return TicketCaptureResult(
        TicketCaptureOutcome.RECORDED, vn, amount_cents, via, out["record"],
        f"ticket recorded via {via}")


# --------------------------------------------------------------------------
# System-validation ticket-out: exception 0x57 -> 0x57 read -> mint -> 0x58
# (the 0x3D completion is handled by capture_printed_ticket, which reconciles
# the minted pending against the printed record — reused, not duplicated)
# --------------------------------------------------------------------------

def build_validation_payload(style: str, validation_number: str,
                             cashout_type: int, amount_raw: bytes) -> bytes:
    """Build the 8-byte 0x58 payload for the given PAYLOAD_STYLE.

    * "bcd_number" (spec-correct): the validation number as packed BCD,
      big-endian, left-zero-padded to 8 bytes (16 digits).
    * "type_amount" (ArduinoTITO): [cashout type][00][00][5-byte amount echo].

    amount_raw MUST be the 5 raw amount bytes from the 0x57 read for the
    type_amount style (the EGM's own bytes, echoed verbatim)."""
    if style == PAYLOAD_STYLE_BCD:
        vn = str(validation_number).rjust(16, "0")
        if len(vn) > 16 or not vn.isdigit():
            raise ValueError(f"validation number must be <=16 digits: "
                             f"{validation_number!r}")
        return int_to_bcd(int(vn), 8)
    if style == PAYLOAD_STYLE_TYPE_AMOUNT:
        if len(amount_raw) != 5:
            raise ValueError("type_amount payload needs the 5 raw amount "
                             f"bytes, got {len(amount_raw)}")
        return bytes([cashout_type & 0xFF, 0x00, 0x00]) + amount_raw
    raise ValueError(f"unknown PAYLOAD_STYLE {style!r}")


class SystemValidationOutcome(enum.Enum):
    SERVICED = "serviced"        # approved: minted a number, sent 0x58 ID=01
    REJECTED = "rejected"        # sent 0x58 ID=00 -> EGM drops to handpay
    NO_DATA = "no_data"          # 0x57 read answered but had no pending cashout
    TIMEOUT = "timeout"          # silence/bad-CRC 0x57 read — NO 0x58 sent
    REFUSED = "refused"          # the machine refused the number (0x58
    #                              status 81, twice)


@dataclass(frozen=True)
class SystemValidationResult:
    outcome: SystemValidationOutcome
    cashout_type: Optional[int]
    amount_cents: Optional[int]
    validation_number: Optional[str]   # the minted number (SERVICED only)
    validation_id: Optional[int]       # the 0x58 ID byte actually sent
    payload_style: Optional[str]
    pending_raw: Optional[bytes]       # raw 0x57 response data bytes captured
    ack_frame: Optional[bytes]         # raw 0x58 ack frame captured, if any
    ack_confirmed: bool                # a well-formed [addr][0x58] ack seen
    detail: str

    @property
    def ok(self) -> bool:
        return self.outcome is SystemValidationOutcome.SERVICED


def service_system_validation(transport, address: int, store: TicketStore,
                              protocol: Optional[SASProtocol] = None,
                              pace: float = SAS_POLL_FLOOR,
                              sleep: Callable[[float], None] = time.sleep,
                              system_id: int = DEFAULT_SYSTEM_ID,
                              payload_style: str = DEFAULT_PAYLOAD_STYLE,
                              approve: bool = True,
                              max_cashout_cents: Optional[int] = None
                              ) -> SystemValidationResult:
    """Service one system-validation request (exception 0x57), mirroring
    capture_printed_ticket's structure:

      1. read LP 0x57 "Send Pending Cashout Information" (Type-R, no outbound
         CRC): [cashout type][amount 5B BCD];
      2. if `approve` and the read is a real positive cash-out, MINT a
         host-side validation number (store.mint_validation_number) and send
         LP 0x58 "Receive Validation Number" with ID=0x01 APPROVE + the
         PAYLOAD_STYLE payload; if `approve` is False, send ID=0x00 REJECT.

    Safety rails on the approve path: an amount above `max_cashout_cents`
    (default store.MAX_TICKET_CENTS) is REJECTED to handpay, never minted; a
    mint that cannot durably persist (ValidationMintError) or is otherwise
    invalid fails SAFE to TIMEOUT with NO 0x58 approve; and the mint is
    IDEMPOTENT per physical cash-out — a re-raised 0x57 with an unreconciled
    (address, amount) pending re-sends that SAME number instead of minting a
    duplicate. In the ArduinoTITO "type_amount" style the EGM supplies its own
    number, so no host mint runs (avoids permanent orphan pendings).

    SILENCE != CONSENT: a silent or bad-CRC 0x57 read returns a typed TIMEOUT
    and NEVER sends 0x58 approve (mint does not run). A zeroed/None amount is
    NO_DATA — also no approve. The REAL success signal is the later 0x3D
    completion, handled by capture_printed_ticket, which reconciles the
    minted pending into a single recorded ticket. Long polls double as the
    implied ACK for the triggering exception (guide §3), so calling this from
    the poller dispatch is safe.

    ⭐ BOTH POLLS ARE ASKED FOR AGAIN, AND THE 0x58 ANSWER IS READ
    (2026-11-16, audit items 18 and 21). The machine raises exception 57
    every 800 ms only "until it receives a long poll 57 or 58" (§15.7) and
    gives up after ten seconds, so a damaged answer to either poll used to
    end the cash-out: nothing was repeated, and no new exception 57 came to
    trigger another try. And the answer to long poll 58 carries a status
    (Table 15.8b) that was never read: 00 acknowledged, 80 "not in
    cashout", 81 "improper validation rejected". 80 and 81 were reported as
    an approval. The cashout type in the 57 answer can also say 80, "not
    waiting for system validation" (Table 15.7b).

    The first byte of long poll 58 is the validation SYSTEM ID in BCD
    (00 = denied), not an approve flag; it is the mint's own system id."""
    proto = protocol or _PROTO

    # -- 1: read the pending cash-out (Type-R, no outbound CRC) --------------
    resp, _h, _n = transact_reread(
        transport, build_send_pending_cashout_poll(address))
    packet = _classify_frame(resp, address, SYSTEM_VALIDATION_REQUEST_EXCEPTION,
                             proto)
    if packet is None:
        return SystemValidationResult(
            SystemValidationOutcome.TIMEOUT, None, None, None, None, None,
            None, None, False,
            "silence/bad-CRC on the 0x57 pending-cashout read — NO 0x58 sent "
            "(silence is never consent; safe to retry)")
    try:
        pend: PendingCashout = parse_pending_cashout(packet.data)
    except ValueError as e:
        return SystemValidationResult(
            SystemValidationOutcome.TIMEOUT, None, None, None, None, None,
            packet.data, None, False,
            f"malformed 0x57 response ({e}) — treated as silence; no 0x58 sent")

    if pend.cashout_type == CASHOUT_TYPE_NOT_WAITING:
        return SystemValidationResult(
            SystemValidationOutcome.NO_DATA, pend.cashout_type,
            pend.amount_cents, None, None, None, packet.data, None, False,
            "0x57 says the machine is not waiting for system validation "
            "(cashout type 80) — no approval sent")

    def send_58(id_byte, payload):
        """Long poll 58, asked for again if its answer is damaged. Returns
        (raw answer | None, answered, status | None)."""
        raw, _h2, _n2 = transact_reread(
            transport, build_receive_validation_number_poll(
                address, id_byte, payload))
        pkt = _classify_frame(raw, address, RECEIVE_VALIDATION_NUMBER, proto)
        if pkt is None:
            return None, False, None
        return raw, True, (pkt.data[0] if pkt.data else None)

    if pend.amount_cents is None or pend.amount_cents <= 0:
        # A non-BCD/zero amount can never be a valid approve — no 0x58 approve.
        return SystemValidationResult(
            SystemValidationOutcome.NO_DATA, pend.cashout_type,
            pend.amount_cents, None, None, None, packet.data, None, False,
            "0x57 reports no positive pending cash-out — no approval sent")

    # -- 2a: explicit reject (host business decision) -----------------------
    if not approve:
        sleep(pace)
        ack, answered, _st = send_58(VALIDATION_REJECT, b"\x00" * 8)
        return SystemValidationResult(
            SystemValidationOutcome.REJECTED, pend.cashout_type,
            pend.amount_cents, None, VALIDATION_REJECT, None, packet.data,
            ack, answered,
            "host rejected the cash-out (0x58 ID=00) — EGM drops to handpay")

    # -- 2b: approve path ---------------------------------------------------
    # Sanity ceiling FIRST: an implausible amount (firmware glitch / spoofed
    # but CRC-valid 0x57) is NEVER minted or approved — reject it to handpay.
    ceiling = (max_cashout_cents if max_cashout_cents is not None
               else getattr(store, "MAX_TICKET_CENTS", DEFAULT_MAX_TICKET_CENTS))
    if pend.amount_cents > ceiling:
        sleep(pace)
        ack, answered, _st = send_58(VALIDATION_REJECT, b"\x00" * 8)
        return SystemValidationResult(
            SystemValidationOutcome.REJECTED, pend.cashout_type,
            pend.amount_cents, None, VALIDATION_REJECT, None, packet.data,
            ack, answered,
            f"cash-out {pend.amount_cents}c exceeds the {ceiling}c ceiling — "
            "rejected to handpay (0x58 ID=00); never minted")

    reused = False
    vn: Optional[str] = None
    mint_sid = system_id
    if payload_style == PAYLOAD_STYLE_TYPE_AMOUNT:
        # ArduinoTITO regime: the EGM generates its OWN validation number, so a
        # host mint would be a permanent orphan pending that never matches the
        # EGM-reported 0x4D number. Do NOT mint here — reconciliation runs off
        # the EGM's number at the 0x3D/0x4D capture (recorded once, no systemId).
        pass
    else:
        # bcd_number (spec/system) regime: the HOST is the authority. Be
        # IDEMPOTENT per physical cash-out — if this (address, amount) already
        # has an unreconciled mint (0x57 re-raised after a lost 0x58/ack),
        # RE-SEND that same number rather than minting a duplicate.
        existing = store.find_open_pending(address, pend.amount_cents)
        if existing is not None:
            vn = existing["validationNumber"]
            mint_sid = existing.get("systemId", system_id)
            reused = True
        else:
            try:
                mint = store.mint_validation_number(pend.amount_cents, address,
                                                    system_id)
            except (ValidationMintError, ValueError) as e:
                # Durability failure / exhaustion / bad config: fail SAFE — no
                # 0x58 approve is ever sent for a number we could not persist.
                return SystemValidationResult(
                    SystemValidationOutcome.TIMEOUT, pend.cashout_type,
                    pend.amount_cents, None, None, None, packet.data, None,
                    False,
                    f"mint failed ({e}) — NO 0x58 approve sent (fail-safe; "
                    "the EGM re-raises 0x57 until serviced or handpays)")
            vn = mint["validation_number"]
            mint_sid = mint["system_id"]

    payload8 = build_validation_payload(payload_style, vn or "0",
                                        pend.cashout_type, pend.amount_raw)
    # the validation system ID, BCD (Table 15.8a); 01 for the default
    id_byte = (int_to_bcd(int(mint_sid), 1)[0]
               if isinstance(mint_sid, int) and 1 <= mint_sid <= 99
               else VALIDATION_APPROVE)
    sleep(pace)
    ack, ack_confirmed, status58 = send_58(id_byte, payload8)
    if status58 == VALIDATION_STATUS_IMPROPER:
        # "Improper validation rejected": a long poll 58 with a number "must
        # be preceded by a valid long poll 57 within the same cashout"
        # (§15.8). Read 57 again and send it once more.
        sleep(pace)
        transact_reread(transport, build_send_pending_cashout_poll(address))
        sleep(pace)
        ack, ack_confirmed, status58 = send_58(id_byte, payload8)
        if status58 == VALIDATION_STATUS_IMPROPER:
            return SystemValidationResult(
                SystemValidationOutcome.REFUSED, pend.cashout_type,
                pend.amount_cents, vn, id_byte, payload_style, packet.data,
                ack, ack_confirmed,
                f"the machine refused validation number {vn} twice (0x58 "
                "status 81, improper validation) — no ticket will print")
    if status58 == VALIDATION_STATUS_NOT_IN_CASHOUT:
        return SystemValidationResult(
            SystemValidationOutcome.NO_DATA, pend.cashout_type,
            pend.amount_cents, vn, id_byte, payload_style, packet.data,
            ack, ack_confirmed,
            "0x58 answered 80: the machine is no longer in a cash-out — "
            f"nothing prints against {vn}")
    if payload_style == PAYLOAD_STYLE_TYPE_AMOUNT:
        detail = (f"approved cash-out {pend.amount_cents}c: 0x58 ID=01 sent "
                  "[type_amount] — EGM supplies its own validation number")
    else:
        detail = (f"approved cash-out {pend.amount_cents}c: "
                  f"{'re-sent' if reused else 'minted'} {vn} "
                  f"(system_id {mint_sid}), 0x58 ID=01 sent [{payload_style}]")
    if not ack_confirmed:
        detail += " — no 0x58 ack yet (watch for the 0x3D print completion)"
    return SystemValidationResult(
        SystemValidationOutcome.SERVICED, pend.cashout_type, pend.amount_cents,
        vn, id_byte, payload_style, packet.data,
        ack if ack_confirmed else None, ack_confirmed, detail)


# --------------------------------------------------------------------------
# Ticket-in: exception 0x67 -> 0x70 read -> adjudicate -> 0x71 -> confirm
# --------------------------------------------------------------------------

class RedemptionOutcome(enum.Enum):
    REDEEMED = "redeemed"             # authorized, machine credited, closed
    DENIED = "denied"                 # host rejected (store said no)
    MACHINE_REJECTED = "machine_rejected"  # host authorized, EGM refused —
                                      # ticket reset to issued (still valid)
    UNCONFIRMED = "unconfirmed"       # authorized but completion never seen;
                                      # store stays redeemPending (retryable)
    NO_TICKET = "no_ticket"           # 0x70 answered: nothing in escrow
    TIMEOUT = "timeout"               # silence where an answer was required


@dataclass(frozen=True)
class TicketRedemptionResult:
    outcome: RedemptionOutcome
    validation_number: Optional[str]
    amount_cents: int                 # amount authorized (0 when denied)
    machine_status: Optional[int]     # last 0x71 status byte seen
    store_reason: str                 # TicketStore's adjudication reason
    confirmed_by_exception: bool      # 0x68 observed
    detail: str

    @property
    def ok(self) -> bool:
        return self.outcome is RedemptionOutcome.REDEEMED


def redeem_inserted_ticket(transport, address: int, store: TicketStore,
                           protocol: Optional[SASProtocol] = None,
                           pace: float = SAS_POLL_FLOOR,
                           sleep: Callable[[float], None] = time.sleep,
                           confirm_polls: int = 10,
                           on_exception: Optional[Callable[[int], None]] = None,
                           on_frame: Optional[Callable[[bytes], None]] = None
                           ) -> TicketRedemptionResult:
    """Run one ticket-in redemption cycle (believed frames — module
    docstring; ALL of it VERIFY_ON_BENCH). Trigger it on exception 0x67.

    THE ADJUDICATION IS THE FEATURE: the 0x71 authorize is never sent unless
    the TicketStore authorized it — an unknown, already-redeemed, in-flight-
    elsewhere, or voided ticket draws an explicit 0x71 REJECT (amount 0),
    and silence/corruption anywhere is a typed result, never an authorize.

    Pacing: `sleep(pace)` between consecutive polls (SAS_POLL_FLOOR default).
    on_exception / on_frame mirror the handpay module: non-0x68 exceptions
    and pending long-poll results drained during the confirm window are
    handed up so the wrapping poller can dispatch them normally."""
    proto = protocol or _PROTO

    # -- 1: read the escrowed ticket -----------------------------------------
    # Every poll of this cycle is asked for again when its answer arrives
    # damaged or not at all (§3.2, transact_reread; audit item 19). For the
    # authorising 71 the document says so itself: "the host may also request
    # the current ticket status by resending the exact same command".
    resp, _h, _n = transact_reread(transport, build_ticket_data_poll(address))
    packet = _classify_frame(resp, address, CMD_SEND_TICKET_VALIDATION_DATA,
                             proto)
    if packet is None:
        return TicketRedemptionResult(
            RedemptionOutcome.TIMEOUT, None, 0, None, "", False,
            "silence/corrupt frame on the 0x70 escrow read — no 0x71 sent")
    try:
        ticket = parse_ticket_in_data(packet.data)
    except ValueError as e:
        return TicketRedemptionResult(
            RedemptionOutcome.TIMEOUT, None, 0, None, "", False,
            f"malformed 0x70 response ({e}) — no 0x71 sent")
    if not ticket.validation_number or \
            not ticket.validation_number.strip("0"):
        return TicketRedemptionResult(
            RedemptionOutcome.NO_TICKET, None, 0, ticket.status, "", False,
            "0x70 reports nothing in escrow")
    vn = ticket.validation_number
    if not vn.isdigit() or len(vn) > 16:
        # A non-BCD nibble (hexed to a-f) or an overlong validation field
        # can never match a stored ticket AND cannot be echoed into a 0x71
        # frame — treat it like any other malformed frame: a typed result,
        # never a crash (and never an authorize).
        return TicketRedemptionResult(
            RedemptionOutcome.TIMEOUT, None, 0, ticket.status, "", False,
            f"malformed validation data in the 0x70 response ({vn!r}: "
            "non-BCD or wider than 16 digits) — treated as silence; "
            f"no 0x71 sent [0x70 raw={packet.data.hex()}]")
    vn = _canonical_vn(vn)

    # -- 2: adjudicate against the store --------------------------------------
    # validation_raw rides along: the hub authority keys CROSS-MACHINE
    # tickets by the full scanned barcode digits (an AVP voucher's 18 random
    # digits — its trailing 16 are meaningless as a vn); a local TicketStore
    # ignores it.
    decision = store.authorize_redemption(address, vn, ticket.amount_cents,
                                          validation_raw=ticket.validation_raw)

    # -- 3: authorize or deny --------------------------------------------------
    sleep(pace)
    if not decision["authorized"]:
        resp, _h, _n = transact_reread(transport, build_redeem_ticket_poll(
            address, REDEEM_REJECT, 0, ticket.validation_raw))
        status = _redeem_status(resp, address, proto)
        return TicketRedemptionResult(
            RedemptionOutcome.DENIED, vn, 0, status, decision["reason"],
            False, f"host denied redemption: {decision['reason']}")

    amount = decision["amount_cents"]
    # A restricted promotional ticket goes back in as restricted credit:
    # transfer code 01 (SAS 6.02 Table 15.12c). The frame is otherwise the
    # proven cashable one. The restricted expiration and pool id fields are
    # left off, which the document allows: "If omitted, the default
    # expiration and pool ID 0000 will be used" — the same pool a
    # restricted AFT push lands in (2026-11-16).
    auth_frame = build_redeem_ticket_poll(
        address, REDEEM_AUTHORIZE_RESTRICTED if decision.get("restricted")
        else REDEEM_AUTHORIZE_CASHABLE, amount, ticket.validation_raw)
    resp, _h, _n = transact_reread(transport, auth_frame)
    status = _redeem_status(resp, address, proto)
    # the exact wire, so a 0x71 the machine refuses can be matched to what
    # it expects (0x81 = validation number does not match)
    _raw = (f" [0x70resp={packet.data.hex()} 0x71sent={auth_frame.hex()} "
            f"0x71resp={resp.hex() if resp else 'none'}]")

    def closed(redeemed: bool, confirmed: bool, detail: str):
        store.close_redemption(address, vn, redeemed=redeemed,
                               validation_raw=ticket.validation_raw)
        return TicketRedemptionResult(
            RedemptionOutcome.REDEEMED if redeemed
            else RedemptionOutcome.MACHINE_REJECTED, vn, amount, status,
            decision["reason"], confirmed, detail)

    kind = redeem_status_kind(status)
    if kind == "redeemed":
        return closed(True, False,
                      "machine credited the ticket immediately" + _raw)
    if kind == "failed":
        return closed(False, False,
                      f"machine rejected the authorized redemption "
                      f"(status 0x{status:02X}); ticket reset to issued"
                      + _raw)

    confirmed = False
    for attempt in (1, 2):
        confirmed = _await_transfer_complete(
            transport, address, proto, pace, sleep, confirm_polls,
            on_exception, on_frame) or confirmed
        sleep(pace)
        resp, _h, _n = transact_reread(
            transport, build_redeem_interrogate_poll(address))
        status = _redeem_status(resp, address, proto)
        kind = redeem_status_kind(status)
        if kind == "redeemed":
            return closed(True, confirmed,
                          "redemption confirmed by interrogate")
        if kind == "failed":
            return closed(False, confirmed,
                          f"interrogate reports rejection (0x{status:02X}); "
                          "ticket reset to issued" + _raw
                          + f" [interrogateResp="
                            f"{resp.hex() if resp else 'none'}]")
        if kind != "waiting" or attempt == 2:
            break
        # 20: the machine never got its long poll 71 (our authorisation or
        # its answer was lost). Send it again, once, and wait once more.
        sleep(pace)
        resp, _h, _n = transact_reread(transport, auth_frame)
        status = _redeem_status(resp, address, proto)
        kind = redeem_status_kind(status)
        if kind == "redeemed":
            return closed(True, confirmed,
                          "machine credited the ticket on the second "
                          "authorisation")
        if kind == "failed":
            return closed(False, confirmed,
                          f"machine rejected the authorized redemption "
                          f"(status 0x{status:02X}); ticket reset to issued")
    return TicketRedemptionResult(
        RedemptionOutcome.UNCONFIRMED, vn, amount, status,
        decision["reason"], confirmed,
        "authorized but completion not confirmed yet — the ticket stays "
        "redeemPending and the machine is asked again until it gives a "
        "final status")


def read_redemption_status(transport, address: int,
                           protocol: Optional[SASProtocol] = None):
    """One 71/FF interrogation, asked for again if its answer is damaged.
    Returns the parsed TicketInData (status, amount, validation number) or
    None. The answer names the ticket, so a redemption can be closed from
    it even by a satellite that was restarted in the middle of one."""
    proto = protocol or _PROTO
    resp, _h, _n = transact_reread(
        transport, build_redeem_interrogate_poll(address))
    packet = _classify_frame(resp, address, CMD_REDEEM_TICKET, proto)
    if packet is None:
        return None
    try:
        return parse_ticket_in_data(packet.data)
    except ValueError:
        return None


def _redeem_status(resp: bytes, address: int,
                   proto: SASProtocol) -> Optional[int]:
    """Machine-status byte from a 0x71 response, or None for silence/corrupt
    frames (never guessed)."""
    packet = _classify_frame(resp, address, CMD_REDEEM_TICKET, proto)
    if packet is None:
        return None
    try:
        return parse_ticket_in_data(packet.data).status
    except ValueError:
        return None


def _await_transfer_complete(transport, address, proto, pace, sleep,
                             confirm_polls, on_exception, on_frame) -> bool:
    """General-poll (paced) until the 0x68 'ticket transfer complete'
    exception. The loop itself is core/sas_wait.await_exception."""
    return await_exception(transport, address, proto,
                           (TICKET_TRANSFER_COMPLETE_EXCEPTION,), pace, sleep,
                           confirm_polls, on_exception, on_frame)


# --------------------------------------------------------------------------
# Poller integration
# --------------------------------------------------------------------------

class SASTITOHost:
    """Wires the TITO flows into a SASPoller without modifying it: chains
    the poller's on_typed_event so that

      * exception 0x3D / 0x3E  -> capture_printed_ticket -> on_ticket_issued
      * exception 0x57 (auto_service) -> service_system_validation
        -> on_system_validation  (the 0x3D print completion that follows is
        captured by the 0x3D branch above and reconciled to one ticket)
      * exception 0x67 (auto_redeem) -> redeem_inserted_ticket -> on_redemption

    run inline in the poll loop (long polls double as the implied ACK, so
    this is protocol-safe — the handpay wrapper's precedent). Any previously
    installed on_typed_event still fires for every event. Callbacks are
    isolated via the poller's _safe so a bad handler can't kill the loop.

    Reentrancy: exceptions drained DURING a capture/redeem cycle re-enter
    this handler via the poller's dispatch; a _busy guard makes those
    observe-only (the FIFO event still reaches on_typed_event listeners, and
    a skipped 0x3D is recoverable — capture is idempotent and can be re-run
    via capture()).
    """

    def __init__(self, poller, store: TicketStore, auto_redeem: bool = True,
                 auto_service: bool = True,
                 on_ticket_issued: Optional[
                     Callable[[int, TicketCaptureResult], None]] = None,
                 on_redemption: Optional[
                     Callable[[int, TicketRedemptionResult], None]] = None,
                 on_system_validation: Optional[
                     Callable[[int, "SystemValidationResult"], None]] = None,
                 on_validation_seeded: Optional[
                     Callable[[int, "ValidationIdResult"], None]] = None,
                 auto_seed: bool = True,
                 seed_sequence: bytes = b"\x00\x00\x01",
                 seed_cooldown: float = 5.0,
                 system_id: int = DEFAULT_SYSTEM_ID,
                 payload_style: str = DEFAULT_PAYLOAD_STYLE,
                 pace: float = SAS_POLL_FLOOR,
                 sleep: Callable[[float], None] = time.sleep,
                 machine_id: Optional[bytes] = None):
        self.poller = poller
        self.store = store
        # The 3 wire bytes of this machine's validation ID for long poll 4C
        # (validation_machine_id). None keeps the old one, the same on every
        # machine: 00 00 <system id>.
        if machine_id is not None and (len(machine_id) != 3
                                       or not any(machine_id)):
            raise ValueError("the validation machine ID is 3 bytes, not zero")
        self.machine_id = machine_id
        self.auto_redeem = auto_redeem
        self.auto_service = auto_service
        self.on_ticket_issued = on_ticket_issued
        self.on_redemption = on_redemption
        self.on_system_validation = on_system_validation
        self.on_validation_seeded = on_validation_seeded
        self.auto_seed = auto_seed
        self._seed_seq = seed_sequence
        self._seed_cooldown = seed_cooldown
        self._last_seed_at = 0.0
        self.system_id = system_id
        self.payload_style = payload_style
        self.pace = pace
        self.sleep = sleep
        self._busy = False
        # A redemption the machine accepted (status 40) whose final status
        # has not been read. SAS 6.02 §15.12: the machine raises exception
        # 68 when the ticket is stacked or returned and "must reissue
        # exception 68 every fifteen seconds until the host polls for and
        # ACKs the completion status. ... It is the responsibility of the
        # host to properly complete a ticket transaction."
        self.open_redemption = None    # {"vn", "amount"}
        self.transfer_complete_seen = False    # exception 68 came in
        self._ask_at = 0.0
        self._asked = 0
        self.clock = time.monotonic
        self._prev_typed = poller.on_typed_event
        poller.on_typed_event = self._typed_event

    # -- manual entry points (bridge/server commands) -------------------------

    def capture(self) -> TicketCaptureResult:
        """Run the ticket-out capture now (idempotent)."""
        p = self.poller
        result = capture_printed_ticket(p.transport, p.address, self.store,
                                        p.protocol, self.pace, self.sleep)
        p._safe(self.on_ticket_issued, p.address, result)
        return result

    def redeem(self) -> TicketRedemptionResult:
        """Run one ticket-in redemption cycle now."""
        p = self.poller
        # "If the host sends long poll 70 without polling for the
        # completion status of the previous ticket, that status will be
        # lost" (§15.12) — so ask about an open one before reading the next.
        if self.open_redemption is not None or self.transfer_complete_seen:
            self.resolve_open_redemption()
        result = redeem_inserted_ticket(
            p.transport, p.address, self.store, p.protocol,
            pace=self.pace, sleep=self.sleep,
            on_exception=p._dispatch_event,
            on_frame=p._handle_long_poll_result)
        if result.outcome is RedemptionOutcome.UNCONFIRMED:
            self.open_redemption = {"vn": result.validation_number,
                                    "amount": result.amount_cents}
            self._asked = 0
            self._ask_at = self.clock() + REDEMPTION_RETRY_SEC[0]
        p._safe(self.on_redemption, p.address, result)
        return result

    def resolve_open_redemption(self) -> Optional[TicketRedemptionResult]:
        """Ask the machine (71/FF) how the last redemption ended and close
        the hub's row from the answer. Returns the END, or None while there
        is still none. Poll thread only.

        The answer carries the ticket's own validation number, so this also
        closes a redemption left open by a satellite that was restarted (it
        no longer remembers which ticket it was)."""
        p = self.poller
        self.transfer_complete_seen = False
        data = read_redemption_status(p.transport, p.address, p.protocol)
        kind = redeem_status_kind(data.status if data is not None else None)
        if kind not in ("redeemed", "failed"):
            return None
        known = self.open_redemption or {}
        vn = None
        if data.validation_number and data.validation_number.isdigit() \
                and data.validation_number.strip("0"):
            vn = _canonical_vn(data.validation_number)
        vn = vn or known.get("vn")
        self.open_redemption = None
        if not vn:
            return None             # a final status with no ticket named
        redeemed = kind == "redeemed"
        self.store.close_redemption(p.address, vn, redeemed=redeemed,
                                    validation_raw=data.validation_raw)
        amount = known.get("amount")
        if amount is None:
            amount = data.amount_cents or 0
        result = TicketRedemptionResult(
            RedemptionOutcome.REDEEMED if redeemed
            else RedemptionOutcome.MACHINE_REJECTED, vn, amount, data.status,
            "", True,
            ("ended late: the machine reports the ticket redeemed"
             if redeemed else
             f"ended late: the machine reports the redemption failed "
             f"(status 0x{data.status:02X}); ticket reset to issued"))
        p._safe(self.on_redemption, p.address, result)
        return result

    def service_open_redemption(self) -> Optional[TicketRedemptionResult]:
        """Poll thread, BETWEEN polls: follow up a redemption with no final
        status. Asks when exception 68 has come in, and otherwise on a wait
        that grows to fifteen seconds while one is open."""
        if self._busy:
            return None
        if self.open_redemption is None and not self.transfer_complete_seen:
            return None
        now = self.clock()
        if not self.transfer_complete_seen and now < self._ask_at:
            return None
        self._busy = True
        try:
            result = self.resolve_open_redemption()
        finally:
            self._busy = False
        if result is None and self.open_redemption is not None:
            self._asked += 1
            self._ask_at = now + REDEMPTION_RETRY_SEC[
                min(self._asked, len(REDEMPTION_RETRY_SEC) - 1)]
        return result

    def service_validation(self, approve: bool = True
                           ) -> SystemValidationResult:
        """Service a system-validation request now (0x57 read -> mint ->
        0x58). Idempotent-safe: silence never approves; the 0x3D completion is
        reconciled by capture(). Call with approve=False to force a handpay."""
        p = self.poller
        result = service_system_validation(
            p.transport, p.address, self.store, p.protocol,
            self.pace, self.sleep, system_id=self.system_id,
            payload_style=self.payload_style, approve=approve)
        p._safe(self.on_system_validation, p.address, result)
        return result

    def configure_validation_id(self, validation_id: bytes,
                                sequence: bytes = b"\x00\x00\x01"
                                ) -> ValidationIdResult:
        """Bring-up helper: push the VGM validation ID (guide §4.6.1)."""
        p = self.poller
        return configure_validation_id(p.transport, p.address,
                                       validation_id, sequence, p.protocol)

    def seed_validation_id(self, force: bool = False
                           ) -> Optional[ValidationIdResult]:
        """Assign the enhanced-validation ID (0x4C) so an EGM stalled on
        exception 0x3F ('validation ID not configured') can mint its OWN
        validation numbers and print without the host — the whole point of
        enhanced vs. system validation. Cooldown-gated: a 0x3F still queued for
        a poll or two after a successful seed must not re-fire 0x4C and reset
        the starting sequence.

        The two fields (SAS 6.02 Table 15.6a) are 3-byte BINARY, so they go
        least significant byte first (§2.2.3). The machine ID is this
        machine's own (`machine_id`, from validation_machine_id): the
        document asks that IDs be allocated so that validation numbers stay
        unique, and a secure enhanced number is computed from the ID and
        the sequence alone. It says nothing about a "system ID namespace";
        in a secure enhanced number the system ID is always 00. Until
        v0.3.16 every machine was given the same ID (00 00 <system id>),
        so two machines were kept apart only by when they were seeded.

        REPLAY GUARD (2026-07-12): the enhanced vn is a DETERMINISTIC function
        of (validation ID, sequence) — re-seeding a RAM-cleared EGM with the
        same ID + a fixed sequence made it REPRINT its old barcodes in order
        (live-proven: the 07-09 series replayed verbatim on 07-12 and collided
        with redeemed/void tickets). With the default static seed_sequence the
        starting sequence is now derived from wall-clock seconds instead, so
        every re-seed starts in fresh number space. Wraps every ~194 days
        (3-byte space); the hub's collision re-issue is the backstop. An
        explicitly-passed non-default seed_sequence still wins (bench use).
        That value is sent least significant byte first, like any binary
        field; until v0.3.16 it went most significant first, so what the
        machine took as its starting sequence was the clock value with its
        bytes reversed, which jumps about instead of rising."""
        now = time.monotonic()
        if not force and (now - self._last_seed_at) < self._seed_cooldown:
            return None
        self._last_seed_at = now
        vid = self.machine_id or bytes([0, 0, self.system_id & 0xFF])
        seq = self._seed_seq
        if seq == b"\x00\x00\x01":
            # replay guard: fresh number space per seed (see docstring); the
            # EGM echoes the accepted sequence in the ValidationIdResult the
            # on_validation_seeded callback logs, so the value is journaled.
            seq = (int(time.time()) & 0xFFFFFF).to_bytes(3, "little")
        result = self.configure_validation_id(vid, seq)
        self.poller._safe(self.on_validation_seeded, self.poller.address,
                          result)
        return result

    # -- event chaining --------------------------------------------------------

    def _typed_event(self, address: int, info) -> None:
        if self._prev_typed is not None:
            self.poller._safe(self._prev_typed, address, info)
        if address != self.poller.address or self._busy:
            return
        code = info.code
        if code == TICKET_TRANSFER_COMPLETE_EXCEPTION:
            # Not answered here: service_open_redemption() reads the status
            # between polls (no long poll inside the exception drain).
            self.transfer_complete_seen = True
            return
        if code in (TICKET_PRINTED_EXCEPTION, HANDPAY_VALIDATED_EXCEPTION):
            self._busy = True
            try:
                self.capture()
            finally:
                self._busy = False
        elif code == SYSTEM_VALIDATION_REQUEST_EXCEPTION and self.auto_service:
            self._busy = True
            try:
                self.service_validation()
            finally:
                self._busy = False
        elif code == TICKET_INSERTED_EXCEPTION and self.auto_redeem:
            self._busy = True
            try:
                self.redeem()
            finally:
                self._busy = False
        elif code == VALIDATION_ID_NOT_CONFIGURED_EXCEPTION and self.auto_seed:
            self._busy = True
            try:
                self.seed_validation_id()
            finally:
                self._busy = False
