"""qbd-to-slowbooks: load a qbd-exporter snapshot into SlowBooks Pro.

    check     show the snapshot and what the server holds, change nothing
    baseline  back up the SlowBooks company as it is now; `reset` returns to it
    reset     restore the baseline backup (wipes everything loaded since)
    load      chart of accounts, customers, vendors, items, journal history
    verify    compare SlowBooks' trial balance with QuickBooks' balances
    run       reset, load, verify

Settings come from the environment, or a .env file in the current directory
(see .env.example).
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import accounts as accounts_mod
from . import journals, lists
from .slowbooks import SlowBooks
from .source import Snapshot, Source, one

ITEM_RECORDS = (
    "ItemInventoryRet",
    "ItemInventoryAssemblyRet",
    "ItemNonInventoryRet",
    "ItemServiceRet",
    "ItemOtherChargeRet",
    "ItemDiscountRet",
    "ItemSubtotalRet",
    "ItemSalesTaxRet",
    "ItemSalesTaxGroupRet",
    "ItemGroupRet",
    "ItemPaymentRet",
    "ItemFixedAssetRet",
)
ALL_TIME = {"start_date": "1900-01-01", "end_date": "2099-12-31"}


def say(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name) or default
    if not value:
        raise SystemExit(f"Set {name} (see .env.example).")
    return value


def source() -> Source:
    return Source(env("QBD_POSTGRES_URL"), env("QBD_POSTGRES_SCHEMA", "quickbooks_ingest"))


def slowbooks() -> SlowBooks:
    return SlowBooks(env("SLOWBOOKS_URL"), env("SLOWBOOKS_TOKEN"))


# --- commands ---------------------------------------------------------------


def cmd_check(args) -> int:
    src = source()
    snap = src.latest_snapshot()
    say(f"snapshot {snap.id} (#{snap.seq}, exported {snap.completed:%Y-%m-%d %H:%M} UTC)")
    sb = slowbooks()
    status = sb.get("/api/auth/status")
    say(f"SlowBooks company: {status.get('company_name')}")
    say(f"  accounts {len(sb.get('/api/accounts'))}, customers {len(sb.get('/api/customers'))}, "
        f"vendors {len(sb.get('/api/vendors'))}, items {len(sb.get('/api/items'))}")
    tb = sb.get("/api/reports/trial-balance", **ALL_TIME)
    say(f"  trial balance: {len(tb['items'])} accounts with postings, difference {tb['difference']}")
    baseline = os.environ.get("SLOWBOOKS_BASELINE_BACKUP")
    names = {b["filename"] for b in sb.get("/api/backups")}
    if baseline:
        say(f"  baseline backup {baseline}: {'present' if baseline in names else 'MISSING'}")
    else:
        say("  no baseline backup set (run `baseline`)")
    return 0


def cmd_baseline(args) -> int:
    sb = slowbooks()
    made = sb.post("/api/backups", {"notes": "qbd-to-slowbooks baseline: `reset` restores this"})
    say(f"baseline backup {made['filename']}")
    print(f"\nSet SLOWBOOKS_BASELINE_BACKUP={made['filename']}")
    return 0


def cmd_reset(args) -> int:
    """Restore the baseline through the API, or with SLOWBOOKS_RESET_COMMAND
    when that is set. SlowBooks 2.25.0 refuses its own backups on a Postgres
    install (it takes a pg_dump file for an encrypted company file and asks
    for a recovery key), so a Postgres server needs the command: anything
    that puts the baseline back, run on this machine with {baseline}
    replaced by the backup's filename."""
    sb = slowbooks()
    baseline = env("SLOWBOOKS_BASELINE_BACKUP")
    command = os.environ.get("SLOWBOOKS_RESET_COMMAND")
    say(f"restoring {baseline}" + (" with SLOWBOOKS_RESET_COMMAND" if command else ""))
    if command:
        subprocess.run(command.replace("{baseline}", shlex.quote(baseline)), shell=True, check=True)
    else:
        sb.post("/api/backups/restore", {"filename": baseline})
    # The restore drops and recreates every table under a running app; wait
    # until it answers with the books again.
    for _ in range(60):
        try:
            if not sb.get("/api/auth/status").get("setup_needed"):
                break
        except Exception:
            pass
        time.sleep(2)
    left = sb.get("/api/reports/trial-balance", **ALL_TIME)["items"]
    say(f"restored; {len(left)} accounts carry postings")
    return 0


