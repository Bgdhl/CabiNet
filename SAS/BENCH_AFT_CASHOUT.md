# Bench bring-up: the machine's CASH OUT button to a wallet (SAS AFT host cashout)

Rewritten for v0.3.14 (2026-11-17) after reading SAS 6.02 section 8.7 and
Table 8.2b. The July version of this sheet told you to set the machine to
"soft cash-out to host, fail to ticket". **Set it to HOST CONTROLLED
instead.** Why is below.

**Where this stands:** the button path has not been seen working on a real
machine under this model. The tests run against a pretend machine that
follows the document. Fill in the blanks when you bench it.

## Two ways credits reach a wallet

| | What starts it | What the machine needs |
|---|---|---|
| **The hub pulls them** | CASH OUT TO WALLET on the player screen, or a tournament sweep | AFT registered, in-house transfers on. Nothing else. |
| **The machine's own CASH OUT button** | The player presses the button on the cabinet | The above, plus the machine's **cashout to host** option |

The pull is proven on a WMS Bluebird 2 and needs nothing from this sheet.
This sheet is about the button.

## Set the machine: cashout to host = HOST CONTROLLED

"Cashout to host" is a setting in the machine's operator menu. With it on,
the CASH OUT button asks the host first (SAS exception 0x6A) and waits up
to eight seconds for the host to take the credits.

Set it to **host controlled**. The machine then leaves the on/off to the
hub:

- Nobody armed: the setting is off. The button prints a ticket straight
  away and the host is never asked.
- A carded player switches **MACHINE CASH OUT BUTTON** to ON (the CASH OUT
  page on the SMIB player screen, or TO WALLET in MY WALLET on the digital
  glass): the satellite switches the setting on. The button now sends the
  credits to that player's wallet.
- They switch it off, or card out: the satellite switches the setting off
  again. Back to tickets.

What the other settings do:

| Machine setting | Nobody armed | A player armed |
|---|---|---|
| **Host controlled** (use this) | Ticket at once, host not asked | Credits go to the wallet |
| On, soft | Host is asked on every press; the satellite declines and the ticket prints about a second later | Credits go to the wallet |
| On, hard | Same, and if the satellite is down the machine tilts instead of printing | Credits go to the wallet |
| Off | Ticket at once | The arm is refused: "This machine's CASH OUT button prints a ticket..." |

```
Machine menu path (fill in on the bench): ____________________
```

## How it shows on the wire

The machine reports the setting in the **host cashout status** byte of its
`0x74` answer. The satellite reads it every 30 seconds and the hub shows it
in `/api/status` for the machine, as `aft.hostCashoutStatus` and decoded as
`sasHostCashout`.

| Byte | `sasHostCashout` | Meaning |
|---|---|---|
| `0x01` | controllable, not enabled | Host controlled, nobody armed. **What you want to see at rest.** |
| `0x03` | controllable, enabled | Host controlled, a player armed |
| `0x02` | enabled | Fixed on at the machine, soft |
| `0x06` | enabled, hard | Fixed on at the machine, hard |
| `0x00` | neither | Off, and not the host's to switch |

