"""Who pays for a run, decided once, before anything is generated.

THREE ANSWERS, IN THIS ORDER
    1. **Nobody has to.** Billing is off. The gateway behaves exactly as it did before any of
       this existed, which is the case every existing test is written against and the case a
       local stack runs in.
    2. **A person.** There is a bearer token this gateway issued, and the account comes out of
       it. Their balance is checked before the first model call.
    3. **A program.** There is an ``X-PAYMENT`` header. It is settled, the tokens are credited
       to whoever signed it, and that address is the account for this run.

    Anything else is ``402 Payment Required`` with the requirements attached — which is an
    answer, not an error. A client that has not paid is being told the price.

WHY THE BODY IS NOT THE USUAL ERROR SHAPE
    x402 says a 402 body is ``{x402Version, error, accepts: [...]}``, and a client that speaks
    the protocol reads exactly that. Wrapping it in this gateway's own ``{"error": {...}}``
    envelope would make it unreadable to every off-the-shelf x402 client, so this one response
    is rendered as the protocol specifies. It is also why the exception carries the whole
    document rather than a message.

WHY THE CHECK IS HERE AND NOT IN THE ORCHESTRATOR
    The orchestrator takes an account and trusts it. Deciding WHICH account — from a token, a
    payment, or neither — is about the request, and the request is the API layer's business.
    Keeping it here means the orchestrator has one rule ("this account pays") rather than
    three, and the three cannot drift apart in the middle of a state machine.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import Depends, Header, Request

from gateway.application.identity_service import IdentityError
from gateway.bootstrap import Container
from gateway.domain.billing import InsufficientFundsError
from gateway.domain.exceptions import GatewayError
from gateway.domain.x402 import PAYMENT_HEADER, PaymentRequired, X402Error

logger = logging.getLogger(__name__)


class PaymentRequiredError(GatewayError):
    """A 402, carrying the document the protocol says goes in the body."""

    def __init__(self, document: PaymentRequired, settlement: str = "") -> None:
        super().__init__(document.error)
        self.document = document
        self.settlement = settlement


async def payer_of(
    request: Request,
    authorization: Annotated[str, Header()] = "",
    x_payment: Annotated[str, Header(alias=PAYMENT_HEADER)] = "",
) -> str:
    """The account that pays for what this request starts. ``""`` when nobody has to.

    Raises :class:`PaymentRequiredError` when the caller must pay first, and the error holds
    the price so the answer is useful rather than merely negative.
    """
    container: Container = request.app.state.container
    billing = container.billing
    if billing is None:
        return ""

    account = _session_account(container, authorization)
    if not account and x_payment and container.x402 is not None:
        account = await _paid_account(container, request, x_payment)
    if not account:
        await _refuse(
            container, "", "this gateway charges for runs: sign in or pay per call", request
        )

    try:
        await billing.authorize(account)
    except InsufficientFundsError as error:
        await _refuse(container, account, str(error), request)
    return account


PayerDep = Annotated[str, Depends(payer_of)]


def _session_account(container: Container, authorization: str) -> str:
    """The signed-in account, or ``""``. A malformed token is not an error here.

    Falling through to the 402 rather than rejecting is deliberate: a client whose session
    expired mid-flow should be told the price and offered the payment path, not handed a 401
    it may have no way to act on.
    """
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token or container.identity is None:
        return ""
    try:
        return container.identity.account_of(token.strip())
    except IdentityError as error:
        logger.info("a bearer token was rejected: %s", error)
        return ""


async def _paid_account(container: Container, request: Request, header: str) -> str:
    """Settle an ``X-PAYMENT`` header and return the address that signed it.

    The payer is read out of the SETTLED transaction, never out of the header's own claims:
    whoever's key moved the money is whoever bought the tokens.
    """
    if container.x402 is None:  # pragma: no cover - guarded by the caller
        return ""
    try:
        account, settlement = await container.x402.redeem(header, str(request.url.path))
    except X402Error as error:
        await _refuse(container, "", str(error), request)
        return ""  # pragma: no cover - `_refuse` always raises
    request.state.x402_settlement = settlement.encode()
    return account


async def _refuse(container: Container, account: str, reason: str, request: Request) -> None:
    """Always raises. Builds the 402 with a live quote, or without one if that fails."""
    if container.x402 is None:
        raise PaymentRequiredError(PaymentRequired(reason, ()))
    try:
        document = await container.x402.challenge(account, reason, str(request.url.path))
    except Exception as error:
        # A quote needs the network, and the network can be down. The refusal still stands;
        # it simply cannot say the price, and saying so beats a 500 on a path whose whole
        # job is to explain what is owed.
        logger.warning("a 402 could not be priced: %s", error)
        raise PaymentRequiredError(PaymentRequired(reason, ())) from error
    raise PaymentRequiredError(document)


__all__ = ["PayerDep", "PaymentRequiredError", "payer_of"]
