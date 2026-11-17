"""
Host-side AFT (Advanced Funds Transfer) — registration, lock, transfer,
interrogation — rebuilt 2026-07-07 on the core/sas_handpay_reset.py template:
pure frame builders/parsers, a pure transaction state machine, transport-
driven orchestration functions with typed results, a poller wrapper
(AFTHost), and MockSASSerialPort tests (tests/test_aft_handler.py).

PROVENANCE — VERIFY_ON_BENCH, all of it. The Montana guide (the only citable
SAS document on disk) documents NO AFT long polls; its only AFT trace is the
'Advanced Fund Transfer' feature bit in the 0xA0 response (§4.4.28). Every
command byte, frame layout, and status code below is a believed value from
the full SAS spec's AFT chapter (not on disk) and must be adjudicated on a
real AFT-capable machine before being treated as fact. Record results in
COMPATIBILITY.md.

THE RECONCILIATION (the old module's 0x74-vs-0x75 conflict, resolved
honestly): the pre-rebuild handler docstring said "transfer funds command
(74)" while sending 0x75, because the old SASCommand enum was SHIFTED one
code (0x72=register, 0x73=lock, 0x74=request transfer, 0x75=transfer funds,
0x76=host cashout, 0x77=status). Believed-correct mapping, now also in
core/sas_protocol.SASCommand:

    0x72  AFT transfer funds        — initiate / cancel / interrogate,
                                      selected by the transfer-code byte
    0x73  AFT register gaming machine
    0x74  AFT game lock and status request  — this IS the interrogate poll
                                      for lock/availability state.
                                      Host cashout is EGM-initiated via
                                      exceptions 0x6A/0x6B (see
                                      core/sas_exceptions.BENCH_EXCEPTIONS).
    0x75  Set AFT Receipt Data (spec §8.11.1) — host->EGM config poll
    0x76  Set Custom AFT Ticket Data (spec §8.12) — host->EGM config poll

    CORRECTION 2026-07-07: 0x75/0x76 ARE real spec-defined AFT config long
    polls (the on-disk spec excerpt's TOC §8.11.1 + change-log "Added section
    8.12, set custom AFT ticket data (long poll 76)" confirm them). The old
    "there is no 0x75/0x76" claim here was wrong. We simply don't IMPLEMENT
    them yet — they're host->EGM config, not part of the transfer choreography.
    They matter for AJ's tournament-win receipt: the receipt template lines
    are pushed via 0x75 (receipt data) / 0x76 (custom ticket data) and printed
    on the machine when a 0x72 transfer sets its receipt-request flag. Build
    them from the FULL spec §8.11/§8.12 (NOT in our on-disk excerpt) when the
    receipt feature is built.

The transfer choreography (mirrors the settled two-step handpay rule:
nothing irreversible goes out until the machine consents):

    with a registration key in use: interrogate registration (73/FF)
                                      -> must be REGISTERED. With a key of
                                         all zeros this step is skipped
                                         (SAS 6.02 §8.1, see ZERO_KEY)
 -> request game lock (74/00)         -> must reach LOCKED (re-interrogate
                                         74/FF while PENDING)
 -> issue the transfer (72/00)        -> immediate FULL/PARTIAL, or PENDING
 -> on PENDING: await exception 0x69 'AFT transfer complete', then
    interrogate (72/FF) for the final status
 -> stuck PENDING: one cancel attempt (72/80) then interrogate — a
    confirmed cancel is the ROLLED_BACK outcome.

UNITS: cents on the wire (5-byte BCD amount fields), matching the guide's
credit unit (§4.2). BCD helpers come from core/sas_meters (cited, with
regression vectors in the tests — the old module's 10x magnitude bugs came
from hand-rolled converters and test vectors that were never green).

No asyncio anywhere — the old handler referenced asyncio without importing
it (NameError on the host-cashout path); the rebuild is synchronous like the
rest of the SAS host stack (SBC-friendly, poll-loop-driven).
"""

import enum
import itertools
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from core.sas_protocol import SASProtocol, sas_crc
from core.sas_meters import SAS_POLL_FLOOR, bcd_to_int, int_to_bcd
from core.sas_wait import await_exception
from modules.aft.open_transfer import OpenTransferJournal
# The registration-INDEPENDENT asset source (CONTRACT assetSource): a 0x7B
# extended-validation-status READ (all-zero control -> never reconfigures)
# still reports the operator-set asset on a freshly RAM-cleared, NOT_REGISTERED
# machine. modules -> core is the right dependency direction.
from core.sas_machine_info import (
    build_extended_validation_status_poll, parse_extended_validation_status)

__all__ = [
    "AFT_CMD_TRANSFER", "AFT_CMD_REGISTER", "AFT_CMD_LOCK_STATUS",
    "REG_CODE_INITIALIZE", "REG_CODE_REGISTER", "REG_CODE_UNREGISTER",
    "REG_CODE_READ_CURRENT",
    "REG_STATUS_READY", "REG_STATUS_REGISTERED", "REG_STATUS_PENDING",
    "REG_STATUS_NOT_REGISTERED",
    "LOCK_CODE_REQUEST", "LOCK_CODE_CANCEL", "LOCK_CODE_INTERROGATE",
    "LOCK_STATUS_LOCKED", "LOCK_STATUS_PENDING", "LOCK_STATUS_NOT_LOCKED",
    "TRANSFER_CODE_FULL_ONLY", "TRANSFER_CODE_PARTIAL_OK",
    "TRANSFER_CODE_CANCEL", "TRANSFER_CODE_INTERROGATE",
    "TRANSFER_TYPE_HOST_TO_EGM", "TRANSFER_TYPE_EGM_TO_HOST",
    "TRANSFER_TYPE_WIN_TO_HOST",
    "TRANSFER_CONDITION_DEFAULT", "TRANSFER_CONDITION_TO_EGM",
    "TRANSFER_CONDITION_FROM_EGM",
    "registration_refusal",
    "TRANSFER_FLAGS_NONE", "AFT_FLAG_LOCK_REQUIRED", "AFT_FLAG_LOCK_AFTER",
    "HOST_CASHOUT_CONTROLLABLE", "HOST_CASHOUT_ENABLED", "HOST_CASHOUT_HARD",
    "AFT_FLAG_HOST_CASHOUT_CONTROL", "AFT_FLAG_HOST_CASHOUT_ENABLE",
    "AFT_FLAG_HOST_CASHOUT_HARD", "ALL_OF_IT_CENTS",
    "AVAIL_XFER_TO_EGM", "AVAIL_XFER_FROM_EGM", "AVAIL_XFER_TO_PRINTER",
    "AVAIL_XFER_WIN_PENDING_TO_HOST", "AVAIL_XFER_BONUS_TO_EGM",
    "AVAIL_XFER_LOCK_AFTER_OK",
    "AFT_ST_RECEIPT_PRINTER", "AFT_ST_PARTIAL_TO_HOST_OK",
    "AFT_ST_CUSTOM_TICKET", "AFT_ST_REGISTERED", "AFT_ST_IN_HOUSE_ENABLED",
    "AFT_ST_BONUS_ENABLED", "AFT_ST_DEBIT_ENABLED", "AFT_ST_ANY_AFT_ENABLED",
    "describe_available_transfers", "describe_aft_status",
    "AFT_TRANSFER_COMPLETE_EXCEPTION", "ZERO_KEY", "REGISTER_RETRY_SEC",
    "AFTStatus", "AFTRegistration", "AFTGameLockStatus",
    "AFTTransferRequest", "AFTTransferStatusData",
    "cents_to_bcd5", "bcd5_to_cents", "asset_number_bytes",
    "make_transaction_id",
    "build_aft_register_poll", "build_aft_register_interrogate_poll",
    "parse_aft_register_response",
    "build_aft_lock_poll", "parse_aft_lock_response",
    "build_aft_transfer_poll", "build_aft_transfer_interrogate_poll",
    "build_aft_cancel_poll", "parse_aft_transfer_response",
    "AFTTxnState", "AFTTxnEvent", "AFTStateError", "advance", "is_terminal",
    "AFTOutcome", "AFTTransferResult", "aft_register", "read_asset_number",
    "aft_transfer", "AFTHost",
]

# --------------------------------------------------------------------------
# Believed command bytes — VERIFY_ON_BENCH (module docstring has the story)
# --------------------------------------------------------------------------
AFT_CMD_TRANSFER = 0x72      # VERIFY_ON_BENCH
AFT_CMD_REGISTER = 0x73      # VERIFY_ON_BENCH
AFT_CMD_LOCK_STATUS = 0x74   # VERIFY_ON_BENCH

# 0x73 registration codes (host -> EGM)           VERIFY_ON_BENCH, all
REG_CODE_INITIALIZE = 0x00
REG_CODE_REGISTER = 0x01
REG_CODE_UNREGISTER = 0x80
REG_CODE_READ_CURRENT = 0xFF

# 0x73 registration status (EGM -> host)          VERIFY_ON_BENCH, all
REG_STATUS_READY = 0x00          # registration initialized / ready
REG_STATUS_REGISTERED = 0x01
REG_STATUS_PENDING = 0x40        # waiting for an operator to acknowledge at
                                 # the machine. SAS 6.02 §8.1: a machine only
                                 # answers this after the HOST asks for the
                                 # acknowledgement (73 with code 40), which
                                 # this code never sends. A machine cannot
                                 # impose it by itself.
REG_STATUS_NOT_REGISTERED = 0x80

# 0x74 lock codes (host -> EGM)                   VERIFY_ON_BENCH, all
LOCK_CODE_REQUEST = 0x00
LOCK_CODE_CANCEL = 0x80
LOCK_CODE_INTERROGATE = 0xFF

# 0x74 game lock status (EGM -> host)             VERIFY_ON_BENCH, all
LOCK_STATUS_LOCKED = 0x00
LOCK_STATUS_PENDING = 0x40
LOCK_STATUS_NOT_LOCKED = 0xFF

# 0x72 transfer codes (host -> EGM)               VERIFY_ON_BENCH, all
TRANSFER_CODE_FULL_ONLY = 0x00   # full transfer or nothing
TRANSFER_CODE_PARTIAL_OK = 0x01  # partial acceptable
TRANSFER_CODE_CANCEL = 0x80      # cancel the pending transfer
TRANSFER_CODE_INTERROGATE = 0xFF  # read current/most-recent status

