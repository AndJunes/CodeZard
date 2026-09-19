"""The routes of the backend agent (Mirag) as clients reach them: ``/api/mirag/...``.

The agent is built on the standard library and serves no OpenAPI document, so its contract is
written down here for Swagger. The source of truth is ``docs/en/api.md`` in the agent's
repository (``agente-backend``): a change there has to be copied here.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from gateway.api.openapi import DownstreamContract, JsonObject, gateway_error_responses, schema_ref

_TAG = "mirag"

# What the gateway answers when it cannot get a response from the agent at all.
_UPSTREAM_FAILURES = gateway_error_responses(502, 503, 504)


class MiragErrorDetail(BaseModel):
    code: str
    message: str


class MiragError(BaseModel):
    """An error answered by the agent itself and relayed by the gateway. No request id."""

    error: MiragErrorDetail


class MiragHealth(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: Literal["ok"]
    version: str = Field(examples=["2.0.0"])
    offline: bool = Field(description="Scripted demos only: no model is called, nothing is spent.")
    execution: bool = Field(description="Whether the agent runs the code it generates.")
    token_required: bool = Field(description="Whether chat and download require the token.")
    model: str | None = Field(examples=["anthropic/claude-haiku-4.5"])
    locales: list[str] = Field(examples=[["en", "es"]])
    default_locale: str = Field(examples=["en"])
    interpreters: list[str] = Field(examples=[["node", "python", "python3"]])
    features: dict[str, bool]


class MiragLocales(BaseModel):
    default: str = Field(
        description="The locale resolved for this client: `Accept-Language`, then `MIRAG_LOCALE`.",
        examples=["es"],
    )
    supported: list[str] = Field(examples=[["en", "es"]])


class MiragUiStrings(BaseModel):
    locale: str = Field(examples=["es"])
    messages: dict[str, Any] = Field(description="The nested UI strings of the locale.")


class MiragDemo(BaseModel):
    key: Literal["knowledge", "construction", "abstention", "project"]
    title: str
    question: str = Field(description="Send it as the chat `question` to run the demo.")
    demonstrates: str
    script: str | None


class MiragDemos(BaseModel):
    locale: str
    offline: bool
    demos: list[MiragDemo]


class MiragBlockchainAgent(BaseModel):
    available: bool
    reason: str | None = Field(
        default=None, examples=["MIRAG_BLOCKCHAIN is off: Stellar is not contacted"]
    )
    identity: dict[str, Any] | None
    wallet: dict[str, Any] | None
    last_payment: dict[str, Any] | None
    links: dict[str, str] = Field(description="Explorer URLs: `account`, `registry`, `tx`.")


class MiragChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=8000, examples=["What is a PostgreSQL index?"])
    mode: Literal["pipeline", "architect"] = Field(
        default="pipeline", description="`pipeline` (production) or `architect` (experimental)."
    )
    locale: Literal["en", "es"] | None = Field(
        default=None, description="When absent: `Accept-Language`, then `MIRAG_LOCALE`."
    )


class MiragStepEvent(BaseModel):
    """One `step` event of the chat stream: a stage of the pipeline."""

    type: Literal["step"]
    name: str = Field(description="Stable stage identifier, e.g. `retrieval` or `model`.")
    status: Literal["executed", "skipped", "fallback", "error", "warning", "simulated", "no_model"]
    summary: str = Field(description="Already localised.")
    ms: float
    source: Literal["execution", "model", "corpus", ""]
    detail: Any = None


class MiragDoneEvent(BaseModel):
    """The last event of every chat stream. A stream that ends without it was interrupted."""

    model_config = ConfigDict(extra="allow")

    type: Literal["done"]
    answer: str = Field(description="Markdown, already localised.")
    mode: str = Field(description="`pipeline`, `architect`, or `state` (self-knowledge).")
    locale: str
    cost_summary: str
    claim_status: (
        Literal["supported", "unsupported", "refuted", "not_executed", "no_claim"] | None
    ) = None
    evidence: dict[str, Any] | None = None
    timeline: list[dict[str, Any]] | None = None
    cost: dict[str, Any] | None = None
    project: dict[str, Any] | None = Field(
        default=None,
        description="The generated project. Its `download_url` is relative to the agent: "
        "prefix it with `/api/mirag`.",
    )
    deliverable: dict[str, Any] | None = None


def _ok(model: type[BaseModel], description: str) -> JsonObject:
    return {
        "description": description,
        "content": {"application/json": {"schema": schema_ref(model)}},
    }


def _agent_error(description: str, code: str, message: str) -> JsonObject:
    example = {"error": {"code": code, "message": message}}
    return {
        "description": description,
        "content": {"application/json": {"schema": schema_ref(MiragError), "example": example}},
    }


_UNAUTHORIZED = _agent_error(
    "`unauthorized`: the token the gateway injects does not match the agent's `MIRAG_TOKEN`. "
    "A deployment error, not a client one.",
    "unauthorized",
    "unauthorized",
)

_LOCALE_QUERY: JsonObject = {
    "name": "locale",
    "in": "query",
    "required": False,
    "description": "When absent: `Accept-Language`, then `MIRAG_LOCALE`.",
    "schema": {"type": "string", "enum": ["en", "es"]},
}

_CHAT_DESCRIPTION = """\
Asks the agent a question. The answer is a **Server-Sent Events** stream: every event is one
`data: <json>` line followed by a blank line, and the gateway relays each one as it arrives.

