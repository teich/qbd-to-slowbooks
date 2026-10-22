"""Customers, vendors and items.

Each record becomes the SlowBooks record that holds what QuickBooks knew of
it. Customer jobs (``Customer:Job``) arrive as customers named by their full
path for now. Item quantities start at zero; history comes in through the
journal entries, not through the item records.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .slowbooks import ApiError, SlowBooks
from .source import one, ref_id, ref_name

EMAIL = re.compile(r"[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+")


@dataclass
class ListResult:
    kind: str
    created: int = 0
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _clip(value: str | None, size: int) -> str | None:
    return value[:size] if value else None


def _email(rec: dict) -> str | None:
    found = EMAIL.search(one(rec, "Email") or "")
    return found.group(0)[:200] if found else None


def _address(rec: dict, key: str, prefix: str) -> dict:
    addr = one(rec, key)
    if not addr:
        return {}
    more = ", ".join(v for v in (one(addr, f"Addr{n}") for n in (3, 4, 5)) if v)
    line2 = ", ".join(v for v in (one(addr, "Addr2"), more) if v)
    return {
        f"{prefix}address1": _clip(one(addr, "Addr1"), 200),
        f"{prefix}address2": _clip(line2, 200),
        f"{prefix}city": _clip(one(addr, "City"), 100),
        f"{prefix}state": _clip(one(addr, "State"), 50),
        f"{prefix}zip": _clip(one(addr, "PostalCode"), 20),
    }


def customer_body(rec: dict) -> dict:
    return {
        "name": _clip(one(rec, "FullName") or one(rec, "Name"), 200),
        "company": _clip(one(rec, "CompanyName"), 200),
        "email": _email(rec),
        "phone": _clip(one(rec, "Phone"), 50),
        "mobile": _clip(one(rec, "Mobile"), 50),
        "fax": _clip(one(rec, "Fax"), 50),
        "terms": _clip(ref_name(rec, "TermsRef"), 50),
        "tax_id": _clip(one(rec, "ResaleNumber"), 50),
        "notes": one(rec, "Notes"),
        **_address(rec, "BillAddress", "bill_"),
        **_address(rec, "ShipAddress", "ship_"),
    }


def vendor_body(rec: dict) -> dict:
    body = {
        "name": _clip(one(rec, "Name"), 200),
        "company": _clip(one(rec, "CompanyName"), 200),
        "email": _email(rec),
        "phone": _clip(one(rec, "Phone"), 50),
        "fax": _clip(one(rec, "Fax"), 50),
        "tax_id": _clip(one(rec, "VendorTaxIdent"), 50),
        "account_number": _clip(one(rec, "AccountNumber"), 50),
        "is_1099_vendor": one(rec, "IsVendorEligibleFor1099") == "true",
        "notes": one(rec, "Notes"),
        **_address(rec, "VendorAddress", ""),
    }
    terms = ref_name(rec, "TermsRef")
    if terms:
        body["terms"] = terms[:50]
    return body


# QuickBooks item record -> (SlowBooks item_type, track_inventory). Other item
# kinds (subtotal, discount, sales tax, group, payment, fixed asset) are parts
# of QuickBooks' form layout, not things sold, and are left out.
ITEM_TYPES = {
    "ItemInventoryRet": ("product", True),
    "ItemInventoryAssemblyRet": ("product", True),
    "ItemNonInventoryRet": ("product", False),
    "ItemServiceRet": ("service", False),
    "ItemOtherChargeRet": ("service", False),
}


def item_body(record_type: str, rec: dict, accounts: dict[str, int]) -> dict:
    item_type, tracked = ITEM_TYPES[record_type]
    # Non-inventory, service and other-charge items keep their accounts and
    # prices under SalesOrPurchase or SalesAndPurchase.
    sop = one(rec, "SalesOrPurchase")
    sap = one(rec, "SalesAndPurchase")

    def acct(*pairs):
        for holder, key in pairs:
            if holder and ref_id(holder, key) in accounts:
                return accounts[ref_id(holder, key)]
        return None

    return {
        "name": _clip(one(rec, "FullName"), 200),
        "item_type": item_type,
        "description": one(rec, "SalesDesc") or one(sop, "Desc") or one(sap, "SalesDesc"),
        "rate": one(rec, "SalesPrice") or one(sop, "Price") or one(sap, "SalesPrice") or "0",
        "cost": one(rec, "PurchaseCost") or one(sap, "PurchaseCost") or "0",
        "income_account_id": acct((rec, "IncomeAccountRef"), (sop, "AccountRef"), (sap, "IncomeAccountRef")),
        "expense_account_id": acct((rec, "COGSAccountRef"), (sap, "ExpenseAccountRef")),
        "asset_account_id": acct((rec, "AssetAccountRef")),
        "is_taxable": ref_name(rec, "SalesTaxCodeRef") != "Non",
        "track_inventory": tracked,
        "quantity_on_hand": "0",
    }


def _post_all(sb: SlowBooks, path: str, records: list[tuple[dict, dict]], result: ListResult, workers: int):
    """Create each record; a QuickBooks record marked inactive is made
    inactive afterwards (the create routes take no is_active)."""
    base = path.split("?")[0]

    def post(pair):
        rec, body = pair
        try:
            created = sb.post(path, body)
            if one(rec, "IsActive") == "false":
                sb.put(f"{base}/{created['id']}", {"is_active": False})
            return None
        except ApiError as e:
            return f"{one(rec, 'FullName') or one(rec, 'Name')}: {e.status} {e.detail}"

    with ThreadPoolExecutor(workers) as pool:
        for err in pool.map(post, records):
            if err:
                result.errors.append(err)
            else:
                result.created += 1


# QuickBooks already keeps names unique. SlowBooks' duplicate-name check
# compares each new name with every existing one (slow at thousands) and
# refuses near matches that QuickBooks holds as different names, so it is
# bypassed.
def load_customers(sb: SlowBooks, customers: list[dict], workers: int) -> ListResult:
    result = ListResult("customers")
    bodies = [(c, customer_body(c)) for c in customers]
    _post_all(sb, "/api/customers?force=true", bodies, result, workers)
    return result


def load_vendors(sb: SlowBooks, vendors: list[dict], workers: int) -> ListResult:
    result = ListResult("vendors")
    bodies = [(v, vendor_body(v)) for v in vendors]
    _post_all(sb, "/api/vendors?force=true", bodies, result, workers)
    return result


def load_items(sb: SlowBooks, items: dict[str, list[dict]], accounts: dict[str, int], workers: int) -> ListResult:
    result = ListResult("items")
    bodies = []
    for record_type, recs in items.items():
        for rec in recs:
            if record_type in ITEM_TYPES:
                bodies.append((rec, item_body(record_type, rec, accounts)))
            else:
                result.skipped.append(f"{one(rec, 'FullName')} ({record_type.removesuffix('Ret')})")
    _post_all(sb, "/api/items", bodies, result, workers)
    return result