This is **not** `availXfers` bit 1 (`0x02`, "transfer from gaming machine
OK"). That bit says the hub's pull might be accepted. Until v0.3.14 the hub
read it as the cashout-to-host setting, armed machines whose button could
not do it, and told owners to "turn on soft cash-out to host".

To switch the setting the satellite sends a `0x72` transfer for a total of
zero with the host-cashout control flag set (`0x03` to switch on, `0x01` to
switch off). No game lock is involved.

## Before you start

- Load a small cashable balance and card in the test player.
- Watch the satellite: `journalctl -u cabinet-sas -f` on the machine's Pi.
  The lines to look for all start with 🏦.
- Watch the money on the hub: the Wallet page, or `/api/accounts`.

## 1. At rest

`/api/status` for the machine shows `sasHostCashout` with `controllable:
true, enabled: false`.

```
hostCashoutStatus at rest: 0x__   (expect 0x01)
```

## 2. Nobody armed: a ticket, and the host is not asked

Press **CASH OUT** with nobody armed.

- The machine prints a ticket at once.
- The satellite logs the ticket (`0x3D cash out ticket printed`) and **no**
  `host-cashout request` line.

```
ticket printed at once?  Y / N      any 0x6A in the log?  Y / N  (expect N)
```

## 3. Arm

Card in and switch **MACHINE CASH OUT BUTTON** to ON.

- Satellite: `🏦 hub command coarm…: AFT host-cashout ARMED (acct pXX;
  machine: ON, soft, host controlled)`.
- `/api/status`: `sasHostCashout` now `enabled: true` (byte `0x03`) at the
  next status read.
- If the line says `NOT armed … machine: off, set at the machine`, the
  machine is not set to host controlled. The page shows the reason and the
  switch stays OFF.

```
armed line seen?  Y / N       hostCashoutStatus while armed: 0x__  (expect 0x03)
```

## 4. Press CASH OUT: the credits go to the wallet

- `🏦 addr 1 host-cashout request 0x6A — armed, answering between polls`
- `🏦 host-cashout: pulling Nc EGM->host (acct pXX)`
- `🏦 0x72 REQ … | RESP …` (transfer type `0x80`)
- `🏦 host-cashout Nc -> completed (full transfer successful)`

The machine's credit meter drops by the amount and the wallet rises by the
same amount, once. The hub credits only what the machine's final status
says moved, under the ledger reference `aftcashout:<txn>`. The House
balance does not move.

```
0x72 type: 0x__ (expect 0x80)   status: 0x__ (expect 0x00, or 0x40 then 0x69)
credit meter  before: $____  after: $____   (expect −$X)
wallet (pXX)  before: $____  after: $____   (expect +$X, once)
House         before: $____  after: $____   (expect unchanged)
time from the press to the transfer: ____ ms   (the machine allows 8 s)
```

A win the machine wants to cash out to the host raises `0x6B` instead. The
satellite takes it with transfer type `0x90` for the whole win.

## 5. Disarm

Switch it OFF, and separately try carding out while it is ON.

- Satellite: `🏦 hub command codis…: AFT host-cashout disarmed (acct -;
  machine: off, host controlled)`.
- Press CASH OUT: a ticket at once, no `host-cashout request` line.

```
disarmed line seen?  Y / N    byte after: 0x__ (expect 0x01)    ticket?  Y / N
card-out disarms too?  Y / N
```

## Failure triage

| Symptom | Meaning | Move |
|---|---|---|
| Switch refuses: "This machine's CASH OUT button prints a ticket…" | The byte is `0x00`: cashout to host is off and set at the machine | Set it to host controlled in the operator menu |
| Switch refuses: "hasn't reported its cash-out setting yet" | The satellite has not read `0x74` since it started | Wait 30 seconds |
| Switch goes ON, then back OFF a moment later | The satellite could not switch the setting on. Its log has `NOT armed` with the machine's state | Read that line. If the byte stayed `0x01`, the machine refused the zero-amount transfer; note the `0x72` status |
| Armed, press prints a ticket, no `0x6A` in the log | The setting did not go on | Check the byte is `0x03` while armed |
| `0x6A` in the log but "declined" | The satellite was not armed when it came (it restarted, or the arm was for another session) | Switch it OFF and ON again |
| `0x6A`, then `no_credits` | The machine reported nothing on the credit meter | Load credits |
| Unarmed press takes a second before the ticket, with a "declined" line | The machine is set to on (soft or hard), not host controlled | Set it to host controlled |
| Wallet credited and the House balance moved | A House entry crept in | Look at `_settle_aft_cashouts`; this leg must not touch House |
| Wallet credited twice | The idempotency guard was bypassed | The `aftcashout:<txn>` reference must be the satellite's transaction id |

Keep the satellite journal from the run. It will be the first record of the
0x6A path on a real machine under this model.

## Regression nets (no bench needed)

- `pytest SAS/tests/test_host_cashout_requests.py`: the strict machine of
  `SAS/tests/by_the_book.py` pressed armed and unarmed, in soft and hard
  mode, host controlled and not; a win; the arm command end to end.
- `pytest SAS/tests/test_aft_host_cashout.py`: the transfer type byte, the
  lockless return, and that arming and disarming never send a game lock.
- `python3 G2S/tools/test_aft_push.py`: the hub's arm gate on each value of
  the byte, the refusal wording, the pin dropped when the satellite cannot
  arm, and that the page shows ON only while the button really is armed.
