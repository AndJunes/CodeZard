"""HTTP 402, spoken properly: the wire format a client pays with, as values.

WHAT x402 IS, IN ONE PARAGRAPH
    A resource that costs money answers ``402 Payment Required`` with a body listing what it
    would accept — asset, amount, where to send it, how long the client has. The client pays,
    puts proof of the payment in an ``X-PAYMENT`` header, and repeats the identical request.
    The server verifies the proof, does the work, and returns the settlement in
    ``X-PAYMENT-RESPONSE``. No account, no key, no signup: the request carries its own money.

WHY IT EARNS ITS PLACE HERE
    The rest of billing assumes a person — someone who signs in, picks a plan and watches a
    balance. An agent calling this gateway is not that, and making it sign up is asking a
    program to do the one thing programs are worst at. 402 is the same ledger reached without
    an account: the payment IS the authorisation, and what it buys is tokens at the same
    price everyone else pays.

THE PART THAT IS OURS
    The scheme is ``exact`` and the network is Stellar, so the proof a client sends is a
    signed Stellar transaction paying the stated amount to the stated address. Verifying it
    means checking the envelope says what it claims BEFORE submitting anything; settling it
    means submitting it and waiting for the ledger. Those two live in the infrastructure
    layer; this file is only the shapes, so that the protocol can be read in one place
    without a network client in the way.

A DELIBERATE OMISSION
    Nothing here trusts a field. ``PaymentPayload`` is what a caller SAID; every amount that
    matters is read back out of the signed envelope by whoever verifies it. A payload whose
    ``payload`` block claims a different amount from its envelope is not an inconsistency to
    reconcile — it is the attack, and it is rejected.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass, field
from typing import Any

from gateway.domain.exceptions import GatewayError

X402_VERSION = 1
"""The only version this speaks. Sent on every document and checked on every payload: a
client written against a different one must fail loudly rather than half-work."""

SCHEME = "exact"
"""The scheme where the client pays a stated amount, as opposed to one where it authorises up
to a maximum and the server draws what it used. ``exact`` is what a token pack is."""

PAYMENT_HEADER = "x-payment"
RESPONSE_HEADER = "x-payment-response"

MAX_PAYLOAD_BYTES = 16 * 1024
"""A signed Stellar envelope is around a kilobyte. Sixteen is room for anything legitimate and
a bound on what an unauthenticated header can make this process decode."""


class X402Error(GatewayError):
    """The payment header is missing, malformed, or does not pay for this.

    A ``GatewayError`` so that one escaping a path that does not catch it still comes back in
    the house envelope rather than as a 500. The ordinary route for it is
    ``payment_gate``, which catches it and answers 402 with the price instead.
    """


@dataclass(frozen=True, slots=True)
class PaymentRequirements:
    """One way to pay for one resource. A 402 may offer several."""

    scheme: str
    network: str
    max_amount_required: str
    """The most the client will be asked to pay, in the asset's own smallest-unit string."""
    resource: str
    description: str
    pay_to: str
    asset: str
    max_timeout_seconds: int = 300
    mime_type: str = "application/json"
    extra: dict[str, Any] = field(default_factory=dict)
    """Scheme-specific. For Stellar: the memo the payment must carry, and the passphrase of
    the network it must be signed for — a client that signs for the wrong network produces a
    transaction that is valid nowhere, and saying so up front is cheaper than a failed
    submission."""

    def as_json(self) -> dict[str, Any]:
        # camelCase, because that is what the protocol says on the wire. The rest of this code
        # base is snake_case and stays so; the translation happens here, once.
        return {
            "scheme": self.scheme,
            "network": self.network,
            "maxAmountRequired": self.max_amount_required,
            "resource": self.resource,
            "description": self.description,
            "mimeType": self.mime_type,
            "payTo": self.pay_to,
            "maxTimeoutSeconds": self.max_timeout_seconds,
            "asset": self.asset,
            "extra": dict(self.extra),
        }


