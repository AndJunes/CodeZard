"""The ledger on disk. SQLite, because money has to survive a restart and nothing else here does.

WHY SQLITE AND NOT A SERVER
    The gateway runs today on six packages and no infrastructure, and the thing being stored
    is a few rows per customer per month. SQLite is in the standard library, is a single file
    to back up, and gives exactly the two guarantees that matter: a write is durable when it
    returns, and a UNIQUE index is honoured under concurrency. Moving to Postgres later is
    implementing ``BillingStore`` again — the port exists for that and for no other reason.

THE UNIQUE INDEX IS THE IDEMPOTENCE
    ``reference`` is unique among non-empty values, and :meth:`append` catches the violation
    and returns the row that was already there. That is not error handling; it is the
    mechanism. Confirming a payment is triggered by a poller, by the payer refreshing the page
    and by whoever calls the endpoint directly, and they routinely arrive together. Checking
    "does it exist" and then inserting would be a race with real money in it; letting the
    database refuse the duplicate is not.

BLOCKING CALLS OFF THE EVENT LOOP
    SQLite is synchronous. Every call here runs in a worker thread through ``anyio``, so a
    slow fsync cannot stall the request that is streaming a generation next door. One
    connection is shared and guarded by a lock: connections are not free, and the volume here
    never justifies a pool.

TIMES ARE STORED AS ISO TEXT IN UTC
    Not as epoch integers, because a human opening the file with `sqlite3` should be able to
    read it, and not as naive local times, because a server that moves timezone would silently
    reorder history.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

import anyio

from gateway.domain.billing import (
    EntryKind,
    Invoice,
    InvoiceStatus,
    LedgerEntry,
    Money,
    Subscription,
    SubscriptionStatus,
)
from gateway.domain.ports import BillingStore

T = TypeVar("T")

MEMORY = ":memory:"

SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger (
    seq       INTEGER PRIMARY KEY AUTOINCREMENT,
    id        TEXT NOT NULL UNIQUE,
    account   TEXT NOT NULL,
    kind      TEXT NOT NULL,
    tokens    INTEGER NOT NULL,
    amount    INTEGER NOT NULL DEFAULT 0,
    at        TEXT NOT NULL,
    reference TEXT NOT NULL DEFAULT '',
    memo      TEXT NOT NULL DEFAULT ''
);

-- Partial, so that the many entries with no reference (an operator adjustment, a manual
-- grant) do not collide with each other while every referenced fact stays unique.
CREATE UNIQUE INDEX IF NOT EXISTS ledger_reference
    ON ledger(reference) WHERE reference <> '';
CREATE INDEX IF NOT EXISTS ledger_account ON ledger(account, seq);

CREATE TABLE IF NOT EXISTS invoices (
    id          TEXT PRIMARY KEY,
    account     TEXT NOT NULL,
    sku         TEXT NOT NULL,
    kind        TEXT NOT NULL,
    tokens      INTEGER NOT NULL,
    price       INTEGER NOT NULL,
    asset       TEXT NOT NULL,
    amount      TEXT NOT NULL,
    destination TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    status      TEXT NOT NULL,
    tx_hash     TEXT NOT NULL DEFAULT '',
    payer       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS invoices_open ON invoices(status, created_at);

CREATE TABLE IF NOT EXISTS subscriptions (
    account    TEXT PRIMARY KEY,
    plan_id    TEXT NOT NULL,
    started_at TEXT NOT NULL,
    renews_at  TEXT NOT NULL,
    status     TEXT NOT NULL
);
"""


