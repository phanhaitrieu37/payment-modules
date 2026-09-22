"""Resolve ``env:NAME`` secret references from process environment variables.

Webhook secrets rotate through ``NAME`` (current) and ``NAME_PREVIOUS`` (still accepted
during the rotation window). An unset, empty or whitespace-only variable counts as not set.
A reference with any other scheme is a configuration error, not a failed signature.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from payment_module.ports.resolvers import ProviderConnection

SCHEME = "env:"
PREVIOUS_SUFFIX = "_PREVIOUS"


class EnvSecretResolver:
    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        """``environ`` defaults to :data:`os.environ`, read on every call."""
        self._environ = os.environ if environ is None else environ

    async def webhook_secrets(self, connection: ProviderConnection) -> list[str]:
        """The current and previous secret, in that order; ``[]`` when neither is set."""
        name = _variable(connection.secret_ref)
        return [
            value
            for value in (self._value(name), self._value(name + PREVIOUS_SUFFIX))
            if value is not None
        ]

    async def api_credential(self, connection: ProviderConnection) -> str | None:
        if connection.api_credential_ref is None:
            return None
        return self._value(_variable(connection.api_credential_ref))

    def _value(self, name: str) -> str | None:
        value = self._environ.get(name)
        if value is None or not value.strip():
            return None
        return value


def _variable(ref: str) -> str:
    if not ref.startswith(SCHEME) or not ref[len(SCHEME) :].strip():
        raise ValueError("secret reference must look like 'env:NAME'")
    return ref[len(SCHEME) :].strip()