# 0x72 transfer types — REAL SAS 6.02 Table 8.3d (transcribed 2026-07-09).
# The old constants here were wrong: 0x60 is DEBIT host->ticket (not in-house
# EGM->host, which is 0x80) and 0x80 is not the win type (0x90 is). The full
# table: 00 in-house host->EGM, 10 bonus-coin-out host->EGM, 11 bonus-jackpot
# host->EGM (forces attendant lockup), 20 in-house host->ticket, 40 DEBIT
# host->EGM, 60 DEBIT host->ticket, 80 in-house EGM->host, 90 win EGM->host.
TRANSFER_TYPE_HOST_TO_EGM = 0x00     # in-house, host -> machine (live-proven ✓)
TRANSFER_TYPE_EGM_TO_HOST = 0x80     # in-house, machine -> host
TRANSFER_TYPE_WIN_TO_HOST = 0x90     # win amount (in-house), machine -> host

# 0x74 transfer-condition byte — a BITMAP of the condition(s) to lock FOR
# (real SAS 6.02 Table 8.2a; adjudicated 2026-07-09). 0x00 names NO condition,
# so a real machine has nothing to lock for and answers 0xFF NOT_LOCKED — THE
# root cause of the 2026-07-08 stuck lock. For a host->EGM push the one correct
# bit is bit 0. DEFAULT stays 0x00 only for the FF-interrogate, which ignores
# this field.
TRANSFER_CONDITION_DEFAULT = 0x00    # interrogate-only (field ignored on FF)
TRANSFER_CONDITION_TO_EGM = 0x01     # bit 0 "transfer TO gaming machine OK"
TRANSFER_CONDITION_FROM_EGM = 0x02   # bit 1 "transfer FROM gaming machine OK":
#                                      the condition to lock for before a
#                                      host-initiated pull (type 80). It has
#                                      nothing to do with host cashout, which
#                                      is not set with 0x74 at all (see the
#                                      host cashout constants below).

# 0x72 transfer-flag byte — bits (real SAS 6.02 Table 8.3d; adjudicated
# 2026-07-09). bit 6 = "accept transfer ONLY if the game is locked": with it
# CLEAR (our push default) the host tells the machine NO lock is required — the
# transfer lands immediately when idle, else the EGM escrows it and reports
# PENDING (0x40), completing at idle. bit 4 = "lock AFTER transfer" (binds the
# 0x72 trailing lock-timeout; NOT a pre-transfer self-lock). 0x00 = lockless.
TRANSFER_FLAGS_NONE = 0x00           # no flags — the lockless host->EGM push
AFT_FLAG_LOCK_REQUIRED = 0x40        # bit 6 — host requires a prior game lock
AFT_FLAG_LOCK_AFTER = 0x10           # bit 4 — hold a lock AFTER the transfer

# Host cashout (SAS 6.02 §8.7). "Cashout to host" is the machine treating
# the host as a cashout device: with it enabled, a press of CASH OUT raises
# exception 6A (and a win that must be cashed out raises 6B) and the host
# answers with a 0x72. It is a setting on the machine, reported in one byte
# of the 0x74 answer and in the flags byte of every 0x72 answer:
HOST_CASHOUT_CONTROLLABLE = 0x01     # bit 0: the host may change the enable
HOST_CASHOUT_ENABLED = 0x02          # bit 1: cashout to host enabled NOW
HOST_CASHOUT_HARD = 0x04             # bit 2: failure tilts (soft: cash out
#                                      another way). Only valid while enabled.
# The host asks for a change in the flags of a 0x72 it sends; the change
# takes effect when that transfer completes, and a transfer for a total of
# zero may be sent for that purpose alone:
AFT_FLAG_HOST_CASHOUT_CONTROL = 0x01  # bit 0: set the enable to bit 1
AFT_FLAG_HOST_CASHOUT_ENABLE = 0x02   # bit 1: the enable (ignored if bit 0=0)
AFT_FLAG_HOST_CASHOUT_HARD = 0x04     # bit 2: the mode  (ignored if bit 0=0)
# Until v0.3.14 this module took "available transfers" bit 1 of the 0x74
# answer (transfer FROM gaming machine OK) for the host cashout enable. That
# bit only says a host-initiated pull might be accepted right now.

# §8.7: "The amount fields may all be set to 9999999999, with partial
# transfer allowed, to transfer the entire cashout amount."
ALL_OF_IT_CENTS = 9999999999

# The completion exception (SAS 6.02 Table A-1 and §8.3; seen on a WMS
# Bluebird 2). Named in sas_exceptions.BENCH_EXCEPTIONS.
AFT_TRANSFER_COMPLETE_EXCEPTION = 0x69

_PROTO = SASProtocol()               # stateless; safe to share

REGISTRATION_KEY_LEN = 20            # VERIFY_ON_BENCH

# SAS 6.02 §8.1: "Registration should not be used for in-house and bonus
# transactions. These transactions should use a registration key of all
# zeros." Table 8.3a reads that key as "registration not required", and
# registration itself is "required for debit transfers" (status 88).
#
# What this code does with it (audit item 17): a machine that registers is
# sent its key, as it always was, because every machine this has run on
# works that way and none has been tried the other way. A machine that
# cannot be registered used to get no transfer at all; it now gets the
# transfer with this key.
ZERO_KEY = bytes(REGISTRATION_KEY_LEN)

# After a registration that did not complete, how long the transfer path
# leaves it alone before trying again. The hub keeps asking on its own
# schedule (the aft_register command) whatever this says.
REGISTER_RETRY_SEC = 300.0
ASSET_NUMBER_LEN = 4                 # VERIFY_ON_BENCH
POS_ID_LEN = 4                       # VERIFY_ON_BENCH
MAX_TRANSACTION_ID_LEN = 20          # VERIFY_ON_BENCH


class AFTStatus(enum.IntEnum):
    """0x72 transfer-status codes — REAL SAS 6.02 Table 8.3e, transcribed
    verbatim 2026-07-09 (genuine 6.02 text; 0x82 additionally live-confirmed
    on the BB2 the same day). The pre-2026-07-09 enum here was WRONG — the
    whole 0x87..0x8A block was shifted one code, 0x82 was labelled "TIMEOUT"
    (masking a real refusal for a debugging round), and INSUFFICIENT_FUNDS
    was pure fiction (no such code exists). The 3 MSbits categorize:
    000=success, 010=pending, 100=failed, 110=incompatible poll, 111=none."""
    FULL_TRANSFER_SUCCESSFUL = 0x00
    PARTIAL_TRANSFER_SUCCESSFUL = 0x01
    TRANSFER_PENDING = 0x40
    TRANSFER_CANCELED = 0x80              # cancelled by host (72/80)
    TRANSACTION_ID_NOT_UNIQUE = 0x81      # same as last logged transfer
    NOT_A_VALID_TRANSFER_FUNCTION = 0x82  # unsupported type/amount/index —
    #                                       ALSO what the BB2 answers when its
    #                                       in-house transfer class is disabled
    #                                       in machine config (live 2026-07-09)
    NOT_A_VALID_AMOUNT_OR_EXPIRATION = 0x83   # non-BCD etc.
    TRANSFER_AMOUNT_EXCEEDS_LIMIT = 0x84
    AMOUNT_NOT_EVEN_MULTIPLE_OF_DENOM = 0x85
    UNABLE_TO_PERFORM_PARTIAL_TO_HOST = 0x86
    UNABLE_TO_TRANSFER_AT_THIS_TIME = 0x87    # door open, tilt, disabled,
    #                                           cashout in progress, ...
    GAMING_MACHINE_NOT_REGISTERED = 0x88
    REGISTRATION_KEY_DOES_NOT_MATCH = 0x89
    NO_POS_ID = 0x8A                      # required for debit transfers
    NO_WON_CREDITS_AVAILABLE = 0x8B
    NO_DENOMINATION_SET = 0x8C            # can't convert cents to credits
    EXPIRATION_NOT_VALID_FOR_TICKET = 0x8D
    TICKET_DEVICE_NOT_AVAILABLE = 0x8E
    RESTRICTED_POOL_MISMATCH = 0x8F
    UNABLE_TO_PRINT_RECEIPT = 0x90
    INSUFFICIENT_RECEIPT_DATA = 0x91
    RECEIPT_NOT_ALLOWED_FOR_TYPE = 0x92
    ASSET_NUMBER_ZERO_OR_DOES_NOT_MATCH = 0x93
    GAME_NOT_LOCKED = 0x94                # transfer specified lock required
    TRANSACTION_ID_NOT_VALID = 0x95
    UNEXPECTED_ERROR = 0x9F
    NOT_COMPATIBLE_WITH_CURRENT_TRANSFER = 0xC0
    UNSUPPORTED_TRANSFER_CODE = 0xC1
    NO_TRANSFER_INFORMATION = 0xFF


# 0x74 response "available transfers" bits — REAL SAS 6.02 Table 8.2b
# (transcribed 2026-07-09; decoded live against the BB2 the same day:
# post-RAM-clear it advertised 0x04 = printer-only, proving the RAM clear
# had disabled the machine's in-house transfer config).
AVAIL_XFER_TO_EGM = 0x01              # transfer to gaming machine OK
AVAIL_XFER_FROM_EGM = 0x02            # transfer from gaming machine OK
AVAIL_XFER_TO_PRINTER = 0x04          # transfer to printer OK
AVAIL_XFER_WIN_PENDING_TO_HOST = 0x08  # win amount pending cashout to host
AVAIL_XFER_BONUS_TO_EGM = 0x10        # bonus award to gaming machine OK
AVAIL_XFER_LOCK_AFTER_OK = 0x80       # Lock-After-Transfer supported (§8.9)

# 0x74 response "AFT status" bits — REAL SAS 6.02 Table 8.2b.
AFT_ST_RECEIPT_PRINTER = 0x01         # printer available for receipts
AFT_ST_PARTIAL_TO_HOST_OK = 0x02      # partial transfer-to-host allowed
AFT_ST_CUSTOM_TICKET = 0x04           # custom ticket data supported
AFT_ST_REGISTERED = 0x08              # AFT registered
AFT_ST_IN_HOUSE_ENABLED = 0x10        # in-house transfers enabled (machine
#                                       config — a RAM clear TURNS THIS OFF)
AFT_ST_BONUS_ENABLED = 0x20           # bonus transfers enabled
AFT_ST_DEBIT_ENABLED = 0x40           # debit transfers enabled
AFT_ST_ANY_AFT_ENABLED = 0x80         # any AFT enabled


def describe_available_transfers(bits: int) -> str:
    """Human words for the 0x74 available-transfers bitmap (Table 8.2b)."""
    names = ((AVAIL_XFER_TO_EGM, "to-machine"),
             (AVAIL_XFER_FROM_EGM, "from-machine"),
             (AVAIL_XFER_TO_PRINTER, "to-printer"),
             (AVAIL_XFER_WIN_PENDING_TO_HOST, "win-pending-to-host"),
             (AVAIL_XFER_BONUS_TO_EGM, "bonus-to-machine"),
             (AVAIL_XFER_LOCK_AFTER_OK, "lock-after-ok"))
    on = [n for bit, n in names if bits & bit]
    return "+".join(on) if on else "none"


