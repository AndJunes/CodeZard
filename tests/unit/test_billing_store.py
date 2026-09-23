"""The ledger on disk. Mostly: does it refuse to record the same fact twice?"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from gateway.domain.billing import (
    EntryKind,
    Invoice,
    InvoiceStatus,
    LedgerEntry,
    Money,
    Subscription,
    SubscriptionStatus,
    entry_id,
    new_invoice_id,
)
from gateway.infrastructure.billing.sqlite_store import SqliteBillingStore

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
ACCOUNT = "G" + "A" * 55
OTHER = "G" + "B" * 55


@pytest.fixture
def store() -> Iterator[SqliteBillingStore]:
    """Closed on the way out: an in-memory database still holds a connection, and leaving
    them open makes every run print a ResourceWarning per test."""
    opened = SqliteBillingStore()
    yield opened
    opened.close()


def entry(
    tokens: int, reference: str = "", account: str = ACCOUNT, kind: EntryKind = EntryKind.PURCHASE
) -> LedgerEntry:
    return LedgerEntry(
        id=entry_id(),
        account=account,
        kind=kind,
        tokens=tokens,
        at=NOW,
        amount=Money.usd(8),
        reference=reference,
        memo="a memo",
    )


def invoice(invoice_id: str = "", **changes: object) -> Invoice:
    base = {
        "id": invoice_id or new_invoice_id(),
        "account": ACCOUNT,
        "sku": "tokens-1m",
        "tokens": 1_000_000,
        "price": Money.usd(8),
        "asset": "XLM",
        "amount": "80.0000000",
        "destination": OTHER,
        "created_at": NOW,
        "expires_at": NOW + timedelta(minutes=30),
    }
    return Invoice(**{**base, **changes})  # type: ignore[arg-type]


class TestLedger:
    async def test_an_entry_comes_back_as_it_went_in(self, store: SqliteBillingStore) -> None:
        written = await store.append(entry(1_000, "invoice:one"))
        [read] = await store.all_entries(ACCOUNT)
        assert read == written
        assert read.at.tzinfo is not None

    async def test_the_same_reference_is_recorded_once(self, store: SqliteBillingStore) -> None:
        """Confirming a payment is triggered by a poller, by a refresh and by the endpoint,
        often at once. The tokens have to appear exactly one time."""
        first = await store.append(entry(1_000, "invoice:one"))
        second = await store.append(entry(1_000, "invoice:one"))

        assert len(await store.all_entries(ACCOUNT)) == 1
        assert second.id == first.id, "the row that persisted is the one handed back"

    async def test_entries_without_a_reference_do_not_collide(
        self, store: SqliteBillingStore
    ) -> None:
        """An operator adjustment has no natural key, and two of them are two facts."""
        await store.append(entry(10))
        await store.append(entry(20))
        assert len(await store.all_entries(ACCOUNT)) == 2

    async def test_a_duplicate_id_is_a_real_error_and_not_swallowed(
        self, store: SqliteBillingStore
    ) -> None:
        shared = entry(10, "ref:one")
        await store.append(shared)
        with pytest.raises(Exception, match="UNIQUE"):
            await store.append(
                LedgerEntry(
                    id=shared.id,
                    account=ACCOUNT,
                    kind=EntryKind.PURCHASE,
                    tokens=5,
                    at=NOW,
                    reference="ref:two",
                )
            )

    async def test_accounts_do_not_see_each_other(self, store: SqliteBillingStore) -> None:
        await store.append(entry(1_000, "a", ACCOUNT))
        await store.append(entry(2_000, "b", OTHER))
        assert [e.tokens for e in await store.all_entries(ACCOUNT)] == [1_000]

    async def test_entries_come_back_oldest_first(self, store: SqliteBillingStore) -> None:
        for n in range(5):
            await store.append(entry(n + 1, f"ref{n}"))
        assert [e.tokens for e in await store.all_entries(ACCOUNT)] == [1, 2, 3, 4, 5]

    async def test_a_limited_read_keeps_the_newest_and_still_reads_forwards(
        self, store: SqliteBillingStore
    ) -> None:
        """Taking the FIRST `limit` would show a long-standing account its opening balance
        forever and never the run it just paid for."""
        for n in range(10):
            await store.append(entry(n + 1, f"ref{n}"))
        assert [e.tokens for e in await store.entries(ACCOUNT, 3)] == [8, 9, 10]

    async def test_an_unknown_account_is_empty_rather_than_missing(
        self, store: SqliteBillingStore
    ) -> None:
        assert await store.all_entries("G" + "Z" * 55) == []


class TestInvoices:
    async def test_it_round_trips(self, store: SqliteBillingStore) -> None:
        written = await store.put_invoice(invoice())
        assert await store.invoice(written.id) == written

    async def test_an_unknown_invoice_is_none(self, store: SqliteBillingStore) -> None:
        assert await store.invoice("czdoesnotexist") is None

    async def test_paying_updates_it_in_place(self, store: SqliteBillingStore) -> None:
        written = await store.put_invoice(invoice())
        await store.put_invoice(written.paid("txhash", OTHER))

        read = await store.invoice(written.id)
        assert read is not None
        assert read.status is InvoiceStatus.PAID
        assert read.tx_hash == "txhash"
        assert read.payer == OTHER

    async def test_only_the_pending_ones_are_offered_to_a_poller(
        self, store: SqliteBillingStore
    ) -> None:
        pending = await store.put_invoice(invoice())
        settled = await store.put_invoice(invoice())
        await store.put_invoice(settled.paid("h", OTHER))

        assert [i.id for i in await store.open_invoices()] == [pending.id]


class TestSubscriptions:
    async def test_it_round_trips(self, store: SqliteBillingStore) -> None:
        subscription = Subscription(ACCOUNT, "starter", NOW, NOW + timedelta(days=30))
        await store.put_subscription(subscription)
        assert await store.subscription(ACCOUNT) == subscription

    async def test_there_is_only_ever_one_per_account(self, store: SqliteBillingStore) -> None:
        await store.put_subscription(Subscription(ACCOUNT, "starter", NOW, NOW))
        await store.put_subscription(
            Subscription(ACCOUNT, "studio", NOW, NOW, SubscriptionStatus.ACTIVE)
        )

        read = await store.subscription(ACCOUNT)
        assert read is not None
        assert read.plan_id == "studio"

    async def test_an_account_without_one_is_none(self, store: SqliteBillingStore) -> None:
        assert await store.subscription(ACCOUNT) is None


async def test_it_survives_being_closed_and_opened_again(tmp_path: Path) -> None:
    """The one thing this store exists for. Runs and logs are allowed to be forgotten; a
    balance is not."""
    path = tmp_path / "nested" / "billing.sqlite3"
    first = SqliteBillingStore(path)
    await first.append(entry(1_000, "invoice:one"))
    await first.put_invoice(invoice("czkept0000000000000000"))
    first.close()

    second = SqliteBillingStore(path)
    try:
        assert [e.tokens for e in await second.all_entries(ACCOUNT)] == [1_000]
        assert await second.invoice("czkept0000000000000000") is not None
    finally:
        second.close()


async def test_a_naive_timestamp_from_an_older_row_is_read_as_utc(tmp_path: Path) -> None:
    """Comparing a naive datetime against an aware one raises, and a row written before this
    file insisted on timezones would take the whole account read down with it."""
    path = tmp_path / "billing.sqlite3"
    store = SqliteBillingStore(path)
    try:
        await store.append(entry(1, "ref"))
        store._connection.execute("UPDATE ledger SET at = ?", ("2026-01-01T00:00:00",))
        store._connection.commit()
        [read] = await store.all_entries(ACCOUNT)
        assert read.at.tzinfo is not None
    finally:
        store.close()
