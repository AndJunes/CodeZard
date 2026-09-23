"""Selling, granting and spending — with the network replaced and the ledger real.

The store here is a real SQLite one (in memory), not a fake. Idempotence is the property
these use cases lean on hardest and it lives in a UNIQUE index; a dictionary standing in for
the store would test the wrong thing and pass.
"""

from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta
from decimal import ROUND_UP, Decimal

import pytest

from gateway.application.billing_service import BillingService
from gateway.domain.billing import (
    MONTH,
    EntryKind,
    InsufficientFundsError,
    Invoice,
    InvoiceStatus,
    Money,
    ObservedPayment,
    Pack,
    Plan,
    Pricing,
    SubscriptionStatus,
    UnknownProductError,
    Usage,
    default_catalog,
    now_utc,
)
from gateway.domain.x402 import SettleResult, VerifyResult
from gateway.infrastructure.billing.sqlite_store import SqliteBillingStore

ACCOUNT = "G" + "A" * 55
DESTINATION = "G" + "B" * 55


class FakeNetwork:
    """Stellar, replaced. It pays whatever it has been told to pay, and remembers questions."""

    def __init__(self, rate: str = "0.10") -> None:
        self.rate = rate
        self.payments: dict[str, ObservedPayment] = {}
        self.asked: list[str] = []
        self.unreachable = False

    @property
    def network(self) -> str:
        return "stellar-testnet"

    @property
    def destination(self) -> str:
        return DESTINATION

    async def payment_for(self, invoice: Invoice) -> ObservedPayment | None:
        self.asked.append(invoice.id)
        if self.unreachable:
            raise RuntimeError("Horizon is down")
        return self.payments.get(invoice.memo)

    async def quote(self, price: Money, asset: str) -> str:
        # Quantised like the real one: without it `Decimal` prints `8E+1`, which is a valid
        # number and not an amount any payment network would accept.
        amount = price.decimal if asset == "USDC" else price.decimal / Decimal(self.rate)
        return str(amount.quantize(Decimal("0.0000001"), rounding=ROUND_UP))

    async def verify(self, transaction: str, invoice: Invoice) -> VerifyResult:
        return VerifyResult(True, payer=ACCOUNT, amount=invoice.amount)

    async def settle(self, transaction: str) -> SettleResult:
        return SettleResult(True, self.network, transaction="tx", payer=ACCOUNT)

    def pay(self, invoice: Invoice, amount: str = "", payer: str = ACCOUNT) -> None:
        self.payments[invoice.memo] = ObservedPayment(
            tx_hash="txhash",
            payer=payer,
            destination=invoice.destination,
            asset=invoice.asset,
            amount=amount or invoice.amount,
            memo=invoice.memo,
            at=now_utc(),
        )


@pytest.fixture
def store() -> Iterator[SqliteBillingStore]:
    """Closed on the way out: an in-memory database still holds a connection."""
    opened = SqliteBillingStore()
    yield opened
    opened.close()


@pytest.fixture
def network() -> FakeNetwork:
    return FakeNetwork()


@pytest.fixture
def billing(store: SqliteBillingStore, network: FakeNetwork) -> BillingService:
    return BillingService(store, network, default_catalog(), "XLM", reserve=1_000)  # type: ignore[arg-type]


class TestCheckout:
    async def test_it_quotes_once_and_freezes_the_amount(self, billing: BillingService) -> None:
        """An invoice whose price moves while the payer reads it is not an invoice."""
        invoice = await billing.checkout(ACCOUNT, "tokens-1m")
        assert invoice.price == Money.parse("8.00")
        assert invoice.amount == "80.0000000", "quoted at the rate, in the asset's own places"
        assert invoice.destination == DESTINATION
        assert invoice.memo == invoice.id

    async def test_a_plan_and_a_pack_are_marked_apart(self, billing: BillingService) -> None:
        assert (await billing.checkout(ACCOUNT, "starter")).kind == "plan"
        assert (await billing.checkout(ACCOUNT, "tokens-1m")).kind == "pack"

    async def test_an_unknown_sku_is_refused(self, billing: BillingService) -> None:
        with pytest.raises(UnknownProductError):
            await billing.checkout(ACCOUNT, "free-forever")

    async def test_usdc_is_quoted_one_to_one(self, billing: BillingService) -> None:
        assert (await billing.checkout(ACCOUNT, "tokens-1m", "USDC")).amount == "8.0000000"


