"""What is sold, what is owed, and what was spent. No HTTP, no SQL, no Stellar.

THREE RULES THIS FILE EXISTS TO KEEP

1. **Money is never a float.** ``0.1 + 0.2`` is not ``0.3``, and a ledger that drifts by a
   millionth per entry is a ledger nobody can reconcile. Every amount here is an integer
   number of USD micros, and the only place a decimal string becomes one is
   :meth:`Money.parse`, which rounds once, explicitly, and says which way.

2. **The ledger is append-only.** A balance is not a number someone keeps up to date; it is
   the sum of what happened. Nothing in this module can edit or delete an entry, so a balance
   can always be explained by listing the rows that produced it — which is the only form of
   "why was I charged this" that survives an argument.

3. **Tokens are the unit of consumption; USD is the unit of price.** They meet in exactly one
   place, :class:`Pricing`, and everywhere else they are different types doing different
   jobs. When they were the same number, a change to the rate silently rewrote history.

WHY TOKENS AT ALL
    A run costs what the model charged for it, which is known only after it ran and varies by
    an order of magnitude between a small plan and a large one. Selling seats would mean
    guessing; selling tokens means the thing sold is the thing consumed, and a subscription
    is then a monthly grant of them rather than a different product.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from gateway.domain.exceptions import GatewayError

MICROS = 1_000_000
"""USD micros per dollar. Six places is what every payment rail this settles on quotes in."""

PER_MILLION = 1_000_000
"""Token rates are quoted per million, the way every model provider quotes them."""

MEMO_BYTES = 28
"""A Stellar text memo is at most 28 bytes. The invoice id travels in one, so the id has to
fit — which is why it is generated here rather than being any string a caller likes."""


class BillingError(GatewayError):
    """Something about money or entitlement went wrong. The API layer picks the status.

    A ``GatewayError`` so that it reaches the one handler this gateway registers and comes
    back in the same ``{"error": {...}}`` envelope as every other failure. Deriving from bare
    ``Exception`` put it in front of the catch-all instead, which answers 500 "Internal server
    error" — so "there is no plan called that" read as the gateway being broken.
    """


class InsufficientFundsError(BillingError):
    """The account cannot pay for what it is asking for."""

    def __init__(self, needed: int, available: int) -> None:
        super().__init__(f"This account has {available:,} tokens and needs {needed:,} for that run")
        self.needed = needed
        self.available = available


class UnknownProductError(BillingError):
    def __init__(self, sku: str) -> None:
        super().__init__(f"There is no plan or token pack called {sku!r}")
        self.sku = sku


class InvoiceNotFoundError(BillingError):
    def __init__(self, invoice_id: str) -> None:
        super().__init__("That invoice does not exist, or it expired")
        self.invoice_id = invoice_id


# ── money ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, order=True, slots=True)
class Money:
    """An amount of USD, as an integer number of micros. Immutable and exact."""

    micros: int = 0

    @classmethod
    def parse(cls, value: str | int | float | Decimal) -> Money:
        """The ONE place a decimal becomes micros, rounding half up, once.

        A float is accepted because JSON has nothing else, and is routed through ``str`` so
        that ``0.1`` means the decimal 0.1 rather than the binary number closest to it.
        """
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, ValueError) as error:
            raise BillingError(f"{value!r} is not an amount of money") from error
        if not amount.is_finite():
            raise BillingError(f"{value!r} is not an amount of money")
        return cls(int((amount * MICROS).quantize(Decimal(1), rounding=ROUND_HALF_UP)))

    @classmethod
    def usd(cls, dollars: int) -> Money:
        return cls(dollars * MICROS)

    @property
    def decimal(self) -> Decimal:
        return Decimal(self.micros) / MICROS

    @property
    def is_zero(self) -> bool:
        return self.micros == 0

    def __str__(self) -> str:
        return f"{self.decimal:.6f}".rstrip("0").rstrip(".") or "0"

    def __add__(self, other: Money) -> Money:
        return Money(self.micros + other.micros)

    def __sub__(self, other: Money) -> Money:
        return Money(self.micros - other.micros)

    def __neg__(self) -> Money:
        return Money(-self.micros)

    def scaled(self, numerator: int, denominator: int) -> Money:
        """``self * numerator / denominator``, rounded half up, in integers throughout."""
        if denominator == 0:
            raise BillingError("a price cannot be scaled by zero")
        return Money((self.micros * numerator + denominator // 2) // denominator)

    def as_json(self) -> dict[str, Any]:
        return {"micros": self.micros, "usd": str(self)}


# ── what is sold ─────────────────────────────────────────────────────────────


class Cadence(StrEnum):
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    ONE_OFF = "one_off"


WEEK = timedelta(days=7)
MONTH = timedelta(days=30)
"""A period, in the only definition that is the same length every time.

