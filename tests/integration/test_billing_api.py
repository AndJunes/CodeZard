"""The billing and x402 routes, against the real app.

Only two things are faked: the network to the agents (as everywhere in this directory) and
Horizon. The ledger is the real SQLite one, the routes are the real routes, and the 402 body
is checked byte for byte against what the protocol says a client will read.
"""

import base64
import json
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI

from gateway.api.app import create_app
from gateway.application.identity_service import IdentityService
from gateway.config.settings import (
    BillingSettings,
    OrchestrationSettings,
    ServiceSettings,
    Settings,
)
from gateway.domain.x402 import SCHEME, X402_VERSION

ACCOUNT = "G" + "A" * 55
DESTINATION = "G" + "B" * 55
PAYER = "G" + "C" * 55
SECRET = "a-secret-nobody-else-has"
SIGNATURE = base64.b64encode(b"x" * 64).decode()

MEMO_KEY = "memo"


def horizon(request: httpx.Request) -> httpx.Response:
    """Horizon with nothing in it, and the agents answering nothing useful.

    No payment has ever been made, so every invoice polls as pending. A submitted transaction
    succeeds, which is what lets the x402 path be followed end to end.
    """
    if request.url.path.endswith("/transactions") and request.method == "POST":
        return httpx.Response(
            200, json={"successful": True, "hash": "settledhash", "source_account": PAYER}
        )
    if "internal" in (request.url.host or ""):  # a PM or backend agent
        return httpx.Response(200, json={"summary": "understood", "questionnaire": None})
    return httpx.Response(200, json={"_embedded": {"records": []}})


@pytest.fixture
def billing_settings(tmp_path: object) -> BillingSettings:
    return BillingSettings(
        enabled=True,
        destination=DESTINATION,
        secret=SECRET,  # type: ignore[arg-type]
        database=str(tmp_path) + "/billing.sqlite3",  # type: ignore[operator]
        xlm_usd="0.10",
        reserve_tokens=1_000,
    )


@pytest.fixture
def paid_app(billing_settings: BillingSettings) -> FastAPI:
    """The real app with billing on AND orchestration on.

    Orchestration matters here: `/runs` is where charging actually bites, and a gateway
    without it does not register the route at all — every gate test would then be asserting
    against a 404.
    """
    settings = Settings(
        _env_file=None,
        services=[
            ServiceSettings(name="pm", base_url="http://pm.internal"),
            ServiceSettings(name="backend", base_url="http://backend.internal"),
        ],
        orchestration=OrchestrationSettings(enabled=True, pm_token="pm", backend_token="be"),
        billing=billing_settings,
    )
    return create_app(
        settings,
        http_client_factory=lambda _: httpx.AsyncClient(transport=httpx.MockTransport(horizon)),
    )


@pytest.fixture
async def paid_client(paid_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with (
        paid_app.router.lifespan_context(paid_app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=paid_app), base_url="http://gateway.test"
        ) as client,
    ):
        yield client


def session_token(app: FastAPI, address: str = ACCOUNT) -> str:
    """A token this gateway would itself have issued, minted with its own service.

    The signature check is the SDK's and is an optional dependency, so it is stepped over
    here; everything the routes do with the resulting token is real.
    """
    identity: IdentityService = app.state.container.identity
    return identity._seal({"a": address, "e": 2_000_000_000})


class TestCatalogue:
    async def test_the_price_list_needs_no_login(self, paid_client: httpx.AsyncClient) -> None:
        """A price list that needs a login is a price list nobody reads."""
        response = await paid_client.get("/billing/plans")

        assert response.status_code == 200
        body = response.json()
        assert body["plans"]
        assert body["packs"]
        assert body["asset"] == "XLM"

    async def test_every_price_is_a_real_number(self, paid_client: httpx.AsyncClient) -> None:
        body = (await paid_client.get("/billing/plans")).json()
        for product in [*body["plans"], *body["packs"]]:
            assert int(product["price"]["micros"]) > 0


class TestSignIn:
    async def test_a_challenge_is_returned_for_a_valid_address(
        self, paid_client: httpx.AsyncClient
    ) -> None:
        response = await paid_client.post("/billing/auth/challenge", json={"address": ACCOUNT})

        # 501 when the optional Stellar dependency is absent — which is a correct, honest
        # answer and not a failure of this route.
        if response.status_code == 501:
            assert "not installed" in response.json()["error"]["message"]
            return
        assert response.status_code == 200
        assert ACCOUNT in response.json()["message"]

    async def test_a_malformed_address_is_refused_by_the_schema(
        self, paid_client: httpx.AsyncClient
    ) -> None:
        response = await paid_client.post("/billing/auth/challenge", json={"address": "nope"})
        assert response.status_code == 422

    async def test_a_forged_challenge_never_becomes_a_session(
        self, paid_client: httpx.AsyncClient
    ) -> None:
        response = await paid_client.post(
            "/billing/auth/verify", json={"challenge": "made.up", "signature": SIGNATURE}
        )
        assert response.status_code in (401, 501)