class TestConfirm:
    async def test_an_unpaid_invoice_credits_nothing(self, billing: BillingService) -> None:
        invoice = await billing.checkout(ACCOUNT, "tokens-1m")
        confirmed = await billing.confirm(invoice.id)

        assert confirmed.status is InvoiceStatus.PENDING
        assert (await billing.balance(ACCOUNT)).total == 0

    async def test_paying_it_credits_the_tokens(
        self, billing: BillingService, network: FakeNetwork
    ) -> None:
        invoice = await billing.checkout(ACCOUNT, "tokens-1m")
        network.pay(invoice)

        confirmed = await billing.confirm(invoice.id)

        assert confirmed.status is InvoiceStatus.PAID
        assert confirmed.tx_hash == "txhash"
        assert (await billing.balance(ACCOUNT)).purchased == 1_000_000

    async def test_confirming_twice_credits_once(
        self, billing: BillingService, network: FakeNetwork
    ) -> None:
        """A poller, a refresh and the endpoint all call this, routinely together."""
        invoice = await billing.checkout(ACCOUNT, "tokens-1m")
        network.pay(invoice)

        await billing.confirm(invoice.id)
        await billing.confirm(invoice.id)
        await billing.confirm(invoice.id)

        assert (await billing.balance(ACCOUNT)).purchased == 1_000_000

    async def test_an_already_paid_invoice_does_not_ask_the_network_again(
        self, billing: BillingService, network: FakeNetwork
    ) -> None:
        invoice = await billing.checkout(ACCOUNT, "tokens-1m")
        network.pay(invoice)
        await billing.confirm(invoice.id)
        asked = len(network.asked)

        await billing.confirm(invoice.id)
        assert len(network.asked) == asked

    async def test_a_payment_that_is_short_does_not_settle_it(
        self, billing: BillingService, network: FakeNetwork
    ) -> None:
        invoice = await billing.checkout(ACCOUNT, "tokens-1m")
        network.pay(invoice, amount="1.0000000")

        confirmed = await billing.confirm(invoice.id)
        assert confirmed.status is InvoiceStatus.PENDING
        assert (await billing.balance(ACCOUNT)).total == 0

    async def test_an_unknown_invoice_is_refused(self, billing: BillingService) -> None:
        from gateway.domain.billing import InvoiceNotFoundError

        with pytest.raises(InvoiceNotFoundError):
            await billing.confirm("czdoesnotexist")

    async def test_an_unpaid_invoice_past_its_window_is_written_off(
        self, billing: BillingService, store: SqliteBillingStore
    ) -> None:
        """Left pending it would look payable forever, and a poller would keep asking."""
        quoted = await billing.checkout(ACCOUNT, "tokens-1m")
        # A separate row rather than an edit of `quoted`: the store deliberately refuses to
        # rewrite an invoice's terms, which the next test pins.
        stale = await store.put_invoice(
            replace(quoted, id="czexpired0000000000000", expires_at=now_utc() - timedelta(hours=1))
        )

        assert (await billing.confirm(stale.id)).status is InvoiceStatus.EXPIRED

    async def test_the_terms_of_an_invoice_cannot_be_rewritten(
        self, billing: BillingService, store: SqliteBillingStore
    ) -> None:
        """What the payer read before they paid is what stands. Only settlement changes."""
        quoted = await billing.checkout(ACCOUNT, "tokens-1m")
        await store.put_invoice(replace(quoted, amount="0.0000001", tokens=99))

        stored = await store.invoice(quoted.id)
        assert stored is not None
        assert stored.amount == quoted.amount
        assert stored.tokens == quoted.tokens


