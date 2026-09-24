"""Who is calling, proved by the key that pays.

THE WHOLE IDEA
    An account IS a Stellar address. There is no password to store, no reset flow to abuse,
    no email to verify, and the thing that proves ownership is the same key that sends the
    money. Signing in is: the server states a challenge, the wallet signs it, the server
    checks the signature against the address.

WHY A SESSION TOKEN AT ALL
    Because asking a wallet to sign every request would put a popup in front of every click.
    The token is an HMAC of the claims — address and expiry — under a server secret. Nothing
    is stored: there is no session table to grow, to replicate or to leak, and revocation is
    rotating the secret. Stateless is the right trade here precisely because the thing being
    protected is a balance, not a mailbox: a stolen token expires in hours, and the money
    itself still needs the key.

WHAT IS CHECKED, AND IN WHICH ORDER
    The challenge carries the address it was issued for, when it was issued, and this
    gateway's own name, all inside its own HMAC. So a challenge cannot be edited, cannot be
    replayed after it expires, and cannot be taken from one deployment to another. That
    matters more than it looks: without the address inside, a challenge issued for one
    account could be signed by the holder of another and presented as theirs.

WHEN SIGNATURES CANNOT BE CHECKED
    ``SignatureVerifier.available`` is ``False`` on a deployment without the optional Stellar
    dependency. The answer is then a refusal that says so, never a login that succeeds.
    "We could not verify you" and "you are verified" must not be the same outcome.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any

from gateway.domain.exceptions import GatewayError
from gateway.domain.ports import SignatureVerifier

logger = logging.getLogger(__name__)

ADDRESS = re.compile(r"\AG[A-Z2-7]{55}\Z")
"""A Stellar public key: 56 base32 characters starting with G. Checked before anything else
touches it, so that an address is either well-formed or rejected — never "probably fine"."""

CHALLENGE_TTL_S = 300
"""Five minutes to open a wallet and press a button. A challenge is single-use in practice
because it expires, and long-lived challenges are what replay attacks are made of."""

SESSION_TTL_S = 12 * 3600

MAX_TOKEN_BYTES = 4096


class IdentityError(GatewayError):
    """The caller is not who they say they are, or did not say.

    A ``GatewayError`` so that it reaches the one error handler this gateway has and comes
    back in the same ``{"error": {code, message, request_id}}`` envelope as everything else.
    A route that raised its own ``HTTPException`` would answer ``{"detail": ...}`` instead,
    and a client would have to know which routes do which.
    """


class UnverifiableError(IdentityError):
    """This deployment cannot check signatures at all. Not the same as a bad signature.

    Kept apart because the caller did nothing wrong and retrying will not help: it is a 501,
    not a 401.
    """


def is_address(value: str) -> bool:
    return bool(ADDRESS.match(value or ""))


@dataclass(frozen=True, slots=True)
class Challenge:
    """What the wallet must sign. Self-contained and tamper-evident."""

    address: str
    nonce: str
    issued_at: int
    audience: str

    @property
    def message(self) -> str:
        """The exact text that is signed. Human-readable on purpose: a wallet shows this to a
        person, and "sign this random blob" is how people are phished."""
        return (
            f"CodeZard sign-in\naccount: {self.address}\n"
            f"audience: {self.audience}\nissued: {self.issued_at}\nnonce: {self.nonce}"
        )

    def expired(self, now: int) -> bool:
        return now - self.issued_at > CHALLENGE_TTL_S

    def as_json(self, token: str) -> dict[str, Any]:
        return {
            "address": self.address,
            "message": self.message,
            "challenge": token,
            "expires_in": CHALLENGE_TTL_S,
        }


@dataclass(frozen=True, slots=True)
class Session:
    address: str
    expires_at: int

    def as_json(self, token: str) -> dict[str, Any]:
        return {"address": self.address, "token": token, "expires_at": self.expires_at}


class IdentityService:
    """Issues challenges, checks signatures, mints and reads session tokens."""

    def __init__(
        self,
        verifier: SignatureVerifier,
        secret: str,
        audience: str = "codezard",
        session_ttl_s: int = SESSION_TTL_S,
    ) -> None:
        if not secret:
            # A default secret is a secret everybody has. Refusing to start beats starting
            # with authentication that anyone can forge.
            raise ValueError("identity needs a signing secret (GATEWAY_BILLING__SECRET)")
        self._verifier = verifier
        self._secret = secret.encode("utf-8")
        self._audience = audience
        self._session_ttl_s = session_ttl_s

    @property
    def available(self) -> bool:
        return self._verifier.available

    # ── the two moves ────────────────────────────────────────────────────────

    def challenge(self, address: str) -> tuple[Challenge, str]:
        """``(challenge, token)``. The token is the challenge, sealed; it comes back unchanged."""
        if not is_address(address):
            raise IdentityError("that is not a Stellar public key")
        if not self._verifier.available:
            raise UnverifiableError(
                "this gateway cannot verify signatures: the Stellar dependency is not "
                'installed (pip install -e ".[stellar]")'
            )
        challenge = Challenge(address, secrets.token_hex(16), int(time.time()), self._audience)
        return challenge, self._seal(
            {
                "a": challenge.address,
                "n": challenge.nonce,
                "i": challenge.issued_at,
                "d": challenge.audience,
            }
        )

    def verify(self, token: str, signature: str) -> tuple[Session, str]:
        """Check the signature over the sealed challenge. ``(session, session token)``.

        The address comes out of the CHALLENGE, never out of the request: a caller who could
        name the address they are verifying against would just name someone else's.
        """
        claims = self._open(token)
        challenge = Challenge(
            str(claims.get("a") or ""),
            str(claims.get("n") or ""),
            int(claims.get("i") or 0),
            str(claims.get("d") or ""),
        )
        if challenge.audience != self._audience:
            raise IdentityError("that challenge was issued for a different service")
        if challenge.expired(int(time.time())):
            raise IdentityError("that challenge expired; ask for a new one")
        if not self._verifier.available:
            raise UnverifiableError("this gateway cannot verify signatures")
        if not self._verifier.verify(
            challenge.address, challenge.message.encode("utf-8"), signature
        ):
            logger.info(
                "sign-in refused for %s: the signature does not match", challenge.address[:8]
            )
            raise IdentityError("the signature does not match that account")
        session = Session(challenge.address, int(time.time()) + self._session_ttl_s)
        return session, self._seal({"a": session.address, "e": session.expires_at})

    def account_of(self, token: str) -> str:
        """The address a session token proves, or raise. What every guarded route calls."""
        claims = self._open(token)
        address = str(claims.get("a") or "")
        expires_at = int(claims.get("e") or 0)
        if not is_address(address):
            raise IdentityError("that token does not name an account")
        if expires_at <= int(time.time()):
            raise IdentityError("that session expired; sign in again")
        return address

    # ── sealing ──────────────────────────────────────────────────────────────

    def _seal(self, claims: dict[str, Any]) -> str:
        """``base64(payload).base64(hmac)``. Readable, unforgeable, and carries no secret."""
        body = json.dumps(claims, separators=(",", ":"), sort_keys=True).encode("utf-8")
        return f"{_b64(body)}.{_b64(self._mac(body))}"

    def _open(self, token: str) -> dict[str, Any]:
        raw = (token or "").strip()
        if not raw or len(raw) > MAX_TOKEN_BYTES:
            raise IdentityError("no token was sent")
        head, _, tail = raw.partition(".")
        if not tail:
            raise IdentityError("that token is malformed")
        try:
            body = _unb64(head)
            signature = _unb64(tail)
        except (binascii.Error, ValueError) as error:
            raise IdentityError("that token is malformed") from error
        # Constant time, because a comparison that returns early tells an attacker how much of
        # a forged signature was right, one byte at a time.
        if not hmac.compare_digest(signature, self._mac(body)):
            raise IdentityError("that token was not issued by this gateway")
        try:
            claims = json.loads(body)
        except ValueError as error:
            raise IdentityError("that token is malformed") from error
        if not isinstance(claims, dict):
            raise IdentityError("that token is malformed")
        return claims

    def _mac(self, body: bytes) -> bytes:
        return hmac.new(self._secret, body, hashlib.sha256).digest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


__all__ = [
    "CHALLENGE_TTL_S",
    "SESSION_TTL_S",
    "Challenge",
    "IdentityError",
    "IdentityService",
    "Session",
    "UnverifiableError",
    "is_address",
]
