"""
SAS general-poll exception codes — the typed event table.

Authority: IGT SAS Protocol 6.02, Appendix A, Table A-1 "General Exception
Codes". Every code that table defines has a name here (114 of them), and
nothing else does: a byte that is not in Table A-1 comes out of lookup() as
"exception 0xNN", category UNKNOWN.

The names live in three tables for a historical reason. Until 2026-11 the
only document on disk was the Montana DOJ SAS Implementation Guide v1.5.0,
whose §3.1 lists 32 of the codes:

  EXCEPTIONS             the guide's §3.1 list, plus 3E (its §4.6.2) and 1F.
                         Still exactly that list; tests pin it.
  BENCH_EXCEPTIONS       eleven codes the ticket, AFT and handpay flows
                         dispatch on (51, 52, 57, 66-6C, 6F). They were
                         carried as "believed, VERIFY_ON_BENCH" while only
                         the guide was on disk, and that tag was in the
                         names the hub showed. Table A-1 confirms every one
                         of them, so the tag is gone; the table keeps its
                         name because other modules refer to it.
  APPENDIX_A_EXCEPTIONS  the rest of Table A-1.

lookup() consults them in that order. No code is in two tables.

  0x51/0x52. SAS 6.02 §7.8.1: 0x51 "handpay is pending" is answered by
  READING long poll 1B (send handpay information), and the machine
  re-issues 0x51 every fifteen seconds until that read is acknowledged. It
  is not a request for a reset: a remote reset (0x94) on a machine that is
  no longer locked up answers code 02, "not currently in a handpay
  condition", and does not stop the 0x51s. 0x52 follows the reset. (An
  earlier version of this note said the opposite, that 0x51 asks for the
  reset and that reading 1B was "the wrong theory". The poller has read 1B
  on every 0x51 since then, which is what stopped the endless 0x51s on the
  BB2.)

Denominations: Table A-1 gives one for each "bill accepted" code except
4F ("use this exception for all bills without a specific exception", and
for every bill in real time event mode). Those entries carry `denom_cents`;
4F carries None.

Names are Table A-1's wording, shortened to fit the hub's 48-character
event label. Category and event token are CabiNet's own layer (the table
has no such columns).
"""

import enum
from dataclasses import dataclass
from typing import Dict, Optional


class ExceptionCategory(enum.Enum):
    """Coarse buckets for routing/UX (door lamp, tilt banner, bill ticker...)."""
    DOOR = "door"
    POWER = "power"
    BILL = "bill"
    TILT = "tilt"
    TICKET = "ticket"
    HANDPAY = "handpay"
    AFT = "aft"
    GAME = "game"
    SYSTEM = "system"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SASExceptionInfo:
    """One decoded general-poll exception.

    code        — the raw exception byte off the wire
    name        — human label (guide wording, shortened)
    category    — ExceptionCategory bucket
    event       — stable machine-readable token ('door_open', 'bill_inserted',
                  ...) for callbacks / the SMIB->server bridge
    denom_cents — bill denomination in cents, ONLY for the bill-accepted
                  codes whose denomination the guide documents (0x47-0x4A)
    """
    code: int
    name: str
    category: ExceptionCategory
    event: str
    denom_cents: Optional[int] = None


_C = ExceptionCategory