@dataclass(frozen=True, slots=True)
class PaymentRequired:
    """The whole 402 body: why, and everything that would be accepted instead."""

    error: str
    accepts: tuple[PaymentRequirements, ...]

    def as_json(self) -> dict[str, Any]:
        return {
            "x402Version": X402_VERSION,
            "error": self.error,
            "accepts": [requirement.as_json() for requirement in self.accepts],
        }


@dataclass(frozen=True, slots=True)
class PaymentPayload:
    """What the client put in ``X-PAYMENT``. Claims, not facts."""

    scheme: str
    network: str
    payload: dict[str, Any]
    x402_version: int = X402_VERSION

    @property
    def transaction(self) -> str:
        """The signed Stellar envelope, base64 XDR. ``""`` when the client sent none."""
        value = self.payload.get("transaction")
        return value if isinstance(value, str) else ""

    @classmethod
    def decode(cls, header: str) -> PaymentPayload:
        """Read the header. Every failure is the same answer: this does not pay for anything.

        Deliberately strict. The header is unauthenticated input on a path that is reached
        before anything else has checked anything, so it is size-bounded, base64-decoded,
        JSON-parsed and type-checked before a single field is read, and any step failing ends
        it.
        """
        raw = (header or "").strip()
        if not raw:
            raise X402Error("the X-PAYMENT header is empty")
        if len(raw) > MAX_PAYLOAD_BYTES:
            raise X402Error("the X-PAYMENT header is too large")
        try:
            body = json.loads(base64.b64decode(raw, validate=True))
        except (binascii.Error, ValueError, UnicodeDecodeError) as error:
            raise X402Error(f"the X-PAYMENT header is not base64 JSON: {error}") from error
        if not isinstance(body, dict):
            raise X402Error("the X-PAYMENT header is not a JSON object")
        version = body.get("x402Version")
        if version != X402_VERSION:
            raise X402Error(
                f"x402 version {version!r} is not supported; this speaks {X402_VERSION}"
            )
        inner = body.get("payload")
        return cls(
            scheme=str(body.get("scheme") or ""),
            network=str(body.get("network") or ""),
            payload=dict(inner) if isinstance(inner, dict) else {},
            x402_version=X402_VERSION,
        )

    def as_json(self) -> dict[str, Any]:
        return {
            "x402Version": self.x402_version,
            "scheme": self.scheme,
            "network": self.network,
            "payload": dict(self.payload),
        }


@dataclass(frozen=True, slots=True)
class VerifyResult:
    """Would this payload pay, if it were submitted? Nothing has been submitted."""

    valid: bool
    payer: str = ""
    reason: str = ""
    amount: str = ""
    """What the ENVELOPE actually pays, read back from it — never what the payload claimed."""

    def as_json(self) -> dict[str, Any]:
        return {
            "isValid": self.valid,
            "payer": self.payer,
            "invalidReason": self.reason or None,
            "amount": self.amount,
        }


@dataclass(frozen=True, slots=True)
class SettleResult:
    """It was submitted, and this is what the ledger said."""

    success: bool
    network: str
    transaction: str = ""
    payer: str = ""
    reason: str = ""

    def as_json(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "transaction": self.transaction,
            "network": self.network,
            "payer": self.payer,
            "errorReason": self.reason or None,
        }

    def encode(self) -> str:
        """The ``X-PAYMENT-RESPONSE`` header value: the same document, base64."""
        return base64.b64encode(
            json.dumps(self.as_json(), separators=(",", ":")).encode("utf-8")
        ).decode("ascii")


@dataclass(frozen=True, slots=True)
class Supported:
    """What ``GET /x402/supported`` answers: which (scheme, network) pairs this speaks."""

    kinds: tuple[tuple[str, str], ...]

    def as_json(self) -> dict[str, Any]:
        return {
            "kinds": [
                {"x402Version": X402_VERSION, "scheme": scheme, "network": network}
                for scheme, network in self.kinds
            ]
        }


__all__ = [
    "MAX_PAYLOAD_BYTES",
    "PAYMENT_HEADER",
    "RESPONSE_HEADER",
    "SCHEME",
    "X402_VERSION",
    "PaymentPayload",
    "PaymentRequired",
    "PaymentRequirements",
    "SettleResult",
    "Supported",
    "VerifyResult",
    "X402Error",
]
