"""The money rules, checked where they are cheapest to check."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from gateway.domain.billing import (
    MONTH,
    Balance,
    BillingError,
    Catalog,
    EntryKind,
    Invoice,
    InvoiceStatus,
    LedgerEntry,
    Money,
    ObservedPayment,
    Pack,
    Plan,
    Pricing,
    Subscription,
    SubscriptionStatus,
    UnknownProductError,
    Usage,
    balance_of,
    default_catalog,
    new_invoice_id,
    now_utc,
)

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
ACCOUNT = "G" + "A" * 55


def entry(kind: EntryKind, tokens: int, reference: str = "") -> LedgerEntry:
    return LedgerEntry(
        id=f"e{tokens}{kind}",
        account=ACCOUNT,
        kind=kind,
        tokens=tokens,
        at=NOW,
        reference=reference,
    )


class TestMoney:
    def test_a_decimal_string_becomes_exact_micros(self) -> None:
        assert Money.parse("19.00").micros == 19_000_000
        assert Money.parse("0.000001").micros == 1

    def test_a_float_is_read_as_the_decimal_it_was_written_as(self) -> None:
        """Through `str`, so `0.1` means a tenth and not the binary number nearest to it."""
        assert Money.parse(0.1) == Money.parse("0.1")
        assert (Money.parse(0.1) + Money.parse(0.2)) == Money.parse("0.3")

    def test_rounding_is_half_up_and_happens_once(self) -> None:
        assert Money.parse("0.0000005").micros == 1
        assert Money.parse("0.0000004").micros == 0

    @pytest.mark.parametrize("value", ["", "abc", "NaN", "Infinity", None])
    def test_what_is_not_money_is_refused(self, value: object) -> None:
        with pytest.raises(BillingError):
            Money.parse(value)  # type: ignore[arg-type]

    def test_arithmetic_stays_in_integers(self) -> None:
        assert (Money.usd(3) - Money.usd(1)).micros == 2_000_000
        assert (-Money.usd(2)).micros == -2_000_000

    def test_scaling_rounds_half_up_without_leaving_integers(self) -> None:
        assert Money(3).scaled(1, 2).micros == 2  # 1.5 -> 2
        assert Money.usd(10).scaled(13_000, 10_000) == Money.usd(13)

    def test_scaling_by_zero_is_refused_rather_than_crashing(self) -> None:
        with pytest.raises(BillingError):
            Money.usd(1).scaled(1, 0)

    def test_it_prints_without_trailing_noise(self) -> None:
        assert str(Money.parse("19.00")) == "19"
        assert str(Money.parse("0.50")) == "0.5"
        assert str(Money()) == "0"

    def test_it_is_ordered_so_a_balance_can_be_compared(self) -> None:
        assert Money.usd(1) < Money.usd(2)


class TestPricing:
    def test_tokens_and_money_convert_both_ways(self) -> None:
        pricing = Pricing(per_million=Money.parse("6.00"))
        assert pricing.cost_of(1_000_000) == Money.parse("6.00")
        assert pricing.tokens_for(Money.parse("6.00")) == 1_000_000

    def test_a_purchase_never_rounds_in_our_favour_by_accident(self) -> None:
        """Tokens sold are rounded DOWN: never deliver more than was paid for."""
        pricing = Pricing(per_million=Money.parse("3.00"))
        assert pricing.tokens_for(Money.parse("0.000001")) == 0

    def test_what_is_billed_follows_the_cost_not_the_token_count(self) -> None:
        """Otherwise switching model silently rewrites the price of everything sold."""
        pricing = Pricing(per_million=Money.parse("6.00"), margin_bps=0, minimum_charge=0)
        usage = Usage(tokens=500_000, calls=3, cost=Money.parse("6.00"))
        assert pricing.billable(usage) == 1_000_000

    def test_the_margin_is_added_on_top_of_measured_cost(self) -> None:
        pricing = Pricing(per_million=Money.parse("10.00"), margin_bps=3_000, minimum_charge=0)
        usage = Usage(tokens=1, calls=1, cost=Money.parse("10.00"))
        assert pricing.billable(usage) == 1_300_000

    def test_a_free_model_is_billed_on_its_tokens_because_there_is_nothing_else(self) -> None:
        pricing = Pricing(minimum_charge=0)
        assert pricing.billable(Usage(tokens=42_000, calls=1, cost=Money())) == 42_000

    def test_every_real_run_costs_at_least_the_minimum(self) -> None:
        """A run that costs a thousandth of a cent still reserved a process and a container."""
        pricing = Pricing(minimum_charge=1_000)
        assert pricing.billable(Usage(tokens=1, calls=1, cost=Money(1))) == 1_000

    def test_a_simulated_run_owes_nothing(self) -> None:
        assert Pricing().billable(Usage(tokens=9_000, calls=4, simulated=True)) == 0

    def test_a_run_that_called_nothing_owes_nothing(self) -> None:
        assert Pricing().billable(Usage()) == 0

    def test_a_price_of_zero_sells_no_tokens_instead_of_dividing_by_it(self) -> None:
        assert Pricing(per_million=Money()).tokens_for(Money.usd(5)) == 0


class TestUsage:
    def test_it_reads_the_numbers_and_not_the_sentence(self) -> None:
        """`text` says "$0.0123", "gratis" or "salió de un guion" depending on the case. The
        numbers are always in the same three keys."""
        panel = {
            "text": "$0.0123",
            "free": False,
            "usage": {"tokens": 5_000, "calls": 2, "cost_usd": 0.0123, "simulated": False},
        }
        usage = Usage.from_agent(panel)
        assert usage.tokens == 5_000
        assert usage.calls == 2
        assert usage.cost == Money.parse("0.0123")

    def test_an_older_agent_without_the_usage_block_still_reads(self) -> None:
        assert Usage.from_agent({"tokens": 700, "calls": 1}).tokens == 700

    @pytest.mark.parametrize(
        "panel",
        [None, {}, {"usage": "nonsense"}, {"usage": {"tokens": "lots", "cost_usd": "free"}}],
    )
    def test_anything_malformed_reads_as_zero_rather_than_raising(self, panel: object) -> None:
        """A bad usage report must not fail a run that already happened, and zero errs
        towards not charging — the right direction to err."""
        assert Usage.from_agent(panel).is_empty  # type: ignore[arg-type]

    def test_a_negative_token_count_cannot_create_credit(self) -> None:
        assert Usage.from_agent({"usage": {"tokens": -5_000}}).tokens == 0


class TestBalance:
    def test_a_balance_is_the_sum_of_what_happened(self) -> None:
        balance = balance_of(
            ACCOUNT, [entry(EntryKind.PURCHASE, 1_000), entry(EntryKind.USAGE, -300)]
        )
        assert balance.purchased == 700
        assert balance.total == 700

    def test_a_debit_spends_the_perishable_pool_first(self) -> None:
        """Granted tokens expire and purchased ones do not, so spending the grant first is
        what leaves the account with more."""
        balance = balance_of(
            ACCOUNT,
            [
                entry(EntryKind.GRANT, 1_000),
                entry(EntryKind.PURCHASE, 1_000),
                entry(EntryKind.USAGE, -600),
            ],
        )
        assert balance.granted == 400
        assert balance.purchased == 1_000

    def test_a_debit_larger_than_the_grant_spills_into_the_purchase(self) -> None:
        balance = balance_of(
            ACCOUNT,
            [
                entry(EntryKind.GRANT, 500),
                entry(EntryKind.PURCHASE, 1_000),
                entry(EntryKind.USAGE, -800),
            ],
        )
        assert balance.granted == 0
        assert balance.purchased == 700

    def test_an_expiry_takes_back_only_what_is_left_of_the_grant(self) -> None:
        balance = balance_of(
            ACCOUNT,
            [
                entry(EntryKind.GRANT, 1_000),
                entry(EntryKind.USAGE, -400),
                entry(EntryKind.EXPIRY, -600),
            ],
        )
        assert balance.granted == 0

    def test_a_refund_puts_the_tokens_back(self) -> None:
        balance = balance_of(
            ACCOUNT,
            [
                entry(EntryKind.PURCHASE, 500),
                entry(EntryKind.USAGE, -100),
                entry(EntryKind.REFUND, 100),
            ],
        )
        assert balance.total == 500

    def test_it_never_goes_below_zero(self) -> None:
        assert balance_of(ACCOUNT, [entry(EntryKind.USAGE, -900)]).total == 0

    def test_an_overspend_is_carried_and_not_forgiven_by_topping_up(self) -> None:
        """Charging happens after the work, so a run CAN take an account past zero. The
        clamp is only on the answer — the debt is still in the sum, and the next purchase
        settles it rather than starting from a clean slate."""
        balance = balance_of(
            ACCOUNT, [entry(EntryKind.USAGE, -900), entry(EntryKind.PURCHASE, 1_000)]
        )
        assert balance.total == 100

    def test_affordability_is_asked_of_the_total(self) -> None:
        assert Balance(ACCOUNT, granted=400, purchased=200).can_afford(600)
        assert not Balance(ACCOUNT, granted=400, purchased=200).can_afford(601)


class TestCatalog:
    def test_plans_and_packs_are_found_by_sku(self) -> None:
        catalog = default_catalog()
        assert isinstance(catalog.product("starter"), Plan)
        assert isinstance(catalog.product("tokens-1m"), Pack)

    def test_an_unknown_sku_is_refused_by_name(self) -> None:
        with pytest.raises(UnknownProductError):
            default_catalog().product("free-forever")

    def test_asking_for_a_plan_and_getting_a_pack_is_refused(self) -> None:
        with pytest.raises(UnknownProductError):
            default_catalog().plan("tokens-1m")

    def test_the_shipped_catalog_sells_nothing_for_nothing(self) -> None:
        """A catalog of zeroes looks configured and gives the product away."""
        catalog = default_catalog()
        assert catalog.plans
        assert catalog.packs
        for product in (*catalog.plans, *catalog.packs):
            assert product.price.micros > 0, product.id
            assert product.tokens > 0, product.id

    def test_it_serialises_without_losing_the_price(self) -> None:
        body = Catalog(plans=(Plan("p", "P", Money.usd(9), 1_000),)).as_json()
        assert body["plans"][0]["price"]["usd"] == "9"


class TestSubscription:
    def test_a_period_is_thirty_days_every_time(self) -> None:
        """Calendar months are 28 to 31 days: pricing them identically charges a February
        subscriber 10% more per day than a March one."""
        plan = Plan("p", "P", Money.usd(19), 1_000)
        subscription = Subscription.begin(ACCOUNT, plan, NOW)
        assert subscription.renews_at - subscription.started_at == MONTH

    def test_it_is_active_until_its_period_ends(self) -> None:
        plan = Plan("p", "P", Money.usd(19), 1_000)
        subscription = Subscription.begin(ACCOUNT, plan, NOW)
        assert subscription.is_active(NOW + timedelta(days=29))
        assert not subscription.is_active(NOW + timedelta(days=31))
        assert subscription.due(NOW + timedelta(days=31))

    def test_renewing_starts_a_fresh_period(self) -> None:
        plan = Plan("p", "P", Money.usd(19), 1_000)
        later = NOW + timedelta(days=35)
        renewed = Subscription.begin(ACCOUNT, plan, NOW).renewed(later)
        assert renewed.renews_at == later + MONTH
        assert renewed.status is SubscriptionStatus.ACTIVE

    def test_a_cancelled_subscription_is_neither_active_nor_due(self) -> None:
        plan = Plan("p", "P", Money.usd(19), 1_000)
        cancelled = Subscription.begin(ACCOUNT, plan, NOW).cancelled()
        assert not cancelled.is_active(NOW)
        assert not cancelled.due(NOW + timedelta(days=99))


def invoice(**changes: object) -> Invoice:
    base = {
        "id": "cz0123456789abcdef0123",
        "account": ACCOUNT,
        "sku": "tokens-1m",
        "tokens": 1_000_000,
        "price": Money.usd(8),
        "asset": "XLM",
        "amount": "80.0000000",
        "destination": "G" + "B" * 55,
        "created_at": NOW,
        "expires_at": NOW + timedelta(minutes=30),
    }
    return Invoice(**{**base, **changes})  # type: ignore[arg-type]


def payment(**changes: object) -> ObservedPayment:
    base = {
        "tx_hash": "abc",
        "payer": "G" + "C" * 55,
        "destination": "G" + "B" * 55,
        "asset": "XLM",
        "amount": "80.0000000",
        "memo": "cz0123456789abcdef0123",
        "at": NOW,
    }
    return ObservedPayment(**{**base, **changes})  # type: ignore[arg-type]


class TestInvoice:
    def test_an_id_fits_a_stellar_text_memo(self) -> None:
        """The memo is 28 bytes. An id that does not fit is an invoice that cannot be paid."""
        for _ in range(20):
            assert len(new_invoice_id().encode("utf-8")) <= 28

    def test_ids_are_not_sequential(self) -> None:
        """The memo is public on the ledger; sequential ids would publish the sales count."""
        assert len({new_invoice_id() for _ in range(50)}) == 50

    def test_it_is_open_until_it_expires(self) -> None:
        assert invoice().is_open(NOW)
        assert not invoice().is_open(NOW + timedelta(hours=2))

    def test_a_paid_invoice_is_no_longer_open(self) -> None:
        assert not invoice().paid("hash", ACCOUNT).is_open(NOW)
        assert invoice().paid("hash", ACCOUNT).status is InvoiceStatus.PAID

    def test_the_memo_is_the_id(self) -> None:
        assert invoice().memo == invoice().id


class TestSettlement:
    def test_the_right_payment_settles_it(self) -> None:
        assert payment().settles(invoice())

    def test_overpaying_settles_it(self) -> None:
        assert payment(amount="99.0000000").settles(invoice())

    def test_a_stroop_short_is_within_tolerance(self) -> None:
        """A wallet that rounds the last stroop down must not leave a paying customer unpaid."""
        assert payment(amount="79.9999999").settles(invoice())

    def test_paying_meaningfully_less_does_not(self) -> None:
        assert not payment(amount="40.0000000").settles(invoice())

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("memo", "czsomethingelse"),
            ("destination", "G" + "Z" * 55),
            ("asset", "USDC"),
        ],
    )
    def test_all_four_things_have_to_agree(self, field: str, value: str) -> None:
        assert not payment(**{field: value}).settles(invoice())

    def test_a_case_difference_in_the_asset_is_not_a_mismatch(self) -> None:
        assert payment(asset="xlm").settles(invoice())

    def test_an_unreadable_amount_does_not_settle_anything(self) -> None:
        assert not payment(amount="lots").settles(invoice())
        assert not payment().settles(invoice(amount="free"))


def test_now_is_timezone_aware() -> None:
    """A naive datetime in a ledger is a bug waiting for a timezone change."""
    assert now_utc().tzinfo is not None
    assert now_utc() - datetime.now(UTC) < timedelta(seconds=5)


def test_a_cost_of_a_million_tokens_is_a_decimal_not_a_float() -> None:
    assert isinstance(Money.usd(6).decimal, Decimal)
