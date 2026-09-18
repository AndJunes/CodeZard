# API Gateway

Servidor en Python (FastAPI + httpx) que actúa como **intermediario** entre los clientes y los
microservicios. Recibe peticiones en `/api/{servicio}/{ruta}` y las reenvía a
`{base_url del servicio}/{ruta}`, añadiendo resiliencia (reintentos y circuit breaker),
trazabilidad (request id) y health checks.

```
Cliente ──► GET /api/users/items/1 ──► Gateway ──► GET http://users:8001/items/1
```

## Características

- **Proxy transparente**: método, query params, body y cabeceras (incluidas las repetidas, como
  `Set-Cookie`). Elimina cabeceras *hop-by-hop* y añade `X-Forwarded-For/Proto/Host`.
- **Reintentos** con backoff exponencial, solo para métodos idempotentes (`GET`, `PUT`,
  `DELETE`, …). `POST` y `PATCH` nunca se reintentan para no duplicar efectos.
- **Circuit breaker por servicio**: si un microservicio falla repetidamente, el gateway deja de
  llamarlo durante un tiempo y responde `503` al instante, sin afectar a los demás.
- **Request ID**: reutiliza el `X-Request-ID` entrante (si es seguro) o genera uno; se propaga al
  microservicio, se devuelve al cliente y aparece en cada línea de log.
- **Errores consistentes** en JSON: `404` servicio desconocido, `502` servicio inaccesible,
  `503` circuito abierto, `504` timeout.
- **Health checks**: `/health` (liveness) y `/health/services` (estado de cada microservicio).

## Arquitectura

```
src/gateway/
├── domain/            # Modelos, errores y puertos (interfaces). Sin dependencias externas.
│   ├── models.py
│   ├── exceptions.py
│   └── ports.py       # ServiceRegistry, UpstreamClient (ABCs)
├── application/       # Casos de uso: solo dependen de los puertos.
│   ├── proxy_service.py
│   ├── health_service.py
│   └── header_policy.py
├── infrastructure/    # Implementaciones concretas de los puertos.
│   ├── registry.py            # InMemoryServiceRegistry
│   ├── httpx_client.py        # HttpxUpstreamClient
│   └── resilience/
│       ├── retry.py           # RetryingUpstreamClient (decorador)
│       └── circuit_breaker.py # CircuitBreakerUpstreamClient (decorador)
├── api/               # Capa HTTP (FastAPI): rutas, middleware, errores, adaptadores.
├── config/settings.py # Configuración tipada (pydantic-settings).
├── bootstrap.py       # Raíz de composición: único lugar que conoce las clases concretas.
└── main.py            # Punto de entrada.
```

Las dependencias apuntan siempre hacia dentro: `api → application → domain ← infrastructure`.

La cadena de llamada a un microservicio se compone con decoradores que implementan la misma
interfaz `UpstreamClient`:

```
ProxyService ─► CircuitBreakerUpstreamClient ─► RetryingUpstreamClient ─► HttpxUpstreamClient ─► red
```

### Principios SOLID aplicados

| Principio | Dónde |
|---|---|
| **S** — Responsabilidad única | Cada clase hace una cosa: `HeaderPolicy` filtra cabeceras, `RetryingUpstreamClient` reintenta, `CircuitBreaker` gestiona estados, `InMemoryServiceRegistry` resuelve servicios, `ProxyService` orquesta. |
| **O** — Abierto/cerrado | Nuevo comportamiento = nueva clase. Ej.: añadir rate limiting o caché es escribir otro decorador de `UpstreamClient` y registrarlo en `bootstrap.py`, sin tocar `ProxyService`. Un nuevo error HTTP es una entrada en `api/errors.py`. |
| **L** — Sustitución de Liskov | `HttpxUpstreamClient`, los decoradores de resiliencia y los fakes de los tests son intercambiables porque respetan el contrato de `UpstreamClient` (lanzan `UpstreamError`, nunca excepciones de httpx). |
| **I** — Segregación de interfaces | Puertos pequeños y específicos: `ServiceRegistry` (2 métodos) y `UpstreamClient` (1 método). |
| **D** — Inversión de dependencias | `ProxyService` y `HealthService` reciben abstracciones por constructor. Las implementaciones concretas solo se instancian en `bootstrap.py`. |