class SqliteBillingStore(BillingStore):
    """A durable ``BillingStore``. Safe to share; every call is serialised on one lock."""

    def __init__(self, path: str | Path = MEMORY) -> None:
        self._path = str(path)
        if self._path != MEMORY:
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(self._path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            # WAL lets the poller read while a payment is being credited. NORMAL synchronous
            # under WAL still survives a process crash, which is the failure this is guarding
            # against; only a power cut can lose the last transaction, and the payment it
            # recorded is still on a public ledger to be reconciled from.
            if self._path != MEMORY:
                self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=NORMAL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.executescript(SCHEMA)
            self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    # ── the ledger ───────────────────────────────────────────────────────────

    async def append(self, entry: LedgerEntry) -> LedgerEntry:
        return await self._run(self._append, entry)

    def _append(self, entry: LedgerEntry) -> LedgerEntry:
        try:
            self._connection.execute(
                "INSERT INTO ledger (id, account, kind, tokens, amount, at, reference, memo)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entry.id,
                    entry.account,
                    entry.kind.value,
                    entry.tokens,
                    entry.amount.micros,
                    entry.at.isoformat(),
                    entry.reference,
                    entry.memo,
                ),
            )
            self._connection.commit()
            return entry
        except sqlite3.IntegrityError:
            # The reference already exists: the same fact, recorded twice. The row that is
            # already there is the truth, and it is returned rather than the one refused —
            # a caller that reads the returned entry's id must see the id that persisted.
            self._connection.rollback()
            existing = self._one(
                "SELECT * FROM ledger WHERE reference = ? AND reference <> ''", (entry.reference,)
            )
            if existing is None:  # a duplicate ``id``, which is a different bug entirely
                raise
            return _entry(existing)

    async def entries(self, account: str, limit: int = 100) -> Sequence[LedgerEntry]:
        """The most recent ``limit``, returned oldest first.

        The inner query takes the LAST rows and the outer one puts them back in order. Taking
        the first ``limit`` instead would show a long-standing account its opening balance
        forever and never the run it just paid for.
        """
        rows = await self._run(
            self._all,
            "SELECT * FROM (SELECT * FROM ledger WHERE account = ? ORDER BY seq DESC LIMIT ?)"
            " ORDER BY seq ASC",
            (account, max(1, limit)),
        )
        return [_entry(row) for row in rows]

    async def all_entries(self, account: str) -> Sequence[LedgerEntry]:
        rows = await self._run(
            self._all, "SELECT * FROM ledger WHERE account = ? ORDER BY seq ASC", (account,)
        )
        return [_entry(row) for row in rows]

    # ── invoices ─────────────────────────────────────────────────────────────

    async def put_invoice(self, invoice: Invoice) -> Invoice:
        """Store it, or update the three fields of it that are allowed to change.

        The upsert lists ``status``, ``tx_hash`` and ``payer`` and nothing else ON PURPOSE.
        An invoice's terms — what is being bought, for how much, to which address, until
        when — are what the payer read before they paid, and a write path that could edit
        them is a write path that can move the price of a quote somebody is acting on. What
        changes about an invoice is whether it has been settled.
        """
        await self._run(
            self._write,
            "INSERT INTO invoices (id, account, sku, kind, tokens, price, asset,"
            " amount, destination, created_at, expires_at, status, tx_hash, payer)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET status = excluded.status,"
            " tx_hash = excluded.tx_hash, payer = excluded.payer",
            (
                invoice.id,
                invoice.account,
                invoice.sku,
                invoice.kind,
                invoice.tokens,
                invoice.price.micros,
                invoice.asset,
                invoice.amount,
                invoice.destination,
                invoice.created_at.isoformat(),
                invoice.expires_at.isoformat(),
                invoice.status.value,
                invoice.tx_hash,
                invoice.payer,
            ),
        )
        return invoice

    async def invoice(self, invoice_id: str) -> Invoice | None:
        row = await self._run(self._one, "SELECT * FROM invoices WHERE id = ?", (invoice_id,))
        return _invoice(row) if row is not None else None

    async def open_invoices(self, limit: int = 100) -> Sequence[Invoice]:
        rows = await self._run(
            self._all,
            "SELECT * FROM invoices WHERE status = ? ORDER BY created_at ASC LIMIT ?",
            (InvoiceStatus.PENDING.value, max(1, limit)),
        )
        return [_invoice(row) for row in rows]

    # ── subscriptions ────────────────────────────────────────────────────────

    async def put_subscription(self, subscription: Subscription) -> Subscription:
        await self._run(
            self._write,
            "INSERT INTO subscriptions (account, plan_id, started_at, renews_at,"
            " status) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(account) DO UPDATE SET plan_id = excluded.plan_id,"
            " started_at = excluded.started_at, renews_at = excluded.renews_at,"
            " status = excluded.status",
            (
                subscription.account,
                subscription.plan_id,
                subscription.started_at.isoformat(),
                subscription.renews_at.isoformat(),
                subscription.status.value,
            ),
        )
        return subscription

    async def subscription(self, account: str) -> Subscription | None:
        row = await self._run(
            self._one, "SELECT * FROM subscriptions WHERE account = ?", (account,)
        )
        if row is None:
            return None
        return Subscription(
            row["account"],
            row["plan_id"],
            _time(row["started_at"]),
            _time(row["renews_at"]),
            SubscriptionStatus(row["status"]),
        )

    # ── the plumbing ─────────────────────────────────────────────────────────

    async def _run(self, function: Callable[..., T], *arguments: Any) -> T:
        """Every database call, in a worker thread and under the lock.

        Generic in the return type so each caller keeps the type its own method declares;
        an ``Any`` here would quietly turn every row-reading method into one that returns
        whatever, which is exactly the kind of hole a strict type check exists to close.

        `anyio.to_thread` rather than a dedicated executor: the gateway already runs on anyio,
        and its thread limiter is the one place the operator can bound this process's threads.
        """

        def call() -> T:
            with self._lock:
                return function(*arguments)

        return await anyio.to_thread.run_sync(call)

    def _write(self, sql: str, parameters: tuple[Any, ...]) -> None:
        self._connection.execute(sql, parameters)
        self._connection.commit()

    def _one(self, sql: str, parameters: tuple[Any, ...]) -> sqlite3.Row | None:
        # `fetchone` is typed as returning `Any`; the row factory is set in `__init__` and is
        # what actually decides. Narrowed here rather than trusted, so the two cannot drift.
        row: sqlite3.Row | None = self._connection.execute(sql, parameters).fetchone()
        return row

    def _all(self, sql: str, parameters: tuple[Any, ...]) -> list[sqlite3.Row]:
        return list(self._connection.execute(sql, parameters).fetchall())


# ── rows back into values ────────────────────────────────────────────────────


def _time(value: str) -> datetime:
    """Parse, and never hand back a naive datetime.

    A row written before this file insisted on timezones, or by a future migration that
    forgets, would otherwise compare as if it were local time and reorder history.
    """
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _entry(row: sqlite3.Row) -> LedgerEntry:
    return LedgerEntry(
        id=row["id"],
        account=row["account"],
        kind=EntryKind(row["kind"]),
        tokens=row["tokens"],
        at=_time(row["at"]),
        amount=Money(row["amount"]),
        reference=row["reference"],
        memo=row["memo"],
    )


def _invoice(row: sqlite3.Row) -> Invoice:
    return Invoice(
        id=row["id"],
        account=row["account"],
        sku=row["sku"],
        tokens=row["tokens"],
        price=Money(row["price"]),
        asset=row["asset"],
        amount=row["amount"],
        destination=row["destination"],
        created_at=_time(row["created_at"]),
        expires_at=_time(row["expires_at"]),
        status=InvoiceStatus(row["status"]),
        kind=row["kind"],
        tx_hash=row["tx_hash"],
        payer=row["payer"],
    )


__all__ = ["MEMORY", "SqliteBillingStore"]