def describe_aft_status(bits: int) -> str:
    """Human words for the 0x74 AFT-status bitmap (Table 8.2b)."""
    names = ((AFT_ST_RECEIPT_PRINTER, "receipt-printer"),
             (AFT_ST_PARTIAL_TO_HOST_OK, "partial-to-host"),
             (AFT_ST_CUSTOM_TICKET, "custom-ticket"),
             (AFT_ST_REGISTERED, "registered"),
             (AFT_ST_IN_HOUSE_ENABLED, "in-house-ENABLED"),
             (AFT_ST_BONUS_ENABLED, "bonus-enabled"),
             (AFT_ST_DEBIT_ENABLED, "debit-enabled"),
             (AFT_ST_ANY_AFT_ENABLED, "any-aft"))
    on = [n for bit, n in names if bits & bit]
    if not bits & AFT_ST_IN_HOUSE_ENABLED:
        on.append("in-house-DISABLED")
    return "+".join(on) if on else "none"


# --------------------------------------------------------------------------
# BCD money helpers (cited converters from core/sas_meters; 5-byte wire form)
# --------------------------------------------------------------------------

def cents_to_bcd5(cents: int) -> bytes:
    """Amount in CENTS -> the 5-byte BCD wire field (max $99,999,999.99).
    $123.45 == 12345 cents == b'\\x00\\x00\\x01\\x23\\x45' — the regression
    vector the old tests got wrong by 10x (they read BCD 0000123450 as
    $123.45; it is $1,234.50)."""
    if cents < 0:
        raise ValueError("amounts are non-negative cents")
    return int_to_bcd(cents, 5)


def bcd5_to_cents(data: bytes) -> Optional[int]:
    """5-byte BCD wire field -> cents (None on a non-BCD nibble)."""
    if len(data) != 5:
        raise ValueError(f"amount field is 5 BCD bytes, got {len(data)}")
    return bcd_to_int(data)


def asset_number_bytes(asset_number: int) -> bytes:
    """Asset number int -> the 4-byte wire field. VERIFY_ON_BENCH: byte
    order is believed LITTLE-endian (SAS's convention for multi-byte binary
    fields — the CRC, the only capture-proven one, is LSB-first)."""
    return int(asset_number).to_bytes(ASSET_NUMBER_LEN, "little")


_TXN_COUNTER = itertools.count()


def make_transaction_id(clock: Callable[[], float] = time.time) -> str:
    """A fresh ASCII transaction id (17 chars, <= the 20-char wire limit):
    millisecond timestamp + a process-lifetime counter, so ids stay unique
    even when minted faster than the clock ticks. Injectable clock for
    tests."""
    return (f"CN{int(clock() * 1000) % 10**9:09d}"
            f"{next(_TXN_COUNTER) % 10**6:06d}")


def _crc_frame(body: bytes) -> bytes:
    return body + sas_crc(body).to_bytes(2, "little")


# --------------------------------------------------------------------------
# 0x73 — registration
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class AFTRegistration:
    """Parsed 0x73 response (VERIFY_ON_BENCH layout: [len][status]
    [asset 4][registration key 20][POS ID 4])."""
    status: int
    asset_number: bytes
    registration_key: bytes
    pos_id: bytes

    @property
    def registered(self) -> bool:
        return self.status == REG_STATUS_REGISTERED


def build_aft_register_poll(address: int, code: int, asset_number: bytes,
                            registration_key: bytes, pos_id: bytes) -> bytes:
    """0x73 initialize (00) or register (01), SAS 6.02 Table 8.1a:
    [addr][73][len][code][asset 4][key 20][POS ID 4][crc].

    Interrogate (FF) and unregister (80) carry the code alone, "omitting
    the remaining fields", and have their own builders. Until v0.3.16
    this one took code 80 and sent it with the full body, which a machine
    may treat as a poll out of sequence (nothing ever called it that
    way)."""
    if code not in (REG_CODE_INITIALIZE, REG_CODE_REGISTER):
        raise ValueError(f"registration code 0x{code:02X} does not take "
                         "the full body (interrogate and unregister have "
                         "their own builders)")
    if len(asset_number) != ASSET_NUMBER_LEN:
        raise ValueError("asset number is 4 wire bytes")
    if len(registration_key) != REGISTRATION_KEY_LEN:
        raise ValueError("registration key is exactly 20 bytes")
    if len(pos_id) != POS_ID_LEN:
        raise ValueError("POS ID is 4 wire bytes")
    data = bytes([code]) + asset_number + registration_key + pos_id
    body = bytes([address, AFT_CMD_REGISTER, len(data)]) + data
    return _crc_frame(body)


def build_aft_unregister_poll(address: int) -> bytes:
    """0x73 unregister, or cancel a registration cycle: [addr][73][len=01]
    [80][crc] (SAS 6.02 §8.1: "a registration code of 80 and omitting the
    remaining fields")."""
    return _crc_frame(bytes([address, AFT_CMD_REGISTER, 1,
                             REG_CODE_UNREGISTER]))


def build_aft_register_interrogate_poll(address: int) -> bytes:
    """0x73 read current registration — VERIFY_ON_BENCH:
    [addr][73][len=01][FF][crc]."""
    return _crc_frame(bytes([address, AFT_CMD_REGISTER, 1,
                             REG_CODE_READ_CURRENT]))


def parse_aft_register_response(data: bytes) -> AFTRegistration:
    """Parse SASPacket.data for 0x73: [len][status][asset 4][key 20]
    [POS ID 4]. Raises ValueError on structural mismatch.

    Bytes after the POS ID are ignored (SAS 6.02 §2.2.3: a host that
    receives a valid variable length response with more data than it
    expects "will process the portion of the message it understands and
    ignore the extra bytes"). A body shorter than the three fields, other
    than the status-only short form, is still refused."""
    if len(data) < 2:
        raise ValueError("register response too short")
    if data[0] != len(data) - 1:
        raise ValueError(f"length byte {data[0]} != {len(data) - 1}")
    status = data[1]
    rest = data[2:]
    need = ASSET_NUMBER_LEN + REGISTRATION_KEY_LEN + POS_ID_LEN
    if rest and len(rest) < need:
        raise ValueError(f"register body must be 0 or at least {need} bytes")
    if not rest:
        return AFTRegistration(status, b"", b"", b"")
    return AFTRegistration(
        status,
        rest[:ASSET_NUMBER_LEN],
        rest[ASSET_NUMBER_LEN:ASSET_NUMBER_LEN + REGISTRATION_KEY_LEN],
        rest[ASSET_NUMBER_LEN + REGISTRATION_KEY_LEN:need])


# --------------------------------------------------------------------------
# 0x74 — game lock & status
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class AFTGameLockStatus:
    """Parsed 0x74 response (VERIFY_ON_BENCH layout: [len][asset 4]
    [game lock status][available transfers][host cashout status][AFT status]
    [max buffer index][cashable 5 BCD][restricted 5 BCD][nonrestricted
    5 BCD][transfer limit 5 BCD][restricted expiration 4][pool ID 2])."""
    asset_number: bytes
    lock_status: int
    available_transfers: int
    host_cashout_status: int
    aft_status: int
    max_buffer_index: int
    current_cashable_cents: Optional[int]
    current_restricted_cents: Optional[int]
    current_nonrestricted_cents: Optional[int]
    transfer_limit_cents: Optional[int]

    @property
    def locked(self) -> bool:
        return self.lock_status == LOCK_STATUS_LOCKED


def build_aft_lock_poll(address: int, lock_code: int,
                        transfer_condition: int = TRANSFER_CONDITION_DEFAULT,
                        lock_timeout_hundredths: int = 500) -> bytes:
    """0x74 lock/cancel/interrogate — VERIFY_ON_BENCH layout:
    [addr][74][lock code][transfer condition][lock timeout 2-byte BCD, in
    hundredths of a second][crc] (no length byte — believed fixed-size)."""
    if lock_code not in (LOCK_CODE_REQUEST, LOCK_CODE_CANCEL,
                         LOCK_CODE_INTERROGATE):
        raise ValueError(f"unknown lock code 0x{lock_code:02X}")
    body = bytes([address, AFT_CMD_LOCK_STATUS, lock_code,
                  transfer_condition]) + int_to_bcd(lock_timeout_hundredths, 2)
    return _crc_frame(body)


def parse_aft_lock_response(data: bytes) -> AFTGameLockStatus:
    """Parse SASPacket.data for 0x74 (layout in AFTGameLockStatus).
    Tolerates a longer tail (expiration/pool id and any extensions are
    ignored); raises ValueError when the core fields can't exist."""
    if len(data) < 1 or data[0] != len(data) - 1:
        raise ValueError("lock response length byte mismatch")
    body = data[1:]
    if len(body) < 9 + 20:
        raise ValueError("lock response too short for core fields")
    return AFTGameLockStatus(
        asset_number=body[0:4],
        lock_status=body[4],
        available_transfers=body[5],
        host_cashout_status=body[6],
        aft_status=body[7],
        max_buffer_index=body[8],
        current_cashable_cents=bcd_to_int(body[9:14]),
        current_restricted_cents=bcd_to_int(body[14:19]),
        current_nonrestricted_cents=bcd_to_int(body[19:24]),
        transfer_limit_cents=bcd_to_int(body[24:29]),
    )