# Guide §3.1 (p.9) + 0x3E from §4.6.2. Names kept consistent with the
# pre-existing poller table (short forms of the guide's wording).
EXCEPTIONS: Dict[int, SASExceptionInfo] = {e.code: e for e in [
    SASExceptionInfo(0x00, "no activity", _C.SYSTEM, "no_activity"),
    # 0x1F cited by spec §12.6 (no-activity exceptions $00 and $1F): sent
    # instead of $00 while the machine is idle "and waiting for player".
    # Live-proven on the WMS BB2 2026-07-08 (streams in attract mode, flips
    # to $00 once credits are on the meter).
    SASExceptionInfo(0x1F, "no activity, waiting for player", _C.SYSTEM,
                     "no_activity"),
    # -- doors / physical access -------------------------------------------
    SASExceptionInfo(0x11, "slot door opened", _C.DOOR, "door_open"),
    SASExceptionInfo(0x12, "slot door closed", _C.DOOR, "door_closed"),
    SASExceptionInfo(0x13, "drop door opened", _C.DOOR, "door_open"),
    SASExceptionInfo(0x14, "drop door closed", _C.DOOR, "door_closed"),
    SASExceptionInfo(0x15, "card cage opened", _C.DOOR, "door_open"),
    SASExceptionInfo(0x16, "card cage closed", _C.DOOR, "door_closed"),
    SASExceptionInfo(0x19, "cashbox door opened", _C.DOOR, "door_open"),
    SASExceptionInfo(0x1A, "cashbox door closed", _C.DOOR, "door_closed"),
    SASExceptionInfo(0x1B, "cashbox removed", _C.DOOR, "cashbox_removed"),
    SASExceptionInfo(0x1C, "cashbox installed", _C.DOOR, "cashbox_installed"),
    SASExceptionInfo(0x1D, "belly door opened", _C.DOOR, "door_open"),
    SASExceptionInfo(0x1E, "belly door closed", _C.DOOR, "door_closed"),
    # -- power --------------------------------------------------------------
    SASExceptionInfo(0x17, "AC power applied", _C.POWER, "power_on"),
    SASExceptionInfo(0x18, "AC power lost", _C.POWER, "power_lost"),
    # -- bill acceptor faults (tilts) & rejections ---------------------------
    SASExceptionInfo(0x27, "cashbox full", _C.TILT, "cashbox_full"),
    SASExceptionInfo(0x28, "bill jam", _C.TILT, "bill_jam"),
    SASExceptionInfo(0x29, "bill acceptor hardware failure", _C.TILT,
                     "bill_acceptor_failure"),
    SASExceptionInfo(0x2B, "bill rejected", _C.BILL, "bill_rejected"),
    # -- config / system ------------------------------------------------------
    SASExceptionInfo(0x3C, "operator changed configuration", _C.SYSTEM,
                     "config_changed"),
    SASExceptionInfo(0x70, "exception buffer overflow", _C.SYSTEM,
                     "exception_buffer_overflow"),
    # -- tickets / validation -------------------------------------------------
    SASExceptionInfo(0x3D, "cash out ticket printed", _C.TICKET,
                     "ticket_printed"),
    SASExceptionInfo(0x3F, "validation ID not configured", _C.TICKET,
                     "validation_id_not_configured"),
    # -- handpay (0x3E cited by §4.6.2, not the §3.1 table) -------------------
    SASExceptionInfo(0x3E, "hand pay validated", _C.HANDPAY,
                     "handpay_validated"),
    # -- bills accepted, non-RTE, with guide-documented denominations ---------
    SASExceptionInfo(0x47, "$1 bill accepted", _C.BILL, "bill_inserted",
                     denom_cents=100),
    SASExceptionInfo(0x48, "$5 bill accepted", _C.BILL, "bill_inserted",
                     denom_cents=500),
    SASExceptionInfo(0x49, "$10 bill accepted", _C.BILL, "bill_inserted",
                     denom_cents=1000),
    SASExceptionInfo(0x4A, "$20 bill accepted", _C.BILL, "bill_inserted",
                     denom_cents=2000),
    # -- printer ---------------------------------------------------------------
    SASExceptionInfo(0x60, "printer comm error", _C.TILT, "printer_error"),
    SASExceptionInfo(0x61, "printer paper out", _C.TILT, "printer_paper_out"),
    # -- game ----------------------------------------------------------------
    SASExceptionInfo(0x7A, "game soft meter reset", _C.GAME, "meters_reset"),
    SASExceptionInfo(0x86, "game out of service", _C.GAME,
                     "game_out_of_service"),
    SASExceptionInfo(0x8C, "game selected", _C.GAME, "game_selected"),
]}

