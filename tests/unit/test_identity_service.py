"""Signing in with a wallet. Mostly: what a forged or edited token does.

The verifier is faked because ed25519 is the SDK's job and an optional dependency; what is
under test is everything around it — what goes into the challenge, what comes out of a
session, and whether either can be tampered with.
"""

import base64
import json
import time

import pytest

from gateway.application.identity_service import (
    CHALLENGE_TTL_S,
    Challenge,
    IdentityError,
    IdentityService,
    UnverifiableError,
    is_address,
)

ADDRESS = "G" + "A" * 55
OTHER = "G" + "B" * 55
SECRET = "a-secret-nobody-else-has"
SIGNATURE = base64.b64encode(b"x" * 64).decode()


class FakeVerifier:
    """Says yes to one (address, signature) pair and no to everything else."""

    def __init__(self, available: bool = True, accepts: str = ADDRESS) -> None:
        self._available = available
        self._accepts = accepts
        self.seen: list[tuple[str, bytes]] = []

    @property
    def available(self) -> bool:
        return self._available

    def verify(self, address: str, message: bytes, signature: str) -> bool:
        self.seen.append((address, message))
        return address == self._accepts and signature == SIGNATURE


@pytest.fixture
def identity() -> IdentityService:
    return IdentityService(FakeVerifier(), SECRET)  # type: ignore[arg-type]


def signed_in(identity: IdentityService) -> str:
    _challenge, token = identity.challenge(ADDRESS)
    _session, session_token = identity.verify(token, SIGNATURE)
    return session_token


class TestAddresses:
    @pytest.mark.parametrize("value", [ADDRESS, OTHER])
    def test_a_stellar_public_key_is_an_address(self, value: str) -> None:
        assert is_address(value)

    @pytest.mark.parametrize(
        "value", ["", "G", "GA", "S" + "A" * 55, "g" + "a" * 55, "G" + "A" * 56, "G" + "1" * 55]
    )
    def test_anything_else_is_not(self, value: str) -> None:
        assert not is_address(value)


class TestChallenge:
    def test_it_is_readable_by_a_person(self, identity: IdentityService) -> None:
        """A wallet shows this text to somebody. "Sign this random blob" is how people are
        phished."""
        challenge, _token = identity.challenge(ADDRESS)
        assert "CodeZard sign-in" in challenge.message
        assert ADDRESS in challenge.message

    def test_it_names_the_account_it_was_issued_for(self, identity: IdentityService) -> None:
        """Without the address inside, a challenge issued for one account could be signed by
        the holder of another and presented as theirs."""
        challenge, _token = identity.challenge(ADDRESS)
        assert challenge.address == ADDRESS

    def test_two_challenges_are_never_the_same(self, identity: IdentityService) -> None:
        first, _ = identity.challenge(ADDRESS)
        second, _ = identity.challenge(ADDRESS)
        assert first.nonce != second.nonce

    def test_a_bad_address_is_refused_before_anything_else(self, identity: IdentityService) -> None:
        with pytest.raises(IdentityError):
            identity.challenge("not-an-address")

    def test_it_expires(self) -> None:
        stale = Challenge(ADDRESS, "n", int(time.time()) - CHALLENGE_TTL_S - 1, "codezard")
        assert stale.expired(int(time.time()))


