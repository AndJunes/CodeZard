"""The chain as the authority for paid subscriptions.

The contract itself is tested in Rust, against a real ledger, in
`contracts/subscriptions/src/test.rs`. What is under test here is the half that lives in
Python: that an on-chain period is mirrored exactly once, that a contract that cannot be
reached is silence rather than a cancellation, and that a paid plan is no longer sold by
invoice once there is a contract to buy it from.
"""

from datetime import timedelta

import pytest

from gateway.application.billing_service import BillingService
from gateway.domain.billing import (
    BillingError,
    EntryKind,
    Money,
    SubscriptionStatus,
    default_catalog,
    now_utc,
)
from gateway.domain.ports import NoSubscriptions
from gateway.infrastructure.billing.sqlite_store import SqliteBillingStore
from gateway.infrastructure.billing.subscriptions import (
    OnChainSubscription,
    SubscriptionContractError,
)
from tests.unit.test_billing_service import FakeNetwork

ACCOUNT = "G" + "A" * 55
CONTRACT = "C" + "D" * 55


class FakeChain:
    """The contract, replaced. Answers whatever the test put in it, or raises."""

    available = True
    contract_id = CONTRACT

    def __init__(
        self, answer: OnChainSubscription | None = None, raises: Exception | None = None
    ) -> None:
        self.answer = answer
        self.raises = raises
        self.asked: list[str] = []

    def subscription(self, address: str) -> OnChainSubscription | None:
        self.asked.append(address)
        if self.raises is not None:
            raise self.raises
        return self.answer


def onchain(plan: str = "starter", days: int = 30, ago: int = 0) -> OnChainSubscription:
    started = now_utc() - timedelta(days=ago)
    return OnChainSubscription(
        plan=plan, started=started, expires=started + timedelta(days=days), periods=1
    )


@pytest.fixture
def store():
    opened = SqliteBillingStore()
    yield opened
    opened.close()


def service(store: SqliteBillingStore, chain: object) -> BillingService:
    return BillingService(
        store,
        FakeNetwork(),
        default_catalog(),
        "XLM",  # type: ignore[arg-type]
        reserve=1_000,
        chain=chain,
    )  # type: ignore[arg-type]


class TestMirroring:
    async def test_an_active_on_chain_period_becomes_the_account_s_plan(self, store) -> None:
        billing = service(store, FakeChain(onchain()))

        view = await billing.view(ACCOUNT)

        assert view.subscription is not None
        assert view.subscription.plan_id == "starter"
        assert view.subscription.status is SubscriptionStatus.ACTIVE
        assert view.balance.granted == default_catalog().plan("starter").tokens

    async def test_reading_the_account_ten_times_grants_one_period(self, store) -> None:
        """Mirroring runs on every read. The grant is keyed on the on-chain period, which is
        the only thing standing between that and crediting on every page load."""
        billing = service(store, FakeChain(onchain()))

        for _ in range(10):
            await billing.view(ACCOUNT)

        grants = [e for e in (await billing.view(ACCOUNT)).entries if e.kind is EntryKind.GRANT]
        assert len(grants) == 1

    async def test_renewing_early_does_not_grant_twice(self, store) -> None:
        """The contract extends `expires` and leaves `started` alone precisely so that this
        cannot happen; the reference is keyed on `started` to match."""
        chain = FakeChain(onchain())
        billing = service(store, chain)
        await billing.view(ACCOUNT)

        extended = chain.answer
        assert extended is not None
        chain.answer = OnChainSubscription(
            plan=extended.plan,
            started=extended.started,
            expires=extended.expires + timedelta(days=30),
            periods=2,
        )

        view = await billing.view(ACCOUNT)
        grants = [e for e in view.entries if e.kind is EntryKind.GRANT]
        assert len(grants) == 1

    async def test_a_new_period_grants_again(self, store) -> None:
        chain = FakeChain(onchain())
        billing = service(store, chain)
        await billing.view(ACCOUNT)

        chain.answer = onchain(ago=0)  # a later `started`: a different period
        chain.answer = OnChainSubscription(
            plan="starter",
            started=now_utc() + timedelta(days=31),
            expires=now_utc() + timedelta(days=61),
            periods=2,
        )

        view = await billing.view(ACCOUNT)
        grants = [e for e in view.entries if e.kind is EntryKind.GRANT]
        assert len(grants) == 2

    async def test_an_expired_on_chain_period_falls_back_to_the_free_plan(self, store) -> None:
        billing = service(store, FakeChain(onchain(days=30, ago=40)))

        view = await billing.view(ACCOUNT)

        assert view.subscription is not None
        assert view.subscription.plan_id == "free"

    async def test_somebody_who_never_subscribed_gets_the_free_plan(self, store) -> None:
        billing = service(store, FakeChain(None))

        view = await billing.view(ACCOUNT)

        assert view.subscription is not None
        assert view.subscription.plan_id == "free"

    async def test_a_plan_the_catalog_does_not_sell_is_ignored(self, store) -> None:
        """The chain is the authority on who paid, not on what this gateway sells."""
        billing = service(store, FakeChain(onchain(plan="legacy")))

        view = await billing.view(ACCOUNT)

        assert view.subscription is not None
        assert view.subscription.plan_id == "free"


