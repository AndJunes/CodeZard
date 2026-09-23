"""Abstractions the application layer depends on (Dependency Inversion Principle).

Concrete implementations live in ``gateway.infrastructure`` and are wired in
``gateway.bootstrap``; nothing in the application layer imports them directly.
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from typing import Any, Protocol

from gateway.domain.billing import (
    Invoice,
    LedgerEntry,
    Money,
    ObservedPayment,
    Subscription,
    Usage,
)
from gateway.domain.models import (
    OutboundRequest,
    ServiceDefinition,
    UpstreamResponse,
    UpstreamStream,
)
from gateway.domain.runs import Run
from gateway.domain.x402 import SettleResult, VerifyResult


class ServiceRegistry(ABC):
    """Resolves service names to their definitions."""

    @abstractmethod
    def get(self, name: str) -> ServiceDefinition:
        """Return the service called ``name`` or raise ``ServiceNotFoundError``."""

    @abstractmethod
    def all(self) -> list[ServiceDefinition]:
        """Return every registered service."""


class UpstreamClient(ABC):
    """Sends requests to downstream services.

    Implementations must translate transport failures into ``UpstreamError``
    subclasses so callers never depend on a specific HTTP library.
    """

    @abstractmethod
    async def send(self, request: OutboundRequest) -> UpstreamResponse:
        """Send ``request``, read the whole body, and return the downstream response."""

    async def stream(self, request: OutboundRequest) -> UpstreamStream:
        """Send ``request`` and return as soon as the status and headers arrive.

        The body is left unread on an open connection, so the guarantee above only covers
        the exchange up to the headers: a failure while the caller consumes ``chunks``
        surfaces as whatever the transport raises, not as an ``UpstreamError``. By then the
        client already holds a status code, so there is nothing left to translate it into.

        The caller owns the returned stream and must await ``aclose``. Implementations may
        override this to return as soon as headers arrive; the fallback adapts ``send`` for
        simple clients and older test doubles.
        """
        response = await self.send(request)

        async def chunks() -> AsyncIterator[bytes]:
            if response.stream is not None:
                async for chunk in response.stream:
                    yield chunk
            elif response.body:
                yield response.body

        return UpstreamStream(
            status_code=response.status_code,
            headers=response.headers,
            chunks=chunks(),
            aclose=response.aclose,
            instance_id=response.instance_id,
        )


class RunStore(ABC):
    """Where runs live between requests.

    It is a port and not a dictionary in the service because the lifetime is a policy: this
    process keeps them in memory and lets them expire, and the PRD forbids carrying them
    between executions. Should that ever change, it changes here and nowhere else.
    """

    @abstractmethod
    async def put(self, run: Run) -> Run:
        """Store ``run`` under its id, replacing any earlier version. Returns what was stored."""

    @abstractmethod
    async def get(self, run_id: str) -> Run:
        """The run, or raise ``RunNotFoundError``. An expired run is a missing one."""


class RunLog(ABC):
    """What a run has already emitted, so a tab that reconnects can catch up.

    A port for the same reason ``RunStore`` is one: how long a log lives, and whether it
    lives anywhere but this process, is a policy — and the PRD's answer today is "in memory,
    bounded, gone on restart". The application layer should not be the place that knows.
    """

    @abstractmethod
    def start(self, run_id: str) -> None:
        """A generation is beginning. Any earlier log for this run is replaced."""

    @abstractmethod
    def append(self, run_id: str, chunk: bytes) -> None:
        """Keep one chunk of the stream."""

    @abstractmethod
    def end(self, run_id: str) -> None:
        """The generation is over, however it ended. Readers stop after draining."""

    @abstractmethod
    def follow(self, run_id: str) -> AsyncIterator[bytes]:
        """Everything said so far, then everything said next, until the run ends."""


class BillingStore(ABC):
    """Where money lives. The one thing in this gateway that MUST survive a restart.

    ``RunStore`` and ``RunLog`` are deliberately in-memory and deliberately forgetful; a run
    that expires costs a person one regeneration. A ledger that forgets costs them what they
    paid, so this port exists to be implemented by something durable and to make that
    difference impossible to blur.

    Appending is idempotent ON ``reference``. That is not a convenience: confirming a payment
    is triggered by a poller, by the payer refreshing the page and by a webhook, all three of
    which can arrive at once, and every one of them must credit the tokens exactly once.
    """

    @abstractmethod
    async def append(self, entry: LedgerEntry) -> LedgerEntry:
        """Record ``entry``. If one with the same non-empty ``reference`` already exists,
        change nothing and return the one that was already there."""

    @abstractmethod
    async def entries(self, account: str, limit: int = 100) -> Sequence[LedgerEntry]:
        """The account's entries, oldest first, capped at ``limit``."""

    @abstractmethod
    async def all_entries(self, account: str) -> Sequence[LedgerEntry]:
        """Every entry, oldest first. What a balance is computed from."""

    @abstractmethod
    async def put_invoice(self, invoice: Invoice) -> Invoice:
        """Store or replace ``invoice`` by id."""

    @abstractmethod
    async def invoice(self, invoice_id: str) -> Invoice | None:
        """The invoice, or ``None``."""

    @abstractmethod
    async def open_invoices(self, limit: int = 100) -> Sequence[Invoice]:
        """Every invoice still awaiting payment, oldest first. What a poller walks."""

    @abstractmethod
    async def put_subscription(self, subscription: Subscription) -> Subscription:
        """Store or replace the account's subscription."""

    @abstractmethod
    async def subscription(self, account: str) -> Subscription | None:
        """The account's subscription, or ``None``."""


