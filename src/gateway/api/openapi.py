"""The OpenAPI document behind Swagger UI (``/docs``) and ReDoc (``/redoc``).

FastAPI documents the gateway's own routes. On top of them, a service reached through the
gateway can be described by a ``DownstreamContract``: its routes then appear under
``/api/{service}/...``, which is where clients call them.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from pydantic import BaseModel
from pydantic.json_schema import models_json_schema

from gateway.api.errors import classify
from gateway.api.schemas import ErrorDetail, ErrorResponse
from gateway.domain.exceptions import (
    CircuitOpenError,
    GatewayError,
    InstanceNotFoundError,
    ServiceNotFoundError,
    UpstreamConnectionError,
    UpstreamTimeoutError,
)

JsonObject = dict[str, Any]

API_DESCRIPTION = """\
Intermediary between clients and microservices: a request to `/api/{service}/{path}` is
forwarded to `{service base_url}/{path}`, and the service's answer comes back unchanged.

- **Request id**: send `X-Request-ID` (letters, digits, `.`, `_` and `-`, up to 128) or let the
  gateway generate one. The service receives it and the response carries it back.
- **Errors**: the ones produced by the gateway always have the `ErrorResponse` shape. The ones
  produced by a service are relayed exactly as the service wrote them.
- **Resilience**: `GET`, `HEAD`, `OPTIONS`, `PUT` and `DELETE` are retried on connection errors,
  timeouts and `502`/`503`/`504`; `POST` and `PATCH` never are. A server that keeps failing
  gets its circuit opened, and the gateway stops calling it for a while.
- **Several instances**: a service can run on several servers. Requests are spread among them,
  one that cannot take a request hands it to the next, and `X-Gateway-Instance` names the one
  that answered. `/api/{service}@{instance}/...` goes back to that same server.
- **Server-Sent Events** (`text/event-stream`) are relayed event by event. Swagger UI shows
  them only when the stream ends: use `curl -N` to watch them arrive.
"""

TAGS: list[JsonObject] = [
    {"name": "health", "description": "The state of the gateway and of every registered service."},
    {
        "name": "proxy",
        "description": "The generic route every registered service is reached through.",
    },
]

_REF_TEMPLATE = "#/components/schemas/{model}"
_EXAMPLE_REQUEST_ID = "5f0c6b0e7d2a4c1e9b3f8a6d4e2c1b0a"

# One example of each error the proxy can answer. Status and code come from the real mapping,
# so the documentation cannot drift from what clients receive.
_GATEWAY_ERRORS: tuple[tuple[GatewayError, str], ...] = (
    (ServiceNotFoundError("billing"), "no service is registered under that name."),
    (
        InstanceNotFoundError("mirag", "0badc0de"),
        "the service has no instance with that id (the request was pinned with `@`).",
    ),
    (
        UpstreamConnectionError("users"),
        "the service could not be reached: connection refused, unknown host or reset.",
    ),
    (
        CircuitOpenError("users"),
        "every instance of the service failed repeatedly and has its circuit open: the gateway "
        "answers at once, without calling them, until the recovery timeout passes.",
    ),
    (
        UpstreamTimeoutError("users"),
        "the service took longer than its `timeout_seconds` to connect or to send the next "
        "piece of its response.",
    ),
)


def schema_ref(model: type[BaseModel]) -> JsonObject:
    return {"$ref": _REF_TEMPLATE.format(model=model.__name__)}


def gateway_error_responses(*status_codes: int) -> dict[int | str, JsonObject]:
    """OpenAPI responses for the errors the gateway itself answers, limited to ``status_codes``.

    Errors sharing a status share its response, with one named example per error code.
    """
    responses: dict[int | str, JsonObject] = {}
    for error, description in _GATEWAY_ERRORS:
        status_code, code = classify(error)
        if status_code not in status_codes:
            continue
        example = ErrorResponse(
            error=ErrorDetail(code=code, message=str(error), request_id=_EXAMPLE_REQUEST_ID)
        )
        response = responses.setdefault(
            str(status_code),
            {
                "description": "",
                "content": {
                    "application/json": {"schema": schema_ref(ErrorResponse), "examples": {}}
                },
            },
        )
        response["description"] = "\n\n".join(
            filter(None, [response["description"], f"`{code}`: {description}"])
        )
        response["content"]["application/json"]["examples"][code] = {"value": example.model_dump()}
    return responses


@dataclass(frozen=True, slots=True)
class DownstreamContract:
    """The OpenAPI description of a service reached through the gateway."""

    tag: JsonObject
    """Groups the service's routes in Swagger: ``{"name": ..., "description": ...}``."""
    paths: JsonObject
    """OpenAPI path items, keyed by the path inside the service (e.g. ``/api/v1/chat``)."""
    models: tuple[type[BaseModel], ...] = ()
    """Models the paths reference through ``schema_ref``."""


def install_openapi(
    app: FastAPI, service_names: Iterable[str], contracts: Mapping[str, DownstreamContract]
) -> None:
    """Make ``app`` document the contract of every registered service that has one."""
    documented = {name: contracts[name] for name in service_names if name in contracts}

    def openapi() -> JsonObject:
        if app.openapi_schema is None:
            schema = get_openapi(
                title=app.title,
                version=app.version,
                description=app.description,
                routes=app.routes,
                # A copy: contract tags are appended to it below.
                tags=list(app.openapi_tags or []),
            )
            contract_models = [model for c in documented.values() for model in c.models]
            _add_models(schema, [ErrorResponse, *contract_models])
            for name, contract in documented.items():
                _add_contract(schema, name, contract)
            app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = openapi  # type: ignore[method-assign]


def _add_models(schema: JsonObject, models: Sequence[type[BaseModel]]) -> None:
    _, definitions = models_json_schema(
        [(model, "validation") for model in models], ref_template=_REF_TEMPLATE
    )
    components = schema.setdefault("components", {}).setdefault("schemas", {})
    components.update(definitions.get("$defs", {}))


def _add_contract(schema: JsonObject, service_name: str, contract: DownstreamContract) -> None:
    prefix = f"/api/{service_name}"
    schema["paths"].update({prefix + path: item for path, item in contract.paths.items()})
    schema.setdefault("tags", []).append(contract.tag)