class TestWhenTheChainCannotBeReached:
    async def test_it_is_silence_and_not_a_cancellation(self, store) -> None:
        """Treating an unreachable RPC as "not subscribed" would cancel a paying customer
        because the network blinked."""
        billing = service(store, FakeChain(raises=SubscriptionContractError("rpc down")))

        view = await billing.view(ACCOUNT)

        assert view.subscription is not None, "the account still works"
        assert view.subscription.plan_id == "free"

    async def test_a_deployment_with_no_contract_never_asks(self, store) -> None:
        billing = service(store, NoSubscriptions())

        view = await billing.view(ACCOUNT)

        assert billing.contract_id == ""
        assert view.subscription is not None
        assert view.subscription.plan_id == "free"


class TestWhatIsStillSoldByInvoice:
    async def test_a_paid_plan_is_not_sold_by_invoice_when_there_is_a_contract(self, store) -> None:
        """Two ways to buy one thing is two ways whose halves can come apart — the exact
        failure the contract exists to remove."""
        billing = service(store, FakeChain(None))

        with pytest.raises(BillingError, match="on chain"):
            await billing.checkout(ACCOUNT, "starter")

    async def test_token_packs_are_still_bought_with_an_invoice(self, store) -> None:
        """They are not a subscription: there is no period to record, so there is nothing a
        contract would add over a payment with a memo."""
        billing = service(store, FakeChain(None))

        invoice = await billing.checkout(ACCOUNT, "tokens-1m")

        assert invoice.tokens == 1_000_000
        assert invoice.memo == invoice.id

    async def test_the_free_plan_is_never_sold(self, store) -> None:
        billing = service(store, FakeChain(None))

        with pytest.raises(BillingError, match="costs nothing"):
            await billing.checkout(ACCOUNT, "free")


class TestTheOnChainValue:
    def test_a_period_names_itself_by_when_it_started(self) -> None:
        started = now_utc()
        first = OnChainSubscription("starter", started, started + timedelta(days=30), 1)
        extended = OnChainSubscription("starter", started, started + timedelta(days=60), 2)
        later = OnChainSubscription(
            "starter", started + timedelta(days=31), started + timedelta(days=61), 2
        )

        assert first.period_key == extended.period_key, "extending is the same period"
        assert first.period_key != later.period_key, "a lapse starts a new one"

    def test_it_knows_whether_it_is_live(self) -> None:
        started = now_utc() - timedelta(days=10)
        subscription = OnChainSubscription("starter", started, started + timedelta(days=30), 1)

        assert subscription.active(now_utc())
        assert not subscription.active(now_utc() + timedelta(days=30))

    def test_it_serialises_for_a_screen(self) -> None:
        started = now_utc()
        body = OnChainSubscription("starter", started, started + timedelta(days=30), 3).as_json()

        assert body["plan"] == "starter"
        assert body["periods"] == 3
        assert body["expires"] > body["started"]


async def test_money_is_never_invented_by_a_mirror(store) -> None:
    """The mirror grants tokens; it must never grant MONEY. A grant carries a zero amount,
    because nothing was paid to us — it was paid to the contract."""
    billing = service(store, FakeChain(onchain()))

    view = await billing.view(ACCOUNT)
    grant = next(e for e in view.entries if e.kind is EntryKind.GRANT)

    assert grant.amount == Money()
    assert grant.reference.startswith("chain:")
