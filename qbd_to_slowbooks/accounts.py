"""The QuickBooks chart of accounts, carried into SlowBooks.

SlowBooks posts its own documents to a handful of control accounts it finds
by number (1100 Accounts Receivable, 2000 Accounts Payable, ...). QuickBooks'
matching accounts are mapped onto those, renamed to the QuickBooks name, so
documents loaded later post where QuickBooks' history already sits. Every
other QuickBooks account is created. Seeded accounts nothing uses are removed
at the end of the load.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .slowbooks import ApiError, SlowBooks
from .source import one, ref_id

# QuickBooks AccountType -> (SlowBooks account_type, bank_kind)
TYPES = {
    "Bank": ("asset", "bank"),
    "AccountsReceivable": ("asset", None),
    "OtherCurrentAsset": ("asset", None),
    "FixedAsset": ("asset", None),
    "OtherAsset": ("asset", None),
    "AccountsPayable": ("liability", None),
    "CreditCard": ("liability", "credit_card"),
    "OtherCurrentLiability": ("liability", None),
    "LongTermLiability": ("liability", None),
    "Equity": ("equity", None),
    "Income": ("income", None),
    "OtherIncome": ("income", None),
    "CostOfGoodsSold": ("cogs", None),
    "Expense": ("expense", None),
    "OtherExpense": ("expense", None),
}

# QuickBooks SpecialAccountType (or AccountType) -> SlowBooks control number
CONTROLS = {
    "AccountsReceivable": "1100",
    "UndepositedFunds": "1200",
    "InventoryAssets": "1300",
    "AccountsPayable": "2000",
    "SalesTaxPayable": "2200",
    "RetainedEarnings": "3200",
    "CostOfGoodsSold": "5000",
}

# Balance-sheet types whose QuickBooks Balance is debit-positive; the other
# balance-sheet types report credit balances as positive.
DEBIT_NORMAL = {"Bank", "AccountsReceivable", "OtherCurrentAsset", "FixedAsset", "OtherAsset"}
BALANCE_SHEET = DEBIT_NORMAL | {
    "AccountsPayable",
    "CreditCard",
    "OtherCurrentLiability",
    "LongTermLiability",
    "Equity",
}


@dataclass
class Chart:
    ids: dict[str, int] = field(default_factory=dict)  # QuickBooks ListID -> SlowBooks id
    created: int = 0
    mapped: list[str] = field(default_factory=list)  # "QB name -> SB number"
    skipped: list[str] = field(default_factory=list)  # non-posting accounts


def _control_number(acct: dict) -> str | None:
    special = one(acct, "SpecialAccountType")
    if special in CONTROLS:
        return CONTROLS[special]
    if one(acct, "AccountType") in ("AccountsReceivable", "AccountsPayable"):
        return CONTROLS[one(acct, "AccountType")]
    return None


def load_chart(sb: SlowBooks, accounts: list[dict]) -> Chart:
    chart = Chart()
    existing = sb.get("/api/accounts")
    by_number = {a["account_number"]: a for a in existing if a.get("account_number")}
    taken_numbers = set(by_number)
    used_controls: set[str] = set()

    # Parents before their sub-accounts.
    for acct in sorted(accounts, key=lambda a: (int(one(a, "Sublevel", "0")), one(a, "FullName"))):
        list_id = one(acct, "ListID")
        qb_type = one(acct, "AccountType")
        if qb_type not in TYPES:
            chart.skipped.append(f"{one(acct, 'FullName')} ({qb_type})")
            continue
        sb_type, bank_kind = TYPES[qb_type]
        parent = ref_id(acct, "ParentRef")
        body = {
            "name": one(acct, "Name"),
            "account_type": sb_type,
            "parent_id": chart.ids.get(parent) if parent else None,
            "description": one(acct, "Desc"),
            "bank_kind": bank_kind,
        }

        control = _control_number(acct)
        if control and control in by_number and control not in used_controls:
            # One QuickBooks account per control (a second A/R account is
            # just another asset account here).
            used_controls.add(control)
            target = by_number[control]
            try:
                sb.put(f"/api/accounts/{target['id']}", {**body, "is_active": True})
            except ApiError as e:
                # The posting code needs it, so a refused rename only costs
                # the QuickBooks name.
                chart.mapped.append(f"{one(acct, 'FullName')} -> {control} (kept SlowBooks name: {e.detail})")
            else:
                chart.mapped.append(f"{one(acct, 'FullName')} -> {control}")
            chart.ids[list_id] = target["id"]
            continue

        number = one(acct, "AccountNumber")
        if number and number not in taken_numbers and len(number) <= 20:
            body["account_number"] = number
            taken_numbers.add(number)
        created = sb.post("/api/accounts", body)
        chart.ids[list_id] = created["id"]
        chart.created += 1
        if one(acct, "IsActive") == "false":
            sb.put(f"/api/accounts/{created['id']}", {"is_active": False})
    return chart


def qb_balance_as_net(acct: dict) -> float | None:
    """A balance-sheet account's QuickBooks Balance as SlowBooks' debit-minus-
    credit net, or None for income-statement and non-posting accounts."""
    qb_type = one(acct, "AccountType")
    if qb_type not in BALANCE_SHEET:
        return None
    bal = float(one(acct, "Balance", "0"))
    return bal if qb_type in DEBIT_NORMAL else -bal
