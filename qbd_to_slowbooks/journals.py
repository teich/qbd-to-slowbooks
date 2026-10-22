"""QuickBooks history as SlowBooks journal entries.

qbd-exporter's ``TransactionRet`` records are QuickBooks' own postings: one
row per transaction line, with the account and a signed amount (debit
positive). Grouped by ``TxnID`` they balance to the cent, and summed by
account they reproduce QuickBooks' balances, so posting them as journal
entries carries the ledger across without re-deriving anything (no inventory
costing, no tax rules). Each entry keeps the QuickBooks ``TxnID`` as its
reference.

Estimates, sales orders and purchase orders are non-posting in QuickBooks but
still appear in the transaction query, so they are dropped here.
"""

from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from decimal import Decimal

from .slowbooks import ApiError, SlowBooks
from .source import one, ref_id, ref_name

NON_POSTING = {"Estimate", "SalesOrder", "PurchaseOrder"}


@dataclass
class JournalResult:
    built: int = 0
    posted: int = 0
    empty: int = 0  # voided transactions: every line zero
    errors: list[str] = field(default_factory=list)


def build_entries(postings: list[dict], accounts: dict[str, int], result: JournalResult) -> list[dict]:
    by_txn: dict[str, list[dict]] = defaultdict(list)
    for p in postings:
        if one(p, "TxnType") in NON_POSTING or not one(p, "AccountRef"):
            continue
        by_txn[one(p, "TxnID")].append(p)

    entries = []
    for txn_id, lines in by_txn.items():
        first = lines[0]
        label = " ".join(v for v in (one(first, "TxnType"), one(first, "RefNumber")) if v)
        total = sum(Decimal(one(p, "Amount", "0")) for p in lines)
        if total != 0:
            result.errors.append(f"{txn_id} {label}: lines do not balance ({total})")
            continue
        missing = {ref_name(p, "AccountRef") for p in lines if ref_id(p, "AccountRef") not in accounts}
        if missing:
            result.errors.append(f"{txn_id} {label}: no SlowBooks account for {', '.join(sorted(missing))}")
            continue

        out_lines = []
        for p in lines:
            amount = Decimal(one(p, "Amount", "0"))
            if amount == 0:
                continue
            words = " — ".join(v for v in (ref_name(p, "EntityRef"), one(p, "Memo")) if v)
            out_lines.append(
                {
                    "account_id": accounts[ref_id(p, "AccountRef")],
                    "debit": str(amount) if amount > 0 else "0",
                    "credit": str(-amount) if amount < 0 else "0",
                    "description": words[:300] or None,
                }
            )
        if not out_lines:
            result.empty += 1
            continue

        entity = next((ref_name(p, "EntityRef") for p in lines if one(p, "EntityRef")), None)
        entries.append(
            {
                "date": one(first, "TxnDate"),
                "description": " · ".join(v for v in (label, entity) if v),
                "reference": txn_id,
                "is_adjusting": False,
                "lines": out_lines,
            }
        )
    entries.sort(key=lambda e: (e["date"], e["reference"]))
    result.built = len(entries)
    return entries


def post_entries(sb: SlowBooks, entries: list[dict], result: JournalResult, workers: int, progress=None):
    def post(entry):
        try:
            sb.post("/api/journal", entry)
            return None
        except ApiError as e:
            return f"{entry['reference']} {entry['description']}: {e.status} {e.detail}"

    with ThreadPoolExecutor(workers) as pool:
        for n, err in enumerate(pool.map(post, entries), 1):
            if err:
                result.errors.append(err)
            else:
                result.posted += 1
            if progress and n % 1000 == 0:
                progress(n, len(entries))
