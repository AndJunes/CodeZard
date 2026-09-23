"""HTTP 402: the wire format, and what happens when the header lies.

`X-PAYMENT` is unauthenticated input on a path reached before anything else has checked
anything, so most of this file is about what it does with garbage.
"""

import base64
import json
from collections.abc import Iterator
from dataclasses import replace

import pytest

from gateway.application.billing_service import BillingService
from gateway.application.x402_service import X402Service
from gateway.domain.billing import Invoice, Money, default_catalog, now_utc
from gateway.domain.x402 import (
    MAX_PAYLOAD_BYTES,
    SCHEME,
    X402_VERSION,
    PaymentPayload,
    PaymentRequired,
    PaymentRequirements,
    SettleResult,
    Supported,
    VerifyResult,
    X402Error,
)
from gateway.infrastructure.billing.sqlite_store import SqliteBillingStore
from tests.unit.test_billing_service import FakeNetwork

ACCOUNT = "G" + "A" * 55
PAYER = "G" + "C" * 55


def header_for(payload: dict[str, object]) -> str:
    return base64.b64encode(json.dumps(payload).encode()).decode()


def valid_payload(invoice_id: str = "czabc", transaction: str = "AAAA") -> dict[str, object]:
    return {
        "x402Version": X402_VERSION,
        "scheme": SCHEME,
        "network": "stellar-testnet",
        "payload": {"transaction": transaction, "invoice": invoice_id},
    }


class TestPaymentPayload:
    def test_a_well_formed_header_decodes(self) -> None:
        payload = PaymentPayload.decode(header_for(valid_payload()))
        assert payload.scheme == SCHEME
        assert payload.transaction == "AAAA"
        assert payload.payload["invoice"] == "czabc"

    @pytest.mark.parametrize(
        "header", ["", "   ", "not base64!!", "YWJj", base64.b64encode(b"[1,2,3]").decode()]
    )
    def test_garbage_is_refused_rather_than_half_read(self, header: str) -> None:
        with pytest.raises(X402Error):
            PaymentPayload.decode(header)

    def test_an_oversized_header_is_refused_before_it_is_decoded(self) -> None:
        with pytest.raises(X402Error, match="too large"):
            PaymentPayload.decode("A" * (MAX_PAYLOAD_BYTES + 1))

    def test_a_different_protocol_version_fails_loudly(self) -> None:
        """A client written against another version must not half-work."""
        with pytest.raises(X402Error, match="not supported"):
            PaymentPayload.decode(header_for({**valid_payload(), "x402Version": 99}))

    def test_a_missing_transaction_reads_as_empty_and_not_as_a_crash(self) -> None:
        payload = PaymentPayload.decode(
            header_for(
                {
                    "x402Version": X402_VERSION,
                    "scheme": SCHEME,
                    "network": "n",
                    "payload": {"transaction": 42},
                }
            )
        )
        assert payload.transaction == ""

    def test_a_payload_that_is_not_an_object_reads_as_empty(self) -> None:
        payload = PaymentPayload.decode(
            header_for(
                {"x402Version": X402_VERSION, "scheme": SCHEME, "network": "n", "payload": "nope"}
            )
        )
        assert payload.payload == {}


class TestWireShapes:
    def test_requirements_go_out_in_the_protocol_s_camel_case(self) -> None:
        """The rest of this code base is snake_case. The wire is not, and the translation
        happens in one place."""
        body = PaymentRequirements(
            SCHEME, "stellar-testnet", "10", "/runs", "tokens", ACCOUNT, "XLM"
        ).as_json()
        assert set(body) >= {"maxAmountRequired", "payTo", "maxTimeoutSeconds", "mimeType"}

    def test_a_402_document_carries_the_version_and_what_would_be_accepted(self) -> None:
        body = PaymentRequired(
            "pay first", (PaymentRequirements(SCHEME, "n", "1", "/r", "d", ACCOUNT, "XLM"),)
        ).as_json()
        assert body["x402Version"] == X402_VERSION
        assert body["error"] == "pay first"
        assert len(body["accepts"]) == 1

    def test_a_settlement_round_trips_through_its_header(self) -> None:
        result = SettleResult(True, "stellar-testnet", transaction="hash", payer=PAYER)
        decoded = json.loads(base64.b64decode(result.encode()))
        assert decoded["success"] is True
        assert decoded["transaction"] == "hash"

    def test_a_verify_result_names_the_reason_it_failed(self) -> None:
        assert VerifyResult(False, reason="too little").as_json()["invalidReason"] == "too little"
        assert VerifyResult(True).as_json()["invalidReason"] is None

    def test_supported_lists_scheme_and_network_pairs(self) -> None:
        body = Supported(((SCHEME, "stellar-testnet"),)).as_json()
        assert body["kinds"] == [{"x402Version": 1, "scheme": SCHEME, "network": "stellar-testnet"}]


