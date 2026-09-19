"""The OpenAPI document behind Swagger UI (``/docs``)."""

import warnings
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from gateway.api.app import create_app
from gateway.config.settings import ServiceSettings, Settings

PROXY_PATH = "/api/{service_name}/{path}"


def _settings(*service_names: str) -> Settings:
    return Settings(
        _env_file=None,
        services=[
            ServiceSettings(name=name, base_url=f"http://{name}.internal") for name in service_names
        ],
    )


def _references(node: Any) -> set[str]:
    if isinstance(node, dict):
        return {node["$ref"]} if "$ref" in node else set().union(*map(_references, node.values()))
    if isinstance(node, list):
        return set().union(*map(_references, node))
    return set()


@pytest.fixture
def agent_app() -> FastAPI:
    return create_app(_settings("httpbin", "mirag"))


async def test_serves_swagger_ui(client: httpx.AsyncClient) -> None:
    response = await client.get("/docs")

    assert response.status_code == 200
    assert "swagger-ui" in response.text


async def test_documents_health_and_the_proxy(client: httpx.AsyncClient) -> None:
    paths = (await client.get("/openapi.json")).json()["paths"]

    assert set(paths) == {"/health", "/health/services", PROXY_PATH}
    assert set(paths[PROXY_PATH]) == {"get", "post", "put", "patch", "delete"}


async def test_documents_a_request_body_only_for_methods_that_carry_one(
    client: httpx.AsyncClient,
) -> None:
    proxy = (await client.get("/openapi.json")).json()["paths"][PROXY_PATH]

    assert {method for method, operation in proxy.items() if "requestBody" in operation} == {
        "post",
        "put",
        "patch",
    }


async def test_documents_the_errors_the_proxy_really_answers(client: httpx.AsyncClient) -> None:
    responses = (await client.get("/openapi.json")).json()["paths"][PROXY_PATH]["get"]["responses"]
    documented = responses["404"]["content"]["application/json"]["example"]["error"]["code"]

    actual = (await client.get("/api/billing/anything")).json()["error"]["code"]

    assert documented == actual
    assert {"502", "503", "504"} <= set(responses)
    # The path parameters cannot fail validation, so FastAPI's default 422 would be a lie.
    assert "422" not in responses


async def test_leaves_out_the_contract_of_a_service_that_is_not_registered(
    client: httpx.AsyncClient,
) -> None:
    spec = (await client.get("/openapi.json")).json()

    assert not [path for path in spec["paths"] if path.startswith("/api/mirag/")]
    assert "mirag" not in {tag["name"] for tag in spec["tags"]}


def test_documents_the_agent_routes_under_the_gateway_prefix(agent_app: FastAPI) -> None:
    spec = agent_app.openapi()

    assert {path for path in spec["paths"] if path.startswith("/api/mirag/")} == {
        "/api/mirag/api/v1/health",
        "/api/mirag/api/v1/locales",
        "/api/mirag/api/v1/i18n/{locale}",
        "/api/mirag/api/v1/demos",
        "/api/mirag/api/v1/chat",
        "/api/mirag/api/v1/artifacts/{artifact_id}/download",
        "/api/mirag/api/v1/blockchain/agent",
    }
    chat = spec["paths"]["/api/mirag/api/v1/chat"]["post"]
    assert "text/event-stream" in chat["responses"]["200"]["content"]
    assert "mirag" in {tag["name"] for tag in spec["tags"]}


def test_every_reference_resolves(agent_app: FastAPI) -> None:
    spec = agent_app.openapi()
    prefix = "#/components/schemas/"

    references = _references(spec)

    assert references
    assert {ref.removeprefix(prefix) for ref in references} <= set(spec["components"]["schemas"])


def test_operation_ids_are_unique(agent_app: FastAPI) -> None:
    operation_ids = [
        operation["operationId"]
        for path_item in agent_app.openapi()["paths"].values()
        for operation in path_item.values()
    ]

    assert len(operation_ids) == len(set(operation_ids))


def test_builds_without_warnings() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        create_app(_settings("httpbin", "mirag")).openapi()


def test_is_built_once(agent_app: FastAPI) -> None:
    assert agent_app.openapi() is agent_app.openapi()


def test_one_app_documenting_the_agent_does_not_leak_into_another(agent_app: FastAPI) -> None:
    agent_app.openapi()

    tags = create_app(_settings("httpbin")).openapi()["tags"]

    assert "mirag" not in {tag["name"] for tag in tags}
