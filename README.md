# qbd-to-slowbooks

Loads QuickBooks Desktop data into [SlowBooks Pro](https://www.slowbookspro.com/)
through its REST API. The data comes from qbd-exporter (to be published
separately), which exports a QuickBooks Desktop company file into Postgres
every night.

```
QuickBooks Desktop ──qbd-exporter──▶ Postgres (quickbooks_ingest) ──qbd-to-slowbooks──▶ SlowBooks Pro API
```

It is built for watching SlowBooks alongside QuickBooks before a switch. Every
run wipes the SlowBooks company back to a baseline backup and reloads
everything from the latest snapshot. Do not keep books of your own in the
company you point it at.

## What comes across

| QuickBooks | SlowBooks |
|---|---|
| Chart of accounts | Accounts, with QuickBooks' parent/child structure. A/R, A/P, Undeposited Funds, Inventory Asset, Sales Tax Payable, Retained Earnings and Cost of Goods Sold map onto SlowBooks' own control accounts and take the QuickBooks name. |
| Customers, vendors | Customers and vendors with addresses, phone, first valid email, terms. Jobs arrive as customers named `Customer:Job`. |
| Inventory, assembly, non-inventory, service and other-charge items | Items with prices, costs and accounts. Quantities start at zero. |
| Every posting transaction | One journal entry per QuickBooks transaction, built from QuickBooks' own postings and referenced by its `TxnID`. Estimates, sales orders and purchase orders are left out because they are non-posting. Voided transactions are skipped. |

Because history arrives as QuickBooks' own postings, SlowBooks' balances
match QuickBooks' without SlowBooks re-costing inventory or recalculating
tax. `verify` checks this account by account.

Not yet: open invoices and bills as documents (for A/R and A/P aging),
inventory quantities on hand, classes, customer jobs as SlowBooks jobs.

## Setup

1. qbd-exporter loading a Postgres database. qbd-to-slowbooks only reads it.
2. A SlowBooks Pro company to load into, with an **admin** API token
   (Settings → API Tokens). Admin is needed because `reset` restores a
   backup.
3. Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
cp .env.example .env   # then fill it in
uv sync
uv run qbd-to-slowbooks check
uv run qbd-to-slowbooks baseline   # once: the state every reset returns to
```

`baseline` prints a backup filename. Put it in `.env` as
`SLOWBOOKS_BASELINE_BACKUP`. Take the baseline while the company is empty and
the API token exists: the restore brings back the token with everything else.

On a SlowBooks server that keeps its books in Postgres (the Docker and
server installs), set `SLOWBOOKS_RESET_COMMAND` as well. SlowBooks 2.25.0
refuses to restore its own Postgres backups through the API: it mistakes the
`pg_dump` file for an encrypted company file and asks for a recovery key. The
command is any shell command, run where qbd-to-slowbooks runs, that restores
the baseline; `{baseline}` in it becomes the filename. `.env.example` has one
for SlowBooks' own docker-compose stack.

## Running

```bash
uv run qbd-to-slowbooks run          # reset, load, verify
uv run qbd-to-slowbooks load --since 2026-01-01   # a quicker trial
uv run qbd-to-slowbooks verify
```

Each command writes a JSON report under `runs/`. `run` exits non-zero when
anything was refused or a balance differs, so it can run from cron.

Each `reset` makes SlowBooks take a safety backup before restoring. Those
`pre-restore` backups build up in the server's backup folder; prune them
there.

## License

MIT. See [LICENSE](LICENSE).
