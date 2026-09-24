"""The x402 endpoints: paying without an account, and checking payments for other people.

TWO THINGS LIVE HERE AND THEY ARE NOT THE SAME

``GET /x402/supported`` and ``POST /x402/quote`` are how a program discovers what this
gateway charges and gets something it can pay. They are always on with billing.

``POST /x402/verify`` and ``POST /x402/settle`` are the FACILITATOR role — doing the checking
and the submitting for somebody else's resource — and they are off unless an operator asks
for them, because settling means this process submits transactions it was handed by
strangers.

WHY NONE OF THESE NEEDS A LOGIN
    That is the entire point of 402. The payment is the authorisation: whoever signed the
    transaction is whoever is buying, and the tokens are credited to the address that signed.
    Asking a program to hold a session first is asking it to do the one thing programs are
    worst at, and it is what the ``/billing`` routes are for when there is a person.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, Field

from gateway.application.x402_service import X402Service
from gateway.bootstrap import Container
from gateway.domain.billing import Money
from gateway.domain.x402 import (
    PaymentPayload,
    PaymentRequirements,
    SettleResult,
    VerifyResult,
    X402Error,
)

router = APIRouter(prefix="/x402", tags=["x402"])


def get_x402(request: Request) -> X402Service:
    container: Container = request.app.state.container
    if container.x402 is None:  # pragma: no cover - guarded by route registration
        raise RuntimeError("x402 is not enabled on this gateway")
    return container.x402


X402Dep = Annotated[X402Service, Depends(get_x402)]


class RequirementsBody(BaseModel):
    """What the wire sends. camelCase, because the protocol says so."""

    scheme: str = Field(default="", max_length=32)
    network: str = Field(default="", max_length=64)
    maxAmountRequired: str = Field(default="", max_length=64)  # noqa: N815
    resource: str = Field(default="", max_length=512)
    description: str = Field(default="", max_length=512)
    payTo: str = Field(default="", max_length=128)  # noqa: N815
    asset: str = Field(default="", max_length=64)
    maxTimeoutSeconds: int = Field(default=300, ge=1, le=3_600)  # noqa: N815
    extra: dict[str, Any] = Field(default_factory=dict)

    def to_domain(self) -> PaymentRequirements:
        return PaymentRequirements(
            scheme=self.scheme,
            network=self.network,
            max_amount_required=self.maxAmountRequired,
            resource=self.resource,
            description=self.description,
            pay_to=self.payTo,
            asset=self.asset,
            max_timeout_seconds=self.maxTimeoutSeconds,
            extra=dict(self.extra),
        )


class PayloadBody(BaseModel):
    x402Version: int = Field(default=1)  # noqa: N815
    scheme: str = Field(default="", max_length=32)
    network: str = Field(default="", max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)

    def to_domain(self) -> PaymentPayload:
        return PaymentPayload(scheme=self.scheme, network=self.network, payload=dict(self.payload))


class FacilitatorBody(BaseModel):
    paymentPayload: PayloadBody  # noqa: N815
    paymentRequirements: RequirementsBody  # noqa: N815


class QuoteBody(BaseModel):
    resource: str = Field(default="", max_length=512)
    payer: str = Field(default="", max_length=64)
    """The address that will pay, when the caller knows it. It only decides which account the
    tokens land in if the payment is never completed; a settled payment always credits
    whoever actually signed."""
    usd: str = Field(default="", max_length=32)


@router.get("/supported", summary="Which x402 schemes and networks this speaks")
async def supported(service: X402Dep) -> dict[str, Any]:
    return service.supported.as_json()


@router.post("/quote", summary="Payment requirements for a resource")
async def quote(body: QuoteBody, service: X402Dep) -> dict[str, Any]:
    """A 402 body, handed over on request instead of as a refusal.

    The same document either way. A client that would rather ask what something costs than
    be told off for not paying should be able to.
    """
    price = Money.parse(body.usd) if body.usd else None
    requirements, invoice = await service.requirements(body.payer, body.resource, price)
    return {"x402Version": 1, "accepts": [requirements.as_json()], "invoice": invoice.id}


@router.post("/verify", summary="Would this payment be accepted? (facilitator)")
async def verify(body: FacilitatorBody, service: X402Dep) -> dict[str, Any]:
    """Nothing is submitted. This is the free half of the protocol."""
    result: VerifyResult = await service.verify(
        body.paymentPayload.to_domain(), body.paymentRequirements.to_domain()
    )
    return result.as_json()


@router.post("/settle", summary="Submit a payment and report the ledger's answer (facilitator)")
async def settle(body: FacilitatorBody, service: X402Dep, response: Response) -> dict[str, Any]:
    """Verified again before submitting, rather than trusting that ``/verify`` was called.

    The two endpoints are independent, and a facilitator that settles an unverified envelope
    is one that submits whatever it is handed.
    """
    result: SettleResult = await service.settle(
        body.paymentPayload.to_domain(), body.paymentRequirements.to_domain()
    )
    if not result.success:
        response.status_code = status.HTTP_402_PAYMENT_REQUIRED
    return result.as_json()


__all__ = ["X402Error", "router"]