| `type` | When |
|---|---|
| `step` | Each pipeline stage (`MiragStepEvent`) |
| `phase`, `thought`, `tool`, `cost` | Progress of the `architect` mode |
| `done` | Always the last event (`MiragDoneEvent`) |

A stream that ends without `done` was interrupted. The gateway never retries this `POST`.
Browsers cannot send a `POST` with `EventSource`: read the body with `fetch` instead.
"""

_SSE_EXAMPLE = (
    'data: {"type": "step", "name": "retrieval", "status": "executed", "summary": "...", '
    '"ms": 48.2, "source": "corpus", "detail": {"ids": ["..."], "methods": ["..."]}}\n\n'
    'data: {"type": "done", "answer": "An index is...", "mode": "pipeline", "locale": "en", '
    '"cost_summary": "1 calls · 0 tokens · $0.0000 of $0.50", "claim_status": "no_claim", '
    '"evidence": null, "timeline": null, "cost": null, "project": null, "deliverable": null}\n\n'
)

_DOWNLOAD_DESCRIPTION = """\
The verified ZIP of a generated project. Take the path from `project.download_url` in the
`done` event; never build it. Artifacts live in the memory of the agent instance that made
them and expire (one hour, a cap of 20, every restart): download as soon as `done` arrives.

**With several agent instances only the one that ran the chat has the ZIP**, so pin the
download to it: `/api/mirag@{instance}` + `download_url`, where `instance` is the
`X-Gateway-Instance` header of the chat response. That form works with a single instance
too. Without the pin, the download lands on any instance and may get `410 gone`.
"""

MIRAG = DownstreamContract(
    tag={
        "name": _TAG,
        "description": "The backend agent, through the gateway. The gateway adds the "
        "`X-Mirag-Token` header itself: clients neither send nor see it.",
    },
    paths={
        "/api/v1/health": {
            "get": {
                "tags": [_TAG],
                "operationId": "mirag_health",
                "summary": "Agent liveness and configuration",
                "description": "Also what `/health/services` calls to report on the agent.",
                "responses": {
                    "200": _ok(MiragHealth, "The agent is up."),
                    **_UPSTREAM_FAILURES,
                },
            }
        },
        "/api/v1/locales": {
            "get": {
                "tags": [_TAG],
                "operationId": "mirag_locales",
                "summary": "Supported locales and this client's default",
                "responses": {"200": _ok(MiragLocales, "The locales."), **_UPSTREAM_FAILURES},
            }
        },
        "/api/v1/i18n/{locale}": {
            "get": {
                "tags": [_TAG],
                "operationId": "mirag_ui_strings",
                "summary": "The UI strings of a locale",
                "parameters": [
                    {
                        "name": "locale",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string", "enum": ["en", "es"]},
                    }
                ],
                "responses": {
                    "200": _ok(MiragUiStrings, "The strings."),
                    "404": _agent_error(
                        "`unknown_locale`: not a supported locale.",
                        "unknown_locale",
                        "supported: en, es",
                    ),
                    **_UPSTREAM_FAILURES,
                },
            }
        },
        "/api/v1/demos": {
            "get": {
                "tags": [_TAG],
                "operationId": "mirag_demos",
                "summary": "The prepared demos, localised",
                "parameters": [_LOCALE_QUERY],
                "responses": {"200": _ok(MiragDemos, "The demos."), **_UPSTREAM_FAILURES},
            }
        },
        "/api/v1/chat": {
            "post": {
                "tags": [_TAG],
                "operationId": "mirag_chat",
                "summary": "Ask a question (Server-Sent Events)",
                "description": _CHAT_DESCRIPTION,
                "requestBody": {
                    "required": True,
                    "description": "At most 64 KiB.",
                    "content": {"application/json": {"schema": schema_ref(MiragChatRequest)}},
                },
                "responses": {
                    "200": {
                        "description": "The event stream.",
                        "headers": {
                            "X-Gateway-Instance": {
                                "description": "The agent instance answering. Keep it to "
                                "download the project from that same instance.",
                                "schema": {"type": "string", "example": "3fa1c2d0"},
                            }
                        },
                        "content": {
                            "text/event-stream": {
                                "schema": {"type": "string"},
                                "example": _SSE_EXAMPLE,
                            }
                        },
                    },
                    "400": _agent_error(
                        "The body is invalid: `invalid_json`, `invalid_body`, `missing_question`, "
                        "`question_too_long`, `invalid_mode`, `invalid_locale` or `bad_length`. "
                        "Always before the stream starts.",
                        "missing_question",
                        "'question' is required and must be a non-empty string",
                    ),
                    "401": _UNAUTHORIZED,
                    "413": _agent_error(
                        "`bad_length`: the body exceeds 64 KiB.",
                        "bad_length",
                        "Content-Length must be between 1 and 65536",
                    ),
                    **_UPSTREAM_FAILURES,
                },
            }
        },
        "/api/v1/artifacts/{artifact_id}/download": {
            "get": {
                "tags": [_TAG],
                "operationId": "mirag_download_artifact",
                "summary": "Download a generated project",
                "description": _DOWNLOAD_DESCRIPTION,
                "parameters": [
                    {
                        "name": "artifact_id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string", "pattern": "^[0-9a-f]{24}$"},
                    }
                ],
                "responses": {
                    "200": {
                        "description": "The ZIP. Compare `X-Mirag-Sha256` with "
                        "`project.zip.sha256`.",
                        "headers": {
                            "Content-Disposition": {
                                "schema": {"type": "string"},
                                "example": 'attachment; filename="books-api.zip"',
                            },
                            "X-Mirag-Sha256": {"schema": {"type": "string"}},
                        },
                        "content": {
                            "application/zip": {"schema": {"type": "string", "format": "binary"}}
                        },
                    },
                    "400": _agent_error(
                        "`malformed_id`: not 24 hex characters.",
                        "malformed_id",
                        "malformed artifact id",
                    ),
                    "401": _UNAUTHORIZED,
                    "409": _agent_error(
                        "`integrity_error`: the package did not pass inspection.",
                        "integrity_error",
                        "ARTIFACT INTEGRITY ERROR: the package did not pass inspection",
                    ),
                    "410": _agent_error(
                        "`gone`: expired or unknown.", "gone", "that artifact no longer exists"
                    ),
                    "500": _agent_error(
                        "`integrity_error`: the bytes changed between inspection and delivery.",
                        "integrity_error",
                        "ARTIFACT INTEGRITY ERROR at delivery",
                    ),
                    **_UPSTREAM_FAILURES,
                },
            }
        },
        "/api/v1/blockchain/agent": {
            "get": {
                "tags": [_TAG],
                "operationId": "mirag_blockchain_agent",
                "summary": "The optional Stellar identity panel",
                "responses": {
                    "200": _ok(MiragBlockchainAgent, "The panel. Never contains a secret."),
                    **_UPSTREAM_FAILURES,
                },
            }
        },
    },
    models=(
        MiragError,
        MiragHealth,
        MiragLocales,
        MiragUiStrings,
        MiragDemos,
        MiragBlockchainAgent,
        MiragChatRequest,
        MiragStepEvent,
        MiragDoneEvent,
    ),
)
