"""OpenAPI contracts of the services reached through the gateway, keyed by service name.

A registered service whose name is a key here gets its routes documented in Swagger under
``/api/{name}/...``. To document another service, describe it in a module of this package
and add it below.
"""

from gateway.api.contracts.mirag import MIRAG
from gateway.api.openapi import DownstreamContract

CONTRACTS: dict[str, DownstreamContract] = {"mirag": MIRAG}