# ── the service ──────────────────────────────────────────────────────────────


@pytest.fixture
def store() -> Iterator[SqliteBillingStore]:
    """Closed on the way out: an in-memory database still holds a connection."""
    opened = SqliteBillingStore()
    yield opened
    opened.close()


@pytest.fixture
def network() -> FakeNetwork:
    return FakeNetwork()


@pytest.fixture
def service(store: SqliteBillingStore, network: FakeNetwork) -> X402Service:
    billing = BillingService(store, network, default_catalog(), "XLM")  # type: ignore[arg-type]
    return X402Service(billing, network, store, Money.parse("0.50"))  # type: ignore[arg-type]


def requirements_for(invoice: Invoice) -> PaymentRequirements:
    return PaymentRequirements(
        SCHEME,
        "stellar-testnet",
        invoice.amount,
        "/runs",
        "",
        invoice.destination,
        invoice.asset,
        extra={"memo": invoice.memo, "invoice": invoice.id},
    )


class TestRequirements:
    async def test_a_quote_creates_the_invoice_that_will_match_the_payment(
        self, service: X402Service, store: SqliteBillingStore
    ) -> None:
        """The memo is how a payment is tied back to an account, and a 402 that is paid and
        then abandoned should still credit the payer rather than vanish."""
        requirements, invoice = await service.requirements(PAYER, "/runs")

        assert requirements.extra["memo"] == invoice.memo
        assert await store.invoice(invoice.id) is not None
        assert invoice.tokens > 0

    async def test_a_challenge_says_why_and_what_would_be_accepted(
        self, service: X402Service
    ) -> None:
        document = await service.challenge(PAYER, "this gateway charges for runs")
        assert document.error == "this gateway charges for runs"
        assert document.accepts[0].scheme == SCHEME

    async def test_it_announces_the_scheme_and_network_it_speaks(
        self, service: X402Service
    ) -> None:
        assert service.supported.kinds == ((SCHEME, "stellar-testnet"),)


class TestVerify:
    async def test_a_good_envelope_verifies(self, service: X402Service) -> None:
        _requirements, invoice = await service.requirements(PAYER)
        result = await service.verify(
            PaymentPayload(SCHEME, "stellar-testnet", {"transaction": "AAAA"}),
            requirements_for(invoice),
        )
        assert result.valid

    async def test_another_scheme_is_refused(self, service: X402Service) -> None:
        _requirements, invoice = await service.requirements(PAYER)
        result = await service.verify(
            PaymentPayload("upto", "stellar-testnet", {"transaction": "AAAA"}),
            requirements_for(invoice),
        )
        assert not result.valid
        assert "not supported" in result.reason

    async def test_another_network_is_refused(self, service: X402Service) -> None:
        _requirements, invoice = await service.requirements(PAYER)
        result = await service.verify(
            PaymentPayload(SCHEME, "base-sepolia", {"transaction": "AAAA"}),
            requirements_for(invoice),
        )
        assert not result.valid
        assert "stellar-testnet" in result.reason

    async def test_a_payload_with_no_transaction_is_refused(self, service: X402Service) -> None:
        _requirements, invoice = await service.requirements(PAYER)
        result = await service.verify(
            PaymentPayload(SCHEME, "stellar-testnet", {}), requirements_for(invoice)
        )
        assert not result.valid

    async def test_a_requirement_naming_no_invoice_is_refused(self, service: X402Service) -> None:
        """The amount is read from the store, never from the requirement: the requirement
        arrived over the wire, so an amount taken from it is an amount the caller chose."""
        result = await service.verify(
            PaymentPayload(SCHEME, "stellar-testnet", {"transaction": "AAAA"}),
            PaymentRequirements(
                SCHEME, "stellar-testnet", "0.0000001", "/runs", "", ACCOUNT, "XLM"
            ),
        )
        assert not result.valid
        assert "unknown or expired" in result.reason

    async def test_an_already_settled_invoice_cannot_be_paid_twice(
        self, service: X402Service, store: SqliteBillingStore
    ) -> None:
        _requirements, invoice = await service.requirements(PAYER)
        await store.put_invoice(invoice.paid("hash", PAYER))

        result = await service.verify(
            PaymentPayload(SCHEME, "stellar-testnet", {"transaction": "AAAA"}),
            requirements_for(invoice),
        )
        assert not result.valid