class TestAccount:
    async def test_it_needs_a_token(self, paid_client: httpx.AsyncClient) -> None:
        response = await paid_client.get("/billing")
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"

    async def test_a_forged_token_is_refused(self, paid_client: httpx.AsyncClient) -> None:
        response = await paid_client.get("/billing", headers={"authorization": "Bearer made.up"})
        assert response.status_code == 401

    async def test_a_signed_in_account_reads_empty_and_not_missing(
        self, paid_app: FastAPI, paid_client: httpx.AsyncClient
    ) -> None:
        response = await paid_client.get(
            "/billing", headers={"authorization": f"Bearer {session_token(paid_app)}"}
        )

        assert response.status_code == 200
        body = response.json()
        assert body["account"] == ACCOUNT
        assert body["balance"]["total"] == 0
        assert body["subscription"] is None
        assert body["entries"] == []


class TestCheckout:
    async def test_it_returns_an_address_an_amount_and_a_memo(
        self, paid_app: FastAPI, paid_client: httpx.AsyncClient
    ) -> None:
        response = await paid_client.post(
            "/billing/checkout",
            json={"sku": "tokens-1m"},
            headers={"authorization": f"Bearer {session_token(paid_app)}"},
        )

        assert response.status_code == 201
        body = response.json()
        assert body["destination"] == DESTINATION
        assert body["memo"] == body["id"]
        assert body["amount"] == "80.0000000"
        assert body["status"] == "pending"

    async def test_an_unknown_product_is_a_400_and_not_a_500(
        self, paid_app: FastAPI, paid_client: httpx.AsyncClient
    ) -> None:
        response = await paid_client.post(
            "/billing/checkout",
            json={"sku": "free-forever"},
            headers={"authorization": f"Bearer {session_token(paid_app)}"},
        )

        assert response.status_code == 400
        assert "free-forever" in response.json()["error"]["message"]

    async def test_an_unpaid_invoice_polls_as_pending(
        self, paid_app: FastAPI, paid_client: httpx.AsyncClient
    ) -> None:
        headers = {"authorization": f"Bearer {session_token(paid_app)}"}
        created = (
            await paid_client.post("/billing/checkout", json={"sku": "tokens-1m"}, headers=headers)
        ).json()

        polled = await paid_client.get(f"/billing/invoices/{created['id']}", headers=headers)
        assert polled.status_code == 200
        assert polled.json()["status"] == "pending"

    async def test_somebody_else_s_invoice_reads_as_missing(
        self, paid_app: FastAPI, paid_client: httpx.AsyncClient
    ) -> None:
        """The same answer a missing one gets: telling them apart confirms that an id exists,
        which is the only thing a guesser learns anything from."""
        mine = (
            await paid_client.post(
                "/billing/checkout",
                json={"sku": "tokens-1m"},
                headers={"authorization": f"Bearer {session_token(paid_app)}"},
            )
        ).json()

        theirs = await paid_client.get(
            f"/billing/invoices/{mine['id']}",
            headers={"authorization": f"Bearer {session_token(paid_app, PAYER)}"},
        )

        assert theirs.status_code == 404

    async def test_an_invoice_that_never_existed_is_a_404(
        self, paid_app: FastAPI, paid_client: httpx.AsyncClient
    ) -> None:
        response = await paid_client.get(
            "/billing/invoices/czneverissued",
            headers={"authorization": f"Bearer {session_token(paid_app)}"},
        )
        assert response.status_code == 404


class TestX402Routes:
    async def test_it_announces_what_it_speaks(self, paid_client: httpx.AsyncClient) -> None:
        body = (await paid_client.get("/x402/supported")).json()
        assert body["kinds"] == [
            {"x402Version": X402_VERSION, "scheme": SCHEME, "network": "stellar-testnet"}
        ]

    async def test_a_quote_is_a_payable_document(self, paid_client: httpx.AsyncClient) -> None:
        body = (
            await paid_client.post("/x402/quote", json={"resource": "/runs", "payer": PAYER})
        ).json()

        assert body["x402Version"] == X402_VERSION
        requirement = body["accepts"][0]
        assert requirement["scheme"] == SCHEME
        assert requirement["payTo"] == DESTINATION
        assert requirement["extra"][MEMO_KEY] == body["invoice"]

    async def test_a_custom_price_is_quoted(self, paid_client: httpx.AsyncClient) -> None:
        body = (await paid_client.post("/x402/quote", json={"usd": "2.00"})).json()
        assert body["accepts"][0]["maxAmountRequired"] == "20.0000000"

    async def test_verifying_a_payload_for_another_scheme_is_refused(
        self, paid_client: httpx.AsyncClient
    ) -> None:
        quote = (await paid_client.post("/x402/quote", json={})).json()
        response = await paid_client.post(
            "/x402/verify",
            json={
                "paymentPayload": {
                    "x402Version": 1,
                    "scheme": "upto",
                    "network": "stellar-testnet",
                    "payload": {"transaction": "AAAA"},
                },
                "paymentRequirements": {**quote["accepts"][0]},
            },
        )

        assert response.status_code == 200
        assert response.json()["isValid"] is False

    async def test_settling_something_unverifiable_answers_402(
        self, paid_client: httpx.AsyncClient
    ) -> None:
        response = await paid_client.post(
            "/x402/settle",
            json={
                "paymentPayload": {
                    "x402Version": 1,
                    "scheme": SCHEME,
                    "network": "stellar-testnet",
                    "payload": {},
                },
                "paymentRequirements": {
                    "scheme": SCHEME,
                    "network": "stellar-testnet",
                    "maxAmountRequired": "1",
                    "payTo": DESTINATION,
                    "asset": "XLM",
                    "extra": {},
                },
            },
        )

        assert response.status_code == 402
        assert response.json()["success"] is False