def cmd_load(args, src: Source | None = None, snap: Snapshot | None = None) -> int:
    src = src or source()
    snap = snap or src.latest_snapshot()
    sb = slowbooks()
    report: dict = {"snapshot": snap.id, "snapshot_seq": snap.seq, "started": _now()}
    say(f"loading snapshot {snap.id} (#{snap.seq}) into {env('SLOWBOOKS_URL')}")

    recs = src.records(snap, "AccountRet", "CustomerRet", "VendorRet", "TransactionRet", *ITEM_RECORDS)

    chart = accounts_mod.load_chart(sb, recs["AccountRet"])
    say(f"accounts: {chart.created} created, {len(chart.mapped)} mapped to SlowBooks control accounts")
    report["accounts"] = {"created": chart.created, "mapped": chart.mapped, "skipped": chart.skipped}

    results = []
    if not args.skip_lists:
        results.append(lists.load_customers(sb, recs["CustomerRet"], args.workers))
        results.append(lists.load_vendors(sb, recs["VendorRet"], args.workers))
        results.append(lists.load_items(sb, {t: recs[t] for t in ITEM_RECORDS}, chart.ids, args.workers))
        for r in results:
            say(f"{r.kind}: {r.created} created, {len(r.errors)} errors, {len(r.skipped)} skipped")
            report[r.kind] = {"created": r.created, "errors": r.errors, "skipped": r.skipped}

    jr = journals.JournalResult()
    entries = journals.build_entries(recs["TransactionRet"], chart.ids, jr)
    if args.since:
        entries = [e for e in entries if e["date"] >= args.since]
    if args.limit:
        entries = entries[: args.limit]
    say(f"journal entries: {len(entries)} to post ({jr.empty} voided skipped, {len(jr.errors)} refused)")
    t0 = time.monotonic()
    journals.post_entries(
        sb, entries, jr, args.workers,
        progress=lambda n, total: say(f"  {n}/{total} ({n / (time.monotonic() - t0):.0f}/s)"),
    )
    say(f"journal entries: {jr.posted} posted, {len(jr.errors)} errors")
    report["journals"] = {"posted": jr.posted, "voided_skipped": jr.empty, "errors": jr.errors}

    removed = sb.post("/api/accounts/unused-seeded/remove")
    say(f"removed {len(removed['removed'])} unused seeded accounts")

    report["finished"] = _now()
    errors = sum(len(r.errors) for r in results) + len(jr.errors)
    _write_report("load", report)
    return 1 if errors else 0


def cmd_verify(args, src: Source | None = None, snap: Snapshot | None = None) -> int:
    """Each QuickBooks balance-sheet account against the SlowBooks account it
    was loaded into, by name. Income-statement accounts are left out: their
    QuickBooks Balance is a fiscal-year figure, not a lifetime one."""
    src = src or source()
    snap = snap or src.latest_snapshot()
    sb = slowbooks()
    qb_accounts = src.records(snap, "AccountRet")["AccountRet"]
    tb = sb.get("/api/reports/trial-balance", **ALL_TIME)
    sb_net = {row["account_path"]: row["net_balance"] for row in tb["items"]}
    sb_by_id = {row["account_id"]: row["net_balance"] for row in tb["items"]}
    sb_accounts = {a["id"]: a for a in sb.get("/api/accounts")}
    path_of = _paths(sb_accounts)

    mismatches = []
    for acct in qb_accounts:
        expected = accounts_mod.qb_balance_as_net(acct)
        if expected is None:
            continue
        # Mapped control accounts keep QuickBooks' leaf name under their own
        # parent, so match on the path SlowBooks reports.
        qb_path = one(acct, "FullName")
        sb_id = next((i for i, p in path_of.items() if p == qb_path), None)
        actual = sb_by_id.get(sb_id, 0.0) if sb_id else sb_net.get(qb_path, 0.0)
        if abs(actual - expected) > 0.005:
            mismatches.append({"account": qb_path, "quickbooks": expected, "slowbooks": round(actual, 2),
                               "difference": round(actual - expected, 2)})

    say(f"trial balance difference (debits - credits): {tb['difference']:.2f}")
    if mismatches:
        say(f"{len(mismatches)} balance-sheet accounts differ from QuickBooks:")
        for m in mismatches:
            say(f"  {m['account']}: QuickBooks {m['quickbooks']:,.2f}, SlowBooks {m['slowbooks']:,.2f} "
                f"(diff {m['difference']:,.2f})")
    else:
        say("every balance-sheet account matches QuickBooks")
    _write_report("verify", {"snapshot": snap.id, "difference": tb["difference"], "mismatches": mismatches})
    return 1 if mismatches or abs(tb["difference"]) > 0.005 else 0


def cmd_run(args) -> int:
    cmd_reset(args)
    src = source()
    snap = src.latest_snapshot()
    loaded = cmd_load(args, src, snap)
    verified = cmd_verify(args, src, snap)
    return loaded or verified


# --- helpers ----------------------------------------------------------------


def _paths(sb_accounts: dict[int, dict]) -> dict[int, str]:
    def path(a):
        parent = sb_accounts.get(a.get("parent_id"))
        return f"{path(parent)}:{a['name']}" if parent else a["name"]

    return {i: path(a) for i, a in sb_accounts.items()}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_report(kind: str, report: dict) -> None:
    out = Path("runs")
    out.mkdir(exist_ok=True)
    path = out / f"{datetime.now():%Y%m%d-%H%M%S}-{kind}.json"
    path.write_text(json.dumps(report, indent=2, default=str))
    say(f"report: {path}")


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="qbd-to-slowbooks", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "baseline", "reset", "verify"):
        sub.add_parser(name)
    for name in ("load", "run"):
        p = sub.add_parser(name)
        p.add_argument("--workers", type=int, default=8, help="concurrent API requests (default 8)")
        p.add_argument("--skip-lists", action="store_true", help="accounts and journals only")
        p.add_argument("--since", help="only journal entries on or after this date (YYYY-MM-DD), for trial runs")
        p.add_argument("--limit", type=int, help="only the first N journal entries, for trial runs")
    args = parser.parse_args(argv)
    return globals()[f"cmd_{args.command}"](args)


if __name__ == "__main__":
    sys.exit(main())