Calendar months are 28 to 31 days, so pricing them identically means a February subscriber
pays 10% more per day than a March one for the same thing. Thirty days is a period, said
plainly, and it renews on a date arithmetic can always produce."""

PERIODS: Mapping[str, timedelta] = MappingProxyType(
    {
        Cadence.WEEKLY.value: WEEK,
        Cadence.MONTHLY.value: MONTH,
    }
)


@dataclass(frozen=True, slots=True)
class Plan:
    """A subscription: a recurring price that grants tokens every period."""

    id: str
    name: str
    price: Money
    tokens: int
    """Granted at the start of every period. They do NOT roll over — see :func:`renew`."""
    cadence: Cadence = Cadence.MONTHLY
    description: str = ""
    overage: bool = True
    """May the account keep working past its grant by spending purchased tokens?"""

    @property
    def period(self) -> timedelta:
        """How long one grant lasts. A one-off plan is a contradiction; it reads as monthly."""
        return PERIODS.get(self.cadence.value, MONTH)

    @property
    def is_free(self) -> bool:
        """Costs nothing, so it is granted rather than sold — and, unlike a paid plan, it can
        renew itself. A crypto payment cannot be pulled; a grant of nothing can."""
        return self.price.is_zero

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": "plan",
            "name": self.name,
            "price": self.price.as_json(),
            "tokens": self.tokens,
            "cadence": self.cadence.value,
            "period_days": self.period.days,
            "free": self.is_free,
            "description": self.description,
            "overage": self.overage,
        }


@dataclass(frozen=True, slots=True)
class Pack:
    """Prepaid tokens, bought once. They never expire: they were paid for."""

    id: str
    name: str
    price: Money
    tokens: int
    description: str = ""

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": "pack",
            "name": self.name,
            "price": self.price.as_json(),
            "tokens": self.tokens,
            "description": self.description,
        }


Product = Plan | Pack


@dataclass(frozen=True, slots=True)
class Catalog:
    """Everything on sale, and the conversion between tokens and money.

    A value, not a table: what is on sale is a deployment decision that has to be reviewable,
    and a catalog that lives in a database is one an operator can change without anyone
    noticing that yesterday's invoices were priced differently.
    """

    plans: tuple[Plan, ...] = ()
    packs: tuple[Pack, ...] = ()
    pricing: Pricing = field(default_factory=lambda: Pricing())

    def product(self, sku: str) -> Product:
        # The list is annotated rather than splatted into a tuple: `(*plans, *packs)` widens
        # to `object` and takes `.id` with it.
        everything: list[Product] = [*self.plans, *self.packs]
        for product in everything:
            if product.id == sku:
                return product
        raise UnknownProductError(sku)

    def plan(self, plan_id: str) -> Plan:
        product = self.product(plan_id)
        if not isinstance(product, Plan):
            raise UnknownProductError(plan_id)
        return product

    def as_json(self) -> dict[str, Any]:
        return {
            "plans": [p.as_json() for p in self.plans],
            "packs": [p.as_json() for p in self.packs],
            "pricing": self.pricing.as_json(),
        }


@dataclass(frozen=True, slots=True)
class Pricing:
    """The only place tokens and money meet.

    ``margin_bps`` is what covers everything that is not the model call itself — the
    machine the code runs on, the packages it installs, the bandwidth of a ZIP. It is
    expressed in basis points so that changing it is one integer and not a new formula.
    """

    per_million: Money = field(default_factory=lambda: Money.parse("6.00"))
    """List price of a million tokens."""
    margin_bps: int = 3_000
    """Basis points added on top of measured cost when converting a spend into tokens."""
    minimum_charge: int = 1_000
    """Tokens debited for any run that did anything at all. A run that costs a thousandth of
    a cent still reserved a process, a container and a disk."""

    def cost_of(self, tokens: int) -> Money:
        return self.per_million.scaled(max(0, tokens), PER_MILLION)

    def tokens_for(self, amount: Money) -> int:
        """How many tokens ``amount`` buys at list price. Rounded DOWN: never sell more than
        was paid for."""
        if self.per_million.micros <= 0:
            return 0
        return max(0, amount.micros * PER_MILLION // self.per_million.micros)

    def billable(self, usage: Usage) -> int:
        """The tokens to debit for a run.

        Not simply ``usage.tokens``. What the account is charged for is what the run COST,
        marked up, expressed in the unit it holds — otherwise a switch to a cheaper or dearer
        model silently rewrites the price of everything sold so far. When the provider reports
        no cost at all (a free model), the token count is the only measure there is and it is
        used directly.
        """
        if usage.simulated or usage.is_empty:
            return 0
        if usage.cost.is_zero:
            return max(self.minimum_charge, usage.tokens)
        marked_up = usage.cost.scaled(10_000 + self.margin_bps, 10_000)
        return max(self.minimum_charge, self.tokens_for(marked_up))

    def as_json(self) -> dict[str, Any]:
        return {
            "per_million": self.per_million.as_json(),
            "margin_bps": self.margin_bps,
            "minimum_charge": self.minimum_charge,
        }


# ── what was spent ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Usage:
    """What one run really consumed, as the agent measured it.

    Read from the agent's `usage` object and never from its `cost_summary` text: that string
    is written for a person and is allowed to change wording, and a billing input that is
    parsed out of a sentence is a billing input that will one day be parsed wrong.
    """

    tokens: int = 0
    calls: int = 0
    cost: Money = field(default_factory=Money)
    simulated: bool = False
    """A demo, a double or the offline lock. Real work was not done, so nothing is owed."""

    @property
    def is_empty(self) -> bool:
        return self.calls == 0 and self.tokens == 0

    @classmethod
    def from_agent(cls, panel: Mapping[str, Any] | None) -> Usage:
        """The agent's cost panel, defensively.

        Anything missing or of the wrong type reads as zero rather than raising: a malformed
        usage report must not be able to fail a run that already happened, and a zero here
        errs towards not charging, which is the right direction to err.
        """
        if not isinstance(panel, Mapping):
            return cls()
        usage = panel.get("usage")
        source: Mapping[str, Any] = usage if isinstance(usage, Mapping) else panel
        return cls(
            tokens=_int(source.get("tokens")),
            calls=_int(source.get("calls")),
            cost=_money(source.get("cost_usd")),
            simulated=bool(source.get("simulated")),
        )

    def as_json(self) -> dict[str, Any]:
        return {
            "tokens": self.tokens,
            "calls": self.calls,
            "cost": self.cost.as_json(),
            "simulated": self.simulated,
        }


def _int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _money(value: Any) -> Money:
    if value is None:
        return Money()
    try:
        return Money.parse(value)
    except BillingError:
        return Money()


# ── the ledger ───────────────────────────────────────────────────────────────


class EntryKind(StrEnum):
    GRANT = "grant"
    """A subscription period's tokens. Expire when the period does."""
    PURCHASE = "purchase"
    """Tokens bought outright. They do not expire."""
    USAGE = "usage"
    """A run, debited."""
    REFUND = "refund"
    ADJUSTMENT = "adjustment"
    """An operator correcting something by hand. Always in the open, never by editing a row."""
    EXPIRY = "expiry"
    """A grant's unused remainder, removed when its period ended."""