class TestVerify:
    def test_a_good_signature_mints_a_session(self, identity: IdentityService) -> None:
        _challenge, token = identity.challenge(ADDRESS)
        session, session_token = identity.verify(token, SIGNATURE)

        assert session.address == ADDRESS
        assert identity.account_of(session_token) == ADDRESS

    def test_the_address_comes_from_the_challenge_and_not_from_the_caller(self) -> None:
        """The whole reason the challenge is sealed: a caller who could name the account
        would name somebody else's and sign their own."""
        verifier = FakeVerifier()
        identity = IdentityService(verifier, SECRET)  # type: ignore[arg-type]
        _challenge, token = identity.challenge(ADDRESS)
        identity.verify(token, SIGNATURE)

        assert [address for address, _ in verifier.seen] == [ADDRESS]

    def test_a_wrong_signature_is_refused(self, identity: IdentityService) -> None:
        _challenge, token = identity.challenge(ADDRESS)
        with pytest.raises(IdentityError):
            identity.verify(token, base64.b64encode(b"y" * 64).decode())

    def test_an_edited_challenge_is_refused(self, identity: IdentityService) -> None:
        """The seal is over the claims; swapping the address inside breaks it."""
        _challenge, token = identity.challenge(ADDRESS)
        body, _, mac = token.partition(".")
        claims = json.loads(base64.urlsafe_b64decode(body + "=="))
        claims["a"] = OTHER
        forged = (
            base64.urlsafe_b64encode(
                json.dumps(claims, separators=(",", ":"), sort_keys=True).encode()
            )
            .rstrip(b"=")
            .decode()
        )

        with pytest.raises(IdentityError, match="not issued by this gateway"):
            identity.verify(f"{forged}.{mac}", SIGNATURE)

    def test_an_expired_challenge_is_refused(self, identity: IdentityService) -> None:
        stale = identity._seal(
            {"a": ADDRESS, "n": "x", "i": int(time.time()) - CHALLENGE_TTL_S - 10, "d": "codezard"}
        )
        with pytest.raises(IdentityError, match="expired"):
            identity.verify(stale, SIGNATURE)

    def test_a_challenge_from_another_deployment_is_refused(self) -> None:
        theirs = IdentityService(FakeVerifier(), SECRET, audience="somebody-else")  # type: ignore[arg-type]
        ours = IdentityService(FakeVerifier(), SECRET)  # type: ignore[arg-type]
        _challenge, token = theirs.challenge(ADDRESS)

        with pytest.raises(IdentityError, match="different service"):
            ours.verify(token, SIGNATURE)


class TestSessions:
    def test_a_token_from_another_gateway_is_refused(self, identity: IdentityService) -> None:
        theirs = IdentityService(FakeVerifier(), "a-different-secret")  # type: ignore[arg-type]
        with pytest.raises(IdentityError):
            identity.account_of(signed_in(theirs))

    @pytest.mark.parametrize("token", ["", "   ", "nodot", "a.b", "x" * 5_000])
    def test_a_malformed_token_names_nobody(self, identity: IdentityService, token: str) -> None:
        with pytest.raises(IdentityError):
            identity.account_of(token)

    def test_an_expired_session_is_refused(self) -> None:
        identity = IdentityService(FakeVerifier(), SECRET, session_ttl_s=-1)  # type: ignore[arg-type]
        with pytest.raises(IdentityError, match="expired"):
            identity.account_of(signed_in(identity))

    def test_a_token_that_names_no_account_is_refused(self, identity: IdentityService) -> None:
        sealed = identity._seal({"e": int(time.time()) + 3_600})
        with pytest.raises(IdentityError):
            identity.account_of(sealed)

    def test_a_sealed_non_object_is_refused(self, identity: IdentityService) -> None:
        body = base64.urlsafe_b64encode(b"[1,2,3]").rstrip(b"=").decode()
        mac = base64.urlsafe_b64encode(identity._mac(b"[1,2,3]")).rstrip(b"=").decode()
        with pytest.raises(IdentityError):
            identity.account_of(f"{body}.{mac}")


class TestUnverifiableDeployments:
    def test_it_refuses_rather_than_letting_everybody_in(self) -> None:
        """ "We could not verify you" and "you are verified" must not be the same outcome."""
        identity = IdentityService(FakeVerifier(available=False), SECRET)  # type: ignore[arg-type]
        assert not identity.available
        with pytest.raises(UnverifiableError):
            identity.challenge(ADDRESS)

    def test_a_session_minted_earlier_still_reads(self) -> None:
        """Losing the dependency must not log everybody out mid-session: the HMAC is ours."""
        working = IdentityService(FakeVerifier(), SECRET)  # type: ignore[arg-type]
        token = signed_in(working)
        broken = IdentityService(FakeVerifier(available=False), SECRET)  # type: ignore[arg-type]
        assert broken.account_of(token) == ADDRESS


def test_a_gateway_without_a_secret_refuses_to_start() -> None:
    """A default secret is one everybody has, and here it would forge sign-ins."""
    with pytest.raises(ValueError, match="signing secret"):
        IdentityService(FakeVerifier(), "")  # type: ignore[arg-type]
