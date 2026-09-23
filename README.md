# API Gateway

A Python server (FastAPI + httpx) that acts as an **intermediary** between clients and
microservices. It receives requests at `/api/{service}/{path}` and forwards them to
`{service base_url}/{path}`, adding resilience (retries and a circuit breaker), traceability
(request ids) and health checks.

```
Client ──► GET /api/users/items/1 ──► Gateway ──► GET http://users:8001/items/1
```

## Features

- **Transparent proxy**: method, query params, body and headers (including repeated ones such as
  `Set-Cookie`). Strips *hop-by-hop* headers and adds `X-Forwarded-For/Proto/Host`.
- **Streamed responses**: the body is forwarded as it arrives, not after it is complete. A service
  that reports progress over minutes (Server-Sent Events) reaches the client as it happens, and a
  large download is never held in memory.
- **Retries** with exponential backoff, only for idempotent methods (`GET`, `PUT`, `DELETE`, …).
  `POST` and `PATCH` are never retried, so side effects are never duplicated.
- **Per-service circuit breaker**: when a microservice keeps failing, the gateway stops calling it
  for a while and answers `503` immediately, without affecting the other services.
- **Request ID**: reuses the incoming `X-Request-ID` (when it is safe) or generates one; it is
  propagated to the microservice, returned to the client and included in every log line.
- **Consistent JSON errors**: `404` unknown service, `502` unreachable service, `503` open
  circuit, `504` timeout.
- **Health checks**: `/health` (liveness) and `/health/services` (status of each microservice).

## Architecture

```
src/gateway/
├── domain/            # Models, errors and ports (interfaces). No external dependencies.
│   ├── models.py
│   ├── exceptions.py
│   └── ports.py       # ServiceRegistry, UpstreamClient (ABCs)
├── application/       # Use cases: depend only on the ports.
│   ├── proxy_service.py
│   ├── health_service.py
│   └── header_policy.py
├── infrastructure/    # Concrete implementations of the ports.
│   ├── registry.py            # InMemoryServiceRegistry
│   ├── httpx_client.py        # HttpxUpstreamClient
│   └── resilience/
│       ├── retry.py           # RetryingUpstreamClient (decorator)
│       └── circuit_breaker.py # CircuitBreakerUpstreamClient (decorator)
├── api/               # HTTP layer (FastAPI): routes, middleware, errors, adapters.
├── config/settings.py # Typed configuration (pydantic-settings).
├── bootstrap.py       # Composition root: the only place that knows the concrete classes.
└── main.py            # Entry point.
```

Dependencies always point inwards: `api → application → domain ← infrastructure`.

The call chain to a microservice is built from decorators that all implement the same
`UpstreamClient` interface:

```
ProxyService ─► CircuitBreakerUpstreamClient ─► RetryingUpstreamClient ─► HttpxUpstreamClient ─► network
```

### SOLID principles

| Principle | Where |
|---|---|
| **S** — Single responsibility | Each class does one thing: `HeaderPolicy` filters headers, `RetryingUpstreamClient` retries, `CircuitBreaker` manages states, `InMemoryServiceRegistry` resolves services, `ProxyService` orchestrates. |
| **O** — Open/closed | New behavior means a new class. E.g. adding rate limiting or caching is writing another `UpstreamClient` decorator and registering it in `bootstrap.py`, without touching `ProxyService`. A new HTTP error is one entry in `api/errors.py`. |
| **L** — Liskov substitution | `HttpxUpstreamClient`, the resilience decorators and the test fakes are interchangeable because they honor the `UpstreamClient` contract (they raise `UpstreamError`, never httpx exceptions). |
| **I** — Interface segregation | Small, focused ports: `ServiceRegistry` (2 methods) and `UpstreamClient` (1 method). |
| **D** — Dependency inversion | `ProxyService` and `HealthService` receive abstractions through their constructors. Concrete implementations are only instantiated in `bootstrap.py`. |

## Getting started

