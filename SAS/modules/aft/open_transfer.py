"""
The one AFT transfer this satellite has started and not yet seen the end of.

Why this exists (2026-11-16, SAS 6.02 audit item 2). A transfer cycle
"begins when the gaming machine receives an initiating 72 long poll, and
ends when the host retrieves the final transfer and transaction receipt
completion status codes" (§8.3). Until then the machine raises exception 69
every fifteen seconds, "must not accept a new transfer", and never enters
the locked state (§8.2). And: "It is the responsibility of the host to read
the completion status."

aft_transfer() waits about three seconds for that status, tries one cancel,
and returns UNCONFIRMED. Nothing asked again afterwards, and nothing was
written down, so a transfer that completed a moment later — or while the
satellite was restarting — moved credits with no ledger entry on the hub:
a push could be sent a second time, a cash-out left the machine and never
reached the wallet, and a machine that keeps the document then refused
every later transfer.

So the transfer is written here BEFORE its 0x72 goes out, and stays until a
final answer for it has been read. One record, because the document allows
one cycle at a time.

path=None keeps it in memory only (mock and test runs, so nothing is left
in the tree). Stdlib only; writes are tmp + fsync + rename, like the ticket
store.
"""

import json
import os
import time
from typing import Dict, Optional

KEYS = ("txnId", "kind", "transferType", "cents", "accountId", "cmdId",
        "startedAt", "reported", "creditType")


class OpenTransferJournal:
    def __init__(self, path: Optional[str] = None):
        self.path = path
        self._rec: Optional[Dict] = None
        self.corrupt = None           # why a file on disk was set aside
        if path:
            self._rec = self._load()

    # -- reading ---------------------------------------------------------------

    def _load(self) -> Optional[Dict]:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                rec = json.load(f)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            return self._set_aside(f"unreadable: {e}")
        if not isinstance(rec, dict) or not isinstance(rec.get("txnId"), str) \
                or not rec.get("txnId"):
            return self._set_aside("no transaction id in it")
        return {k: rec.get(k) for k in KEYS}

    def _set_aside(self, why: str) -> None:
        """A journal we cannot read is not proof that nothing is open. Keep
        the file under another name for a person, and say so."""
        self.corrupt = why
        try:
            os.replace(self.path, f"{self.path}.corrupt-{int(time.time())}")
        except OSError:
            pass
        return None

    @property
    def current(self) -> Optional[Dict]:
        return dict(self._rec) if self._rec else None

    # -- writing ---------------------------------------------------------------

    def open(self, txn_id: str, kind: str, transfer_type: int, cents: int,
             account_id: Optional[str] = None,
             cmd_id: Optional[str] = None,
             credit_type: str = "cashable") -> Dict:
        """Write the transfer down. Raises OSError if it cannot be saved:
        the caller must then NOT send the transfer."""
        rec = {"txnId": str(txn_id), "kind": str(kind),
               "transferType": int(transfer_type), "cents": int(cents),
               "accountId": account_id, "cmdId": cmd_id,
               "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "reported": None, "creditType": str(credit_type)}
        self._save(rec)
        self._rec = rec
        return dict(rec)

    def mark_reported(self, outcome: str) -> None:
        """The first verdict the hub was given for it (e.g. 'unconfirmed')."""
        if self._rec is None:
            return
        rec = dict(self._rec, reported=str(outcome))
        try:
            self._save(rec)
        except OSError:
            pass                      # the record itself is already on disk
        self._rec = rec

    def close(self) -> None:
        self._rec = None
        if not self.path:
            return
        try:
            os.remove(self.path)
        except FileNotFoundError:
            pass
        self._sync_dir()

    def _save(self, rec: Dict) -> None:
        if not self.path:
            return
        folder = os.path.dirname(self.path) or "."
        os.makedirs(folder, exist_ok=True)
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rec, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
        self._sync_dir()

    def _sync_dir(self) -> None:
        try:
            dfd = os.open(os.path.dirname(self.path) or ".", os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(dfd)
        except OSError:
            pass
        finally:
            os.close(dfd)