# --------------------------------------------------------------------------
# The codes the ticket, AFT and handpay flows dispatch on (core/
# sas_tito_host.py, modules/aft/, core/sas_handpay_reset.py). The Montana
# guide documents none of them, so they were kept apart from its table and
# marked VERIFY_ON_BENCH. SAS 6.02 Table A-1 has every one, with the meaning
# used here, and each has since been seen on a WMS Bluebird 2. The table
# keeps its name; the tag is gone from the names (it was showing in the
# hub's event list, and pushed two labels past the hub's 48 characters).
# --------------------------------------------------------------------------
BENCH_EXCEPTIONS: Dict[int, SASExceptionInfo] = {e.code: e for e in [
    # -- handpay queue (SAS 6.01 §7.8.1, spec-read 2026-07-08) ---------------
    # 0x51 = "handpay is pending" — but in the MODERN handpay-queue mode the
    # EGM RE-ISSUES 0x51 every 15s for as long as an unresolved handpay entry
    # sits in its FIFO queue, EVEN WHEN the physical lockup is already keyed
    # off and the machine is playable (AJ, bench-correct: 0x51 spam on an
    # UNLOCKED machine is a stuck/queued handpay, NOT a live lock). The host
    # is expected to answer 0x51 by reading LP 0x1B (handpay info); the queue
    # entry clears through the 1B read+ack lifecycle, and 0x52 is sent only
    # once the handpay is also reset. LP 0x94 does NOT clear this: on an
    # unlocked machine it returns reset-code 02 "not currently in a handpay
    # condition" (§7.9). §7.8.2 gives the operator config to disable the 15s
    # re-issue / enable legacy reporting. See [[reference_cabinet_sas_adjudication]].
    SASExceptionInfo(0x51, "handpay pending (queued; re-issued per §7.8.1)",
                     _C.HANDPAY, "handpay_pending"),
    SASExceptionInfo(0x52, "handpay was reset", _C.HANDPAY,
                     "handpay_was_reset"),
    # -- system-validation ticket-out (core/sas_tito_host.py) ----------------
    # 0x57 "system validation request" is the real-time cash-out trigger
    # (SAS 6.02 §15.8) and 0x66 "cash out button pressed" is its
    # informational precursor. 0x57 is serviced by
    # SASTITOHost.service_validation; 0x66 is observe-only.
    SASExceptionInfo(0x57, "system validation request",
                     _C.TICKET, "system_validation_request"),
    SASExceptionInfo(0x66, "cash out button pressed",
                     _C.TICKET, "cashout_button_pressed"),
    # -- ticket-in redemption cycle (core/sas_tito_host.py) ------------------
    SASExceptionInfo(0x67, "ticket inserted", _C.TICKET,
                     "ticket_inserted"),
    SASExceptionInfo(0x68, "ticket transfer complete",
                     _C.TICKET, "ticket_transfer_complete"),
    # -- AFT (modules/aft/aft_handler.py) ------------------------------------
    SASExceptionInfo(0x69, "AFT transfer complete", _C.AFT,
                     "aft_transfer_complete"),
    SASExceptionInfo(0x6A, "AFT request for host cashout",
                     _C.AFT, "aft_host_cashout_request"),
    SASExceptionInfo(0x6B, "AFT request for host to cash out win", _C.AFT,
                     "aft_host_cashout_win_request"),
    SASExceptionInfo(0x6C, "AFT request to register",
                     _C.AFT, "aft_register_request"),
    # 0x6F "game locked" — LIVE-PROVEN 2026-07-09 21:27 (BB2): fired the
    # instant our lock-first credit push established the AFT game lock
    # (6.02 §8.2: "If the gaming machine is able to establish the lock, it
    # will issue exception 6F, game locked" — a PRIORITY exception, issued
    # live while locked, never queued). Benign/expected during every
    # lock-first aft_transfer; observe-only (the push flow already reads
    # the 0x74 status itself).
    SASExceptionInfo(0x6F, "game locked (AFT lock established)", _C.AFT,
                     "aft_game_locked"),
]}

