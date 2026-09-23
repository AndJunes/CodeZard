"""Stellar, reached over Horizon's REST API with the HTTP client this gateway already has.

WHAT NEEDS THE SDK AND WHAT DOES NOT — THE POINT OF THIS FILE
    Reading payments, quoting a price against the DEX and submitting a signed transaction are
    all plain HTTP against Horizon, and they are done here with ``httpx``. Only two things
    genuinely need Stellar's cryptography: checking an ed25519 signature (sign-in) and
    reading what a signed envelope actually pays (x402 verification). Those live behind a lazy
    import of ``stellar_sdk``, and a deployment without it keeps every other capability and
    says plainly which two it lacks.

    That split is why the gateway's dependency list is unchanged for anyone not billing.

NOTHING HERE RAISES FOR AN ORDINARY "NO"
    An unpaid invoice is ``None``. An envelope that does not pay is a ``VerifyResult`` saying
    why. Only a Horizon that cannot be reached raises, because that is the one case where the
    answer is genuinely unknown rather than negative.

THE MAINNET GATE IS NOT HERE, AND THAT IS DELIBERATE
    This file will talk to whatever network it is configured for. Refusing to be pointed at
    mainnet without an explicit acknowledgement is a SETTINGS decision (see
    ``config/settings.py``), because that is where an operator can see it while writing the
    value, and a guard buried in a client is a guard nobody reads.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_UP, Decimal, InvalidOperation
from typing import Any

import httpx

from gateway.domain.billing import Invoice, Money, ObservedPayment
from gateway.domain.ports import PaymentNetwork, SignatureVerifier
from gateway.domain.x402 import SettleResult, VerifyResult

logger = logging.getLogger(__name__)

TESTNET = "testnet"
PUBLIC = "public"

NETWORKS: dict[str, dict[str, str]] = {
    TESTNET: {
        "horizon": "https://horizon-testnet.stellar.org",
        "passphrase": "Test SDF Network ; September 2015",
        "x402": "stellar-testnet",
        "explorer": "https://stellar.expert/explorer/testnet",
    },
    PUBLIC: {
        "horizon": "https://horizon.stellar.org",
        "passphrase": "Public Global Stellar Network ; September 2015",
        "x402": "stellar",
        "explorer": "https://stellar.expert/explorer/public",
    },
}

STROOPS = Decimal("0.0000001")
"""Stellar's smallest unit. Every amount that goes out is quantised to it, rounding UP, so a
quote can never be a stroop short of the price it quotes."""

PAYMENT_PAGE = 50
"""How far back one lookup reads. An invoice lives half an hour; fifty incoming payments in
half an hour is far past what this ever sees, and paging further would turn one poll into
many."""


@dataclass(frozen=True, slots=True)
class StellarConfig:
    network: str = TESTNET
    horizon_url: str = ""
    destination: str = ""
    asset: str = "XLM"
    usdc_issuer: str = ""
    xlm_usd: str = ""
    """A fixed fallback rate, as a decimal string. Used when the DEX cannot be asked — a
    testnet with no liquidity, or a Horizon that is having a bad minute."""
    timeout_s: float = 10.0

    @property
    def settings(self) -> dict[str, str]:
        return NETWORKS.get(self.network, NETWORKS[TESTNET])

    @property
    def horizon(self) -> str:
        return (self.horizon_url or self.settings["horizon"]).rstrip("/")

    @property
    def passphrase(self) -> str:
        return self.settings["passphrase"]

    @property
    def x402_network(self) -> str:
        return self.settings["x402"]

    def explorer_tx(self, tx_hash: str) -> str:
        return f"{self.settings['explorer']}/tx/{tx_hash}"


class StellarNetwork(PaymentNetwork):
    """Horizon, behind :class:`PaymentNetwork`."""

    def __init__(self, config: StellarConfig, client: httpx.AsyncClient) -> None:
        self._config = config
        self._client = client

    @property
    def network(self) -> str:
        return self._config.x402_network

    @property
    def destination(self) -> str:
        return self._config.destination

    @property
    def config(self) -> StellarConfig:
        return self._config

    # ── reading ──────────────────────────────────────────────────────────────

    async def payment_for(self, invoice: Invoice) -> ObservedPayment | None:
        """The payment carrying this invoice's memo, or ``None``.

        ``join=transactions`` is what makes this one request instead of fifty-one: a payment
        record names its transaction but not its memo, and the memo is the only thing tying a
        payment to an invoice.
        """
        try:
            response = await self._client.get(
                f"{self._config.horizon}/accounts/{invoice.destination}/payments",
                params={"order": "desc", "limit": PAYMENT_PAGE, "join": "transactions"},
                timeout=self._config.timeout_s,
            )
        except httpx.HTTPError as error:
            raise PaymentNetworkError(f"Horizon could not be reached: {error}") from error
        if response.status_code == 404:
            # The receiving account does not exist on this network yet. Not an error to a
            # caller polling an invoice: nobody has paid, because nobody can.
            logger.warning(
                "the billing destination %s does not exist on %s",
                invoice.destination[:8],
                self._config.network,
            )
            return None
        if response.status_code >= 400:
            raise PaymentNetworkError(f"Horizon answered {response.status_code}")

        for record in _records(response.json()):
            payment = _payment(record)
            if payment is not None and payment.memo == invoice.memo:
                return payment
        return None

    async def quote(self, price: Money, asset: str) -> str:
        """``price`` in ``asset``, as Stellar spells amounts (seven decimals, rounded up).

        USDC is one dollar by construction — that is what the asset is — so it needs no rate
        and no network call. XLM is asked of the DEX and falls back to the configured rate,
        because an invoice that cannot be priced is an invoice that cannot be paid, and a
        stale rate is better than a blank page.
        """
        asset = (asset or self._config.asset).upper()
        if asset == "USDC":
            return _amount(price.decimal)
        rate = await self._xlm_usd()
        if rate <= 0:
            raise PaymentNetworkError(
                "there is no XLM/USD rate: the DEX could not be asked and no fallback rate "
                "is configured (GATEWAY_BILLING__XLM_USD)"
            )
        return _amount(price.decimal / rate)

    async def _xlm_usd(self) -> Decimal:
        """What one XLM is worth in USD, from the DEX when it can be asked.

        A strict-send path of one XLM into USDC is the rate the network itself will give,
        which is the right number to price against: it is what the payer's wallet would get
        if they swapped.
        """
        if self._config.usdc_issuer:
            try:
                response = await self._client.get(
                    f"{self._config.horizon}/paths/strict-send",
                    params={
                        "source_asset_type": "native",
                        "source_amount": "1",
                        "destination_assets": f"USDC:{self._config.usdc_issuer}",
                    },
                    timeout=self._config.timeout_s,
                )
                if response.status_code < 400:
                    for record in _records(response.json()):
                        amount = _decimal(record.get("destination_amount"))
                        if amount > 0:
                            return amount
            except (httpx.HTTPError, ValueError) as error:
                logger.info("the XLM/USD rate could not be read from the DEX: %s", error)
        return _decimal(self._config.xlm_usd)

    # ── writing ──────────────────────────────────────────────────────────────

    async def settle(self, transaction: str) -> SettleResult:
        """Submit a signed envelope. Plain HTTP: no SDK needed to hand Horizon bytes."""
        try:
            response = await self._client.post(
                f"{self._config.horizon}/transactions",
                data={"tx": transaction},
                headers={"content-type": "application/x-www-form-urlencoded"},
                timeout=max(self._config.timeout_s, 30.0),
            )
        except httpx.HTTPError as error:
            return SettleResult(
                False, self.network, reason=f"Horizon could not be reached: {error}"
            )
        body = _json(response)
        if response.status_code >= 400:
            return SettleResult(False, self.network, reason=_horizon_error(body))
        if not body.get("successful", True):
            return SettleResult(
                False,
                self.network,
                transaction=str(body.get("hash") or ""),
                reason="the transaction was included and failed",
            )
        return SettleResult(
            True,
            self.network,
            transaction=str(body.get("hash") or ""),
            payer=str(body.get("source_account") or ""),
        )

    async def verify(self, transaction: str, invoice: Invoice) -> VerifyResult:
        """Read the envelope back and check it really pays this invoice.

        Every number is taken from the SIGNED bytes, never from what the caller said around
        them. This is the one call that needs Stellar's own XDR, and a deployment without the
        dependency answers "cannot verify" instead of "valid".
        """
        try:
            from stellar_sdk import Keypair, TransactionEnvelope
            from stellar_sdk.operation import Payment as PaymentOperation
        except ImportError:
            return VerifyResult(
                False,
                reason=(
                    "this gateway cannot read signed transactions: the Stellar dependency is not "
                    'installed (pip install -e ".[stellar]")'
                ),
            )
        try:
            envelope = TransactionEnvelope.from_xdr(transaction, self._config.passphrase)
        except Exception as error:  # the SDK raises a dozen different things for bad input
            return VerifyResult(False, reason=f"the transaction could not be read: {error}")

        inner = envelope.transaction
        payer = _account_id(inner.source)
        memo = _memo_text(inner.memo)
        if memo != invoice.memo:
            return VerifyResult(
                False,
                payer=payer,
                reason=f"the transaction's memo is {memo!r}, not {invoice.memo!r}",
            )
        if not _signed_by(envelope, payer, Keypair):
            # `from_xdr` takes a passphrase and does NOT check it: the XDR does not carry one,
            # so parsing an envelope signed for another network succeeds and every field reads
            # correctly. What the passphrase actually decides is `envelope.hash()`, and that
            # is what the signature is over — so checking the signature here is the only thing
            # that ties this envelope to THIS network. Without it a mainnet gateway would call
            # a testnet-signed payment valid, hand over the goods, and only find out at
            # submission that it can never settle.
            return VerifyResult(
                False,
                payer=payer,
                reason=(
                    "the transaction is not signed by its source account for "
                    f"{self._config.network}"
                ),
            )

        owed = _decimal(invoice.amount)
        for operation in inner.operations:
            if not isinstance(operation, PaymentOperation):
                continue
            if _account_id(operation.destination) != invoice.destination:
                continue
            if _asset_code(operation.asset) != invoice.asset.upper():
                continue
            paid = _decimal(operation.amount)
            if paid >= owed:
                return VerifyResult(True, payer=payer, amount=_amount(paid))
            return VerifyResult(
                False,
                payer=payer,
                amount=_amount(paid),
                reason=f"it pays {_amount(paid)} and the price is {invoice.amount}",
            )
        return VerifyResult(
            False, payer=payer, reason=f"no operation pays {invoice.asset} to {invoice.destination}"
        )


class PaymentNetworkError(Exception):
    """The network could not be asked. Distinct from "the answer is no"."""


class StellarSignatures(SignatureVerifier):
    """ed25519 verification through ``stellar_sdk.Keypair``.

    Not reimplemented here. Hand-rolling signature verification for a system that guards a
    balance is the kind of cleverness that is wrong once and wrong silently, and the SDK is
    already an optional dependency for reading envelopes.
    """

    def __init__(self) -> None:
        self._keypair: Any | None = None
        try:
            from stellar_sdk import Keypair
        except ImportError:
            logger.warning(
                "stellar_sdk is not installed: sign-in is unavailable on this gateway "
                '(pip install -e ".[stellar]")'
            )
            return
        self._keypair = Keypair

    @property
    def available(self) -> bool:
        return self._keypair is not None

    def verify(self, address: str, message: bytes, signature: str) -> bool:
        """``False`` for anything that is not a genuine signature, including malformed input.

        Everything is caught: a bad address, base64 that is not base64, a signature of the
        wrong length. They are all the same answer to the only question being asked.
        """
        if self._keypair is None:
            return False
        import base64
        import binascii

        try:
            raw = base64.b64decode(signature, validate=True)
        except (binascii.Error, ValueError):
            return False
        try:
            self._keypair.from_public_key(address).verify(message, raw)
        except Exception:
            return False
        return True


# ── reading Horizon's JSON ───────────────────────────────────────────────────


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _records(body: Any) -> list[dict[str, Any]]:
    embedded = body.get("_embedded") if isinstance(body, dict) else None
    records = embedded.get("records") if isinstance(embedded, dict) else None
    return [r for r in records if isinstance(r, dict)] if isinstance(records, list) else []


def _payment(record: dict[str, Any]) -> ObservedPayment | None:
    """One Horizon payment record, or ``None`` when it is not a payment to read.

    ``create_account`` counts: funding an account IS how a first payment arrives on Stellar,
    and it carries the amount under a different name.
    """
    kind = record.get("type")
    if kind not in ("payment", "create_account"):
        return None
    transaction = record.get("transaction")
    memo = ""
    if isinstance(transaction, dict) and transaction.get("memo_type") == "text":
        memo = str(transaction.get("memo") or "")
    return ObservedPayment(
        tx_hash=str(record.get("transaction_hash") or ""),
        payer=str(record.get("from") or record.get("funder") or ""),
        destination=str(record.get("to") or record.get("account") or ""),
        asset=(
            "XLM"
            if record.get("asset_type", "native") == "native"
            else str(record.get("asset_code") or "?").upper()
        ),
        amount=str(record.get("amount") or record.get("starting_balance") or "0"),
        memo=memo,
        at=_time(record.get("created_at")),
    )


def _horizon_error(body: dict[str, Any]) -> str:
    """Horizon's failure, said in a sentence rather than in result codes.

    The useful part is buried three levels deep in `extras.result_codes`, and the top-level
    `title` on its own is "Transaction Failed" for every possible cause.
    """
    extras = body.get("extras")
    codes = extras.get("result_codes") if isinstance(extras, dict) else None
    if isinstance(codes, dict):
        operations = codes.get("operations")
        detail = ", ".join(str(code) for code in operations) if isinstance(operations, list) else ""
        headline = str(codes.get("transaction") or "the transaction failed")
        return f"{headline} ({detail})" if detail else headline
    return str(body.get("title") or "Horizon refused the transaction")


def _time(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return datetime.now(UTC)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _decimal(value: Any) -> Decimal:
    try:
        amount = Decimal(str(value or "0"))
    except (InvalidOperation, ValueError):
        return Decimal(0)
    return amount if amount.is_finite() else Decimal(0)


def _amount(value: Decimal) -> str:
    """A Stellar amount: seven decimals, rounded UP so a quote is never short."""
    return str(value.quantize(STROOPS, rounding=ROUND_UP))


def _signed_by(envelope: Any, address: str, keypair: Any) -> bool:
    """Did ``address`` sign this envelope, for the network the envelope was parsed with?

    ``envelope.hash()`` mixes in the network passphrase, so a signature made for another
    network simply does not verify here — which is the whole point of asking.

    Only the SOURCE account's own signature counts. A multi-signature account whose payment
    is authorised by other signers would be rejected, and that is the right trade for now:
    telling those apart means asking Horizon for the account's signer list and weights, and
    an x402 payer signing its own payment is the case this serves. A rejected multisig payer
    gets a refusal that says what is missing rather than a silent acceptance.
    """
    try:
        digest = envelope.hash()
        verifier = keypair.from_public_key(address)
    except Exception:
        return False
    for signature in getattr(envelope, "signatures", ()):
        try:
            verifier.verify(digest, signature.signature)
        except Exception:
            continue
        return True
    return False


def _account_id(account: Any) -> str:
    """The ``G…`` address of a source or a destination, whatever shape the SDK hands over.

    Both are ``MuxedAccount`` objects, and ``str()`` on one is NOT its address — it is the
    dataclass repr. Comparing that against an address silently never matches, so every
    payment in a signed envelope read as "no operation pays this", and every x402
    verification would have failed for a reason that looked like the payer's fault.
    """
    return str(getattr(account, "account_id", None) or account)


def _asset_code(asset: Any) -> str:
    code = getattr(asset, "code", None)
    if code in (None, "XLM") and getattr(asset, "issuer", None) is None:
        return "XLM"
    return str(code or "XLM").upper()


def _memo_text(memo: Any) -> str:
    value = getattr(memo, "memo_text", None)
    if value is None:
        return ""
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)


__all__ = [
    "NETWORKS",
    "PUBLIC",
    "TESTNET",
    "PaymentNetworkError",
    "StellarConfig",
    "StellarNetwork",
    "StellarSignatures",
]
