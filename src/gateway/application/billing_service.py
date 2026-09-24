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
from dataclasses import dataclass, field
from datetime import datetime
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
    Usage,
    UsageSummary,
    balance_of,
    entry_id,
    new_invoice_id,
    now_utc,
    usage_of,
)
from gateway.domain.ports import (
    BillingStore,
    NoSubscriptions,
    PaymentNetwork,
    SubscriptionRegistry,
)

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
    usage: UsageSummary = field(default_factory=UsageSummary)
    """What has been spent in the CURRENT period, not since the beginning of time.

    A lifetime total answers a question nobody asks. What a person wants to know is how much
    of this week's grant is gone, which is only meaningful against the window it belongs to —
    so the summary carries its own `since`."""
    lifetime: UsageSummary = field(default_factory=UsageSummary)

    def as_json(self) -> dict[str, Any]:
        return {
            "account": self.account,
            "balance": self.balance.as_json(),
            "subscription": self.subscription.as_json() if self.subscription else None,
            "plan": self.plan.as_json() if self.plan else None,
            "usage": self.usage.as_json(),
            "lifetime": self.lifetime.as_json(),
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
        chain: SubscriptionRegistry | None = None,
    ) -> None:
        self._store = store
        self._network = network
        self._catalog = catalog
        self._asset = asset
        self._reserve = reserve
        self._chain: SubscriptionRegistry = chain or NoSubscriptions()
        """Where a PAID subscription really lives. `NoContract` answers "nobody", so a
        deployment without one behaves as if nobody ever subscribed — which is the truth."""

    @property
    def catalog(self) -> Catalog:
        return self._catalog

    @property
    def asset(self) -> str:
        return self._asset

    @property
    def contract_id(self) -> str:
        """The subscriptions contract, so a browser can send its `subscribe` there. ``""``
        when this deployment has none and paid plans are therefore not on offer."""
        return self._chain.contract_id if self._chain.available else ""

    @property
    def destination(self) -> str:
        """Where payments go. Public data — this process receives and never sends, so it
        holds no key at all. ``""`` when nothing can be bought here yet."""
        return self._network.destination

    @property
    def network(self) -> str:
        """The x402 network identifier — `stellar-testnet` or `stellar`. The browser needs it
        to tell its wallet which Stellar to sign for."""
        return self._network.network

    @property
    def reserve(self) -> int:
        return self._reserve

    # ── reading ──────────────────────────────────────────────────────────────

    async def balance(self, account: str) -> Balance:
        """The sum of everything that happened to this account. Never a stored number."""
        return balance_of(account, await self._store.all_entries(account))

    async def view(self, account: str, entries: int = 50) -> AccountView:
        subscription = await self.settle_period(account)
        plan = None
        if subscription is not None:
            try:
                plan = self._catalog.plan(subscription.plan_id)
            except BillingError:  # a plan withdrawn from the catalog: the row still stands
                plan = None
        history = await self._store.all_entries(account)
        return AccountView(
            usage=usage_of(history, subscription.started_at if subscription else None),
            lifetime=usage_of(history),
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
        if isinstance(product, Plan) and not product.is_free and self._chain.available:
            # Paid plans are bought by calling the contract, which takes the payment and
            # records the period together. Selling one by invoice as well would be a second
            # way in whose two halves can come apart — the exact failure the contract exists
            # to remove.
            raise BillingError(
                f"{product.name} is subscribed to on chain, not paid by invoice: call "
                f"subscribe() on {self._chain.contract_id}"
            )
        if isinstance(product, Plan) and product.is_free:
            # There is nothing to pay. It is granted on sight and renews itself; selling it
            # would be an invoice for zero that can never be settled.
            raise BillingError(f"{product.name} costs nothing: it is already on this account")
        if not self._network.destination:
            raise BillingError(
                "this gateway has no payment destination configured "
                "(GATEWAY_BILLING__DESTINATION), so nothing can be bought here yet"
            )
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
                current.renewed(product, now)
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

    # ── subscribing, which happens on chain ──────────────────────────────────

    async def subscribe_transaction(self, account: str, plan_id: str) -> dict[str, Any]:
        """The unsigned transaction that subscribes ``account`` to ``plan_id``.

        The gateway says what the transaction IS; only the subscriber's key can say who
        agrees to it, because the payment comes out of their account. Nothing here is signed
        and nothing is charged until they send it back.
        """
        plan = self._catalog.plan(plan_id)
        if plan.is_free:
            raise BillingError(f"{plan.name} costs nothing: it is already on this account")
        if not self._chain.available:
            raise BillingError(
                "this gateway has no subscriptions contract configured "
                "(GATEWAY_BILLING__CONTRACT_ID), so plans cannot be subscribed to yet"
            )
        builder = getattr(self._chain, "build_subscribe", None)
        if builder is None:  # pragma: no cover - only a double lacks it
            raise BillingError("this subscriptions registry cannot build transactions")
        return {
            "plan": plan.as_json(),
            "contract": self._chain.contract_id,
            "network": self.network,
            "xdr": builder(account, plan.id),
        }

    async def submit_subscription(self, account: str, signed_xdr: str) -> dict[str, Any]:
        """Send the signed transaction, then read back what the chain now says.

        Submitting here rather than from the browser: a wallet that signs and then fails to
        send leaves somebody having authorised a payment that never happened, with no way to
        tell. The answer comes back from the same call, and the account is settled against it
        before it returns — so the tokens are there by the time the screen redraws.
        """
        submitter = getattr(self._chain, "submit", None)
        if not self._chain.available or submitter is None:
            raise BillingError("this gateway has no subscriptions contract configured")
        tx_hash = submitter(signed_xdr)
        subscription = await self.settle_period(account)
        return {
            "transaction": tx_hash,
            "subscription": subscription.as_json() if subscription else None,
        }

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
        await self.settle_period(account)
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

    @property
    def free_plan(self) -> Plan | None:
        """The plan every account starts on, or ``None`` if this catalog has none."""
        return next((plan for plan in self._catalog.plans if plan.is_free), None)

    async def settle_period(self, account: str) -> Subscription | None:
        """Bring the account's period up to date, and start the free one if it has none.

        Three things happen here and they are one decision, which is why they are one method:

        - **No subscription at all** → the free plan begins. Every account has one from the
          moment it is first seen; there is nothing to buy and nothing to accept.
        - **A free period that is over** → it renews itself, and the next week is granted.
          Paid plans cannot do this — nothing here holds a card and a Stellar payment cannot
          be pulled — but a grant of nothing can be given again.
        - **A paid period that is over** → it ends, and what it granted is taken back. The
          next period starts when the next invoice is paid.

        Granted tokens never roll over, and that is enforced by writing an EXPIRY row for
        exactly what is left rather than by resetting a counter: the balance stays a sum of
        rows, and a person can see the day their grant ended and how much they had not used.

        Called on every read of the account and before every run, so it has to be idempotent.
        It is: both the grant and the expiry are keyed on the period they belong to.
        """
        now = now_utc()
        # The chain first, and it wins. A paid subscription is recorded there in the same
        # transaction that paid for it, so it is the thing that cannot be half-true; our own
        # row is a mirror of it and is rewritten from it rather than argued with.
        if mirrored := await self._mirror_chain(account, now):
            return mirrored

        subscription = await self._store.subscription(account)
        if subscription is None:
            return await self._begin_free(account, now)
        if not subscription.due(now):
            return subscription

        try:
            plan = self._catalog.plan(subscription.plan_id)
        except BillingError:  # withdrawn from the catalog: end it, there is nothing to renew
            plan = None

        await self._expire_grant(account, subscription, now)
        if plan is not None and plan.is_free:
            renewed = await self._store.put_subscription(subscription.renewed(plan, now))
            await self._grant(account, plan, renewed)
            logger.info("account %s: the free week renewed", _short(account))
            return renewed

        logger.info("account %s: the %s period ended", _short(account), subscription.plan_id)
        return await self._store.put_subscription(subscription.ended())

    async def _mirror_chain(self, account: str, now: datetime) -> Subscription | None:
        """Copy an ACTIVE on-chain subscription into our own row, and grant its tokens.

        ``None`` when the chain has nothing live to say, which leaves the free plan below to
        do its job. The grant is keyed on the on-chain period, so calling this on every read
        of the account — which is what happens — credits exactly once per period paid for.

        A contract that cannot be reached is logged and treated as silence rather than as
        "not subscribed": the second would cancel a paying customer because an RPC blinked.
        """
        if not self._chain.available:
            return None
        try:
            onchain = self._chain.subscription(account)
        except Exception as error:
            logger.warning("the subscriptions contract could not be read: %s", error)
            return None
        if onchain is None or not onchain.active(now):
            return None
        try:
            plan = self._catalog.plan(onchain.plan)
        except BillingError:
            logger.warning(
                "the chain reports plan %r, which this catalog does not sell", onchain.plan
            )
            return None

        mirrored = Subscription(account, plan.id, onchain.started, onchain.expires)
        await self._store.put_subscription(mirrored)
        await self.credit(
            account,
            plan.tokens,
            kind=EntryKind.GRANT,
            reference=f"chain:{onchain.period_key}",
            memo=f"{plan.name} · on-chain",
        )
        return mirrored

    async def _begin_free(self, account: str, now: datetime) -> Subscription | None:
        """Put a brand-new account on the free plan and grant its first week.

        NOTE, because it is a real exposure and not a detail: an account here is a Stellar
        address, and addresses are free to generate. Nothing in this stops somebody minting a
        thousand keypairs and collecting a thousand free weeks. What would stop it is
        requiring the address to EXIST on the ledger — which on Stellar costs a base reserve,
        so it is a real cost rather than a captcha — and that check is deliberately not here
        yet because it puts a Horizon call on the sign-in path. It belongs in front of this
        method when the free tier is worth farming.
        """
        plan = self.free_plan
        if plan is None:
            return None
        subscription = await self._store.put_subscription(Subscription.begin(account, plan, now))
        await self._grant(account, plan, subscription)
        logger.info("account %s starts on %s", _short(account), plan.id)
        return subscription

    async def _grant(self, account: str, plan: Plan, subscription: Subscription) -> None:
        """This period's tokens. Keyed on the period, so it lands exactly once."""
        await self.credit(
            account,
            plan.tokens,
            kind=EntryKind.GRANT,
            reference=f"grant:{subscription.period_key}",
            memo=f"{plan.name} · {plan.cadence.value}",
        )

    async def _expire_grant(self, account: str, subscription: Subscription, now: datetime) -> None:
        balance = await self.balance(account)
        if balance.granted <= 0:
            return
        await self._store.append(
            LedgerEntry(
                id=entry_id(),
                account=account,
                kind=EntryKind.EXPIRY,
                tokens=-balance.granted,
                at=now,
                reference=f"expiry:{subscription.period_key}",
                memo="the period ended and its tokens did not roll over",
            )
        )

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
