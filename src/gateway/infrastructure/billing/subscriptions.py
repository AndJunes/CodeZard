"""Reading the subscriptions contract, so the chain decides who is subscribed.

WHY THE CHAIN AND NOT OUR OWN TABLE
    A subscription used to be an invoice, a memo, and a poller that noticed the payment and
    wrote a row — three steps, any one of which can be the one that does not happen. The
    contract does the payment and the record in a single invocation, so either both happened
    or neither did, and what it says is checkable by the subscriber without asking us.

    So this is the authority for PAID plans. The ledger in `sqlite_store.py` keeps counting
    tokens, which is what it is good at; it is no longer the thing that decides what somebody
    bought.

    The free plan is not here and never will be. It costs nothing, so there is nothing to
    prove and nobody to prove it to — putting it on a public ledger would spend real fees
    writing down that somebody got something for free.

READ ONLY, AND SIMULATED
    Every call here is a simulation: unsigned, never submitted, no fee, no ledger entry. It
    is the Soroban equivalent of a SELECT. Subscribing is the SUBSCRIBER's transaction, signed
    in their own wallet — this gateway holds no key and could not write here if it wanted to.

NEVER RAISES FOR AN ORDINARY "NO"
    Not subscribed is ``None``. A contract that has not been configured is "off". Only a
    genuinely unusable RPC raises, because that is the one case where the answer is unknown
    rather than negative — and an unknown must not read as "your subscription has lapsed".
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from gateway.domain.billing import BillingError

logger = logging.getLogger(__name__)

ADDRESS = re.compile(r"\AG[A-Z2-7]{55}\Z")
"""Checked before the SDK is asked to build one.

Not belt and braces: `Address()` raises on anything malformed, and that exception would come
back as "the contract could not be read" — which reads as an RPC problem and would be logged
as one. A string that is not an address is simply not subscribed."""

RPC_URLS = {
    "stellar-testnet": "https://soroban-testnet.stellar.org",
    "stellar": "https://mainnet.sorobanrpc.com",
}


@dataclass(frozen=True, slots=True)
class OnChainSubscription:
    """One subscriber's standing, as the contract records it."""

    plan: str
    started: datetime
    expires: datetime
    periods: int

    def active(self, now: datetime) -> bool:
        return now < self.expires

    @property
    def period_key(self) -> str:
        """Names THIS period, so the token grant behind it lands exactly once.

        Keyed on ``started`` and not on ``expires``: renewing early extends the end and leaves
        the start alone, which is precisely so that paying twice inside one period cannot be
        made to grant twice.
        """
        return f"{self.plan}:{int(self.started.timestamp())}"

    def as_json(self) -> dict[str, Any]:
        return {
            "plan": self.plan,
            "started": self.started.isoformat(),
            "expires": self.expires.isoformat(),
            "periods": self.periods,
        }


class SubscriptionContractError(BillingError):
    """The contract could not be asked, or could not be written to.

    A ``BillingError`` so it reaches the gateway's one error handler and comes back in the
    house envelope with a 400 and its own sentence — "the network refused the transaction:
    …" is something a person can act on, and a 500 is not.

    Different from "they are not subscribed", which is ``None`` and not an error at all.
    """