# --------------------------------------------------------------------------
# 0x72 — transfer funds
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class AFTTransferRequest:
    """One host-initiated transfer. Amounts in CENTS."""
    transaction_id: str
    transfer_type: int = TRANSFER_TYPE_HOST_TO_EGM
    cashable_cents: int = 0
    restricted_cents: int = 0
    nonrestricted_cents: int = 0
    transfer_code: int = TRANSFER_CODE_FULL_ONLY
    transfer_flags: int = TRANSFER_FLAGS_NONE
    expiration_mmddyyyy: int = 0          # 0 = no expiration
    pool_id: int = 0
    lock_timeout_hundredths: int = 0      # 0x72 trailing lock timeout (0 =
    #                                       no lock requested); hundredths of
    #                                       a second, 2-byte BCD
    # A total of zero is refused unless the caller says it means it. §8.7
    # gives a zero transfer two uses: to set the host cashout enable, and
    # to answer a 6A/6B with "cash out by whatever other means".
    zero_ok: bool = False

    def __post_init__(self):
        if not (0 < len(self.transaction_id) <= MAX_TRANSACTION_ID_LEN):
            raise ValueError("transaction id must be 1..20 ASCII chars")
        try:
            self.transaction_id.encode("ascii")
        except UnicodeEncodeError:
            raise ValueError("transaction id must be ASCII")
        for name in ("cashable_cents", "restricted_cents",
                     "nonrestricted_cents"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be non-negative")
        if self.total_cents <= 0 and not self.zero_ok:
            raise ValueError("total transfer amount must be positive")
        if self.transfer_code not in (TRANSFER_CODE_FULL_ONLY,
                                      TRANSFER_CODE_PARTIAL_OK):
            raise ValueError("transfer code must be full-only or partial-ok")
        if self.transfer_type not in (TRANSFER_TYPE_HOST_TO_EGM,
                                      TRANSFER_TYPE_EGM_TO_HOST,
                                      TRANSFER_TYPE_WIN_TO_HOST):
            raise ValueError(f"unknown transfer type "
                             f"0x{self.transfer_type:02X}")

    @property
    def total_cents(self) -> int:
        return (self.cashable_cents + self.restricted_cents
                + self.nonrestricted_cents)


@dataclass(frozen=True)
class AFTTransferStatusData:
    """Parsed 0x72 response (VERIFY_ON_BENCH layout: [len][transaction
    index][transfer status][receipt status][transfer type][cashable 5 BCD]
    [restricted 5 BCD][nonrestricted 5 BCD][flags][asset 4][txn id len]
    [txn id ...] — any tail beyond the txn id (dates, pool, cumulative
    meters) is tolerated and ignored). Amounts in CENTS."""
    transaction_index: int
    status: int
    receipt_status: int
    transfer_type: int
    cashable_cents: Optional[int]
    restricted_cents: Optional[int]
    nonrestricted_cents: Optional[int]
    transfer_flags: int
    asset_number: bytes
    transaction_id: str

    @property
    def completed(self) -> bool:
        return self.status in (AFTStatus.FULL_TRANSFER_SUCCESSFUL,
                               AFTStatus.PARTIAL_TRANSFER_SUCCESSFUL)

    @property
    def pending(self) -> bool:
        return self.status == AFTStatus.TRANSFER_PENDING


def build_aft_transfer_poll(address: int, request: AFTTransferRequest,
                            asset_number: bytes,
                            registration_key: bytes) -> bytes:
    """0x72 initiate — VERIFY_ON_BENCH layout (authority: saspy sas.py:1290-
    1309, cross-checked against our own 0x74 lock builder; the full SAS §8.3
    table is NOT in our on-disk spec excerpt — pp.1-57 of 181 — so this is
    saspy-adjudicated, not spec-proven, still bench-confirm):
    [addr][72][len][transfer code][transaction index=00][transfer type]
    [cashable 5 BCD][restricted 5 BCD][nonrestricted 5 BCD][flags][asset 4]
    [registration key 20][txn id len][txn id][expiration 4 BCD][pool 2 BCD]
    [receipt data len=00][lock timeout 2 BCD][crc].
    Two 2026-07-07 adjudication fixes: pool_id is packed BCD (MSB-first, like
    every other SAS id/amount), NOT the earlier little-endian binary guess;
    and the trailing 2-byte BCD lock timeout was missing entirely (its absence
    left the frame 2 bytes short so the EGM misread the CRC). Both are 0 for
    the common host->EGM cashable transfer.

    ⚠️ 2026-07-09 DISCREPANCY (unresolved, INERT here): a full SAS 6.02 text
    (Table 8.3a) says Pool ID is 2 BINARY, not BCD. This contradicts saspy.
    It is a reference-vs-reference disagreement (neither bench-proven), and it
    does NOT matter for the cashable push we build — pool_id=0 encodes to
    0x0000 either way. Left BCD to keep the frame stable; MUST be resolved on
    the wire before any RESTRICTED/pool transfer (pool_id != 0)."""
    if len(asset_number) != ASSET_NUMBER_LEN:
        raise ValueError("asset number is 4 wire bytes")
    if len(registration_key) != REGISTRATION_KEY_LEN:
        raise ValueError("registration key is exactly 20 bytes")
    txn = request.transaction_id.encode("ascii")
    data = bytearray()
    data.append(request.transfer_code)
    data.append(0x00)                            # transaction index: current
    data.append(request.transfer_type)
    data += cents_to_bcd5(request.cashable_cents)
    data += cents_to_bcd5(request.restricted_cents)
    data += cents_to_bcd5(request.nonrestricted_cents)
    data.append(request.transfer_flags)
    data += asset_number
    data += registration_key
    data.append(len(txn))
    data += txn
    data += int_to_bcd(request.expiration_mmddyyyy, 4)
    # Pool ID: "2 binary" (SAS 6.02 Table 8.3a), and binary fields go least
    # significant byte first (§2). This was packed BCD on saspy's word and
    # flagged as unresolved; it is the document's layout now that restricted
    # amounts are sent (2026-11-16). Pool 0000 is the same bytes either way.
    data += int(request.pool_id).to_bytes(2, "little")
    data.append(0x00)                            # receipt data length: none
    data += int_to_bcd(request.lock_timeout_hundredths, 2)  # trailing lock TO
    body = bytes([address, AFT_CMD_TRANSFER, len(data)]) + bytes(data)
    return _crc_frame(body)


def build_aft_transfer_interrogate_poll(address: int,
                                        transaction_index: int = 0) -> bytes:
    """0x72 interrogate — VERIFY_ON_BENCH: [addr][72][len=02][FF][index]
    [crc]; index 00 = current/most recent transfer."""
    return _crc_frame(bytes([address, AFT_CMD_TRANSFER, 2,
                             TRANSFER_CODE_INTERROGATE, transaction_index]))


def build_aft_cancel_poll(address: int) -> bytes:
    """0x72 cancel the pending transfer — VERIFY_ON_BENCH:
    [addr][72][len=01][80][crc]."""
    return _crc_frame(bytes([address, AFT_CMD_TRANSFER, 1,
                             TRANSFER_CODE_CANCEL]))


def parse_aft_transfer_response(data: bytes) -> AFTTransferStatusData:
    """Parse SASPacket.data for 0x72 (layout in AFTTransferStatusData).
    Raises ValueError on structural mismatch."""
    if len(data) < 2 or data[0] != len(data) - 1:
        raise ValueError("transfer response length byte mismatch")
    body = data[1:]
    if len(body) in (2, 3):
        # The short forms of SAS 6.02 §8.3, where the machine "omits the
        # remaining fields": status C1 (unsupported transfer code), C0 (not
        # compatible with the transfer in progress), and the answer to an
        # interrogation when there is no current or previous transfer —
        # "a transfer buffer position of 00, a transfer status of FF and a
        # receipt status of FF". These used to raise here and so read as
        # silence, which made a definite "not taken" look like an unknown.
        # No transaction id comes with them (transaction_id is "").
        return AFTTransferStatusData(
            body[0], body[1], body[2] if len(body) == 3 else 0xFF, 0,
            None, None, None, 0, b"", "")
    if len(body) < 25:
        raise ValueError("transfer response too short for core fields")
    idx, status, receipt, ttype = body[0], body[1], body[2], body[3]
    cashable = bcd_to_int(body[4:9])
    restricted = bcd_to_int(body[9:14])
    nonrestricted = bcd_to_int(body[14:19])
    flags = body[19]
    asset = body[20:24]
    txn_len = body[24]
    if len(body) < 25 + txn_len:
        raise ValueError("transfer response truncated inside txn id")
    txn_id = body[25:25 + txn_len].decode("ascii", errors="replace")
    return AFTTransferStatusData(idx, status, receipt, ttype, cashable,
                                 restricted, nonrestricted, flags, asset,
                                 txn_id)


# --------------------------------------------------------------------------
# Pure transaction state machine (register -> lock -> transfer ->
# interrogate -> complete/rollback)
# --------------------------------------------------------------------------

class AFTTxnState(enum.Enum):
    CREATED = "created"
    LOCKED = "locked"
    ISSUED = "issued"                 # 72 initiate accepted onto the wire
    PENDING = "pending"               # machine says 0x40
    COMPLETED_FULL = "completed_full"
    COMPLETED_PARTIAL = "completed_partial"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


class AFTTxnEvent(enum.Enum):
    LOCK_CONFIRMED = "lock_confirmed"
    TRANSFER_ISSUED = "transfer_issued"
    STATUS_PENDING = "status_pending"
    STATUS_FULL = "status_full"
    STATUS_PARTIAL = "status_partial"
    STATUS_ERROR = "status_error"
    CANCEL_CONFIRMED = "cancel_confirmed"


class AFTStateError(Exception):
    """An illegal state transition — a host-side logic bug, never a wire
    condition; raised loudly instead of corrupting the ledger."""


_S, _E = AFTTxnState, AFTTxnEvent
_TRANSITIONS: Dict[Tuple[AFTTxnState, AFTTxnEvent], AFTTxnState] = {
    (_S.CREATED, _E.LOCK_CONFIRMED): _S.LOCKED,
    # a LOCKLESS host->EGM push (flags bit 6 clear) issues straight from
    # CREATED — no 0x74 lock precedes it (real SAS 6.02: the machine does not
    # require a lock for a bit6=0 transfer). Added 2026-07-09.
    (_S.CREATED, _E.TRANSFER_ISSUED): _S.ISSUED,
    (_S.CREATED, _E.STATUS_ERROR): _S.FAILED,
    (_S.LOCKED, _E.TRANSFER_ISSUED): _S.ISSUED,
    (_S.LOCKED, _E.STATUS_ERROR): _S.FAILED,
    (_S.LOCKED, _E.CANCEL_CONFIRMED): _S.ROLLED_BACK,
    (_S.ISSUED, _E.STATUS_FULL): _S.COMPLETED_FULL,
    (_S.ISSUED, _E.STATUS_PARTIAL): _S.COMPLETED_PARTIAL,
    (_S.ISSUED, _E.STATUS_PENDING): _S.PENDING,
    (_S.ISSUED, _E.STATUS_ERROR): _S.FAILED,
    (_S.ISSUED, _E.CANCEL_CONFIRMED): _S.ROLLED_BACK,
    (_S.PENDING, _E.STATUS_PENDING): _S.PENDING,
    (_S.PENDING, _E.STATUS_FULL): _S.COMPLETED_FULL,
    (_S.PENDING, _E.STATUS_PARTIAL): _S.COMPLETED_PARTIAL,
    (_S.PENDING, _E.STATUS_ERROR): _S.FAILED,
    (_S.PENDING, _E.CANCEL_CONFIRMED): _S.ROLLED_BACK,
}

_TERMINAL = frozenset({_S.COMPLETED_FULL, _S.COMPLETED_PARTIAL, _S.FAILED,
                       _S.ROLLED_BACK})


def advance(state: AFTTxnState, event: AFTTxnEvent) -> AFTTxnState:
    """Pure transition function. Raises AFTStateError on an illegal move
    (e.g. completing a transaction that was never issued)."""
    try:
        return _TRANSITIONS[(state, event)]
    except KeyError:
        raise AFTStateError(f"illegal transition: {state.value} "
                            f"+ {event.value}") from None


def is_terminal(state: AFTTxnState) -> bool:
    return state in _TERMINAL


def status_to_event(status: Optional[int]) -> Optional[AFTTxnEvent]:
    """Map a 0x72 transfer-status byte to a state-machine event (None for
    silence — silence never advances the ledger)."""
    if status is None:
        return None
    if status == AFTStatus.FULL_TRANSFER_SUCCESSFUL:
        return AFTTxnEvent.STATUS_FULL
    if status == AFTStatus.PARTIAL_TRANSFER_SUCCESSFUL:
        return AFTTxnEvent.STATUS_PARTIAL
    if status == AFTStatus.TRANSFER_PENDING:
        return AFTTxnEvent.STATUS_PENDING
    if status == AFTStatus.TRANSFER_CANCELED:
        return AFTTxnEvent.CANCEL_CONFIRMED
    return AFTTxnEvent.STATUS_ERROR


# --------------------------------------------------------------------------
# Transport-driven orchestration
# --------------------------------------------------------------------------

class AFTOutcome(enum.Enum):
    COMPLETED = "completed"               # full transfer confirmed
    PARTIAL = "partial"                   # partial transfer confirmed
    NOT_REGISTERED = "not_registered"     # machine refused: no registration
    LOCK_FAILED = "lock_failed"           # never reached game lock
    REFUSED = "refused"                   # machine returned an error status
    ROLLED_BACK = "rolled_back"           # cancel confirmed after pending
    UNCONFIRMED = "unconfirmed"           # issued; final status never seen —
                                          # verify meters before retrying
    TIMEOUT = "timeout"                   # silence where consent was needed
    UNRESOLVED_EARLIER = "unresolved_earlier"   # an earlier transfer has no
    #                                       final status yet; nothing sent
    NOT_SAVED = "not_saved"               # could not write the transfer down
    #                                       first; nothing sent


@dataclass(frozen=True)
class AFTTransferResult:
    outcome: AFTOutcome
    state: AFTTxnState                    # final ledger state
    request: Optional[AFTTransferRequest]
    registration: Optional[AFTRegistration]
    lock: Optional[AFTGameLockStatus]
    final: Optional[AFTTransferStatusData]
    confirmed_by_exception: bool          # 0x69 observed
    detail: str

    @property
    def ok(self) -> bool:
        return self.outcome in (AFTOutcome.COMPLETED, AFTOutcome.PARTIAL)


def _frame_of(resp: bytes, address: int, command: int, proto: SASProtocol):
    """Corrupt/foreign/short frames are silence, never consent (the handpay
    rule)."""
    if not resp or len(resp) < 4:
        return None
    packet = proto.parse_packet(resp)
    if packet is None or packet.address != address \
            or packet.command != command:
        return None
    return packet


def _read_registration(transport, address, proto) -> Optional[AFTRegistration]:
    resp = transport.transact(build_aft_register_interrogate_poll(address))
    packet = _frame_of(resp, address, AFT_CMD_REGISTER, proto)
    if packet is None:
        return None
    try:
        return parse_aft_register_response(packet.data)
    except ValueError:
        return None


def _read_lock(transport, address, proto,
               lock_code=LOCK_CODE_INTERROGATE,
               transfer_condition=TRANSFER_CONDITION_DEFAULT,
               timeout_hundredths=500) -> Optional[AFTGameLockStatus]:
    resp = transport.transact(build_aft_lock_poll(
        address, lock_code, transfer_condition, timeout_hundredths))
    packet = _frame_of(resp, address, AFT_CMD_LOCK_STATUS, proto)
    if packet is None:
        return None
    try:
        return parse_aft_lock_response(packet.data)
    except ValueError:
        return None


def _read_transfer_status(transport, address,
                          proto) -> Optional[AFTTransferStatusData]:
    resp = transport.transact(build_aft_transfer_interrogate_poll(address))
    packet = _frame_of(resp, address, AFT_CMD_TRANSFER, proto)
    if packet is None:
        return None
    try:
        return parse_aft_transfer_response(packet.data)
    except ValueError:
        return None


def _own_status(status_data: Optional[AFTTransferStatusData],
                request: AFTTransferRequest
                ) -> Optional[AFTTransferStatusData]:
    """A foreign transaction's status — or one echoing NO transaction id
    at all (an unidentified status is not ours to trust; a zeroed body
    would read as FULL_TRANSFER_SUCCESSFUL) — must never settle OUR
    ledger. Returns status_data only when the echoed id matches."""
    if status_data is None \
            or status_data.transaction_id != request.transaction_id:
        return None
    return status_data


def read_asset_number(transport, address: int,
                      protocol: Optional[SASProtocol] = None,
                      pace: float = SAS_POLL_FLOOR,
                      sleep: Callable[[float], None] = time.sleep,
                      reg: Optional[AFTRegistration] = None
                      ) -> Optional[bytes]:
    """Read the machine's OPERATOR-SET asset number WITHOUT registering
    (CONTRACT assetSource). First non-zero 4 OPAQUE bytes wins, in order:

      1. the 0x73/FF register-interrogate echo — passed in as `reg` (free:
         the caller already sent 73/FF for the status decision). Used only
         when non-zero (a status-only reply carries asset b"").
      2. LP 0x7B extended validation status — the decisive primary: it is a
         VALIDATION poll, not an AFT poll, so it is registration-INDEPENDENT
         by construction and still reports the operator asset on a freshly
         RAM-cleared, NOT_REGISTERED machine. Read-only (all-zero control).
      3. LP 0x74/FF AFT lock interrogate — the only source LIVE-PROVEN on the
         BB2, but only proven to echo AFTER a registration exists.

    The 4 bytes are OPAQUE: the caller carries them verbatim into
    build_aft_register_poll (NEVER round-tripped through
    asset_number_bytes(int) — that risks a byte-order flip). Decode
    little-endian ONLY for display. Returns None when every source answers
    zero/silent — the caller then yields the typed 'asset_unknown' outcome
    and NEVER fabricates an asset."""
    proto = protocol or _PROTO
    if reg is not None and len(reg.asset_number) == ASSET_NUMBER_LEN \
            and any(reg.asset_number):
        return bytes(reg.asset_number)
    # 0x7B — the registration-independent primary (read-only validation poll)
    resp = transport.transact(build_extended_validation_status_poll(address))
    packet = _frame_of(resp, address, 0x7B, proto)
    if packet is not None:
        try:
            raw = parse_extended_validation_status(packet.data).asset_number_raw
        except ValueError:
            raw = b""
        if len(raw) == ASSET_NUMBER_LEN and any(raw):
            return bytes(raw)
    # 0x74/FF — the BB2-proven echo (fallback)
    sleep(pace)
    lock = _read_lock(transport, address, proto, LOCK_CODE_INTERROGATE)
    if lock is not None and len(lock.asset_number) == ASSET_NUMBER_LEN \
            and any(lock.asset_number):
        return bytes(lock.asset_number)
    return None


def _unanswered(what: str, resp: bytes, address: int) -> str:
    """Why a registration step has no usable answer, worded for the journal
    and the support bundle. "The machine said nothing" and "the machine said
    no" need different fixes, and until 2026-11-16 both read as silence.
    True silence keeps that wording. At most the first three bytes of
    anything else are shown: a 0x73 answer carries the registration key."""
    if not resp:
        return f"silence on {what}"
    if len(resp) == 1 and resp[0] == (0x80 | address):
        return f"{what} refused: the machine answered NACK (0x{resp[0]:02X})"
    if len(resp) == 2 and resp[0] == address and resp[1] == 0x00:
        return f"{what} not taken: the machine answered busy"
    return (f"{what} answered with {len(resp)} bytes that do not check out "
            f"(starts {resp[:3].hex()})")


def aft_register(transport, address: int, asset_number: bytes,
                 registration_key: bytes, pos_id: bytes,
                 protocol: Optional[SASProtocol] = None,
                 pace: float = SAS_POLL_FLOOR,
                 sleep: Callable[[float], None] = time.sleep
                 ) -> Tuple[bool, Optional[AFTRegistration], str]:
    """Register the machine for AFT (0x73): initialize, then register, then
    read back. Returns (ok, final_registration, detail).

    SAS 6.02 §8.1. 73 with code 00 starts the cycle; "the asset number
    specified by the host must exactly match the current operator-entered
    non-zero value", and the machine goes to status 00. 73 with code 01
    then completes it: status 01. "If the gaming machine receives a
    registration long poll 73 with an asset number that is not valid, or
    any long poll 73 ... that does not conform to the proper sequence and
    data, the gaming machine will set its registration status to 80."

    Status 40 (pending an operator's acknowledgement) only ever follows a
    73 with code 40 from the host, which is never sent here. Until v0.3.14
    every refusal was reported as "possibly awaiting operator
    acknowledgment at the machine", which sent owners looking for a prompt
    that cannot be there; the usual cause, status 80, is the asset number
    or the sequence. A 40 is still re-read once, in case a machine strays.

    The register poll is sent whatever the initialize answered, as it
    always was (a machine that strays there may still register); what the
    initialize answered is kept for the message when it does not.

    Every poll here is command 0x73, so each answer has to be ACKed before
    the next goes out (SAS 6.02 §3.1-3.2). The satellite's transport does
    that (core/sas_implied_ack.py); a bare transport does not, and a strict
    machine then leaves the register poll unanswered."""
    proto = protocol or _PROTO
    resp = transport.transact(build_aft_register_poll(
        address, REG_CODE_INITIALIZE, asset_number, registration_key, pos_id))
    packet = _frame_of(resp, address, AFT_CMD_REGISTER, proto)
    if packet is None:
        return False, None, _unanswered(
            "registration initialize (73/00)", resp, address)
    try:
        initialized = parse_aft_register_response(packet.data).status
    except ValueError:
        initialized = None
    sleep(pace)
    resp = transport.transact(build_aft_register_poll(
        address, REG_CODE_REGISTER, asset_number, registration_key, pos_id))
    packet = _frame_of(resp, address, AFT_CMD_REGISTER, proto)
    if packet is None:
        return False, None, _unanswered("register (73/01)", resp, address)
    try:
        reg = parse_aft_register_response(packet.data)
    except ValueError as e:
        return False, None, f"malformed 73 response: {e}"
    if reg.status == REG_STATUS_PENDING:
        sleep(pace)
        reg = _read_registration(transport, address, proto) or reg
    if reg.registered:
        return True, reg, "registered"
    return False, reg, registration_refusal(reg.status, initialized)


def registration_refusal(status: int, initialized: Optional[int]) -> str:
    """Why a registration did not end at status 01, in words an owner can
    act on (SAS 6.02 §8.1). `initialized` is the status the machine gave
    to the initialize poll (73/00), None when that could not be read."""
    if status == REG_STATUS_NOT_REGISTERED:
        why = ("the machine did not accept the asset number, or it "
               "rejected the order of the polls")
    elif status == REG_STATUS_PENDING:
        why = ("the machine says it is waiting for an operator to "
               "acknowledge at the machine")
    elif status == REG_STATUS_READY:
        why = "the machine started the cycle and did not complete it"
    else:
        why = "the machine did not register"
    start = ("" if initialized is None or initialized == REG_STATUS_READY
             else f"; it had answered the initialize (73/00) with status "
                  f"0x{initialized:02X} where 0x00 is expected")
    return f"registration refused (status 0x{status:02X}): {why}{start}"


def aft_transfer(transport, address: int, request: AFTTransferRequest,
                 asset_number: bytes, registration_key: bytes,
                 protocol: Optional[SASProtocol] = None,
                 pace: float = SAS_POLL_FLOOR,
                 sleep: Callable[[float], None] = time.sleep,
                 lock_attempts: int = 5,
                 confirm_polls: int = 10,
                 require_lock: bool = True,
                 on_exception: Optional[Callable[[int], None]] = None,
                 on_frame: Optional[Callable[[bytes], None]] = None,
                 on_wire: Optional[Callable[[str, bytes, bytes], None]] = None
                 ) -> AFTTransferResult:
    """Run one complete AFT transfer against one machine:

      1. with a registration key in use, interrogate registration (73/FF)
         and REQUIRE registered: the machine must still hold that key. With
         a key of all zeros (ZERO_KEY) there is nothing to check and no 73
         is sent;
      2. request game lock (74/00), re-interrogating while PENDING —
         REQUIRE locked (a failed lock is cancelled, 74/80, on the way out).
         SKIPPED when require_lock=False (the LOCKLESS host->EGM push: real
         SAS 6.02 says a flags-bit6=0 transfer needs no lock — the machine
         takes it immediately if idle, else escrows->PENDING->completes at
         idle). The lock request names the transfer_condition matched to the
         transfer DIRECTION — bit0 "transfer TO machine" for a host->EGM push,
         bit1 "transfer FROM machine" for an EGM->host cash-out (or win) pull —
         since the 0x00 default (or the wrong-direction bit) names no condition
         the machine can lock for and is the proven cause of a 0xFF NOT_LOCKED
         stick;
      3. issue the transfer (72) and classify the machine's status;
      4. PENDING -> await the believed 0x69 'AFT transfer complete'
         exception off the general-poll FIFO (implied-ACK aware), then
         interrogate (72/FF) for the final status;
      5. stuck PENDING -> one cancel attempt (72/80) + interrogate: a
         confirmed cancel is ROLLED_BACK, anything else UNCONFIRMED (verify
         meters before retrying — the transfer may have landed).

    NOTHING IRREVERSIBLE MOVES WITHOUT CONSENT: the 72 initiate is never
    sent unless registration was read back REGISTERED (when a key is in
    use) and the lock was confirmed LOCKED; silence/corruption at any step
    yields a typed result.
    The pure state machine (advance()) runs alongside as the ledger — an
    illegal transition raises AFTStateError (host bug) rather than lying.

    Pacing: sleep(pace) between consecutive polls (SAS_POLL_FLOOR default);
    inject a no-op sleep only in mock tests. on_exception/on_frame mirror
    the handpay module (drained events are handed up, not lost)."""
    proto = protocol or _PROTO
    state = AFTTxnState.CREATED
    if not require_lock and (request.transfer_flags & AFT_FLAG_LOCK_REQUIRED):
        # Contradiction (caller bug): lockless orchestration but the wire flag
        # tells the EGM to accept ONLY if locked — it would reject the
        # unlocked transfer. Refuse loudly rather than quietly fail on-wire.
        raise ValueError(
            "lockless transfer requested but transfer_flags sets the "
            "lock-required bit (0x40); clear it or pass require_lock=True")

    # -- 1: registration gate, when a key is in use ---------------------------
    reg: Optional[AFTRegistration] = None
    if any(registration_key):
        reg = _read_registration(transport, address, proto)
        if reg is None:
            return AFTTransferResult(
                AFTOutcome.TIMEOUT, state, request, None, None, None, False,
                "silence on registration interrogate (73/FF); nothing sent")
        if not reg.registered:
            state = advance(state, AFTTxnEvent.STATUS_ERROR)
            return AFTTransferResult(
                AFTOutcome.NOT_REGISTERED, state, request, reg, None, None,
                False, f"machine not AFT-registered "
                       f"(status 0x{reg.status:02X}); run aft_register first")

    # -- 2: game lock (skipped for a LOCKLESS host->EGM push) ------------------
    lock: Optional[AFTGameLockStatus] = None
    if require_lock:
        sleep(pace)
        # Name the condition to lock FOR, matched to the transfer DIRECTION: a
        # host->EGM push locks for TO_EGM (bit0); an EGM->host cash-out pull (or
        # a win-to-host) locks for FROM_EGM (bit1). The wrong bit — or the 0x00
        # default — leaves the machine nothing to lock for and it answers
        # 0xFF NOT_LOCKED (the classic stuck-lock).
        lock_condition = (
            TRANSFER_CONDITION_FROM_EGM
            if request.transfer_type in (TRANSFER_TYPE_EGM_TO_HOST,
                                         TRANSFER_TYPE_WIN_TO_HOST)
            else TRANSFER_CONDITION_TO_EGM)
        lock = _read_lock(transport, address, proto, LOCK_CODE_REQUEST,
                          transfer_condition=lock_condition)
        attempts = 0
        while lock is not None and lock.lock_status == LOCK_STATUS_PENDING \
                and attempts < lock_attempts:
            sleep(pace)
            lock = _read_lock(transport, address, proto)
            attempts += 1
        if lock is None or not lock.locked:
            # Cancel on EVERY failed exit, the lost answer included. §8.2:
            # a machine "may stay in the lock pending state indefinitely",
            # so a request whose answer we never saw can still be waiting,
            # and would lock the game for five seconds the next time it
            # goes idle, with no transfer coming. Until v0.3.16 the cancel
            # was only sent when an answer had been read.
            sleep(pace)
            _read_lock(transport, address, proto, LOCK_CODE_CANCEL)
            state = advance(state, AFTTxnEvent.STATUS_ERROR)
            return AFTTransferResult(
                AFTOutcome.LOCK_FAILED, state, request, reg, lock, None, False,
                "game lock never confirmed"
                + ("" if lock is None else
                   f" (last status 0x{lock.lock_status:02X}, availXfers "
                   f"0x{lock.available_transfers:02X}, aftStatus "
                   f"0x{lock.aft_status:02X})")
                + "; transfer NOT sent")
        state = advance(state, AFTTxnEvent.LOCK_CONFIRMED)

    # -- 3: the transfer --------------------------------------------------------
    sleep(pace)
    req_frame = build_aft_transfer_poll(
        address, request, asset_number, registration_key)
    resp = transport.transact(req_frame)
    if on_wire is not None:                       # raw 0x72 req+resp (debug)
        try:
            on_wire("0x72", req_frame, resp or b"")
        except Exception:                         # never let a tap break money
            pass
    state = advance(state, AFTTxnEvent.TRANSFER_ISSUED)
    packet = _frame_of(resp, address, AFT_CMD_TRANSFER, proto)
    status_data: Optional[AFTTransferStatusData] = None
    if packet is not None:
        try:
            status_data = parse_aft_transfer_response(packet.data)
        except ValueError:
            status_data = None

    result = _settle(status_data, state, request, reg, lock, False)
    if result is not None:
        return result
    if status_data is not None and status_data.pending:
        state = advance(state, AFTTxnEvent.STATUS_PENDING)

    # -- 4: pending — await completion, then interrogate ------------------------
    confirmed = _await_aft_complete(transport, address, proto, pace, sleep,
                                    confirm_polls, on_exception, on_frame)
    sleep(pace)
    status_data = _own_status(
        _read_transfer_status(transport, address, proto), request)
    result = _settle(status_data, state, request, reg, lock, confirmed)
    if result is not None:
        return result

    # -- 5: stuck — one cancel attempt, then honest UNCONFIRMED -----------------
    sleep(pace)
    transport.transact(build_aft_cancel_poll(address))
    sleep(pace)
    status_data = _own_status(
        _read_transfer_status(transport, address, proto), request)
    if status_data is not None \
            and status_data.status == AFTStatus.TRANSFER_CANCELED:
        state = advance(state, AFTTxnEvent.CANCEL_CONFIRMED)
        return AFTTransferResult(
            AFTOutcome.ROLLED_BACK, state, request, reg, lock, status_data,
            confirmed, "pending transfer cancelled and confirmed — "
            "no funds moved")
    result = _settle(status_data, state, request, reg, lock, confirmed)
    if result is not None:
        return result
    return AFTTransferResult(
        AFTOutcome.UNCONFIRMED, state, request, reg, lock, status_data,
        confirmed, "transfer issued but final status never confirmed — "
        "VERIFY METERS before retrying (the credits may have landed)")


def _settle(status_data: Optional[AFTTransferStatusData],
            state: AFTTxnState, request, reg, lock,
            confirmed: bool) -> Optional[AFTTransferResult]:
    """Terminal-status classifier shared by every settle point. Returns a
    result for full/partial/error, None for pending/silence (caller keeps
    going)."""
    if status_data is None:
        return None
    event = status_to_event(status_data.status)
    if event == AFTTxnEvent.STATUS_FULL:
        return AFTTransferResult(
            AFTOutcome.COMPLETED, advance(state, event), request, reg, lock,
            status_data, confirmed, "full transfer successful")
    if event == AFTTxnEvent.STATUS_PARTIAL:
        return AFTTransferResult(
            AFTOutcome.PARTIAL, advance(state, event), request, reg, lock,
            status_data, confirmed,
            f"partial transfer: {status_data.cashable_cents} cashable cents")
    if event == AFTTxnEvent.STATUS_ERROR:
        try:
            name = AFTStatus(status_data.status).name
        except ValueError:
            name = f"0x{status_data.status:02X}"
        return AFTTransferResult(
            AFTOutcome.REFUSED, advance(state, event), request, reg, lock,
            status_data, confirmed, f"machine refused the transfer: {name}")
    return None    # pending / cancel handled by the caller


def _await_aft_complete(transport, address, proto, pace, sleep,
                        confirm_polls, on_exception, on_frame) -> bool:
    """General-poll (paced) until the 0x69 'AFT transfer complete'
    exception. The loop itself is core/sas_wait.await_exception."""
    return await_exception(transport, address, proto,
                           (AFT_TRANSFER_COMPLETE_EXCEPTION,), pace, sleep,
                           confirm_polls, on_exception, on_frame)


# --------------------------------------------------------------------------
# A transfer with no final status yet (see open_transfer.py)
# --------------------------------------------------------------------------

OPEN_RETRY_SEC = (2.0, 5.0, 15.0)   # wait before asking the machine again;
#                                     the last value repeats. Exception 69,
#                                     which the machine raises every fifteen
#                                     seconds until its status is read, asks
#                                     at once whatever this says.


def judge_open_transfer(rec: Dict,
                        status: Optional[AFTTransferStatusData]
                        ) -> Optional[Dict]:
    """What a 72/FF interrogation says about the transfer we have open.

    None means "still open": no answer, or the machine says pending. Else a
    dict {ok, outcome, amountCents, detail} — the END of that transfer:

      completed / partial   it happened, for the amount the machine names;
      rolled_back, refused  it ended without moving anything;
      not_taken             the machine does not know it. Its answer is the
                            short "no transfer information" form, or names a
                            different transaction as its latest. Our 0x72
                            never started a cycle, so nothing moved.

    The amount is the machine's own figure, in the kind of credit the
    transfer was for (cashable, or restricted for a restricted promotional
    push). Where it cannot be read, a push falls back to what was asked for
    and a cash-out credits nothing — the same rule the live paths use."""
    if status is None:
        return None
    if status.transaction_id != rec["txnId"]:
        why = ("the machine has no transfer on record"
               if not status.transaction_id else
               f"the machine's latest transfer is {status.transaction_id!r}")
        return {"ok": False, "outcome": "not_taken", "amountCents": 0,
                "detail": f"{why} — this one never started, nothing moved"}
    event = status_to_event(status.status)
    if event == AFTTxnEvent.STATUS_PENDING:
        return None
    if event in (AFTTxnEvent.STATUS_FULL, AFTTxnEvent.STATUS_PARTIAL):
        amount = (status.restricted_cents
                  if rec.get("creditType") == "restricted"
                  else status.cashable_cents)
        note = ""
        if amount is None:
            if rec.get("kind") == "aft_transfer":
                amount = int(rec.get("cents") or 0)
            else:
                amount, note = 0, (" — amount unreadable, NOT credited "
                                   "(verify meters)")
        full = event == AFTTxnEvent.STATUS_FULL
        return {"ok": True, "outcome": "completed" if full else "partial",
                "amountCents": int(amount),
                # a cash-out can carry restricted credit as well as cash
                "restrictedCents": int(status.restricted_cents or 0)
                if rec.get("kind") != "aft_transfer" else 0,
                "detail": ("full transfer successful" if full else
                           f"partial transfer: {amount} cashable cents")
                + note}
    if event == AFTTxnEvent.CANCEL_CONFIRMED:
        return {"ok": False, "outcome": "rolled_back", "amountCents": 0,
                "detail": "transfer cancelled — no funds moved"}
    try:
        name = AFTStatus(status.status).name
    except ValueError:
        name = f"0x{status.status:02X}"
    return {"ok": False, "outcome": "refused", "amountCents": 0,
            "detail": f"machine refused the transfer: {name}"}


# --------------------------------------------------------------------------
# Poller integration
# --------------------------------------------------------------------------

class AFTHost:
    """Wires AFT into a SASPoller without modifying it (the SASTITOHost
    pattern): operator-triggered transfers run through the poller's
    transport with drained exceptions still dispatched normally, and the
    EGM-initiated host-cashout request exceptions (believed 0x6A/0x6B) are
    surfaced via on_cashout_request — the DECISION stays with the server
    layer (player_accounts), never automatic here.

    per-machine identity: asset_number (4 wire bytes) + registration_key
    (20 bytes) + pos_id (4 wire bytes). `rig_key` is the key this host
    registers a machine with, fixed at construction. `registration_key` is
    the key its transfers carry: the machine's own once it is registered,
    all zeros for a machine that could not be (prepare_transfer)."""

    def __init__(self, poller, asset_number: Optional[bytes] = None,
                 registration_key: bytes = b"",
                 pos_id: bytes = b"\x00\x00\x00\x00",
                 on_transfer: Optional[
                     Callable[[int, AFTTransferResult], None]] = None,
                 on_cashout_request: Optional[
                     Callable[[int, int], None]] = None,
                 pace: float = SAS_POLL_FLOOR,
                 sleep: Callable[[float], None] = time.sleep,
                 journal: Optional[OpenTransferJournal] = None,
                 clock: Callable[[], float] = time.monotonic):
        # asset_number is OPTIONAL (None) so the runner can build one AFTHost
        # purely for registration — auto_register() READS the machine's real
        # asset and never leaves this None for a transfer. A non-None asset
        # is still length-checked (the short-asset rejection test passes
        # b"\x01", non-None, still fails).
        if asset_number is not None and len(asset_number) != ASSET_NUMBER_LEN:
            raise ValueError("asset number is 4 wire bytes")
        if len(registration_key) != REGISTRATION_KEY_LEN:
            raise ValueError("registration key is exactly 20 bytes")
        self.poller = poller
        self.asset_number = asset_number
        self.rig_key = registration_key
        self.registration_key = registration_key
        self.pos_id = pos_id
        self._register_again_at = 0.0  # prepare_transfer's next registration
        self._unregistered_why = ""    # ... and why the last one failed
        self.on_transfer = on_transfer
        self.on_cashout_request = on_cashout_request
        self.pace = pace
        self.sleep = sleep
        # The transfer in flight, written down before its 0x72 goes out and
        # kept until its final status is read (open_transfer.py).
        self.journal = journal if journal is not None \
            else OpenTransferJournal(None)
        self.clock = clock
        self.complete_seen = False     # exception 69 came off the FIFO
        self._ended = []               # verdicts waiting for service_open()
        self._ask_at = 0.0             # clock time of the next 72/FF
        self._asked = 0                # times asked without a final answer
        self._last_txn = None          # the last transfer we saw the end of
        self._prev_typed = poller.on_typed_event
        poller.on_typed_event = self._typed_event

    def register(self) -> Tuple[bool, Optional[AFTRegistration], str]:
        p = self.poller
        return aft_register(p.transport, p.address, self.asset_number,
                            self.rig_key, self.pos_id, p.protocol,
                            pace=self.pace, sleep=self.sleep)

    def registration_status(self) -> Optional[AFTRegistration]:
        p = self.poller
        return _read_registration(p.transport, p.address, p.protocol)

    def read_asset(self, reg: Optional[AFTRegistration] = None
                   ) -> Optional[bytes]:
        """The operator-set asset read WITHOUT registering (73/FF echo ->
        0x7B -> 0x74/FF; CONTRACT assetSource). Passing `reg` (a prior 73/FF
        read) lets a REGISTERED machine short-circuit on its echoed asset
        with no extra polls. Used by the report cache so the hub can see the
        asset on an UNREGISTERED machine (the auto-trigger needs it)."""
        p = self.poller
        return read_asset_number(p.transport, p.address, p.protocol,
                                 pace=self.pace, sleep=self.sleep, reg=reg)

    def auto_register(self) -> Tuple[bool, Optional[AFTRegistration], str]:
        """READ-current-then-register — the ONE idempotent code path behind
        both the online auto-trigger and the manual button (CONTRACT
        registrationFlow). Never assigns an asset; it READS the operator-set
        asset off the machine and registers the fixed rig key against it.

          1. 73/FF status. Already REGISTERED -> adopt its key/asset and
             return (True, reg, "already registered") — idempotent, so it is
             safe to call every cooldown cycle and from the button with no
             double-register.
          2. else READ the asset (73/FF echo -> 0x7B -> 0x74/FF, first
             non-zero wins). No asset from any poll -> (False, reg,
             "machine reports no asset number") = outcome 'asset_unknown';
             DO NOT register, DO NOT invent.
          3. else aft_register(... READ asset ...) — 73/00 -> 73/01 ->
             read-back (tolerating one 0x40 PENDING). On success ADOPT the
             authoritative key + asset from the response record so a later
             0x72 transfer on this instance uses the machine's key, not the
             construction-time one (keyPolicy DEFENSIVE RULE)."""
        p = self.poller
        reg = _read_registration(p.transport, p.address, p.protocol)
        if reg is not None and reg.registered:
            self._adopt(reg)
            return True, reg, "already registered"
        self.sleep(self.pace)
        asset = self.read_asset(reg=reg)
        if asset is None:
            return False, reg, "machine reports no asset number"
        ok, final, detail = aft_register(
            p.transport, p.address, asset, self.rig_key, self.pos_id,
            p.protocol, pace=self.pace, sleep=self.sleep)
        if ok and final is not None:
            self._adopt(final)
        return ok, final, detail

    def prepare_transfer(self) -> Tuple[bool, Optional[AFTRegistration], str]:
        """What a transfer needs before it is sent: the machine's asset
        number and a registration key. Returns (ok, registration, detail).
        Call it before every transfer(); a False means nothing can be sent.

        A machine that is registered, or registers now: exactly what
        auto_register does, poll for poll, and the transfer carries the
        machine's key.

        A machine that cannot be registered (audit item 17): the transfer
        goes with a key of all zeros. SAS 6.02 §8.1 asks for that key on
        every in-house transfer ("registration not required", Table 8.3a);
        registration is "required for debit transfers", which this host
        never sends. Until v0.3.17 such a machine got no transfer at all.
        One that insists on registration answers the transfer with status
        88 or 89 and no money moves.

        The asset number is still needed: the machine checks it on every
        0x72 (status 93). No asset from any poll is still a False.

        A registration that did not complete is tried again from here no
        sooner than REGISTER_RETRY_SEC later, so a machine that never
        registers does not spend three polls on it before every transfer."""
        p = self.poller
        reg = _read_registration(p.transport, p.address, p.protocol)
        if reg is not None and reg.registered:
            self._adopt(reg)
            self._register_again_at = 0.0
            return True, reg, "already registered"
        self.sleep(self.pace)
        asset = self.read_asset(reg=reg)
        if asset is None:
            return False, reg, "machine reports no asset number"
        if self.clock() >= self._register_again_at:
            ok, final, detail = aft_register(
                p.transport, p.address, asset, self.rig_key, self.pos_id,
                p.protocol, pace=self.pace, sleep=self.sleep)
            if ok and final is not None:
                self._adopt(final)
                self._register_again_at = 0.0
                return ok, final, detail
            reg = final if final is not None else reg
            self._unregistered_why = detail
            self._register_again_at = self.clock() + REGISTER_RETRY_SEC
        self.asset_number = asset
        self.registration_key = ZERO_KEY
        return True, reg, (
            f"not registered ({self._unregistered_why}); sending with a "
            "registration key of all zeros")

    def _adopt(self, reg: Optional[AFTRegistration]) -> None:
        """Adopt the authoritative key + asset (+ POS) from a register/
        read-back response record (keyPolicy DEFENSIVE RULE): overwrite the
        construction-time values so a later 0x72 transfer uses the key the
        MACHINE returned. On the BB2 the echoed key == the sent rig key, so
        this is defensive, not behavioral. Only non-zero fields overwrite —
        a status-only reply (all-empty) must never blank a good key."""
        if reg is None:
            return
        if len(reg.asset_number) == ASSET_NUMBER_LEN and any(reg.asset_number):
            self.asset_number = reg.asset_number
        if len(reg.registration_key) == REGISTRATION_KEY_LEN \
                and any(reg.registration_key):
            self.registration_key = reg.registration_key
        if len(reg.pos_id) == POS_ID_LEN and any(reg.pos_id):
            self.pos_id = reg.pos_id

    def read_lock_status(self) -> Optional[AFTGameLockStatus]:
        """Read the machine's AFT lock/availability state via a 0x74/FF
        interrogate — READ-ONLY (no lock requested, no money). Surfaces
        available_transfers / aft_status / balances / transfer_limit, the
        fields that say WHY a lockless push was refused. Returns None on
        silence/corruption."""
        p = self.poller
        return _read_lock(p.transport, p.address, p.protocol,
                          LOCK_CODE_INTERROGATE)

    def set_host_cashout(self, enable: bool) -> Optional[AFTGameLockStatus]:
        """Arm or disarm the machine's CASH OUT button for cashout to host
        (SAS 6.02 §8.7). Returns the machine's 0x74 status as it stands
        afterwards (None on silence); the caller reads
        `host_cashout_status` from it.

        The setting this is built for is HOST CONTROLLED: the machine
        leaves the enable to the host (status bit 0). Then the button
        prints a ticket until a player arms it, goes to the host while they
        are armed, and prints a ticket again once they disarm or card out.

        Arming, by what the host cashout status byte says:

          off, host controlled  switch it on: a 0x72 for a total of zero
                                whose flags carry "set the enable" and
                                "enable", soft mode.
          on already            nothing to do. The button raises exception
                                6A and the caller answers it.
          off, set at the       nothing is sent. The caller tells the owner
          machine               the button will keep printing a ticket
                                until the machine is set to host
                                controlled.

        Disarming switches it off again wherever the host controls it,
        whoever switched it on (a satellite that restarted while a player
        was armed does not remember that it did). A machine whose operator
        fixed it ON stays on; a 6A it raises while nobody is armed is
        declined by the caller with a zero-amount transfer, so the player
        gets a ticket at once.

        What this used to do (until v0.3.14): one read of 0x74 on an arm,
        and the caller took "available transfers" bit 1 for the answer.
        That bit is "a transfer from the machine might be accepted right
        now", so a machine with host cashout off was reported ready, the
        page said cash-out goes to the wallet, and the button printed a
        ticket. Earlier still it sent a game LOCK here, which locked the
        cabinet and expired in five seconds."""
        p = self.poller
        ls = _read_lock(p.transport, p.address, p.protocol,
                        LOCK_CODE_INTERROGATE)
        if ls is None:
            return None
        status = ls.host_cashout_status
        if not status & HOST_CASHOUT_CONTROLLABLE \
                or bool(status & HOST_CASHOUT_ENABLED) == bool(enable):
            return ls                   # not ours to set, or already there
        self._request_host_cashout(enable)
        self.sleep(self.pace)
        return _read_lock(p.transport, p.address, p.protocol,
                          LOCK_CODE_INTERROGATE) or ls

    def _request_host_cashout(self, enable: bool) -> bool:
        """Ask the machine to change its host cashout enable: a transfer
        for a total of zero with the control flag set (§8.7). True when the
        machine completed it. The enable only changes on a completed
        transfer, and "it is permissible for the gaming machine to override
        the host's requested Host Cashout Enable state", so the caller
        reads the status again instead of trusting this."""
        flags = AFT_FLAG_HOST_CASHOUT_CONTROL | (
            AFT_FLAG_HOST_CASHOUT_ENABLE if enable else 0)
        request = AFTTransferRequest(
            transaction_id=make_transaction_id(),
            transfer_type=TRANSFER_TYPE_HOST_TO_EGM,
            transfer_flags=flags, zero_ok=True)
        result = self.transfer(request, require_lock=False,
                               kind="aft_host_cashout_enable")
        return result.ok

    def transfer(self, request: AFTTransferRequest,
                 require_lock: bool = True,
                 on_wire: Optional[Callable[[str, bytes, bytes], None]] = None,
                 kind: str = "aft_transfer",
                 account_id: Optional[str] = None,
                 cmd_id: Optional[str] = None,
                 key: Optional[bytes] = None) -> AFTTransferResult:
        """Run one transfer. kind / account_id / cmd_id are what the caller
        will need to report its END if that comes later than this call
        (they are written down with it — see open_transfer.py).

        `key` sends this one transfer with that registration key instead of
        the one prepare_transfer settled on, and leaves the latter as it
        is. It exists for one experiment: ZERO_KEY to a machine that IS
        registered, which SAS 6.02 §8.1 recommends for every in-house
        transfer and which had not been tried on any machine."""
        p = self.poller
        if key is not None and len(key) != REGISTRATION_KEY_LEN:
            raise ValueError("registration key is exactly 20 bytes")
        if self.asset_number is None:
            # Built for registration only, never READ/adopted an asset.
            # build_aft_transfer_poll would len(None) -> TypeError; refuse
            # with a typed result instead. auto_register() reads+adopts the
            # asset, so the caller runs it first (the sas_host push does).
            result = AFTTransferResult(
                AFTOutcome.NOT_REGISTERED, AFTTxnState.CREATED, request,
                None, None, None, False,
                "no asset number known — auto_register (read asset) first")
            p._safe(self.on_transfer, p.address, result)
            return result
        # One cycle at a time (§8.3): "the host may not send another
        # initiating long poll 72 until the current transfer cycle is
        # complete" and its final status read. Ask once more; if the earlier
        # one still has no end, this one does not start.
        if self.journal.current is not None:
            ended = self.resolve_open()
            if ended is not None:
                self._ended.append(ended)
        earlier = self.journal.current
        if earlier is not None:
            result = AFTTransferResult(
                AFTOutcome.UNRESOLVED_EARLIER, AFTTxnState.CREATED, request,
                None, None, None, False,
                f"an earlier transfer (txn {earlier['txnId']}, "
                f"{earlier['cents']}c, started {earlier['startedAt']}) has "
                "no final status from the machine yet; nothing sent")
            p._safe(self.on_transfer, p.address, result)
            return result
        try:
            self.journal.open(
                request.transaction_id, kind, request.transfer_type,
                request.total_cents, account_id, cmd_id,
                credit_type=("restricted" if request.restricted_cents
                             and not request.cashable_cents else "cashable"))
        except OSError as e:
            result = AFTTransferResult(
                AFTOutcome.NOT_SAVED, AFTTxnState.CREATED, request, None,
                None, None, False,
                f"could not write the transfer down first ({e}); "
                "nothing sent")
            p._safe(self.on_transfer, p.address, result)
            return result
        # If this raises, the record stays: we do not know what the machine
        # did, and service_open() will find out.
        result = aft_transfer(
            p.transport, p.address, request, self.asset_number,
            self.registration_key if key is None else key, p.protocol,
            pace=self.pace, sleep=self.sleep, require_lock=require_lock,
            on_exception=p._dispatch_event,
            on_frame=p._handle_long_poll_result, on_wire=on_wire)
        if result.outcome is AFTOutcome.UNCONFIRMED:
            self.journal.mark_reported(result.outcome.value)
            self._asked = 0
            self._ask_at = self.clock() + OPEN_RETRY_SEC[0]
        else:
            # every other outcome is an end: a final status was read, or
            # nothing was sent
            self.journal.close()
            self._last_txn = request.transaction_id
        p._safe(self.on_transfer, p.address, result)
        return result

    def resolve_open(self) -> Optional[Dict]:
        """Ask the machine (72/FF) about the open transfer. Returns its END
        as {"rec", "ok", "outcome", "amountCents", "detail"} and forgets it,
        or None while it is still open. Poll thread only."""
        rec = self.journal.current
        if rec is None:
            return None
        p = self.poller
        verdict = judge_open_transfer(
            rec, _read_transfer_status(p.transport, p.address, p.protocol))
        if verdict is None:
            return None
        self.journal.close()
        self._last_txn = rec["txnId"]
        return dict(verdict, rec=rec)

    def service_open(self) -> List[Dict]:
        """Poll thread, BETWEEN polls: follow up the open transfer. Returns
        the ENDs to report (usually none).

        Asks when exception 69 has come in, and otherwise on a wait that
        grows to fifteen seconds. Exception 69 with nothing of ours open is
        still answered with one 72/FF: the machine raises it every fifteen
        seconds until its status has been read, whoever started the
        transfer (that END has rec=None and is only logged)."""
        ended, self._ended = self._ended, []
        rec = self.journal.current
        if rec is None:
            if self.complete_seen:
                self.complete_seen = False
                p = self.poller
                status = _read_transfer_status(p.transport, p.address,
                                               p.protocol)
                # a repeat of the 69 for a transfer we already saw end is
                # nothing new; reading it again is all the machine wants
                if status is not None and status.transaction_id \
                        and status.transaction_id != self._last_txn:
                    ended.append({
                        "rec": None, "ok": status.completed,
                        "outcome": "foreign", "txnId": status.transaction_id,
                        "amountCents": status.cashable_cents or 0,
                        "detail": "exception 69 for a transfer this "
                                  "satellite has no record of starting "
                                  f"(status 0x{status.status:02X})"})
            return ended
        now = self.clock()
        if not self.complete_seen and now < self._ask_at:
            return ended
        self.complete_seen = False
        verdict = self.resolve_open()
        if verdict is not None:
            ended.append(verdict)
        else:
            self._asked += 1
            self._ask_at = now + OPEN_RETRY_SEC[
                min(self._asked, len(OPEN_RETRY_SEC) - 1)]
        return ended

    def _typed_event(self, address: int, info) -> None:
        if self._prev_typed is not None:
            self.poller._safe(self._prev_typed, address, info)
        if address != self.poller.address:
            return
        if info.code in (0x6A, 0x6B):     # believed host-cashout requests
            self.poller._safe(self.on_cashout_request, address, info.code)
        elif info.code == AFT_TRANSFER_COMPLETE_EXCEPTION:
            # Not answered here (no transact inside an exception callback):
            # service_open() reads the status between polls.
            self.complete_seen = True