CREDITS = frozenset({EntryKind.GRANT, EntryKind.PURCHASE, EntryKind.REFUND})


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """One immutable fact. The balance is the sum of these and nothing else."""

    id: str
    account: str
    kind: EntryKind
    tokens: int
    """Signed: positive credits, negative debits."""
    at: datetime
    amount: Money = field(default_factory=Money)
    """What it cost in USD, when it was a purchase. Zero for a grant or a debit."""
    reference: str = ""
    """The idempotency key: an invoice id, a run id, a transaction hash.

    Two entries with the same reference are the same fact recorded twice, and the store
    refuses the second. It is what makes "confirm this payment" safe to call from a poller,
    a webhook and an impatient person at the same time."""
    memo: str = ""

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "tokens": self.tokens,
            "at": self.at.isoformat(),
            "amount": self.amount.as_json(),
            "reference": self.reference,
            "memo": self.memo,
        }


@dataclass(frozen=True, slots=True)
class Balance:
    """What an account can spend right now, and where it came from."""

    account: str
    granted: int = 0
    """From the current subscription period. Lost at renewal."""
    purchased: int = 0
    """Bought outright. Kept."""

    @property
    def total(self) -> int:
        return self.granted + self.purchased

    def can_afford(self, tokens: int) -> bool:
        return self.total >= tokens

    def as_json(self) -> dict[str, Any]:
        return {
            "account": self.account,
            "granted": self.granted,
            "purchased": self.purchased,
            "total": self.total,
        }


