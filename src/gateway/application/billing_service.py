"""Use case: selling tokens, granting them, and taking them away when they are spent.

THE ORDER OF OPERATIONS, WHICH IS THE WHOLE DESIGN
    A run is AUTHORISED before it starts and CHARGED after it finishes, and those are
    different questions asked at different times. Authorising asks "can this account afford
    to begin" and may refuse. Charging asks "what did it actually cost" and may never refuse:
    the work is done, the provider has been paid, and an account that ends up at zero ends up
    at zero. Refusing after the fact would mean either giving the work away or billing for
    something that was declined, and both are worse than a balance of nothing.

WHAT THIS SERVICE REFUSES TO DECIDE
    Where the money came from. A payment settling an invoice, a signed transaction arriving
    in an ``X-PAYMENT`` header and an operator crediting an account by hand all end in the
    same call — :meth:`credit` — with a different ``reference``. There is exactly one path
    from "money happened" to "tokens exist", which is what makes the ledger explainable.

IDEMPOTENCE IS NOT OPTIONAL HERE
    Every write carries a ``reference``: an invoice id, a run id, a transaction hash. The
    store refuses a second entry with the same one. Confirming a payment is triggered by a
    poller, by the payer refreshing, and by whoever calls the endpoint directly — often at
    once — and the tokens must appear exactly once.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from gateway.domain.billing import (
    INVOICE_TTL,
    Balance,
    BillingError,
    Catalog,
    EntryKind,
    InsufficientFundsError,
    Invoice,
    InvoiceNotFoundError,
    InvoiceStatus,
    LedgerEntry,
    Money,
    Pack,
    Plan,
    Subscription,
    SubscriptionStatus,
    Usage,
    balance_of,
    entry_id,
    new_invoice_id,
    now_utc,
)
from gateway.domain.ports import BillingStore, PaymentNetwork

logger = logging.getLogger(__name__)

DEFAULT_RESERVE = 50_000
"""Tokens an account must hold before a run may START.

