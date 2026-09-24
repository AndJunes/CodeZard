"""Horizon, with the network replaced by a mock transport.

What is under test is the reading: which JSON shapes count as a payment, what a memo is, how
a price becomes an amount, and what a refusal from Horizon says. The cryptography is the
SDK's and is an optional dependency, so the two calls that need it are checked for the answer
they give when it is absent.
"""

import base64
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from gateway.domain.billing import Invoice, Money
from gateway.infrastructure.billing.stellar import (
    NETWORKS,
    PUBLIC,
    TESTNET,
    PaymentNetworkError,
    StellarConfig,
    StellarNetwork,
    StellarSignatures,
)

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
ACCOUNT = "G" + "A" * 55
DESTINATION = "G" + "B" * 55
PAYER = "G" + "C" * 55
USDC_ISSUER = "G" + "D" * 55


def invoice(**changes: object) -> Invoice:
    base = {
        "id": "cz0123456789abcdef0123",
        "account": ACCOUNT,
        "sku": "tokens-1m",
        "tokens": 1_000_000,
        "price": Money.usd(8),
        "asset": "XLM",
        "amount": "80.0000000",
        "destination": DESTINATION,
        "created_at": NOW,
        "expires_at": NOW + timedelta(minutes=30),
    }
    return Invoice(**{**base, **changes})  # type: ignore[arg-type]


def payment_record(**changes: object) -> dict[str, object]:
    base = {
        "type": "payment",
        "transaction_hash": "txhash",
        "from": PAYER,
        "to": DESTINATION,
        "asset_type": "native",
        "amount": "80.0000000",
        "created_at": "2026-09-23T12:00:00Z",
        "transaction": {"memo_type": "text", "memo": "cz0123456789abcdef0123"},
    }
    return {**base, **changes}


def network_with(handler: object, **config: object) -> StellarNetwork:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    settings = {"destination": DESTINATION, **config}
    return StellarNetwork(StellarConfig(**settings), client)  # type: ignore[arg-type]


def records(*rows: dict[str, object]) -> httpx.Response:
    return httpx.Response(200, json={"_embedded": {"records": list(rows)}})


class TestConfiguration:
    def test_the_passphrases_are_the_canonical_ones(self) -> None:
        """Pinned literally, because a passphrase that is one character off does not fail
        loudly: every signature simply stops verifying, on a path where "invalid" is an
        ordinary answer nobody investigates."""
        assert NETWORKS[TESTNET]["passphrase"] == "Test SDF Network ; September 2015"
        assert NETWORKS[PUBLIC]["passphrase"] == ("Public Global Stellar Network ; September 2015")

    def test_each_network_knows_its_horizon_and_its_x402_name(self) -> None:
        for name in (TESTNET, PUBLIC):
            settings = NETWORKS[name]
            assert settings["horizon"].startswith("https://")
            assert settings["x402"].startswith("stellar")

    def test_the_x402_network_name_follows_the_configured_one(self) -> None:
        assert network_with(lambda r: records()).network == "stellar-testnet"
        assert network_with(lambda r: records(), network=PUBLIC).network == "stellar"

    def test_a_custom_horizon_wins_over_the_default(self) -> None:
        config = StellarConfig(horizon_url="https://mirror.invalid/")
        assert config.horizon == "https://mirror.invalid"

    def test_an_unknown_network_falls_back_to_testnet_rather_than_guessing(self) -> None:
        """Not a silent default onto mainnet, which is the one wrong direction to fall."""
        assert StellarConfig(network="nonsense").passphrase == NETWORKS[TESTNET]["passphrase"]

    def test_it_can_point_at_the_explorer_for_a_transaction(self) -> None:
        assert "stellar.expert" in StellarConfig().explorer_tx("abc")