## Puesta en marcha

Requisitos: Python 3.11+.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate   |   Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env      # y edita la lista de servicios
gateway                   # o: python -m gateway
```

Documentación interactiva: <http://localhost:8000/docs>

### Con Docker

```bash
docker compose up --build
curl http://localhost:8000/api/httpbin/get
curl http://localhost:8000/health/services
```

`docker-compose.yml` levanta el gateway y un microservicio de ejemplo (`go-httpbin`).

## Configuración

Por variables de entorno con prefijo `GATEWAY_` (o en `.env`). Los valores anidados usan `__`.

| Variable | Por defecto | Descripción |
|---|---|---|
| `GATEWAY_SERVICES` | `[]` | JSON con los microservicios (ver abajo). |
| `GATEWAY_HOST` / `GATEWAY_PORT` | `0.0.0.0` / `8000` | Dirección de escucha. |
| `GATEWAY_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. |
| `GATEWAY_MAX_CONNECTIONS` | `100` | Tamaño del pool de conexiones salientes. |
| `GATEWAY_RETRY__MAX_ATTEMPTS` | `3` | Intentos totales (1 = sin reintentos). |
| `GATEWAY_RETRY__BASE_DELAY_SECONDS` | `0.1` | Backoff: `base * 2^(intento-1)`. |
| `GATEWAY_RETRY__MAX_DELAY_SECONDS` | `2.0` | Tope del backoff. |
| `GATEWAY_RETRY__RETRY_ON_STATUS` | `[502,503,504]` | Códigos que se reintentan. |
| `GATEWAY_CIRCUIT_BREAKER__FAILURE_THRESHOLD` | `5` | Fallos consecutivos para abrir el circuito. |
| `GATEWAY_CIRCUIT_BREAKER__RECOVERY_TIMEOUT_SECONDS` | `30` | Tiempo abierto antes de probar de nuevo. |
| `GATEWAY_CIRCUIT_BREAKER__FAILURE_STATUS_CODES` | `[502,503,504]` | Códigos que cuentan como fallo. |

Cada servicio de `GATEWAY_SERVICES` admite:

```json
[
  {
    "name": "users",
    "base_url": "http://users:8001",
    "timeout_seconds": 5,
    "health_path": "/health"
  }
]
```

`name` es el segmento de la URL del gateway (`/api/users/...`): minúsculas, dígitos, `-` y `_`.

## Tests y calidad

```bash
pytest --cov          # tests unitarios + integración, cobertura mínima 90 %
ruff check .          # lint
ruff format --check . # formato
mypy src              # tipado estricto
```

- `tests/unit/`: cada clase por separado, usando fakes de los puertos (`tests/fakes.py`) y un
  reloj falso, así que los tests de reintentos y circuit breaker no esperan tiempo real.
- `tests/integration/`: la aplicación completa (middleware, rutas, lifespan, errores); solo se
  sustituye la red hacia los microservicios con `httpx.MockTransport`.

El workflow `.github/workflows/ci.yml` ejecuta todo lo anterior en cada push y pull request.

## Extender el gateway

- **Añadir un microservicio**: solo configuración (`GATEWAY_SERVICES`).
- **Registro dinámico** (Consul, Kubernetes, base de datos): implementa `ServiceRegistry` y
  cámbialo en `bootstrap.py`.
- **Nueva política** (rate limiting, caché, métricas): escribe una clase que implemente
  `UpstreamClient` envolviendo a otra y añádela a la cadena en `bootstrap.py`.

## Limitaciones conocidas

- Los cuerpos de petición y respuesta se cargan completos en memoria (no hay *streaming*), así
  que no está pensado para subir o descargar ficheros grandes.
- No incluye autenticación de clientes; si el gateway es la puerta de entrada pública, conviene
  añadirla (p. ej. como dependencia de FastAPI en `api/routes/proxy.py`).
- El estado del circuit breaker vive en memoria de cada proceso: con varias réplicas, cada una
  lleva su propia cuenta.