class TestSettle:
    async def test_settling_credits_the_address_that_signed(
        self, service: X402Service, store: SqliteBillingStore, network: FakeNetwork
    ) -> None:
        """In a 402 there is no session: whoever signed is whoever is buying."""
        _requirements, invoice = await service.requirements("")
        result = await service.settle(
            PaymentPayload(SCHEME, "stellar-testnet", {"transaction": "AAAA"}),
            requirements_for(invoice),
        )

        assert result.success
        billing = BillingService(store, network, default_catalog(), "XLM")  # type: ignore[arg-type]
        assert (await billing.balance(ACCOUNT)).purchased == invoice.tokens

    async def test_it_verifies_again_before_submitting(self, service: X402Service) -> None:
        """The two endpoints are independent. A facilitator that settles an unverified
        envelope is one that submits whatever it is handed."""
        _requirements, invoice = await service.requirements(PAYER)
        result = await service.settle(
            PaymentPayload("upto", "stellar-testnet", {"transaction": "AAAA"}),
            requirements_for(invoice),
        )
        assert not result.success
        assert "not supported" in result.reason

    async def test_a_failed_submission_credits_nothing(
        self, service: X402Service, store: SqliteBillingStore, network: FakeNetwork
    ) -> None:
        async def refuse(transaction: str) -> SettleResult:
            return SettleResult(False, "stellar-testnet", reason="tx_bad_seq")

        network.settle = refuse  # type: ignore[method-assign]
        _requirements, invoice = await service.requirements(PAYER)

        result = await service.settle(
            PaymentPayload(SCHEME, "stellar-testnet", {"transaction": "AAAA"}),
            requirements_for(invoice),
        )

        assert not result.success
        billing = BillingService(store, network, default_catalog(), "XLM")  # type: ignore[arg-type]
        assert (await billing.balance(ACCOUNT)).total == 0


class TestRedeem:
    async def test_a_paid_header_names_the_account_for_the_request(
        self, service: X402Service
    ) -> None:
        _requirements, invoice = await service.requirements("")
        account, settlement = await service.redeem(header_for(valid_payload(invoice.id)), "/runs")

        assert account == ACCOUNT
        assert settlement.success

    async def test_a_header_naming_no_invoice_is_refused(self, service: X402Service) -> None:
        with pytest.raises(X402Error, match="does not name an invoice"):
            await service.redeem(header_for(valid_payload("czneverissued")), "/runs")

    async def test_a_header_that_cannot_be_settled_is_refused(
        self, service: X402Service, network: FakeNetwork
    ) -> None:
        async def refuse(transaction: str) -> SettleResult:
            return SettleResult(False, "stellar-testnet", reason="tx_insufficient_balance")

        network.settle = refuse  # type: ignore[method-assign]
        _requirements, invoice = await service.requirements("")

        with pytest.raises(X402Error, match="tx_insufficient_balance"):
            await service.redeem(header_for(valid_payload(invoice.id)), "/runs")

    async def test_an_expired_quote_cannot_be_redeemed(
        self, service: X402Service, store: SqliteBillingStore
    ) -> None:
        _requirements, invoice = await service.requirements("")
        await store.put_invoice(
            replace(invoice, id="czstale0000000000000000", expires_at=now_utc())
        )
        stale = await store.invoice("czstale0000000000000000")
        assert stale is not None

        with pytest.raises(X402Error):
            await service.redeem(header_for(valid_payload(stale.id)), "/runs")
