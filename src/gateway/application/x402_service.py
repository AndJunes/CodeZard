"""Use case: being payable over HTTP 402, and being a facilitator for it.

TWO ROLES, ONE FILE, AND THEY ARE NOT THE SAME
    As a RESOURCE SERVER this gateway answers 402 with what it would accept, and takes an
    ``X-PAYMENT`` header as authorisation to do the work. As a FACILITATOR it answers
    ``/x402/verify`` and ``/x402/settle`` for anyone who wants the checking done for them.
    The second is optional and exists because the first needs the machinery anyway; keeping
    them in one service is what stops the two from disagreeing about what "valid" means.

THE ORDER OF OPERATIONS, WHICH IS THE WHOLE SECURITY ARGUMENT
    Verify, then work, then settle. Verifying is free and proves the envelope pays; the work
    is what was bought; settling submits the transaction. Settling FIRST would charge for
    work that may fail. Settling never would be giving it away. The window between "verified"
    and "settled" is the risk, and it is bounded by the envelope's own time bounds — which is
    why an envelope without them is refused.

WHAT IS NEVER TRUSTED
    The ``payload`` block of an ``X-PAYMENT`` header is what a caller SAID. Every number that
    matters — who pays, how much, to whom, on which network — is read back out of the signed
    envelope by :class:`PaymentNetwork`. A payload that claims one amount and carries another
    is not a mismatch to reconcile; it is the attack, and the envelope wins.

WHY THE TOKENS GO THROUGH THE SAME LEDGER
    A 402 payment credits the payer's address exactly as an invoice would, with the
    transaction hash as the reference. An agent that pays per request and a person who buys a
    pack end up in the same ledger at the same price, and "what did this account pay and get"
    has one answer rather than two.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from gateway.application.billing_service import BillingService
from gateway.domain.billing import (
    EntryKind,
    Invoice,
    Money,
    new_invoice_id,
    now_utc,
)
from gateway.domain.ports import BillingStore, PaymentNetwork
from gateway.domain.x402 import (
    SCHEME,
    PaymentPayload,
    PaymentRequired,
    PaymentRequirements,
    SettleResult,
    Supported,
    VerifyResult,
    X402Error,
)

logger = logging.getLogger(__name__)

QUOTE_TTL = timedelta(minutes=10)
"""How long a 402 quote stands. Shorter than an invoice's: a program reads the number and
pays within a second, and a stale exchange rate helps nobody."""

DEFAULT_TIMEOUT_S = 120


class X402Service:
    """Prices a resource in x402 terms, and turns a paid header into tokens."""

    def __init__(
        self,
        billing: BillingService,
        network: PaymentNetwork,
        store: BillingStore,
        price: Money,
        resource: str = "/runs",
    ) -> None:
        self._billing = billing
        self._network = network
        self._store = store
        self._price = price
        self._resource = resource

    @property
    def supported(self) -> Supported:
        return Supported(((SCHEME, self._network.network),))

    # ── the 402 ──────────────────────────────────────────────────────────────

    async def requirements(
        self, account: str, resource: str = "", price: Money | None = None
    ) -> tuple[PaymentRequirements, Invoice]:
        """What this resource costs, and the invoice that will match the payment.

        An invoice is created even though nobody asked for one, because the memo is how a
        payment is tied back to an account — and because a 402 that is paid and then abandoned
        should still credit the payer rather than vanish. It is the same row an ordinary
        checkout produces, so `reconcile` picks it up like any other.
        """
        amount = price or self._price
        asset = self._billing.asset
        now = now_utc()
        invoice = await self._store.put_invoice(
            Invoice(
                id=new_invoice_id(),
                account=account,
                sku="x402",
                tokens=self._billing.tokens_worth(amount),
                price=amount,
                asset=asset,
                amount=await self._network.quote(amount, asset),
                destination=self._network.destination,
                created_at=now,
                expires_at=now + QUOTE_TTL,
                kind="x402",
            )
        )
        requirements = PaymentRequirements(
            scheme=SCHEME,
            network=self._network.network,
            max_amount_required=invoice.amount,
            resource=resource or self._resource,
            description=f"{invoice.tokens:,} CodeZard tokens",
            pay_to=invoice.destination,
            asset=asset,
            max_timeout_seconds=DEFAULT_TIMEOUT_S,
            extra={"memo": invoice.memo, "invoice": invoice.id, "tokens": invoice.tokens},
        )
        return requirements, invoice

    async def challenge(self, account: str, reason: str, resource: str = "") -> PaymentRequired:
        """The body of a 402: why, and what would be accepted instead."""
        requirements, _invoice = await self.requirements(account, resource)
        return PaymentRequired(reason, (requirements,))

    # ── the two facilitator calls ────────────────────────────────────────────

    async def verify(
        self, payload: PaymentPayload, requirements: PaymentRequirements
    ) -> VerifyResult:
        """Does this envelope pay that requirement? Nothing is submitted."""
        if payload.scheme != SCHEME:
            return VerifyResult(False, reason=f"scheme {payload.scheme!r} is not supported")
        if payload.network != self._network.network:
            return VerifyResult(
                False, reason=f"this is {self._network.network}, not {payload.network!r}"
            )
        if not payload.transaction:
            return VerifyResult(False, reason="the payload carries no signed transaction")
        invoice = await self._invoice_of(requirements)
        if invoice is None:
            return VerifyResult(False, reason="that payment requirement is unknown or expired")
        return await self._network.verify(payload.transaction, invoice)

    async def settle(
        self, payload: PaymentPayload, requirements: PaymentRequirements
    ) -> SettleResult:
        """Submit, and credit the payer once the ledger has it.

        Verified again first, rather than trusting that whoever called ``verify`` did. The two
        endpoints are independent and a facilitator that settles an unverified envelope is a
        facilitator that submits whatever it is handed.
        """
        verified = await self.verify(payload, requirements)
        if not verified.valid:
            return SettleResult(False, self._network.network, reason=verified.reason)

        result = await self._network.settle(payload.transaction)
        if not result.success:
            return result
        invoice = await self._invoice_of(requirements)
        if invoice is not None:
            payer = result.payer or verified.payer or invoice.account
            await self._store.put_invoice(invoice.paid(result.transaction, payer))
            # The payer's OWN address is credited, not the account named in the invoice: in a
            # 402 there is no session, so whoever signed is whoever is buying.
            await self._billing.credit(
                payer,
                invoice.tokens,
                kind=EntryKind.PURCHASE,
                reference=f"x402:{result.transaction}",
                amount=invoice.price,
                memo=f"x402 · {invoice.amount} {invoice.asset}",
            )
            logger.info(
                "x402 %s: %s tokens to %s", result.transaction[:10], invoice.tokens, payer[:8]
            )
        return result

    async def redeem(self, header: str, resource: str = "") -> tuple[str, SettleResult]:
        """The resource-server path: a header in, ``(account, settlement)`` out.

        Raises :class:`X402Error` when the header does not pay, which the route turns back
        into another 402 — a client that pays the wrong amount gets told what the right one
        is rather than a bare rejection.
        """
        payload = PaymentPayload.decode(header)
        invoice_id = str(payload.payload.get("invoice") or "")
        invoice = await self._store.invoice(invoice_id) if invoice_id else None
        if invoice is None:
            raise X402Error("that payment does not name an invoice this gateway issued")
        requirements = PaymentRequirements(
            scheme=SCHEME,
            network=self._network.network,
            max_amount_required=invoice.amount,
            resource=resource or self._resource,
            description="",
            pay_to=invoice.destination,
            asset=invoice.asset,
            extra={"memo": invoice.memo, "invoice": invoice.id},
        )
        result = await self.settle(payload, requirements)
        if not result.success:
            raise X402Error(result.reason or "the payment could not be settled")
        return (result.payer or invoice.account), result

    async def _invoice_of(self, requirements: PaymentRequirements) -> Invoice | None:
        """The invoice a requirement refers to, if it is still payable.

        Read from the store rather than reconstructed from the requirement, because the
        requirement arrived over the wire: an amount taken from it would be an amount the
        caller chose.
        """
        invoice_id = str(requirements.extra.get("invoice") or "")
        if not invoice_id:
            return None
        invoice = await self._store.invoice(invoice_id)
        # `is_open` and not merely "still pending". A quote holds an exchange rate, and a
        # quote that never goes stale is one an agent can sit on until the rate moves and
        # then pay at yesterday's price. Ten minutes is the whole of the window.
        if invoice is None or not invoice.is_open(now_utc()):
            return None
        return invoice


__all__ = ["DEFAULT_TIMEOUT_S", "QUOTE_TTL", "X402Service"]