class TestPaymentLookup:
    async def test_a_matching_memo_is_the_payment(self) -> None:
        network = network_with(lambda request: records(payment_record()))
        found = await network.payment_for(invoice())

        assert found is not None
        assert found.tx_hash == "txhash"
        assert found.payer == PAYER
        assert found.asset == "XLM"
        assert found.settles(invoice())

    async def test_another_memo_is_not(self) -> None:
        other = payment_record(transaction={"memo_type": "text", "memo": "czsomebodyelse"})
        network = network_with(lambda request: records(other))
        assert await network.payment_for(invoice()) is None

    async def test_a_payment_without_a_text_memo_is_not(self) -> None:
        """A memo of another type cannot be an invoice id, and reading it as one would match
        an arbitrary hash against an arbitrary invoice."""
        untagged = payment_record(transaction={"memo_type": "hash", "memo": "deadbeef"})
        assert await network_with(lambda r: records(untagged)).payment_for(invoice()) is None

    async def test_funding_an_account_counts_as_a_payment(self) -> None:
        """On Stellar, `create_account` IS how a first payment arrives."""
        funding = payment_record(
            type="create_account",
            amount=None,
            starting_balance="80.0000000",
            account=DESTINATION,
            funder=PAYER,
            to=None,
            **{"from": None},
        )
        found = await network_with(lambda r: records(funding)).payment_for(invoice())

        assert found is not None
        assert found.amount == "80.0000000"
        assert found.payer == PAYER

    async def test_operations_that_are_not_payments_are_skipped(self) -> None:
        noise = payment_record(type="change_trust")
        assert await network_with(lambda r: records(noise)).payment_for(invoice()) is None

    async def test_a_usdc_payment_reports_its_code(self) -> None:
        usdc = payment_record(asset_type="credit_alphanum4", asset_code="USDC")
        found = await network_with(lambda r: records(usdc)).payment_for(
            invoice(asset="USDC", amount="8.0000000")
        )

        assert found is not None
        assert found.asset == "USDC"

    async def test_a_destination_that_does_not_exist_yet_is_nobody_paying(self) -> None:
        """Not an error to a caller polling an invoice: nobody has paid, because nobody can."""
        network = network_with(lambda request: httpx.Response(404, json={"title": "Not Found"}))
        assert await network.payment_for(invoice()) is None

    async def test_horizon_failing_is_raised_and_not_read_as_unpaid(self) -> None:
        """ "The answer is no" and "we could not ask" are different, and confusing them would
        expire invoices that were paid."""
        network = network_with(lambda request: httpx.Response(503, json={}))
        with pytest.raises(PaymentNetworkError):
            await network.payment_for(invoice())

    async def test_a_transport_failure_is_raised_too(self) -> None:
        def explode(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route to host")

        with pytest.raises(PaymentNetworkError, match="could not be reached"):
            await network_with(explode).payment_for(invoice())

    async def test_it_joins_the_transactions_so_the_memo_arrives_in_one_request(self) -> None:
        seen: list[httpx.Request] = []

        def record(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return records()

        await network_with(record).payment_for(invoice())
        assert "join=transactions" in str(seen[0].url)


class TestQuoting:
    async def test_usdc_is_a_dollar_and_needs_no_network(self) -> None:
        asked: list[httpx.Request] = []

        def record(request: httpx.Request) -> httpx.Response:
            asked.append(request)
            return records()

        network = network_with(record, asset="USDC", usdc_issuer=USDC_ISSUER)
        assert await network.quote(Money.parse("8.00"), "USDC") == "8.0000000"
        assert asked == []

    async def test_xlm_is_priced_from_the_dex(self) -> None:
        def paths(request: httpx.Request) -> httpx.Response:
            return records({"destination_amount": "0.1000000"})

        network = network_with(paths, usdc_issuer=USDC_ISSUER)
        assert await network.quote(Money.parse("8.00"), "XLM") == "80.0000000"

    async def test_the_configured_rate_is_the_fallback_when_the_dex_cannot_be_asked(
        self,
    ) -> None:
        def broken(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={})

        network = network_with(broken, usdc_issuer=USDC_ISSUER, xlm_usd="0.20")
        assert await network.quote(Money.parse("8.00"), "XLM") == "40.0000000"

    async def test_with_no_rate_at_all_it_refuses_instead_of_guessing(self) -> None:
        """An invoice that cannot be priced is refused. A guessed price is worse than none."""
        network = network_with(lambda request: httpx.Response(500, json={}))
        with pytest.raises(PaymentNetworkError, match="no XLM/USD rate"):
            await network.quote(Money.parse("8.00"), "XLM")

    async def test_an_amount_is_rounded_up_so_a_quote_is_never_short(self) -> None:
        network = network_with(lambda request: records(), xlm_usd="3")
        # 1 / 3 = 0.333... and the payer must not be asked for less than the price.
        assert await network.quote(Money.usd(1), "XLM") == "0.3333334"


class TestSettling:
    async def test_a_successful_submission_reports_the_hash(self) -> None:
        def horizon(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST"
            return httpx.Response(
                200, json={"successful": True, "hash": "abc123", "source_account": PAYER}
            )

        result = await network_with(horizon).settle("AAAA")
        assert result.success
        assert result.transaction == "abc123"
        assert result.payer == PAYER

    async def test_horizon_s_result_codes_become_a_sentence(self) -> None:
        """`title` alone is "Transaction Failed" for every possible cause."""

        def horizon(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400,
                json={
                    "title": "Transaction Failed",
                    "extras": {
                        "result_codes": {
                            "transaction": "tx_failed",
                            "operations": ["op_underfunded"],
                        }
                    },
                },
            )

        result = await network_with(horizon).settle("AAAA")
        assert not result.success
        assert "tx_failed" in result.reason
        assert "op_underfunded" in result.reason

    async def test_a_refusal_without_result_codes_still_says_something(self) -> None:
        def horizon(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"title": "Bad Request"})

        assert "Bad Request" in (await network_with(horizon).settle("AAAA")).reason

    async def test_a_transaction_that_was_included_and_failed_is_not_a_success(self) -> None:
        def horizon(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"successful": False, "hash": "abc"})

        result = await network_with(horizon).settle("AAAA")
        assert not result.success
        assert result.transaction == "abc"

    async def test_an_unreachable_horizon_is_a_failure_and_not_an_exception(self) -> None:
        def explode(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down")

        result = await network_with(explode).settle("AAAA")
        assert not result.success
        assert "could not be reached" in result.reason

    async def test_a_body_that_is_not_json_does_not_take_the_call_down(self) -> None:
        def horizon(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, content=b"<html>gateway timeout</html>")

        assert not (await network_with(horizon).settle("AAAA")).success


# ── the half that genuinely needs Stellar's cryptography ─────────────────────
#
# Imported optionally rather than with `importorskip` at module level, so that a machine
# without the extra still runs everything above — which is most of this file, and all of the
# Horizon reading. The addresses above are SHAPED like Stellar keys and are not valid ones
# (no checksum), which is fine for parsing JSON and not fine for building a transaction, so
# the tests below mint real keypairs.

try:
    import stellar_sdk as sdk
except ImportError:  # pragma: no cover - exercised on a machine without the extra
    sdk = None  # type: ignore[assignment]

requires_sdk = pytest.mark.skipif(sdk is None, reason="the optional Stellar extra is absent")


def signed_payment(
    destination: str,
    amount: str = "80.0000000",
    memo: str = "cz0123456789abcdef0123",
    asset: object = None,
) -> tuple[str, str]:
    """A real, signed Stellar envelope paying ``amount`` to ``destination`` with ``memo``.

    Built with the SDK rather than pasted in as a fixture string: what is under test is that
    every number is read back out of the SIGNED bytes, and a hand-written blob would only
    prove that a parser parses.
    """
    keypair = sdk.Keypair.random()
    source = sdk.Account(keypair.public_key, 1)
    envelope = (
        sdk.TransactionBuilder(source, NETWORKS[TESTNET]["passphrase"], base_fee=100)
        .append_payment_op(
            destination=destination, amount=amount, asset=asset or sdk.Asset.native()
        )
        .add_text_memo(memo)
        .set_timeout(300)
        .build()
    )
    envelope.sign(keypair)
    return envelope.to_xdr(), keypair.public_key


@requires_sdk
class TestVerifyingASignedEnvelope:
    """The one call that needs XDR. Every refusal here is a different way of not paying."""

    @pytest.fixture
    def destination(self) -> str:
        return sdk.Keypair.random().public_key

    @pytest.fixture
    def network(self, destination: str) -> StellarNetwork:
        return network_with(lambda request: records(), destination=destination)

    async def test_an_envelope_that_pays_the_invoice_verifies(
        self, network: StellarNetwork, destination: str
    ) -> None:
        xdr, payer = signed_payment(destination)
        result = await network.verify(xdr, invoice(destination=destination))

        assert result.valid, result.reason
        assert result.payer == payer
        assert result.amount == "80.0000000"

    async def test_overpaying_still_verifies(
        self, network: StellarNetwork, destination: str
    ) -> None:
        xdr, _payer = signed_payment(destination, amount="100.0000000")
        assert (await network.verify(xdr, invoice(destination=destination))).valid

    async def test_paying_too_little_is_refused_with_both_numbers(
        self, network: StellarNetwork, destination: str
    ) -> None:
        xdr, _payer = signed_payment(destination, amount="1.0000000")
        result = await network.verify(xdr, invoice(destination=destination))

        assert not result.valid
        assert "80" in result.reason

    async def test_a_different_memo_is_refused(
        self, network: StellarNetwork, destination: str
    ) -> None:
        """Without this, any signed payment to our address would settle any invoice."""
        xdr, _payer = signed_payment(destination, memo="czsomebodyelse")
        result = await network.verify(xdr, invoice(destination=destination))

        assert not result.valid
        assert "memo" in result.reason

    async def test_paying_a_different_address_is_refused(
        self, network: StellarNetwork, destination: str
    ) -> None:
        xdr, _payer = signed_payment(sdk.Keypair.random().public_key)
        result = await network.verify(xdr, invoice(destination=destination))

        assert not result.valid
        assert "no operation pays" in result.reason

    async def test_paying_in_the_wrong_asset_is_refused(
        self, network: StellarNetwork, destination: str
    ) -> None:
        issuer = sdk.Keypair.random().public_key
        xdr, _payer = signed_payment(destination, asset=sdk.Asset("USDC", issuer))
        result = await network.verify(xdr, invoice(destination=destination))

        assert not result.valid

    async def test_an_envelope_signed_for_another_network_cannot_be_read(
        self, destination: str
    ) -> None:
        """A transaction signed for one network is valid nowhere else, and the passphrase is
        what makes that impossible to miss."""
        xdr, _payer = signed_payment(destination)
        mainnet = network_with(lambda request: records(), destination=destination, network=PUBLIC)
        assert not (await mainnet.verify(xdr, invoice(destination=destination))).valid

    @pytest.mark.parametrize("xdr", ["", "not-xdr", "AAAAAA=="])
    async def test_garbage_is_refused_rather_than_raising(
        self, network: StellarNetwork, xdr: str
    ) -> None:
        result = await network.verify(xdr, invoice())
        assert not result.valid
        assert result.reason


@requires_sdk
class TestSignatures:
    def test_a_wallets_sep53_signature_verifies(self) -> None:
        """What every browser wallet actually produces, and what sign-in used to refuse.

        A wallet asked to sign a message does not sign the message: SEP-53 has it sign
        ``SHA-256(b"Stellar Signed Message:\\n" + message)``. The gateway checked the raw
        bytes, so every genuine wallet was told "the signature does not match that account"
        — while the test below stayed green, because it signed the way the gateway checked
        rather than the way a wallet signs. This is the case that was missing.
        """
        keypair = sdk.Keypair.random()
        message = b"CodeZard sign-in\naccount: ...\nnonce: abc"
        signature = base64.b64encode(keypair.sign_message(message)).decode()

        assert StellarSignatures().verify(keypair.public_key, message, signature)

    def test_a_raw_signature_verifies_too(self) -> None:
        """For wallets that predate SEP-53. Both conventions prove the same thing over the
        same challenge, and the challenge's nonce is what makes it unrepeatable."""
        keypair = sdk.Keypair.random()
        message = b"CodeZard sign-in\naccount: ...\nnonce: abc"
        signature = base64.b64encode(keypair.sign(message)).decode()

        assert StellarSignatures().verify(keypair.public_key, message, signature)

    def test_neither_convention_saves_a_signature_by_another_key(self) -> None:
        """Accepting two conventions must not mean giving an impostor two chances."""
        keypair = sdk.Keypair.random()
        stranger = sdk.Keypair.random().public_key
        for signed in (keypair.sign_message(b"message"), keypair.sign(b"message")):
            assert not StellarSignatures().verify(
                stranger, b"message", base64.b64encode(signed).decode()
            )

    def test_the_same_signature_under_another_key_does_not(self) -> None:
        keypair = sdk.Keypair.random()
        signature = base64.b64encode(keypair.sign(b"message")).decode()

        assert not StellarSignatures().verify(
            sdk.Keypair.random().public_key, b"message", signature
        )

    def test_a_signature_over_another_message_does_not(self) -> None:
        """The whole point of a challenge: a signature is over specific text, so one taken
        from somewhere else does not authenticate anything here."""
        keypair = sdk.Keypair.random()
        signature = base64.b64encode(keypair.sign(b"message")).decode()

        assert not StellarSignatures().verify(keypair.public_key, b"other", signature)

    def test_it_reports_itself_available(self) -> None:
        assert StellarSignatures().available


class TestSignatureInput:
    def test_anything_that_is_not_a_signature_is_false_and_never_raises(self) -> None:
        """A bad address, base64 that is not base64, a signature of the wrong length: all
        the same answer to the only question being asked. True with the extra and without."""
        signatures = StellarSignatures()
        for address, signature in [
            (ACCOUNT, "not base64!!"),
            ("not-an-address", "c2ln"),
            (ACCOUNT, ""),
            (ACCOUNT, base64.b64encode(b"short").decode()),
        ]:
            assert not signatures.verify(address, b"message", signature)