# --------------------------------------------------------------------------
# The rest of SAS 6.02 Table A-1: every code the two tables above leave out.
# Kept apart so the guide-cited table stays what it says it is.
#
# Two reasons these need names. The log and the hub's event list showed
# "exception 0x7E" for every game start on a machine that reports them
# (audit item 29). And each machine's address ORed with 0x80 falls in
# 80-9B, where the poller used to take that one byte for a NACK and drop it
# (audit item 9): at address 2 that was 82, every trip into the attendant
# menu.
APPENDIX_A_EXCEPTIONS: Dict[int, SASExceptionInfo] = {e.code: e for e in [
    # -- tilts -----------------------------------------------------------------
    SASExceptionInfo(0x20, "general tilt", _C.TILT, "general_tilt"),
    SASExceptionInfo(0x21, "coin in tilt", _C.TILT, "coin_in_tilt"),
    SASExceptionInfo(0x22, "coin out tilt", _C.TILT, "coin_out_tilt"),
    SASExceptionInfo(0x23, "hopper empty detected", _C.TILT, "hopper_empty"),
    SASExceptionInfo(0x24, "extra coin paid", _C.TILT, "extra_coin_paid"),
    SASExceptionInfo(0x25, "diverter malfunction", _C.TILT,
                     "diverter_malfunction"),
    SASExceptionInfo(0x2A, "reverse bill detected", _C.BILL, "reverse_bill"),
    SASExceptionInfo(0x2C, "counterfeit bill detected", _C.BILL,
                     "counterfeit_bill"),
    SASExceptionInfo(0x2D, "reverse coin in detected", _C.TILT,
                     "reverse_coin_in"),
    SASExceptionInfo(0x2E, "cashbox near full detected", _C.TILT,
                     "cashbox_near_full"),
    # -- memory ----------------------------------------------------------------
    SASExceptionInfo(0x31, "CMOS RAM error (data recovered from EEPROM)",
                     _C.TILT, "cmos_ram_error_recovered"),
    SASExceptionInfo(0x32, "CMOS RAM error (no data recovered)", _C.TILT,
                     "cmos_ram_error_not_recovered"),
    SASExceptionInfo(0x33, "CMOS RAM error (bad device)", _C.TILT,
                     "cmos_ram_bad_device"),
    SASExceptionInfo(0x34, "EEPROM error (data error)", _C.TILT,
                     "eeprom_data_error"),
    SASExceptionInfo(0x35, "EEPROM error (bad device)", _C.TILT,
                     "eeprom_bad_device"),
    SASExceptionInfo(0x36, "EPROM error (checksum differs, version changed)",
                     _C.TILT, "eprom_version_changed"),
    SASExceptionInfo(0x37, "EPROM error (bad checksum compare)", _C.TILT,
                     "eprom_bad_checksum"),
    SASExceptionInfo(0x38, "partitioned EPROM error (version changed)",
                     _C.TILT, "partitioned_eprom_version_changed"),
    SASExceptionInfo(0x39, "partitioned EPROM error (bad checksum compare)",
                     _C.TILT, "partitioned_eprom_bad_checksum"),
    SASExceptionInfo(0x3A, "memory error reset (self test switch used)",
                     _C.SYSTEM, "memory_error_reset"),
    SASExceptionInfo(0x3B, "low backup battery detected", _C.SYSTEM,
                     "low_backup_battery"),
    # -- reels -----------------------------------------------------------------
    SASExceptionInfo(0x40, "reel tilt", _C.TILT, "reel_tilt"),
    SASExceptionInfo(0x41, "reel 1 tilt", _C.TILT, "reel_tilt"),
    SASExceptionInfo(0x42, "reel 2 tilt", _C.TILT, "reel_tilt"),
    SASExceptionInfo(0x43, "reel 3 tilt", _C.TILT, "reel_tilt"),
    SASExceptionInfo(0x44, "reel 4 tilt", _C.TILT, "reel_tilt"),
    SASExceptionInfo(0x45, "reel 5 tilt", _C.TILT, "reel_tilt"),
    SASExceptionInfo(0x46, "reel mechanism disconnected", _C.TILT,
                     "reel_disconnected"),
    # -- bills accepted. 4B-4E and 50 are sent outside real time event mode
    # only. 4F is the code for a bill with no code of its own, and for every
    # bill in real time event mode, so it has no denomination. ---------------
    SASExceptionInfo(0x4B, "$50 bill accepted", _C.BILL, "bill_inserted",
                     denom_cents=5000),
    SASExceptionInfo(0x4C, "$100 bill accepted", _C.BILL, "bill_inserted",
                     denom_cents=10000),
    SASExceptionInfo(0x4D, "$2 bill accepted", _C.BILL, "bill_inserted",
                     denom_cents=200),
    SASExceptionInfo(0x4E, "$500 bill accepted", _C.BILL, "bill_inserted",
                     denom_cents=50000),
    SASExceptionInfo(0x4F, "bill accepted", _C.BILL, "bill_inserted"),
    SASExceptionInfo(0x50, "$200 bill accepted", _C.BILL, "bill_inserted",
                     denom_cents=20000),
    # -- progressives ----------------------------------------------------------
    SASExceptionInfo(0x53, "no progressive information for 5 seconds",
                     _C.SYSTEM, "no_progressive_info"),
    SASExceptionInfo(0x54, "progressive win (cashout device/credit paid)",
                     _C.GAME, "progressive_win"),
    # 55 ends a handpay request with no 52: the HANDPAY banner clears on it
    # (sas_host.HandpayLatch).
    SASExceptionInfo(0x55, "player cancelled the handpay request",
                     _C.HANDPAY, "handpay_cancelled"),
    SASExceptionInfo(0x56, "SAS progressive level hit", _C.GAME,
                     "progressive_level_hit"),
    # -- AFT registration (the two the flows do not act on) --------------------
    SASExceptionInfo(0x6D, "AFT registration acknowledged", _C.AFT,
                     "aft_registration_acknowledged"),
    SASExceptionInfo(0x6E, "AFT registration cancelled", _C.AFT,
                     "aft_registration_cancelled"),
    # -- lamp, printer, coin mechanism -----------------------------------------
    SASExceptionInfo(0x71, "change lamp on", _C.SYSTEM, "change_lamp_on"),
    SASExceptionInfo(0x72, "change lamp off", _C.SYSTEM, "change_lamp_off"),
    SASExceptionInfo(0x74, "printer paper low", _C.TILT, "printer_paper_low"),
    SASExceptionInfo(0x75, "printer power off", _C.TILT, "printer_power_off"),
    SASExceptionInfo(0x76, "printer power on", _C.SYSTEM, "printer_power_on"),
    SASExceptionInfo(0x77, "replace printer ribbon", _C.TILT,
                     "printer_ribbon"),
    SASExceptionInfo(0x78, "printer carriage jammed", _C.TILT,
                     "printer_carriage_jam"),
    SASExceptionInfo(0x79, "coin in lockout malfunction", _C.TILT,
                     "coin_lockout_malfunction"),
    # -- meters and game -------------------------------------------------------
    SASExceptionInfo(0x7B, "bill validator period totals reset", _C.SYSTEM,
                     "bill_validator_totals_reset"),
    SASExceptionInfo(0x7C, "legacy bonus pay or multiplied jackpot", _C.GAME,
                     "legacy_bonus_pay"),
    SASExceptionInfo(0x7E, "game started", _C.GAME, "game_started"),
    SASExceptionInfo(0x7F, "game ended", _C.GAME, "game_ended"),
    # -- 80-9B -----------------------------------------------------------------
    SASExceptionInfo(0x80, "hopper full detected", _C.TILT, "hopper_full"),
    SASExceptionInfo(0x81, "hopper level low detected", _C.TILT,
                     "hopper_low"),
    SASExceptionInfo(0x82, "display meters or attendant menu entered",
                     _C.SYSTEM, "attendant_menu_entered"),
    SASExceptionInfo(0x83, "display meters or attendant menu exited",
                     _C.SYSTEM, "attendant_menu_exited"),
    SASExceptionInfo(0x84, "self test or operator menu entered", _C.SYSTEM,
                     "operator_menu_entered"),
    SASExceptionInfo(0x85, "self test or operator menu exited", _C.SYSTEM,
                     "operator_menu_exited"),
    SASExceptionInfo(0x87, "player has requested draw cards", _C.GAME,
                     "draw_requested"),
    SASExceptionInfo(0x88, "reel has stopped", _C.GAME, "reel_stopped"),
    SASExceptionInfo(0x89, "coin/credit wagered", _C.GAME, "credit_wagered"),
    SASExceptionInfo(0x8A, "game recall entry displayed", _C.GAME,
                     "game_recall"),
    SASExceptionInfo(0x8B, "card held/not held", _C.GAME, "card_held"),
    SASExceptionInfo(0x8E, "component list changed", _C.SYSTEM,
                     "component_list_changed"),
    SASExceptionInfo(0x8F, "authentication complete", _C.SYSTEM,
                     "authentication_complete"),
    SASExceptionInfo(0x98, "power off card cage access", _C.DOOR,
                     "power_off_card_cage_access"),
    SASExceptionInfo(0x99, "power off slot door access", _C.DOOR,
                     "power_off_slot_door_access"),
    SASExceptionInfo(0x9A, "power off cashbox door access", _C.DOOR,
                     "power_off_cashbox_door_access"),
    SASExceptionInfo(0x9B, "power off drop door access", _C.DOOR,
                     "power_off_drop_door_access"),
]}