class PaymentNetwork(ABC):
    """Stellar, behind an interface that does not mention it.

    Four questions, and the first two are what billing actually needs: has this invoice been
    paid, and what is this amount of USD worth in the asset being charged. The other two are
    x402's, where a client hands over a signed transaction instead of sending one itself.

    Implementations never raise for an ordinary "no": an unpaid invoice is ``None``, not an
    exception. They raise only when the network itself could not be asked.
    """

    @property
    @abstractmethod
    def network(self) -> str:
        """The x402 network identifier, e.g. ``stellar-testnet``."""

    @property
    @abstractmethod
    def destination(self) -> str:
        """The address payments are made to."""

    @abstractmethod
    async def payment_for(self, invoice: Invoice) -> ObservedPayment | None:
        """The payment settling ``invoice``, or ``None`` if the ledger has none yet."""

    @abstractmethod
    async def quote(self, price: Money, asset: str) -> str:
        """``price`` expressed in ``asset``, as the network spells amounts.

        A quote, and it is why an invoice freezes its amount: this can move between the
        moment a person is shown a price and the moment they pay it.
        """

    @abstractmethod
    async def verify(self, transaction: str, invoice: Invoice) -> VerifyResult:
        """Would this signed envelope pay that invoice? Nothing is submitted."""

    @abstractmethod
    async def settle(self, transaction: str) -> SettleResult:
        """Submit the envelope and report what the ledger said."""


class SignatureVerifier(ABC):
    """Proves that whoever is calling holds the key of the account they claim.

    It is the whole of authentication here: an account IS a Stellar address, so there is no
    password to store, no reset flow to abuse, and the thing that proves ownership is the same
    key that pays. Separate from ``PaymentNetwork`` because verifying a signature touches no
    network at all — it is arithmetic — and a deployment with no Stellar connectivity can
    still authenticate.
    """

    @property
    @abstractmethod
    def available(self) -> bool:
        """``False`` when this deployment cannot check signatures, saying so honestly instead
        of letting everybody in."""

    @abstractmethod
    def verify(self, address: str, message: bytes, signature: str) -> bool:
        """Did ``address`` sign ``message``? Never raises: a malformed anything is ``False``."""


class RunMeter(Protocol):
    """What the orchestrator needs from billing, and nothing more.

    A ``Protocol`` rather than an ABC, unlike everything above it, and for a reason: the
    implementation is ``BillingService``, which is an APPLICATION service and cannot inherit
    from a domain port without the dependency pointing the wrong way. Structural typing lets
    the orchestrator depend on the two calls it makes while billing stays where it belongs.

    ``None`` in place of one of these is a gateway that is not charging for runs, which is
    the default and has to keep working exactly as it did.
    """

    async def authorize(self, account: str, needed: int = 0) -> Any:
        """Raise ``InsufficientFundsError`` if this account may not start a run."""

    async def charge(self, account: str, run_id: str, usage: Usage) -> Any:
        """Debit what the finished run really cost. Never refuses."""