def balance_of(account: str, entries: Iterable[LedgerEntry]) -> Balance:
    """Replay the entries. THE definition of a balance — there is no other.

    Debits are taken from the granted pool first, because a grant expires and a purchase does
    not: spending the perishable one first is the answer that leaves the account with more.
    """
    granted = 0
    purchased = 0
    for entry in entries:
        if entry.kind is EntryKind.GRANT:
            granted += entry.tokens
        elif entry.kind is EntryKind.EXPIRY:
            granted = max(0, granted + entry.tokens)
        elif entry.kind in CREDITS:
            purchased += entry.tokens
        else:
            owed = -entry.tokens
            from_grant = min(granted, owed)
            granted -= from_grant
            purchased -= owed - from_grant
    # A debit that overshot both pools leaves `purchased` negative, and clamping it away on
    # its own would FORGIVE the overspend. That matters because of the free plan: a run is
    # only ever authorised against a balance, so the only way to overshoot is a single run
    # costing more than was left — and if next week's grant did not absorb it, overshooting
    # would be free money, once a week, forever. The shortfall follows the account into
    # whatever it holds next.
    if purchased < 0:
        granted += purchased
        purchased = 0
    return Balance(account, max(0, granted), max(0, purchased))


# ── subscriptions ────────────────────────────────────────────────────────────


class SubscriptionStatus(StrEnum):
    ACTIVE = "active"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class Subscription:
    account: str
    plan_id: str
    started_at: datetime
    renews_at: datetime
    status: SubscriptionStatus = SubscriptionStatus.ACTIVE

    @classmethod
    def begin(cls, account: str, plan: Plan, now: datetime) -> Subscription:
        return cls(account, plan.id, now, now + plan.period)

    def is_active(self, now: datetime) -> bool:
        return self.status is SubscriptionStatus.ACTIVE and now < self.renews_at

    def due(self, now: datetime) -> bool:
        """The period is over and the next one has not been paid for."""
        return self.status is SubscriptionStatus.ACTIVE and now >= self.renews_at

    def renewed(self, plan: Plan, now: datetime) -> Subscription:
        """The next period. Takes the plan because the plan owns how long a period is."""
        return replace(
            self,
            started_at=now,
            renews_at=now + plan.period,
            status=SubscriptionStatus.ACTIVE,
        )

    def ended(self) -> Subscription:
        return replace(self, status=SubscriptionStatus.EXPIRED)

    def cancelled(self) -> Subscription:
        return replace(self, status=SubscriptionStatus.CANCELLED)

    @property
    def period_key(self) -> str:
        """Names THIS period, so a grant for it can be written exactly once.

        Settling a period happens on every read of the account, which makes the reference the
        only thing standing between "the free plan renews weekly" and "the free plan grants
        tokens on every page load".
        """
        return f"{self.plan_id}:{self.renews_at.isoformat()}"

    def as_json(self) -> dict[str, Any]:
        return {
            "account": self.account,
            "plan": self.plan_id,
            "status": self.status.value,
            "started_at": self.started_at.isoformat(),
            "renews_at": self.renews_at.isoformat(),
        }