class SubscriptionContract:
    """The deployed contract, read through Soroban RPC.

    Needs the optional ``[stellar]`` extra: reading a contract means building and parsing XDR,
    which is the SDK's job. Without it this reports itself unavailable and says why, exactly
    like signature verification does — a gateway that cannot ask must not pretend the answer
    is no.
    """

    def __init__(self, contract_id: str, network: str, source: str, rpc_url: str = "") -> None:
        self.contract_id = contract_id
        self.network = network
        self._rpc_url = rpc_url or RPC_URLS.get(network, RPC_URLS["stellar-testnet"])
        self._source = source
        """An address that exists on the ledger. It is only the origin of the simulation and
        signs nothing — Soroban wants a source account even for a read."""
        self._reason = ""
        self._client: Any | None = None

    @property
    def available(self) -> bool:
        return bool(self.contract_id) and self._load() is not None

    @property
    def unavailable_reason(self) -> str:
        self._load()
        return self._reason

    def _load(self) -> Any | None:
        """The SDK, on first use. Cached, including the failure: a missing package does not
        become present between two requests, and retrying the import per call is wasted work
        on a path that answers "no" either way."""
        if self._client is not None or self._reason:
            return self._client
        if not self.contract_id:
            self._reason = "no subscriptions contract is configured"
            return None
        try:
            from stellar_sdk import Network
            from stellar_sdk.contract import ContractClient
        except ImportError as error:
            self._reason = (
                "this gateway cannot read the subscriptions contract: the Stellar dependency "
                f'is not installed ({error}). Install it with: pip install -e ".[stellar]"'
            )
            logger.warning(self._reason)
            return None
        passphrase = (
            Network.PUBLIC_NETWORK_PASSPHRASE
            if self.network == "stellar"
            else Network.TESTNET_NETWORK_PASSPHRASE
        )
        self._client = ContractClient(self.contract_id, self._rpc_url, passphrase)
        return self._client

    def subscription(self, address: str) -> OnChainSubscription | None:
        """What the contract says about ``address``. ``None`` when it says nothing.

        An EXPIRED subscription still comes back, deliberately: "it ran out last Tuesday" and
        "you never had one" lead to different sentences on a screen, and flattening them here
        would throw the difference away before anybody could use it.
        """
        client = self._load()
        if client is None or not ADDRESS.match(address or ""):
            return None
        try:
            from stellar_sdk import scval

            result = client.invoke(
                "subscription",
                [scval.to_address(address)],
                parse_result_xdr_fn=lambda xdr: xdr,
                source=self._source,
            ).result()
        except Exception as error:  # the RPC, the network, a contract that moved
            raise SubscriptionContractError(
                f"the subscriptions contract could not be read: {error}"
            ) from error
        return _read(result)

    # ── the half that writes, which this gateway does not sign ───────────────

    def build_subscribe(self, address: str, plan: str, fee: int = 1_000_000) -> str:
        """The unsigned transaction that subscribes ``address`` to ``plan``, as XDR.

        Built here and signed in the browser, which is the only arrangement that works: the
        payment comes out of the SUBSCRIBER's account, so only their key can authorise it,
        and this gateway holds no key at all. What it has instead is the SDK — assembling a
        Soroban invocation means simulating it first to learn its footprint, and shipping
        that machinery to a browser to do the same job would be a second implementation of it.

        So: the gateway says what the transaction is, the wallet says who agrees to it, and
        neither can do the other's half.
        """
        if not ADDRESS.match(address or ""):
            raise SubscriptionContractError("that is not a Stellar address")
        if self._load() is None:
            raise SubscriptionContractError(self._reason or "no subscriptions contract")
        try:
            from stellar_sdk import SorobanServer, TransactionBuilder, scval

            rpc = SorobanServer(self._rpc_url)
            source = rpc.load_account(address)
            transaction = (
                TransactionBuilder(source, self._passphrase(), base_fee=fee)
                .append_invoke_contract_function_op(
                    contract_id=self.contract_id,
                    function_name="subscribe",
                    parameters=[scval.to_address(address), scval.to_symbol(plan)],
                )
                # Five minutes to open a wallet and press a button. An unsigned transaction
                # that never expires is one somebody can sign a week later, at a price and a
                # rate that have both moved.
                .set_timeout(300)
                .build()
            )
            # Simulation is not an optimisation here: without it the transaction carries no
            # resource footprint and the network rejects it outright.
            prepared = rpc.prepare_transaction(transaction)
        except Exception as error:
            raise SubscriptionContractError(
                f"the subscription transaction could not be built: {error}"
            ) from error
        return prepared.to_xdr()

    def submit(self, signed_xdr: str) -> str:
        """Send a signed transaction and return its hash. Waits for the ledger to take it.

        The gateway submits rather than the browser, for one reason: a wallet that signs and
        then fails to send leaves the subscriber having authorised a payment that never
        happened and no way to tell. Here the answer comes back from the same call.
        """
        if self._load() is None:
            raise SubscriptionContractError(self._reason or "no subscriptions contract")
        try:
            from stellar_sdk import SorobanServer, TransactionEnvelope

            rpc = SorobanServer(self._rpc_url)
            envelope = TransactionEnvelope.from_xdr(signed_xdr, self._passphrase())
            sent = rpc.send_transaction(envelope)
            if str(sent.status) not in ("SendTransactionStatus.PENDING", "PENDING"):
                raise SubscriptionContractError(
                    f"the network refused the transaction: {sent.status}"
                )
            settled = rpc.poll_transaction(sent.hash)
            if str(settled.status) not in ("GetTransactionStatus.SUCCESS", "SUCCESS"):
                raise SubscriptionContractError(
                    f"the transaction failed on the ledger: {settled.status}"
                )
            return str(sent.hash)
        except SubscriptionContractError:
            raise
        except Exception as error:
            raise SubscriptionContractError(
                f"the subscription could not be submitted: {error}"
            ) from error

    def _passphrase(self) -> str:
        from stellar_sdk import Network

        return (
            Network.PUBLIC_NETWORK_PASSPHRASE
            if self.network == "stellar"
            else Network.TESTNET_NETWORK_PASSPHRASE
        )


def _read(value: Any) -> OnChainSubscription | None:
    """Turn the contract's `Option<Subscription>` into ours.

    Defensive on purpose. This is the boundary between XDR and everything else, and the shape
    the SDK hands over has changed between versions; a field that is missing should read as
    "not subscribed" rather than take a billing request down with an AttributeError.
    """
    fields = _map(value)
    if not fields:
        return None
    try:
        plan = _symbol(fields.get("plan"))
        started = _u64(fields.get("started"))
        expires = _u64(fields.get("expires"))
        periods = _u64(fields.get("periods"))
    except (TypeError, ValueError, AttributeError) as error:
        logger.warning("the subscriptions contract returned something unreadable: %s", error)
        return None
    if not plan or expires == 0:
        return None
    return OnChainSubscription(
        plan=plan,
        started=datetime.fromtimestamp(started, UTC),
        expires=datetime.fromtimestamp(expires, UTC),
        periods=int(periods),
    )


def _map(value: Any) -> dict[str, Any]:
    """The `Subscription` struct as `{field: value}`, or `{}` for a `None`/void."""
    if value is None:
        return {}
    entries = getattr(getattr(value, "map", None), "sc_map", None)
    if entries is None:
        entries = getattr(value, "map", None)
    if entries is None:
        return {}
    fields: dict[str, Any] = {}
    for entry in entries:
        key = _symbol(getattr(entry, "key", None))
        if key:
            fields[key] = getattr(entry, "val", None)
    return fields


def _symbol(value: Any) -> str:
    raw = getattr(value, "sym", None)
    raw = getattr(raw, "sc_symbol", raw)
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return str(raw) if raw is not None else ""


def _u64(value: Any) -> int:
    for attribute in ("u64", "u32"):
        raw = getattr(value, attribute, None)
        if raw is not None:
            return int(getattr(raw, "uint64", getattr(raw, "uint32", raw)))
    return 0


__all__ = [
    "RPC_URLS",
    "OnChainSubscription",
    "SubscriptionContract",
    "SubscriptionContractError",
]