# Every code Table A-1 defines, as numbers, transcribed from the table
# itself and not from the naming tables above, so a test can hold the two
# against each other. The link layer asks it one question: is a lone byte
# equal to a machine's own address its loop-break chirp, or an exception
# (sas_wait.is_chirp)? Asking the naming tables used to get that wrong for
# every address whose value they happened to leave out.
APPENDIX_A_CODES = frozenset(
    list(range(0x11, 0x26)) + list(range(0x27, 0x2F))
    + list(range(0x31, 0x58)) + [0x60, 0x61]
    + list(range(0x66, 0x73)) + list(range(0x74, 0x7D)) + [0x7E, 0x7F]
    + list(range(0x80, 0x8D)) + [0x8E, 0x8F] + list(range(0x98, 0x9C))
    + [0x00])

# Convenience: bill-accepted exception code -> denomination in cents, for
# every code Table A-1 gives one to (47-4E and 50; not 4F).
BILL_DENOM_CENTS: Dict[int, int] = {
    c: e.denom_cents
    for table in (EXCEPTIONS, APPENDIX_A_EXCEPTIONS)
    for c, e in table.items()
    if e.event == "bill_inserted" and e.denom_cents is not None
}

# Back-compat flat name map (what sas_poller historically exported).
EXCEPTION_NAMES: Dict[int, str] = {c: e.name for c, e in EXCEPTIONS.items()}


def lookup(code: int) -> SASExceptionInfo:
    """Return the table entry for a code: the guide's list first, then the
    codes the ticket, AFT and handpay flows dispatch on, then the rest of
    SAS 6.02 Table A-1. A byte that table does not define gets a
    synthesized UNKNOWN entry. Never raises, so such a byte still flows
    through the event path with its raw value visible."""
    info = EXCEPTIONS.get(code)
    if info is None:
        info = BENCH_EXCEPTIONS.get(code)
    if info is None:
        info = APPENDIX_A_EXCEPTIONS.get(code)
    if info is not None:
        return info
    return SASExceptionInfo(code, f"exception 0x{code:02X}",
                            ExceptionCategory.UNKNOWN, "unknown_exception")


def exception_name(code: int) -> str:
    return lookup(code).name