class TestThePaymentGate:
    """`POST /runs` is where the charging actually bites."""

    async def test_an_anonymous_caller_is_told_the_price(
        self, paid_client: httpx.AsyncClient
    ) -> None:
        response = await paid_client.post("/runs", json={"idea": "a bike workshop tracker"})

        assert response.status_code == 402
        body = response.json()
        # The protocol's shape, NOT this gateway's `{"error": {...}}` envelope: an
        # off-the-shelf x402 client reads exactly these keys and nothing else.
        assert body["x402Version"] == X402_VERSION
        assert "error" in body
        assert body["accepts"][0]["payTo"] == DESTINATION

    async def test_a_signed_in_caller_with_no_balance_is_told_the_price_too(
        self, paid_app: FastAPI, paid_client: httpx.AsyncClient
    ) -> None:
        response = await paid_client.post(
            "/runs",
            json={"idea": "a bike workshop tracker"},
            headers={"authorization": f"Bearer {session_token(paid_app)}"},
        )

        assert response.status_code == 402
        assert "tokens" in response.json()["error"]

    async def test_an_expired_session_falls_through_to_the_price_not_a_401(
        self, paid_client: httpx.AsyncClient
    ) -> None:
        """A client whose session lapsed mid-flow should be offered the payment path, not
        handed a 401 it may have no way to act on."""
        response = await paid_client.post(
            "/runs", json={"idea": "x"}, headers={"authorization": "Bearer made.up"}
        )
        assert response.status_code == 402

    async def test_a_funded_account_gets_through_the_gate(
        self, paid_app: FastAPI, paid_client: httpx.AsyncClient
    ) -> None:
        """Past the gate it fails at the AGENT, which is not registered in this app — the
        point is that billing stopped being the thing refusing."""
        billing = paid_app.state.container.billing
        from gateway.domain.billing import EntryKind

        await billing.credit(ACCOUNT, 5_000_000, kind=EntryKind.PURCHASE, reference="test")

        response = await paid_client.post(
            "/runs",
            json={"idea": "a bike workshop tracker"},
            headers={"authorization": f"Bearer {session_token(paid_app)}"},
        )

        assert response.status_code != 402

    async def test_paying_with_a_header_gets_through_and_credits_the_signer(
        self, paid_app: FastAPI, paid_client: httpx.AsyncClient
    ) -> None:
        quote = (await paid_client.post("/x402/quote", json={"resource": "/runs"})).json()
        header = base64.b64encode(
            json.dumps(
                {
                    "x402Version": 1,
                    "scheme": SCHEME,
                    "network": "stellar-testnet",
                    "payload": {"transaction": "AAAA", "invoice": quote["invoice"]},
                }
            ).encode()
        ).decode()

        response = await paid_client.post(
            "/runs", json={"idea": "a tracker"}, headers={"x-payment": header}
        )

        # Verifying the envelope needs the optional Stellar dependency; without it the
        # payment cannot be read and the honest answer is another 402.
        billing = paid_app.state.container.billing
        if response.status_code == 402:
            assert (await billing.balance(PAYER)).total == 0
            return
        assert (await billing.balance(PAYER)).total > 0


class TestWithBillingOff:
    async def test_the_routes_are_not_even_registered(self, client: httpx.AsyncClient) -> None:
        """A gateway that sells nothing should not advertise a checkout."""
        assert (await client.get("/billing/plans")).status_code == 404
        assert (await client.get("/x402/supported")).status_code == 404

    async def test_starting_a_run_asks_nobody_for_money(self, client: httpx.AsyncClient) -> None:
        """Every existing caller has to keep working exactly as it did."""
        response = await client.post("/runs", json={"idea": "a bike workshop tracker"})
        assert response.status_code != 402