class TestSubscriptions:
    async def test_paying_for_a_plan_starts_a_period(
        self, billing: BillingService, network: FakeNetwork
    ) -> None:
        invoice = await billing.checkout(ACCOUNT, "starter")
        network.pay(invoice)
        await billing.confirm(invoice.id)

        view = await billing.view(ACCOUNT)
        assert view.subscription is not None
        assert view.subscription.plan_id == "starter"
        assert view.plan is not None
        assert view.balance.granted == 4_000_000

    async def test_a_plan_grants_and_a_pack_purchases(
        self, billing: BillingService, network: FakeNetwork
    ) -> None:
        """Same credit, different shelf life: one expires with the period, one never does."""
        plan = await billing.checkout(ACCOUNT, "starter")
        pack = await billing.checkout(ACCOUNT, "tokens-1m")
        network.pay(plan)
        network.pay(pack)
        await billing.confirm(plan.id)
        await billing.confirm(pack.id)

        balance = await billing.balance(ACCOUNT)
        assert balance.granted == 4_000_000
        assert balance.purchased == 1_000_000

    async def test_paying_the_same_plan_again_renews_it(
        self, billing: BillingService, network: FakeNetwork, store: SqliteBillingStore
    ) -> None:
        first = await billing.checkout(ACCOUNT, "starter")
        network.pay(first)
        await billing.confirm(first.id)
        started = await store.subscription(ACCOUNT)
        assert started is not None

        second = await billing.checkout(ACCOUNT, "starter")
        network.pay(second)
        await billing.confirm(second.id)

        renewed = await store.subscription(ACCOUNT)
        assert renewed is not None
        assert renewed.renews_at > started.renews_at

    async def test_an_ended_period_takes_back_what_it_granted(
        self, billing: BillingService, network: FakeNetwork, store: SqliteBillingStore
    ) -> None:
        """Granted tokens do not roll over, and that is enforced by writing the expiry down
        rather than by resetting a counter."""
        invoice = await billing.checkout(ACCOUNT, "starter")
        network.pay(invoice)
        await billing.confirm(invoice.id)
        subscription = await store.subscription(ACCOUNT)
        assert subscription is not None
        await store.put_subscription(
            type(subscription)(
                ACCOUNT,
                "starter",
                subscription.started_at - MONTH * 2,
                now_utc() - timedelta(days=1),
            )
        )

        view = await billing.view(ACCOUNT)

        assert view.balance.granted == 0
        assert view.subscription is not None
        assert view.subscription.status is SubscriptionStatus.EXPIRED
        assert any(e.kind is EntryKind.EXPIRY for e in view.entries)

    async def test_a_plan_withdrawn_from_the_catalog_does_not_break_the_account(
        self, store: SqliteBillingStore, network: FakeNetwork
    ) -> None:
        catalog = default_catalog()
        billing = BillingService(store, network, catalog, "XLM", reserve=1_000)  # type: ignore[arg-type]
        invoice = await billing.checkout(ACCOUNT, "starter")
        network.pay(invoice)
        await billing.confirm(invoice.id)

        trimmed = BillingService(
            store,
            network,  # type: ignore[arg-type]
            type(catalog)(plans=(), packs=catalog.packs, pricing=catalog.pricing),
        )
        view = await trimmed.view(ACCOUNT)

        assert view.subscription is not None
        assert view.plan is None, "the row stands even when the product no longer sells"


