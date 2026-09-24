"""The billing API: what is on sale, who you are, and what you owe.

ON THE PREFIX. Like ``/runs``, these live outside ``/api`` and are registered before the
proxy — a catch-all at ``/api/{service}/{path}`` would swallow ``/api/billing`` and try to
forward it to a service called ``billing``.

ON AUTHENTICATION. Three routes are public because they have to be: the catalog is a price
list, and the two sign-in routes are how anyone becomes authenticated in the first place.
Everything else takes a bearer token that the gateway itself issued, and the account is read
OUT of that token. No route anywhere takes an account as a parameter, which is what makes it
impossible to ask about — or spend — somebody else's.

ON POLLING. ``GET /billing/invoices/{id}`` asks the network every time rather than reading a
cached status. An invoice is paid by a wallet the gateway never hears from, so there is no
event to wait for: the only way to know is to look, and the person staring at the screen is
the one who wants it looked at now.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request, status
from pydantic import BaseModel, Field

from gateway.application.billing_service import BillingService
from gateway.application.identity_service import IdentityError, IdentityService
from gateway.bootstrap import Container
from gateway.domain.billing import InvoiceNotFoundError

router = APIRouter(prefix="/billing", tags=["billing"])

MAX_SIGNATURE = 512
MAX_CHALLENGE = 4096


def get_billing(request: Request) -> BillingService:
    container: Container = request.app.state.container
    if container.billing is None:  # pragma: no cover - guarded by route registration
        raise RuntimeError("billing is not enabled on this gateway")
    return container.billing


def get_identity(request: Request) -> IdentityService:
    container: Container = request.app.state.container
    if container.identity is None:  # pragma: no cover - guarded by route registration
        raise RuntimeError("billing is not enabled on this gateway")
    return container.identity


BillingDep = Annotated[BillingService, Depends(get_billing)]
IdentityDep = Annotated[IdentityService, Depends(get_identity)]


def account_of(identity: IdentityDep, authorization: Annotated[str, Header()] = "") -> str:
    """The signed-in account, from the bearer token and from nowhere else.

    A dependency rather than a line in each route, so that adding a route cannot accidentally
    add one that trusts a parameter.

    `IdentityError` travels on rather than being turned into an `HTTPException`: it is a
    `GatewayError`, so the one error handler answers it in the same envelope as everything
    else, with the `WWW-Authenticate` header a 401 needs to be actionable.
    """
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise IdentityError("sign in first")
    return identity.account_of(token.strip())


AccountDep = Annotated[str, Depends(account_of)]


# ── the price list ───────────────────────────────────────────────────────────


@router.get("/plans", summary="Everything on sale, and what a token costs")
async def plans(billing: BillingDep) -> dict[str, Any]:
    """Public. A price list that needs a login is a price list nobody reads.

    It also names the NETWORK. A browser wallet has to be told which Stellar it is signing
    for, and the only correct answer is the one this gateway is configured for — a front end
    guessing it produces signatures that verify nowhere.
    """
    return {
        **billing.catalog.as_json(),
        "asset": billing.asset,
        "reserve": billing.reserve,
        "network": billing.network,
        # Public on purpose: it is the address payments are made TO, it appears on every
        # invoice, and it is on a public ledger either way. A screen that can only show it
        # after a checkout is a screen that cannot answer "where do I send tokens to".
        # Empty means this deployment is not selling yet, and the page says exactly that.
        "destination": billing.destination,
        # Where a paid plan is actually bought. Empty means this deployment has no contract,
        # and the screen then offers only the free plan and the token packs.
        "contract": billing.contract_id,
    }


# ── signing in with a wallet ─────────────────────────────────────────────────


class ChallengeBody(BaseModel):
    address: str = Field(min_length=56, max_length=56)


class VerifyBody(BaseModel):
    challenge: str = Field(min_length=1, max_length=MAX_CHALLENGE)
    signature: str = Field(min_length=1, max_length=MAX_SIGNATURE)


@router.post("/auth/challenge", summary="Get the text your wallet has to sign")
async def challenge(body: ChallengeBody, identity: IdentityDep) -> dict[str, Any]:
    """`IdentityError` becomes a 401 and `UnverifiableError` a 501, both in the house
    envelope, decided once in `api/errors.py` rather than here."""
    issued, token = identity.challenge(body.address)
    return issued.as_json(token)


@router.post("/auth/verify", summary="Exchange a signed challenge for a session")
async def verify(body: VerifyBody, identity: IdentityDep) -> dict[str, Any]:
    """The address comes out of the CHALLENGE, never out of this request.

    A caller who could name the account they are verifying against would simply name someone
    else's and sign their own.
    """
    session, token = identity.verify(body.challenge, body.signature)
    return session.as_json(token)


# ── the account ──────────────────────────────────────────────────────────────


@router.get("", summary="Balance, subscription and recent movements")
async def account(billing: BillingDep, account: AccountDep) -> dict[str, Any]:
    view = await billing.view(account)
    return view.as_json()


# ── buying ───────────────────────────────────────────────────────────────────


class CheckoutBody(BaseModel):
    sku: str = Field(min_length=1, max_length=64)
    asset: str = Field(default="", max_length=12)


@router.post(
    "/checkout",
    summary="Create an invoice for a plan or a token pack",
    status_code=status.HTTP_201_CREATED,
)
async def checkout(body: CheckoutBody, billing: BillingDep, account: AccountDep) -> dict[str, Any]:
    """The amount is frozen here and never recomputed.

    An invoice whose price moves while the payer is reading it is not an invoice: the payment
    they send would be a fraction short of a number they never saw.

    An unknown SKU raises `UnknownProductError`, which is a `BillingError` and answers 400
    with the sku named — decided in `api/errors.py`, not caught and re-raised here.
    """
    invoice = await billing.checkout(account, body.sku, body.asset)
    return invoice.as_json()


class SubscribeBody(BaseModel):
    sku: str = Field(min_length=1, max_length=64)


class SubmitBody(BaseModel):
    # A prepared Soroban envelope is around 1.5 KB; four is room for a bigger footprint and a
    # bound on what an authenticated caller can make this process decode.
    xdr: str = Field(min_length=1, max_length=4096)


@router.post("/subscribe", summary="The unsigned transaction that subscribes you to a plan")
async def subscribe(
    body: SubscribeBody, billing: BillingDep, account: AccountDep
) -> dict[str, Any]:
    """Nothing is signed here and nothing is charged.

    The payment comes out of the subscriber's own account, so only their key can authorise
    it. What this returns is what the transaction would do; the wallet decides whether it
    happens.
    """
    return await billing.subscribe_transaction(account, body.sku)


@router.post("/subscribe/submit", summary="Send the signed transaction")
async def submit(body: SubmitBody, billing: BillingDep, account: AccountDep) -> dict[str, Any]:
    """The account is settled against the chain before this returns, so the tokens are there
    by the time the screen redraws."""
    return await billing.submit_subscription(account, body.xdr)


@router.get("/invoices/{invoice_id}", summary="Has it been paid?")
async def invoice(invoice_id: str, billing: BillingDep, account: AccountDep) -> dict[str, Any]:
    """Asks the network, then answers. Safe to poll, and safe to call twice at once.

    The account is checked against the invoice's own: an invoice id is short enough to be
    worth guessing at, and it names an amount and an address.
    """
    confirmed = await billing.confirm(invoice_id)
    if confirmed.account != account:
        # The same answer a missing invoice gets. Telling them apart would confirm that an id
        # exists, which is the only thing a guesser learns anything from.
        raise InvoiceNotFoundError(invoice_id)
    return confirmed.as_json()


__all__ = ["account_of", "router"]