Requirements: Python 3.11+.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate   |   Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env      # then edit the list of services
gateway                   # or: python -m gateway
```

Interactive docs: <http://localhost:8000/docs>

### With Docker

```bash
docker compose up --build
curl http://localhost:8000/api/httpbin/get
curl http://localhost:8000/health/services
```

`docker-compose.yml` starts the gateway plus a sample microservice (`go-httpbin`).

## Configuration

Through environment variables prefixed with `GATEWAY_` (or a `.env` file). Nested values use `__`.

| Variable | Default | Description |
|---|---|---|
| `GATEWAY_SERVICES` | `[]` | JSON list of microservices (see below). |
| `GATEWAY_HOST` / `GATEWAY_PORT` | `0.0.0.0` / `8000` | Listen address. |
| `GATEWAY_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. |
| `GATEWAY_MAX_CONNECTIONS` | `100` | Size of the outbound connection pool. |
| `GATEWAY_RETRY__MAX_ATTEMPTS` | `3` | Total attempts (1 = no retries). |
| `GATEWAY_RETRY__BASE_DELAY_SECONDS` | `0.1` | Backoff: `base * 2^(attempt-1)`. |
| `GATEWAY_RETRY__MAX_DELAY_SECONDS` | `2.0` | Backoff cap. |
| `GATEWAY_RETRY__RETRY_ON_STATUS` | `[502,503,504]` | Status codes that are retried. |
| `GATEWAY_CIRCUIT_BREAKER__FAILURE_THRESHOLD` | `5` | Consecutive failures that open the circuit. |
| `GATEWAY_CIRCUIT_BREAKER__RECOVERY_TIMEOUT_SECONDS` | `30` | Time the circuit stays open before a new attempt. |
| `GATEWAY_CIRCUIT_BREAKER__FAILURE_STATUS_CODES` | `[502,503,504]` | Status codes counted as failures. |

Each entry in `GATEWAY_SERVICES` accepts:

```json
[
  {
    "name": "users",
    "base_url": "http://users:8001",
    "timeout_seconds": 5,
    "read_timeout_seconds": 600,
    "health_path": "/health"
  }
]
```

`name` is the gateway URL segment (`/api/users/...`): lowercase letters, digits, `-` and `_`.

`timeout_seconds` bounds connecting, writing and waiting for a pool slot.
`read_timeout_seconds` (optional, defaults to `timeout_seconds`) bounds the **gap between two
chunks** of the response. They are separate because a streaming service needs a generous gap
without also being given minutes to complete a handshake.

## Tests and quality

```bash
pytest --cov          # unit + integration tests, 90% minimum coverage
ruff check .          # lint
ruff format --check . # formatting
mypy src              # strict type checking
```

- `tests/unit/`: each class in isolation, using fakes of the ports (`tests/fakes.py`) and a fake
  clock, so the retry and circuit breaker tests never wait in real time.
- `tests/integration/`: the whole application (middleware, routes, lifespan, errors); only the
  network to the microservices is replaced, using `httpx.MockTransport`.

The `.github/workflows/ci.yml` workflow runs all of the above on every push and pull request.

## Extending the gateway

- **Add a microservice**: configuration only (`GATEWAY_SERVICES`).
- **Dynamic service discovery** (Consul, Kubernetes, a database): implement `ServiceRegistry` and
  swap it in `bootstrap.py`.
- **New policy** (rate limiting, caching, metrics): write a class that implements
  `UpstreamClient` by wrapping another one, and add it to the chain in `bootstrap.py`.

## Known limitations

- **Request** bodies are still read into memory before being forwarded; only responses stream.
- A failure **after the first byte** cannot become a JSON error: the status line and headers are
  already on their way, so the response ends mid-body. There is no way to un-send a `200`. The
  cut is logged with its request id, which is what makes it diagnosable.
- The circuit breaker judges a streamed call by its status code, which arrives with the headers.
  A stream that dies three minutes later still counts as a success — including a HALF_OPEN probe,
  so a service that answers and then breaks can close the circuit.
- There is no client authentication; if the gateway is the public entry point, add it (e.g. as a
  FastAPI dependency in `api/routes/proxy.py`).
- Circuit breaker state lives in each process's memory: with several replicas, each one keeps
  its own count.