# ── invoices ─────────────────────────────────────────────────────────────────


class InvoiceStatus(StrEnum):
    PENDING = "pending"
    PAID = "paid"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


INVOICE_TTL = timedelta(minutes=30)
"""Long enough to open a wallet and read the amount twice; short enough that a quoted
exchange rate is still roughly the rate."""


def new_invoice_id() -> str:
    """24 characters, which fits a Stellar text memo with room to spare.

    Unguessable rather than sequential: the memo is public on the ledger, and sequential ids
    would publish how many invoices have been issued.
    """
    return "cz" + secrets.token_hex(11)


@dataclass(frozen=True, slots=True)
class Invoice:
    """A quote that has been written down: this account, this product, this exact amount.

    The AMOUNT is frozen at creation and never recomputed. An invoice whose price moves while
    the payer is looking at it is not an invoice.
    """

    id: str
    account: str
    sku: str
    tokens: int
    """What is delivered when it is paid."""
    price: Money
    """The USD list price. What is actually transferred is :attr:`amount` of :attr:`asset`."""
    asset: str
    amount: str
    """The settlement amount, as the payment network spells it (7 decimals on Stellar)."""
    destination: str
    created_at: datetime
    expires_at: datetime
    status: InvoiceStatus = InvoiceStatus.PENDING
    kind: str = "pack"
    """``plan`` or ``pack``: what paying it does, beyond crediting tokens."""
    tx_hash: str = ""
    payer: str = ""

    @property
    def memo(self) -> str:
        """What the payer must attach so the payment can be matched to this invoice."""
        return self.id

    def is_open(self, now: datetime) -> bool:
        return self.status is InvoiceStatus.PENDING and now < self.expires_at

    def paid(self, tx_hash: str, payer: str) -> Invoice:
        return replace(self, status=InvoiceStatus.PAID, tx_hash=tx_hash, payer=payer)

    def expired(self) -> Invoice:
        return replace(self, status=InvoiceStatus.EXPIRED)

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "account": self.account,
            "sku": self.sku,
            "kind": self.kind,
            "tokens": self.tokens,
            "price": self.price.as_json(),
            "asset": self.asset,
            "amount": self.amount,
            "destination": self.destination,
            "memo": self.memo,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "tx_hash": self.tx_hash,
        }


@dataclass(frozen=True, slots=True)
class ObservedPayment:
    """A payment the network says happened. Nothing here is taken on the payer's word."""

    tx_hash: str
    payer: str
    destination: str
    asset: str
    amount: str
    memo: str
    at: datetime

    def settles(self, invoice: Invoice, tolerance_bps: int = 100) -> bool:
        """Does this payment pay that invoice?

        Four things have to agree — the memo, the destination, the asset and the amount — and
        the amount only has to be at LEAST what was asked, minus a tolerance. A wallet that
        rounds the last stroop down must not leave a paying customer unpaid, and anyone
        overpaying has paid.
        """
        if self.memo != invoice.memo or self.destination != invoice.destination:
            return False
        if self.asset.upper() != invoice.asset.upper():
            return False
        try:
            paid = Decimal(self.amount)
            owed = Decimal(invoice.amount)
        except (InvalidOperation, ValueError):
            return False
        return paid >= owed * (Decimal(10_000 - tolerance_bps) / Decimal(10_000))


# ── the default catalog ──────────────────────────────────────────────────────


FREE_PLAN_ID = "free"

FREE_WEEKLY_VALUE = Money.parse("5.00")
"""What the free plan is worth every week, in money.

Money and not a token count, because the token count follows from the price of a token and
saying it twice is how the two drift apart. At the shipped rate this is 833,333 tokens; change
`Pricing.per_million` and the free tier stays worth five dollars a week, which is the promise
that was actually made."""