Not a deposit and never debited — only a floor. A run whose account is at 400 tokens will
overspend before its first model call returns, and the honest moment to say so is before it
begins rather than in the middle of a generation somebody is watching.
"""


@dataclass(frozen=True, slots=True)
class AccountView:
    """Everything a billing screen needs, in one read."""

    account: str
    balance: Balance
    subscription: Subscription | None
    plan: Plan | None
    entries: Sequence[LedgerEntry]

    def as_json(self) -> dict[str, Any]:
        return {
            "account": self.account,
            "balance": self.balance.as_json(),
            "subscription": self.subscription.as_json() if self.subscription else None,
            "plan": self.plan.as_json() if self.plan else None,
            "entries": [entry.as_json() for entry in self.entries],
        }


class BillingService:
    """One instance, shared. All state is in the store."""

    def __init__(
        self,
        store: BillingStore,
        network: PaymentNetwork,
        catalog: Catalog,
        asset: str = "XLM",
        reserve: int = DEFAULT_RESERVE,
    ) -> None:
        self._store = store
        self._network = network
        self._catalog = catalog
        self._asset = asset
        self._reserve = reserve

    @property
    def catalog(self) -> Catalog:
        return self._catalog

    @property
    def asset(self) -> str:
        return self._asset

    @property
    def reserve(self) -> int:
        return self._reserve

    # ── reading ──────────────────────────────────────────────────────────────

    async def balance(self, account: str) -> Balance:
        """The sum of everything that happened to this account. Never a stored number."""
        return balance_of(account, await self._store.all_entries(account))

    async def view(self, account: str, entries: int = 50) -> AccountView:
        await self.expire_if_due(account)
        subscription = await self._store.subscription(account)
        plan = None
        if subscription is not None:
            try:
                plan = self._catalog.plan(subscription.plan_id)
            except BillingError:  # a plan withdrawn from the catalog: the row still stands
                plan = None
        return AccountView(
            account=account,
            balance=await self.balance(account),
            subscription=subscription,
            plan=plan,
            entries=await self._store.entries(account, entries),
        )

    # ── selling ──────────────────────────────────────────────────────────────

    async def checkout(self, account: str, sku: str, asset: str = "") -> Invoice:
        """Turn a product into an invoice with a FROZEN amount and a memo to pay it with.

        The amount is quoted once, here, and never recomputed. An exchange rate that moves
        while the payer is reading the number would otherwise mean the payment they send is
        rejected for being a fraction short of a price they never saw.
        """
        product = self._catalog.product(sku)
        asset = (asset or self._asset).upper()
        now = now_utc()
        invoice = Invoice(
            id=new_invoice_id(),
            account=account,
            sku=product.id,
            tokens=product.tokens,
            price=product.price,
            asset=asset,
            amount=await self._network.quote(product.price, asset),
            destination=self._network.destination,
            created_at=now,
            expires_at=now + INVOICE_TTL,
            kind="plan" if isinstance(product, Plan) else "pack",
        )
        logger.info(
            "invoice %s: %s for %s, %s %s",
            invoice.id,
            product.id,
            _short(account),
            invoice.amount,
            invoice.asset,
        )
        return await self._store.put_invoice(invoice)

    async def confirm(self, invoice_id: str) -> Invoice:
        """Ask the network whether this invoice was paid, and credit it if so.

        Safe to call repeatedly and from several places at once: a paid invoice returns
        itself, and the credit behind it is keyed on the invoice id.
        """
        invoice = await self._store.invoice(invoice_id)
        if invoice is None:
            raise InvoiceNotFoundError(invoice_id)
        if invoice.status is InvoiceStatus.PAID:
            return invoice

        payment = await self._network.payment_for(invoice)
        if payment is None:
            if not invoice.is_open(now_utc()):
                # Expired, and it is written down as such rather than left to look pending
                # forever. A payment that lands afterwards is still matched by `reconcile`.
                return await self._store.put_invoice(invoice.expired())
            return invoice
        if not payment.settles(invoice):
            logger.warning(
                "invoice %s: a payment carried the memo but does not settle it", invoice.id
            )
            return invoice

        paid = await self._store.put_invoice(invoice.paid(payment.tx_hash, payment.payer))
        await self._deliver(paid)
        return paid

    async def reconcile(self, limit: int = 100) -> list[Invoice]:
        """Walk the open invoices and confirm whatever the ledger now shows paid.

        The background half of ``confirm``. A payer who closes the tab the moment their wallet
        submits must still end up with their tokens, and nothing in a browser can be relied on
        to come back and say so.
        """
        settled: list[Invoice] = []
        for invoice in await self._store.open_invoices(limit):
            try:
                confirmed = await self.confirm(invoice.id)
            except Exception as error:  # one bad invoice must not stop the sweep
                logger.warning("invoice %s could not be reconciled: %s", invoice.id, error)
                continue
            if confirmed.status is InvoiceStatus.PAID:
                settled.append(confirmed)
        return settled

    async def _deliver(self, invoice: Invoice) -> None:
        """What paying actually buys: the tokens, and a subscription if it was a plan."""
        product = self._catalog.product(invoice.sku)
        await self.credit(
            invoice.account,
            invoice.tokens,
            # A plan's tokens are a GRANT and expire with its period; a pack's are a PURCHASE
            # and never do. Same credit, different shelf life, and the product decides which.
            kind=EntryKind.GRANT if isinstance(product, Plan) else EntryKind.PURCHASE,
            reference=f"invoice:{invoice.id}",
            amount=invoice.price,
            memo=f"{product.name} · {invoice.amount} {invoice.asset}",
        )
        if isinstance(product, Plan):
            now = now_utc()
            current = await self._store.subscription(invoice.account)
            subscription = (
                current.renewed(now)
                if current is not None and current.plan_id == product.id
                else Subscription.begin(invoice.account, product, now)
            )
            await self._store.put_subscription(subscription)
            logger.info(
                "account %s is on %s until %s",
                _short(invoice.account),
                product.id,
                subscription.renews_at.date(),
            )

    # ── crediting and debiting ───────────────────────────────────────────────

    async def credit(
        self,
        account: str,
        tokens: int,
        *,
        kind: EntryKind,
        reference: str,
        amount: Money | None = None,
        memo: str = "",
    ) -> LedgerEntry:
        """The ONE way tokens come into existence. Idempotent on ``reference``."""
        if tokens < 0:
            raise BillingError("a credit cannot be negative; use a debit")
        return await self._store.append(
            LedgerEntry(
                id=entry_id(),
                account=account,
                kind=kind,
                tokens=tokens,
                at=now_utc(),
                amount=amount or Money(),
                reference=reference,
                memo=memo,
            )
        )

    async def authorize(self, account: str, needed: int = 0) -> Balance:
        """May a run START? Raises :class:`InsufficientFundsError` when it may not.

        The only place billing can say no, and it says it before anything is generated.
        """
        await self.expire_if_due(account)
        balance = await self.balance(account)
        required = max(needed, self._reserve)
        if not balance.can_afford(required):
            raise InsufficientFundsError(required, balance.total)
        return balance

    async def charge(self, account: str, run_id: str, usage: Usage) -> LedgerEntry | None:
        """Debit what a finished run really cost. Never refuses; may take the balance to zero.

        ``None`` when there is nothing to charge — a simulated run, a run that made no call.
        Charging zero would put a row in the ledger that says nothing happened, which is
        exactly what an empty ledger already says.
        """
        tokens = self._catalog.pricing.billable(usage)
        if tokens <= 0:
            return None
        entry = await self._store.append(
            LedgerEntry(
                id=entry_id(),
                account=account,
                kind=EntryKind.USAGE,
                tokens=-tokens,
                at=now_utc(),
                amount=usage.cost,
                reference=f"run:{run_id}",
                memo=f"{usage.tokens:,} tokens · {usage.calls} calls",
            )
        )
        logger.info("run %s charged %s tokens to %s", run_id, tokens, _short(account))
        return entry

    # ── periods ──────────────────────────────────────────────────────────────

    async def expire_if_due(self, account: str) -> Subscription | None:
        """End a period whose time is up, and take back what it granted.

        Granted tokens do not roll over, and this is where that is enforced — by writing an
        EXPIRY entry for exactly what is left, not by resetting a counter. The balance stays
        a sum of rows, and a person can see the day their grant ended and how much of it they
        had not used.

        There is no auto-renewal: nothing here holds a card, and a Stellar payment cannot be
        pulled. The next period starts when the next invoice is paid.
        """
        subscription = await self._store.subscription(account)
        if subscription is None or not subscription.due(now_utc()):
            return subscription
        balance = await self.balance(account)
        if balance.granted > 0:
            await self._store.append(
                LedgerEntry(
                    id=entry_id(),
                    account=account,
                    kind=EntryKind.EXPIRY,
                    tokens=-balance.granted,
                    at=now_utc(),
                    reference=f"expiry:{subscription.plan_id}:{subscription.renews_at.isoformat()}",
                    memo="the subscription period ended",
                )
            )
        ended = Subscription(
            subscription.account,
            subscription.plan_id,
            subscription.started_at,
            subscription.renews_at,
            SubscriptionStatus.EXPIRED,
        )
        logger.info("account %s: the %s period ended", _short(account), subscription.plan_id)
        return await self._store.put_subscription(ended)

    # ── helpers the API layer needs ──────────────────────────────────────────

    def product_named(self, sku: str) -> Plan | Pack:
        return self._catalog.product(sku)

    def tokens_worth(self, amount: Money) -> int:
        return self._catalog.pricing.tokens_for(amount)


def _short(account: str) -> str:
    """An address in a log line, shortened. Public data, but 56 characters of it per line
    makes a log unreadable and encourages people to stop reading them."""
    return f"{account[:6]}…{account[-4:]}" if len(account) > 12 else account


__all__ = ["DEFAULT_RESERVE", "AccountView", "BillingService"]