class TestAuthorizeAndCharge:
    async def test_an_empty_account_may_not_start_a_run(self, billing: BillingService) -> None:
        with pytest.raises(InsufficientFundsError) as raised:
            await billing.authorize(ACCOUNT)
        assert raised.value.available == 0
        assert raised.value.needed == 1_000

    async def test_a_funded_account_may(
        self, billing: BillingService, network: FakeNetwork
    ) -> None:
        invoice = await billing.checkout(ACCOUNT, "tokens-1m")
        network.pay(invoice)
        await billing.confirm(invoice.id)

        assert (await billing.authorize(ACCOUNT)).total == 1_000_000

    async def test_charging_debits_what_the_run_cost(
        self, billing: BillingService, network: FakeNetwork
    ) -> None:
        invoice = await billing.checkout(ACCOUNT, "tokens-1m")
        network.pay(invoice)
        await billing.confirm(invoice.id)

        entry = await billing.charge(
            ACCOUNT, "run-1", Usage(tokens=50_000, calls=4, cost=Money.parse("0.30"))
        )

        assert entry is not None
        assert entry.tokens < 0
        assert (await billing.balance(ACCOUNT)).total == 1_000_000 + entry.tokens

    async def test_charging_the_same_run_twice_debits_once(self, billing: BillingService) -> None:
        usage = Usage(tokens=50_000, calls=4, cost=Money.parse("0.30"))
        first = await billing.charge(ACCOUNT, "run-1", usage)
        second = await billing.charge(ACCOUNT, "run-1", usage)

        assert first is not None
        assert second is not None
        assert first.id == second.id
        assert len((await billing.view(ACCOUNT)).entries) == 1

    async def test_charging_may_take_the_balance_to_zero_and_never_refuses(
        self, billing: BillingService
    ) -> None:
        """The work is done and the provider has been paid. Refusing now would mean either
        giving it away or billing for something that was declined."""
        entry = await billing.charge(
            ACCOUNT, "run-1", Usage(tokens=9_000_000, calls=40, cost=Money.usd(50))
        )
        assert entry is not None
        assert (await billing.balance(ACCOUNT)).total == 0

    async def test_a_simulated_run_writes_no_row(self, billing: BillingService) -> None:
        """A row that says nothing happened is what an empty ledger already says."""
        assert await billing.charge(ACCOUNT, "run-1", Usage(tokens=5, simulated=True)) is None
        assert (await billing.view(ACCOUNT)).entries == []

    async def test_a_run_that_called_nothing_writes_no_row(self, billing: BillingService) -> None:
        assert await billing.charge(ACCOUNT, "run-1", Usage()) is None


class TestReconcile:
    async def test_it_settles_what_the_ledger_now_shows_paid(
        self, billing: BillingService, network: FakeNetwork
    ) -> None:
        """A payer who closes the tab must still get their tokens: nothing in a browser can
        be relied on to come back and say so."""
        invoice = await billing.checkout(ACCOUNT, "tokens-1m")
        network.pay(invoice)

        settled = await billing.reconcile()

        assert [i.id for i in settled] == [invoice.id]
        assert (await billing.balance(ACCOUNT)).purchased == 1_000_000

    async def test_one_bad_invoice_does_not_stop_the_sweep(
        self, billing: BillingService, network: FakeNetwork
    ) -> None:
        await billing.checkout(ACCOUNT, "tokens-1m")
        network.unreachable = True

        assert await billing.reconcile() == []


class TestView:
    async def test_it_reads_balance_subscription_and_movements_at_once(
        self, billing: BillingService, network: FakeNetwork
    ) -> None:
        invoice = await billing.checkout(ACCOUNT, "tokens-1m")
        network.pay(invoice)
        await billing.confirm(invoice.id)
        await billing.charge(ACCOUNT, "run-1", Usage(tokens=1_000, calls=1, cost=Money(100)))

        body = (await billing.view(ACCOUNT)).as_json()

        assert body["account"] == ACCOUNT
        assert body["balance"]["total"] > 0
        assert [e["kind"] for e in body["entries"]] == ["purchase", "usage"]


async def test_a_credit_cannot_be_negative(billing: BillingService) -> None:
    from gateway.domain.billing import BillingError

    with pytest.raises(BillingError):
        await billing.credit(ACCOUNT, -5, kind=EntryKind.ADJUSTMENT, reference="r")


async def test_the_catalog_and_asset_are_readable_for_the_api(billing: BillingService) -> None:
    assert billing.asset == "XLM"
    assert billing.reserve == 1_000
    assert isinstance(billing.product_named("starter"), Plan)
    assert isinstance(billing.product_named("tokens-1m"), Pack)
    assert billing.tokens_worth(Money.parse("6.00")) == Pricing().tokens_for(Money.parse("6.00"))