def default_catalog(pricing: Pricing | None = None) -> Catalog:
    """What ships when a deployment has not said otherwise.

    Real numbers rather than placeholders, because a catalog of zeroes is one that looks
    configured and sells everything for nothing.
    """
    pricing = pricing or Pricing()
    return Catalog(
        pricing=pricing,
        plans=(
            Plan(
                FREE_PLAN_ID,
                "Free",
                Money(),
                pricing.tokens_for(FREE_WEEKLY_VALUE),
                cadence=Cadence.WEEKLY,
                description=(
                    f"US$ {FREE_WEEKLY_VALUE} en tokens por semana, sin pagar nada. "
                    "Se renueva solo y no se acumula."
                ),
            ),
            Plan(
                "starter",
                "Starter",
                Money.parse("19.00"),
                4_000_000,
                description="Para probar la idea: un proyecto mediano por semana.",
            ),
            Plan(
                "builder",
                "Builder",
                Money.parse("49.00"),
                12_000_000,
                description="Uso continuo, varios proyectos en paralelo.",
            ),
            Plan(
                "studio",
                "Studio",
                Money.parse("149.00"),
                40_000_000,
                description="Equipos que generan todos los dias.",
            ),
        ),
        packs=(
            Pack("tokens-1m", "1M tokens", Money.parse("8.00"), 1_000_000),
            Pack("tokens-5m", "5M tokens", Money.parse("35.00"), 5_000_000),
            Pack("tokens-20m", "20M tokens", Money.parse("120.00"), 20_000_000),
        ),
    )


@dataclass(frozen=True, slots=True)
class UsageSummary:
    """What an account has actually spent, and on how many runs.

    Derived from the ledger like everything else here — it is the USAGE rows added up, not a
    counter kept somewhere. `since` is what the window means, so a screen can say "this week"
    rather than leaving a number to be read as a lifetime total.
    """

    tokens: int = 0
    runs: int = 0
    cost: Money = field(default_factory=Money)
    """What those runs cost US, at the provider. Zero on a deployment that never saw one."""
    since: datetime | None = None
    """The start of the window. ``None`` means "everything in the ledger"."""

    def as_json(self) -> dict[str, Any]:
        return {
            "tokens": self.tokens,
            "runs": self.runs,
            "cost": self.cost.as_json(),
            "since": self.since.isoformat() if self.since else None,
        }


def usage_of(entries: Iterable[LedgerEntry], since: datetime | None = None) -> UsageSummary:
    """Add up the debits. THE definition of "what have I used".

    Only USAGE rows: an expiry also takes tokens away, and counting it as consumption would
    tell somebody they had spent a grant they never touched.
    """
    tokens = 0
    runs = 0
    cost = Money()
    for entry in entries:
        if entry.kind is not EntryKind.USAGE:
            continue
        if since is not None and entry.at < since:
            continue
        tokens += -entry.tokens
        runs += 1
        cost = cost + entry.amount
    return UsageSummary(tokens=tokens, runs=runs, cost=cost, since=since)


def now_utc() -> datetime:
    """UTC, with the timezone attached. A naive datetime in a ledger is a bug waiting for a
    timezone change."""
    return datetime.now(UTC)


def entry_id() -> str:
    return secrets.token_hex(12)


__all__ = [
    "FREE_PLAN_ID",
    "FREE_WEEKLY_VALUE",
    "MEMO_BYTES",
    "MONTH",
    "WEEK",
    "Balance",
    "BillingError",
    "Cadence",
    "Catalog",
    "EntryKind",
    "InsufficientFundsError",
    "Invoice",
    "InvoiceNotFoundError",
    "InvoiceStatus",
    "LedgerEntry",
    "Money",
    "ObservedPayment",
    "Pack",
    "Plan",
    "Pricing",
    "Product",
    "Subscription",
    "SubscriptionStatus",
    "UnknownProductError",
    "Usage",
    "UsageSummary",
    "balance_of",
    "default_catalog",
    "entry_id",
    "new_invoice_id",
    "now_utc",
    "usage_of",
]
