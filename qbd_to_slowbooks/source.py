"""Read one committed qbd-exporter snapshot from Postgres.

qbd-exporter stores each QuickBooks SDK record as JSONB in its native shape:
every element is a list (``{"Name": ["Sales"], "AccountRef": [{"ListID":
[...]}]}``). ``landing_record_as_of(snapshot_id)`` reconstructs the records a
snapshot captured; everything here reads through it, pinned to one snapshot,
so a load never mixes two exports.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg import sql


@dataclass(frozen=True)
class Snapshot:
    id: str
    seq: int
    completed: datetime


def one(rec: dict | None, key: str, default=None):
    """The first value of a QuickBooks element, or ``default``."""
    if not rec:
        return default
    vals = rec.get(key)
    return vals[0] if vals else default


def ref_id(rec: dict, key: str) -> str | None:
    """The ListID a ``...Ref`` element points to."""
    return one(one(rec, key), "ListID")


def ref_name(rec: dict, key: str) -> str | None:
    """The FullName a ``...Ref`` element points to."""
    return one(one(rec, key), "FullName")


class Source:
    def __init__(self, url: str, schema: str = "quickbooks_ingest"):
        self.conn = psycopg.connect(url, autocommit=True)
        self.conn.execute("SET default_transaction_read_only = on")
        self.schema = schema

    def latest_snapshot(self) -> Snapshot:
        row = self.conn.execute(
            sql.SQL(
                "SELECT snapshot_id::text, snapshot_seq, source_completed_utc "
                "FROM {}.delivery_receipt ORDER BY snapshot_seq DESC LIMIT 1"
            ).format(sql.Identifier(self.schema))
        ).fetchone()
        if row is None:
            raise SystemExit("No committed qbd-exporter snapshot in the database.")
        return Snapshot(*row)

    def records(self, snapshot: Snapshot, *record_types: str) -> dict[str, list[dict]]:
        """Records of the given types (``AccountRet``, ``CustomerRet``...).

        The exporter reads some records through more than one query: every
        customer and vendor also comes back from the Entity query, every
        item from the Item query. Each type is read only from the query of
        its own name (``CustomerRet`` from ``Customer``), once."""
        rows = self.conn.execute(
            sql.SQL(
                "SELECT record_type, data FROM {}.landing_record_as_of(%s::uuid) "
                "WHERE record_type = ANY(%s) AND entity = left(record_type, -3)"
            ).format(sql.Identifier(self.schema)),
            (snapshot.id, list(record_types)),
        )
        out: dict[str, list[dict]] = defaultdict(list)
        for record_type, data in rows:
            out[record_type].append(data)
        return out
